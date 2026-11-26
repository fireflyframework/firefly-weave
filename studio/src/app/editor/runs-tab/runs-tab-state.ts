/*
Copyright 2026 Firefly Software Foundation.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
Author: Firefly Software Foundation
SPDX-License-Identifier: Apache-2.0
*/
// The Runs tab's state: the workflow's runs, refreshed every 5 s while the
// page is visible, and the chosen run, read again every 2 s until it ends
// (for 30 minutes at most). Every read notes the run, workflow, account and
// environment it was asked for, and its answer applies only while all of
// them still hold; a change of account, environment or workflow forgets
// the list and the run. No timers or clock outside the poll environment.
import { signal } from "@angular/core";
import { ApiError } from "../../api";
import { describeError } from "../../errors";
import type { Workflow } from "../../model";
import {
  browserEnvironment,
  Poller,
  type PollEnvironment,
} from "../../operate/operate-store";
import {
  isUnavailableRun,
  listRunSteps,
  listRunSummaries,
  terminalRunStatuses,
  type RunStatus,
  type RunSummary,
  type RunSummaryItem,
  type RunViewsApi,
  type StepFact,
} from "../../operate/run-contracts";
import { RunData } from "./run-data";
import {
  OVERLAY_FACTS_LIMIT,
  overlayOf,
  picksFor,
  recordedLoops,
  type RunOverlay,
} from "./run-overlay";
import { dropDebug } from "./run-links";
import { readRunRecord, type RunRecord } from "./run-record";
import {
  inDoubtCaption,
  readRunTransactions,
  TransactionReadBackoff,
  type RunTransactionsRead,
  type RunTransactionView,
} from "./run-transactions";
import { allSteps } from "../ndv/step-walk";
import {
  defaultRunsTabFilters,
  MAX_LISTED_RUNS,
  refreshed,
  runRow,
  runsTabQuery,
  type RunRow,
  type RunsTabFilters,
} from "./runs-list-model";

/** What the Runs tab needs from Studio. */
export interface RunsTabHost {
  readonly api: RunViewsApi & { readonly project: string };
  /** The account and the environment: `${account scope}|${environment ID}`. */
  scope(): string;
  /** The open workflow's name (`metadata.name`). */
  workflowName(): string;
  /** The draft open in the editor. */
  draft(): Workflow;
  /** Whether the account may read runs here (`run.read`). */
  canRead(): boolean;
  environmentLabel(): string;
  /** The session's environment ID, kept with the run's data. */
  environmentId(): string;
}

/** The chosen run's error when it is a run of another workflow; its message names that workflow. */
export const OTHER_WORKFLOW = "WV-STUDIO-OTHER-WORKFLOW";
export interface ChosenRun {
  id: string;
  /** Its row in the list; null for a run that isn't listed. */
  summary: RunSummary | null;
  record: RunRecord | null;
  /** The version the run used; null until read, or when it can't be. */
  definition: Workflow | null;
  overlay: RunOverlay | null;
  data: RunData | null;
  /** Read again every 2 s until it ends. */
  following: boolean;
  /** Followed for 30 minutes without ending: Refresh reads it again. */
  stopped: boolean;
  /** It has more step instances than the overlay reads. */
  limited: boolean;
  error: { code: string; message: string } | null;
  /**
   * Its transactions' first page, or why they can't be read; null while
   * not read, and for a run of a workflow without a transaction.
   */
  transactions: RunTransactionsRead | null;
}

/** Whether a workflow holds a transaction: only then are a run's transactions read. */
const holdsTransaction = (workflow: Workflow): boolean =>
  allSteps(workflow.spec.steps ?? []).some(
    (step) => (step.kind as string) === "transaction",
  );
const viewsOf = (
  read: RunTransactionsRead | null | undefined,
): readonly RunTransactionView[] | undefined =>
  read && "views" in read ? read.views : undefined;
export type RunsTabStatus =
  | "idle"
  | "loading"
  | "ready"
  | "empty"
  | "error"
  | "access"
  | "unavailable";

