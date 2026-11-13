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
import { OVERLAY_FACTS_LIMIT, overlayOf, type RunOverlay } from "./run-overlay";
import { readRunRecord, type RunRecord } from "./run-record";
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

export interface ChosenRun {
  id: string;
  /** Its row in the list; null for a run that isn't listed. */
  summary: RunSummary | null;
  record: RunRecord | null;
  overlay: RunOverlay | null;
  data: RunData | null;
  /** Read again every 2 s until it ends. */
  following: boolean;
  /** Followed for 30 minutes without ending: Refresh reads it again. */
  stopped: boolean;
  /** It has more step instances than the overlay reads. */
  limited: boolean;
  error: { code: string; message: string } | null;
}
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

export class RunsTabState {
  readonly items = signal<readonly RunSummaryItem[]>([]);
  readonly rows = signal<readonly RunRow[]>([]);
  readonly state = signal<RunsTabStatus>("idle");
  readonly error = signal<{ code: string; message: string } | null>(null);
  /** When the list last loaded (epoch milliseconds). */
  readonly updatedAt = signal<number | null>(null);
  readonly filters = signal<RunsTabFilters>(defaultRunsTabFilters());
  readonly chosen = signal<ChosenRun | null>(null);
  /** Older runs: another page to read, the 500-run limit reached, or none. */
  readonly older = signal<"more" | "limit" | "none">("none");

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
    const ticket = this.runTicket(id, ++this.runGeneration);
    const listed = this.items().find((item) => item.id === id);
    const summary = listed && !isUnavailableRun(listed) ? listed : null;
    this.chosen.set({
      id,
      summary,
      record: null,
      overlay: null,
      data: null,
      following: false,
      stopped: false,
      limited: false,
      error: null,
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
      data,
      limited: read.limited,
      overlay: this.overlay(read, summary, definition),
    });
    if (!ended(read.record.status)) void this.follow(id, false);
  }
  /** Back to the list: forgets the run and its data. */
  leave(): void {
    this.runGeneration++;
    this.stopFollowing();
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
    const now = new Date(this.env.now());
    this.items.set(items);
    this.rows.set(items.map((item) => runRow(item, now)));
    this.cursor = cursor;
    this.pages = pages;
    this.older.set(
      !cursor ? "none" : items.length >= MAX_LISTED_RUNS ? "limit" : "more",
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
  ): RunOverlay {
    const draft = this.host.draft();
    return overlayOf(read.record, read.facts, {
      run: definition,
      draft,
      runVersion:
        summary?.workflow.version ??
        definition?.metadata.version ??
        draft.metadata.version,
      test: summary?.test ?? false,
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
function workflowOf(document: unknown): Workflow | null {
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