export const LIST_INTERVAL_MS = 5000;
export const FOLLOW_INTERVAL_MS = 2000;
export const FOLLOW_LIMIT_MS = 30 * 60_000;
/** The longest wait a Retry-After can ask for. */
export const MAX_RETRY_AFTER_MS = 5 * 60_000;
const FACTS_PAGE = 500;
const OUTPUT_PAGE = 200;
const UNREADABLE = {
  code: "WV-STUDIO-RUN-UNREADABLE",
  message: "Studio can't read this run's state.",
};
const VERSION_UNREADABLE = {
  code: "WV-STUDIO-RUN-VERSION-UNREADABLE",
  message: "Studio couldn't read the version this run used.",
};

interface ListTicket {
  generation: number;
  scope: string;
  workflow: string;
  filters: RunsTabFilters;
}
interface RunTicket {
  generation: number;
  scope: string;
  workflow: string;
  id: string;
}
interface RunRead {
  record: RunRecord;
  facts: StepFact[] | null;
  limited: boolean;
}

const problem = (error: unknown) =>
  error instanceof ApiError
    ? {
        status: error.status,
        code: error.code || `HTTP ${error.status}`,
        message: error.message,
      }
    : { status: 0, code: "", message: describeError(error).message };
const ended = (status: RunStatus) => terminalRunStatuses.includes(status);
/** Two reads for the same run, as it was chosen. */
const sameRun = (a: RunTicket, b: RunTicket) =>
  a.generation === b.generation &&
  a.id === b.id &&
  a.scope === b.scope &&
  a.workflow === b.workflow;

export class RunsTabState {
  readonly items = signal<readonly RunSummaryItem[]>([]);
  readonly rows = signal<readonly RunRow[]>([]);
  readonly state = signal<RunsTabStatus>("idle");
  readonly error = signal<{ code: string; message: string } | null>(null);
  /** When the list last loaded (epoch milliseconds). */
  readonly updatedAt = signal<number | null>(null);
  readonly filters = signal<RunsTabFilters>(defaultRunsTabFilters());
  readonly chosen = signal<ChosenRun | null>(null);
  /**
   * The caption of each run whose transaction in doubt Studio read
   * ("Failed at submit · move in doubt"), by run ID, for its row. Forgotten
   * with the list.
   */
  readonly captions = signal<ReadonlyMap<string, string>>(new Map());
  /** When the chosen run's transactions may be read again after a failed read. */
  private transactionsWait = new TransactionReadBackoff();
  /** The read of the chosen run's transactions on its way: one at a time. */
  private transactionsReading: RunTicket | null = null;
  /** They were asked for while that read was on its way: read once more when it lands. */
  private transactionsAgain = false;
  /** The next read of them, when the wait after a failed read is over. */
  private transactionsTimer: number | null = null;
  /** The run's record as it was when they were last read, as JSON. */
  private transactionsOf: string | null = null;
  /** The items picked on the chosen run's loops (loop step ID to index), and its last read. */
  private picked: Record<string, number> = {};
  private lastRead: RunRead | null = null;
  /** Older runs: another page to read, the 500-run limit reached, or none. */
  readonly older = signal<"more" | "limit" | "none">("none");
  /**
   * Every version the workflow's listed runs used, test runs aside. A filter
   * that narrows the list keeps them, so Version still offers them; another
   * account, environment or workflow starts over.
   */
  readonly usedVersions = signal<readonly string[]>([]);

  private readonly lister: Poller;
  private follower: Poller | null = null;
  private followedSince = 0;
  private retryTimer: number | null = null;
  private listGeneration = 0;
  private runGeneration = 0;
  private cursor: string | null = null;
  private pages = 0;
  private seen: { scope: string; workflow: string };
  private readonly versions = new Map<string, Promise<Workflow | null>>();
  private destroyed = false;
  /** start() ran: the list is wanted. */
  private started = false;
  /** The platform refused: nothing is read until Refresh or another scope. */
  private refused = false;
  private loadingMore: Promise<void> | null = null;

  constructor(
    private readonly host: RunsTabHost,
    private readonly env: PollEnvironment = browserEnvironment,
  ) {
    this.seen = { scope: host.scope(), workflow: host.workflowName() };
    this.lister = new Poller(() => this.loadList(), LIST_INTERVAL_MS, env);
  }

  /** Starts listing: at once, then every 5 s while the page is visible. */
  start(): void {
    if (this.destroyed) return;
    this.started = true;
    if (!this.host.canRead()) return this.refuse();
    this.refused = false;
    this.lister.start();
    const chosen = this.chosen();
    if (chosen?.following) this.follower?.start(false);
  }
  /** Stops reading; what was read stays. */
  stop(): void {
    this.lister.stop();
    this.follower?.stop();
    this.clearRetry();
    this.clearTransactionsRetry();
  }
  /** Refresh and Try again: the list now, and a run that stopped being followed. */
  async refresh(): Promise<void> {
    if (this.destroyed) return;
    if (!this.host.canRead()) return this.refuse();
    if (this.refused) {
      // Asked again after a refusal: list on the interval once more.
      this.refused = false;
      this.lister.start(false);
    }
    const chosen = this.chosen();
    const follow =
      chosen?.stopped && chosen.record ? this.follow(chosen.id) : null;
    await Promise.all([this.lister.refresh(), follow]);
  }
  setFilters(filters: RunsTabFilters): void {
    this.filters.set(filters);
    this.listGeneration++;
    this.setItems([], null, 0);
    this.state.set("loading");
    void this.lister.refresh();
  }
  /** Show older runs: the next page, up to 500 runs; one read at a time. */
  more(): Promise<void> {
    this.loadingMore ??= this.loadMore().finally(() => {
      this.loadingMore = null;
    });
    return this.loadingMore;
  }
  private async loadMore(): Promise<void> {
    this.sync();
    const cursor = this.cursor;
    if (!cursor || this.items().length >= MAX_LISTED_RUNS) return;
    const ticket = this.listTicket();
    try {
      const page = await listRunSummaries(
        this.host.api,
        runsTabQuery(ticket.workflow, ticket.filters, cursor),
      );
      if (!this.listHolds(ticket)) return;
      const known = new Set(this.items().map((item) => item.id));
      const added = page.items.filter((item) => !known.has(item.id));
      this.setItems(
        [...this.items(), ...added].slice(0, MAX_LISTED_RUNS),
        page.next_cursor,
        this.pages + 1,
      );
    } catch (error) {
      if (this.listHolds(ticket)) this.listFailed(error);
    }
  }

  /** Shows a run: reads it, the version it used and its steps. */
  async choose(id: string): Promise<void> {
    this.sync();
    if (this.destroyed) return;
    this.stopFollowing();
    // Each run opens its loops on their first failed items.
    this.picked = {};
    this.lastRead = null;
    this.transactionsWait = new TransactionReadBackoff();
    this.clearTransactionsRetry();
    this.transactionsOf = null;
    const ticket = this.runTicket(id, ++this.runGeneration);
    const listed = this.items().find((item) => item.id === id);
    const summary = listed && !isUnavailableRun(listed) ? listed : null;
    this.chosen.set({
      id,
      summary,
      record: null,
      definition: null,
      overlay: null,
      data: null,
      following: false,
      stopped: false,
      limited: false,
      error: null,
      transactions: null,
    });
    if (!this.host.canRead()) return this.refuse();
    let read: RunRead | null;
    try {
      read = await this.readRun(ticket);
    } catch (error) {
      if (!this.runHolds(ticket)) return;
      const failed = problem(error);
      if (failed.status === 403) return this.refuse();
      this.updateChosen(ticket, {
        error: { code: failed.code, message: failed.message },
      });
      return;
    }
    if (!this.runHolds(ticket)) return;
    if (!read) {
      this.updateChosen(ticket, { error: UNREADABLE });
      return;
    }
    const versionId =
      summary?.workflow.definition_version_id ?? read.record.versionId;
    const definition = await this.runDefinition(versionId);
    if (!this.runHolds(ticket)) return;
    // A run of another workflow (a link can name any run): nothing of it
    // draws here, and its data can't become this workflow's test data.
    if (definition && definition.metadata.name !== this.host.workflowName()) {
      this.updateChosen(ticket, {
        error: { code: OTHER_WORKFLOW, message: definition.metadata.name },
      });
      return;
    }
    // Neither the list nor the version says the run is this workflow's: it
    // could be another's, its steps matched to these by ID only.
    if (!definition && !summary) {
      this.updateChosen(ticket, { error: VERSION_UNREADABLE });
      return;
    }
    const data = new RunData(
      read.record,
      this.host.environmentId(),
      definition,
      (stepId) => void this.readOutput(stepId),
      () => this.refuse(),
    );
    data.setFacts(read.facts ?? []);
    this.updateChosen(ticket, {
      record: read.record,
      definition,
      data,
      limited: read.limited,
      overlay: this.overlay(read, summary, definition),
    });
    if (!ended(read.record.status)) void this.follow(id, false);
    // The run draws first; its transactions follow when its workflow has one.
    if (this.wantsTransactions(summary, definition))
      await this.readTransactions(ticket);
  }
  /** A run's transactions are read when its version (else the draft) holds a transaction, or its summary names one. */
  private wantsTransactions(
    summary: RunSummary | null,
    definition: Workflow | null,
  ): boolean {
    return (
      summary?.transaction_state !== undefined ||
      holdsTransaction(definition ?? this.host.draft())
    );
  }
  /**
   * The chosen run's transactions, the first page: drawn with the run, and
   * kept for its row's caption. A refusal shows no transaction words. Every
   * ask reads them now or leaves one read to come, so a run that ends
   * meanwhile still shows how its transactions ended:
   * - asked while a read of them is on its way, they are read once more when
   *   it lands (one at a time: an older answer could land after a newer one);
   * - when a read fails, or they are asked for during the wait after one
   *   (5 s, doubling to a minute), a timer reads them when the wait is over.
   */
  private async readTransactions(ticket: RunTicket): Promise<void> {
    const reading = this.transactionsReading;
    if (reading && sameRun(reading, ticket)) {
      this.transactionsAgain = true;
      return;
    }
    if (!this.transactionsWait.ready(this.env.now()))
      return this.retryTransactions(ticket);
    this.clearTransactionsRetry();
    this.transactionsReading = ticket;
    this.transactionsAgain = false;
    const record = JSON.stringify(this.chosen()?.record ?? null);
    let read: RunTransactionsRead;
    try {
      read = await readRunTransactions(this.host.api, ticket.id);
    } catch {
      if (!this.runHolds(ticket)) return;
      this.transactionsWait.failed(this.env.now());
      return this.retryTransactions(ticket);
    } finally {
      if (this.transactionsReading === ticket) this.transactionsReading = null;
    }
    if (!this.runHolds(ticket)) return;
    this.transactionsWait.succeeded();
    this.transactionsOf = record;
    const views = viewsOf(read);
    const caption = views ? inDoubtCaption(views) : null;
    const captions = new Map(this.captions());
    if (caption) captions.set(ticket.id, caption);
    else if (views) captions.delete(ticket.id);
    this.captions.set(captions);
    this.rows.set(this.rowsOf(this.items()));
    const chosen = this.chosen()!;
    // Step details read them too: a transaction in doubt, a step undone.
    chosen.data?.setTransactions(views ?? []);
    this.updateChosen(ticket, {
      transactions: read,
      overlay:
        this.lastRead && chosen.record === this.lastRead.record
          ? this.overlay(this.lastRead, chosen.summary, chosen.definition, read)
          : chosen.overlay,
    });
    if (this.transactionsAgain) await this.readTransactions(ticket);
  }
  /** Reads a run's transactions again once the wait after a failed read is over. */
  private retryTransactions(ticket: RunTicket) {
    this.clearTransactionsRetry();
    this.transactionsTimer = this.env.setTimeout(() => {
      this.transactionsTimer = null;
      if (this.runHolds(ticket)) void this.readTransactions(ticket);
    }, this.transactionsWait.left(this.env.now()));
  }
  private clearTransactionsRetry() {
    if (this.transactionsTimer !== null)
      this.env.clearTimeout(this.transactionsTimer);
    this.transactionsTimer = null;
  }
  /** Back to the list: forgets the run, its data and a Debug request for it. */
  leave(): void {
    dropDebug();
    this.runGeneration++;
    this.stopFollowing();
    this.clearTransactionsRetry();
    this.chosen.set(null);
  }
  /** One step's recorded outputs, read when step details shows it. */
  async readOutput(stepId: string): Promise<void> {
    const chosen = this.chosen();
    const data = chosen?.data;
    if (!chosen || !data) return;
    const ticket = this.runTicket(chosen.id, this.runGeneration);
    data.markLoading(stepId);
    for (let limit = OUTPUT_PAGE; ; limit = Math.floor(limit / 2)) {
      try {
        const page = await listRunSteps(this.host.api, chosen.id, {
          step: stepId,
          include: "output",
          limit,
        });
        if (this.runHolds(ticket)) data.setOutputs(page.items, stepId);
        return;
      } catch (error) {
        if (!this.runHolds(ticket)) return;
        const failed = problem(error);
        // A page over the platform's byte limit: fewer instances a page.
        if (failed.status === 429 && failed.code === "WV-PAGE-LIMIT") {
          if (limit > 1) continue;
          return data.markTooLarge(stepId);
        }
        if (failed.status === 403) return this.refuse();
        return data.markError(stepId, failed.message);
      }
    }
  }
  /** The tab closed: nothing more is read or applied. */
  destroy(): void {
    this.destroyed = true;
    this.listGeneration++;
    this.runGeneration++;
    this.stop();
    this.follower = null;
    this.chosen.set(null);
  }
  /**
   * Forgets the list and the run when the account, the environment or the
   * workflow changed since they were read. Every read checks it first.
   */
  sync(): void {
    const now = {
      scope: this.host.scope(),
      workflow: this.host.workflowName(),
    };
    if (now.scope === this.seen.scope && now.workflow === this.seen.workflow)
      return;
    this.seen = now;
    this.versions.clear();
    this.usedVersions.set([]);
    this.forget(this.destroyed ? this.state() : "idle");
    // Another account or environment may read runs: ask it at once.
    if (this.refused && this.started && !this.destroyed) {
      this.refused = false;
      this.lister.start();
    }
  }

  // --- the list ---------------------------------------------------------------

  private async loadList(): Promise<boolean> {
    this.sync();
    if (!this.host.canRead()) {
      this.refuse();
      return false;
    }
    const ticket = this.listTicket();
    // Only a first read shows loading: an access or error screen stays put.
    if (!this.items().length && this.state() === "idle")
      this.state.set("loading");
    let page;
    try {
      page = await listRunSummaries(
        this.host.api,
        runsTabQuery(ticket.workflow, ticket.filters),
      );
    } catch (error) {
      if (!this.listHolds(ticket)) return false;
      this.listFailed(error);
      throw error;
    }
    if (!this.listHolds(ticket)) return false;
    const items = refreshed(this.items(), page.items);
    // A refresh reads the first page again; the cursor of an older page stays.
    this.setItems(
      items,
      this.pages > 1 ? this.cursor : page.next_cursor,
      Math.max(this.pages, 1),
    );
    this.error.set(null);
    this.state.set(items.length ? "ready" : "empty");
    this.updatedAt.set(this.env.now());
    return true;
  }
  private listFailed(error: unknown) {
    const failed = problem(error);
    if (failed.status === 403) return this.refuse();
    if (failed.status === 501) {
      // An older platform: it can't list runs, so Studio stops asking.
      this.stop();
      this.setItems([], null, 0);
      this.state.set("unavailable");
      return;
    }
    this.error.set({ code: failed.code, message: failed.message });
    this.state.set("error");
    const wait = error instanceof ApiError ? error.retryAfter : undefined;
    if (failed.status === 429 && wait !== undefined) {
      // The platform said when to ask again: no request before then, never
      // sooner than the list's interval, and never more than 5 minutes (a
      // larger delay overflows the timer).
      this.lister.stop();
      this.clearRetry();
      this.retryTimer = this.env.setTimeout(
        () => {
          this.retryTimer = null;
          if (!this.destroyed) this.lister.start(this.env.visible());
        },
        Math.max(LIST_INTERVAL_MS, Math.min(wait * 1000, MAX_RETRY_AFTER_MS)),
      );
    }
  }
  private setItems(
    items: readonly RunSummaryItem[],
    cursor: string | null,
    pages: number,
  ) {
    const rows = this.rowsOf(items);
    this.items.set(items);
    this.rows.set(rows);
    const used = new Set(this.usedVersions());
    for (const row of rows)
      if (!row.unavailable && !row.test && row.version) used.add(row.version);
    if (used.size > this.usedVersions().length)
      this.usedVersions.set([...used]);
    this.cursor = cursor;
    this.pages = pages;
    this.older.set(
      !cursor ? "none" : items.length >= MAX_LISTED_RUNS ? "limit" : "more",
    );
    // A run chosen before the list arrived (from a link) whose version
    // couldn't be read: the list now says it is this workflow's, so it draws.
    const chosen = this.chosen();
    if (
      chosen?.error?.code === VERSION_UNREADABLE.code &&
      items.some((item) => item.id === chosen.id && !isUnavailableRun(item))
    )
      void this.choose(chosen.id);
  }
  /** The list's rows, each run whose transaction in doubt Studio read with its caption. */
  private rowsOf(items: readonly RunSummaryItem[]): RunRow[] {
    const now = new Date(this.env.now());
    const captions = this.captions();
    return items.map((item) =>
      runRow(item, now, captions.get(item.id) ?? null),
    );
  }
  private listTicket(): ListTicket {
    return {
      generation: this.listGeneration,
      scope: this.host.scope(),
      workflow: this.host.workflowName(),
      filters: this.filters(),
    };
  }
  private listHolds(ticket: ListTicket): boolean {
    this.sync();
    return (
      !this.destroyed &&
      ticket.generation === this.listGeneration &&
      ticket.scope === this.seen.scope &&
      ticket.workflow === this.seen.workflow
    );
  }

  // --- the chosen run ---------------------------------------------------------

  /** runs.read and the step facts; null when the run's state can't be read. */
  private async readRun(ticket: RunTicket): Promise<RunRead | null> {
    const api = this.host.api;
    const view = await api.request<unknown>(
      `${api.environment}/runs/${encodeURIComponent(ticket.id)}`,
    );
    const record = readRunRecord(view);
    if (!record || record.id.toLowerCase() !== ticket.id.toLowerCase())
      return null;
    const facts: StepFact[] = [];
    let cursor: string | undefined;
    let limited = false;
    try {
      for (let page = 0; ; page++) {
        if (page === OVERLAY_FACTS_LIMIT / FACTS_PAGE) {
          limited = true;
          break;
        }
        if (!this.runHolds(ticket)) break;
        const answer = await listRunSteps(api, ticket.id, {
          limit: FACTS_PAGE,
          ...(cursor ? { cursor } : {}),
        });
        facts.push(...answer.items);
        if (!answer.next_cursor) break;
        cursor = answer.next_cursor;
      }
    } catch (error) {
      // A refusal counts; other failures leave the run's state to draw it.
      if (problem(error).status === 403) throw error;
      return { record, facts: null, limited: false };
    }
    return { record, facts, limited };
  }
  /** The version the run used, once per version; null when it can't be read. */
  private runDefinition(versionId: string): Promise<Workflow | null> {
    let read = this.versions.get(versionId);
    if (!read) {
      const api = this.host.api;
      read = api
        .request<{ document?: unknown }>(
          `${api.project}/workflows/${encodeURIComponent(versionId)}/export`,
        )
        .then((exported) => workflowOf(exported?.document))
        .catch(() => null);
      this.versions.set(versionId, read);
    }
    return read;
  }
  private overlay(
    read: RunRead,
    summary: RunSummary | null,
    definition: Workflow | null,
    transactions = this.chosen()?.transactions,
  ): RunOverlay {
    this.lastRead = read;
    const draft = this.host.draft();
    const views = viewsOf(transactions);
    // A loop's steps show one item at a time: the one picked, else the first failed one.
    const loops = recordedLoops(draft, read.record, read.facts, views);
    const picks = loops.length
      ? picksFor(draft, loops, this.picked)
      : undefined;
    const overlay = overlayOf(
      read.record,
      read.facts,
      {
        run: definition,
        draft,
        runVersion:
          summary?.workflow.version ??
          definition?.metadata.version ??
          draft.metadata.version,
        test: summary?.test ?? false,
      },
      this.env.now(),
      picks,
      views,
    );
    return picks
      ? {
          ...overlay,
          canvas: {
            ...overlay.canvas,
            pick: (loop: string, index: number) => this.pickItem(loop, index),
          },
        }
      : overlay;
  }
  /** Shows another item of a loop in the chosen run, without reading it again. */
  pickItem(loop: string, index: number): void {
    this.picked = { ...this.picked, [loop]: index };
    const chosen = this.chosen();
    if (!chosen || !this.lastRead || chosen.record !== this.lastRead.record)
      return;
    this.chosen.set({
      ...chosen,
      overlay: this.overlay(this.lastRead, chosen.summary, chosen.definition),
    });
  }
  /** Reads the run every 2 s until it ends, for 30 minutes at most. */
  private follow(id: string, now = true): Promise<void> {
    this.stopFollowing();
    const ticket = this.runTicket(id, this.runGeneration);
    this.followedSince = this.env.now();
    this.follower = new Poller(
      () => this.followRun(ticket),
      FOLLOW_INTERVAL_MS,
      this.env,
    );
    this.updateChosen(ticket, { following: true, stopped: false });
    this.follower.start(false);
    return now ? this.follower.refresh() : Promise.resolve();
  }
  private async followRun(ticket: RunTicket): Promise<boolean> {
    if (!this.runHolds(ticket)) {
      this.stopFollowing();
      return false;
    }
    if (this.env.now() - this.followedSince >= FOLLOW_LIMIT_MS) {
      this.stopFollowing();
      this.updateChosen(ticket, { following: false, stopped: true });
      return false;
    }
    let read: RunRead | null;
    try {
      read = await this.readRun(ticket);
    } catch (error) {
      if (!this.runHolds(ticket)) return false;
      // A refusal counts at once: nothing read before is kept.
      if (problem(error).status === 403) {
        this.refuse();
        return false;
      }
      throw error;
    }
    if (!this.runHolds(ticket)) return false;
    const chosen = this.chosen();
    if (!read || !chosen) return false;
    chosen.data?.setRecord(read.record);
    if (read.facts) chosen.data?.setFacts(read.facts);
    const definition = await this.runDefinition(
      chosen.summary?.workflow.definition_version_id ?? read.record.versionId,
    );
    if (!this.runHolds(ticket)) return false;
    const done = ended(read.record.status);
    if (done) this.stopFollowing();
    this.updateChosen(ticket, {
      record: read.record,
      limited: read.limited,
      overlay: this.overlay(read, chosen.summary, definition),
      following: !done,
    });
    // A run on hold changes only when someone acts on it: its transactions
    // are read again when its record changed.
    const unchanged =
      read.record.status === "suspended" &&
      this.transactionsOf === JSON.stringify(read.record);
    if (!unchanged && this.wantsTransactions(chosen.summary, definition))
      await this.readTransactions(ticket);
    return true;
  }
  private stopFollowing() {
    this.follower?.stop();
    this.follower = null;
  }
  private runTicket(id: string, generation: number): RunTicket {
    return {
      generation,
      scope: this.host.scope(),
      workflow: this.host.workflowName(),
      id,
    };
  }
  private runHolds(ticket: RunTicket): boolean {
    this.sync();
    return (
      !this.destroyed &&
      ticket.generation === this.runGeneration &&
      ticket.scope === this.seen.scope &&
      ticket.workflow === this.seen.workflow &&
      this.chosen()?.id === ticket.id
    );
  }
  private updateChosen(ticket: RunTicket, change: Partial<ChosenRun>) {
    const chosen = this.chosen();
    if (chosen && chosen.id === ticket.id && this.runHolds(ticket))
      this.chosen.set({ ...chosen, ...change });
  }

  // --- forgetting --------------------------------------------------------------

  /** No run.read here, or the platform refused: nothing read before is kept. */
  private refuse() {
    // Asking again on the interval would only be refused again.
    this.refused = true;
    this.lister.stop();
    this.clearRetry();
    this.forget("access");
  }
  private forget(state: RunsTabStatus) {
    this.listGeneration++;
    this.leave();
    this.captions.set(new Map());
    this.setItems([], null, 0);
    this.error.set(null);
    this.updatedAt.set(null);
    this.state.set(state);
  }
  private clearRetry() {
    if (this.retryTimer !== null) this.env.clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }
}

/** A workflow document with the fields the overlay reads; null otherwise. */
export function workflowOf(document: unknown): Workflow | null {
  if (!document || typeof document !== "object") return null;
  const candidate = document as Partial<Workflow>;
  return candidate.metadata &&
    typeof candidate.metadata.name === "string" &&
    typeof candidate.metadata.version === "string" &&
    candidate.spec &&
    Array.isArray(candidate.spec.steps)
    ? (candidate as Workflow)
    : null;
}
