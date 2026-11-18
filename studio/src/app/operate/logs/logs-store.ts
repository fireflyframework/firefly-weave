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
// The reads behind a run's Logs section and the Logs page. Each read takes
// note of the run, the environment and person, and the filters before its
// first await, and its answer applies only while all three still hold: a run
// switch, an environment switch or a filter change drops it. State is held in
// signals, so OnPush templates follow it. A platform that answered it doesn't
// serve logs is not asked again this session.
import { signal } from "@angular/core";
import type { PlainError } from "../../errors";
import { MAX_PAGES } from "../operate-limits";
import type { ExecutionLogQuery, RunViewsApi } from "../run-contracts";
import { readExecutionLogs, readRunLogs, type LogRow } from "./logs-api";
import {
  LOG_PAGE,
  defaultRunLogsFilters,
  logFailure,
  mergeFollowed,
  runLogQuery,
  type LogFailure,
  type RunLogsFilters,
} from "./logs-model";

/** What the stores read through: the current platform and whose view it is. */
export interface LogsHost {
  /** The platform's run views; its environment may be unknown (it throws). */
  api(): RunViewsApi;
  /** The environment, person and sign-in: a change means a new view. */
  scope(): string;
}
/** Environments that answered they don't serve logs, for this session. */
export const unservedPlatforms = new Set<string>();
/** What a read did: its answer was applied, it was dropped, or it failed. */
export type ReadResult = "applied" | "dropped" | "failed";

function environmentOf(host: LogsHost): string {
  try {
    return host.api().environment;
  } catch {
    return "";
  }
}
const withoutRepeats = (rows: LogRow[]) => {
  const seen = new Set<string>();
  return rows.filter((row) => !seen.has(row.id) && !!seen.add(row.id));
};

export type RunLogsState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; rows: LogRow[]; more: boolean }
  | LogFailure;

/**
 * One run's entries in time order, newest last: as many pages of 500 as are
 * shown, read again from the start on each refresh, because a page that ends
 * a run's entries carries no cursor to continue from.
 */
export class RunLogsStore {
  readonly state = signal<RunLogsState>({ kind: "idle" });
  readonly filters = signal<RunLogsFilters>(defaultRunLogsFilters);
  /** A refresh failed while entries show: they stay, and this says why. */
  readonly refreshError = signal<PlainError | null>(null);
  readonly appending = signal(false);
  private runId = "";
  private pages = 1;
  private cursor: string | null = null;
  private generation = 0;
  private refreshing: Promise<ReadResult> | null = null;

  constructor(
    private readonly host: LogsHost,
    private readonly unserved: Set<string> = unservedPlatforms,
  ) {}

  /** The run whose entries are shown, or "". */
  get run() {
    return this.runId;
  }
  /** Shows a run's entries on every level and step. */
  open(runId: string): Promise<ReadResult> {
    if (runId === this.runId && this.state().kind !== "idle")
      return Promise.resolve("dropped");
    this.runId = runId;
    this.filters.set(defaultRunLogsFilters);
    return this.restart();
  }
  setFilters(filters: RunLogsFilters): Promise<ReadResult> {
    this.filters.set(filters);
    return this.restart();
  }
  /** Reads what is shown again, keeping it on screen meanwhile. */
  refresh(): Promise<ReadResult> {
    this.refreshing ??= this.read(false).finally(() => {
      this.refreshing = null;
    });
    return this.refreshing;
  }
  /** Another environment or person: nothing read before stays. */
  forget() {
    this.generation++;
    this.runId = "";
    this.pages = 1;
    this.cursor = null;
    this.state.set({ kind: "idle" });
    this.refreshError.set(null);
    this.appending.set(false);
  }
  private restart() {
    this.generation++;
    this.pages = 1;
    this.cursor = null;
    this.refreshing = null;
    this.appending.set(false);
    return this.read(true);
  }
  private async read(fresh: boolean): Promise<ReadResult> {
    const environment = environmentOf(this.host);
    const run = this.runId;
    if (!environment || !run) return "dropped";
    if (this.unserved.has(environment)) {
      this.state.set({ kind: "unserved" });
      return "dropped";
    }
    const generation = this.generation;
    const scope = this.host.scope();
    const filters = this.filters();
    const wanted = Math.min(Math.max(1, this.pages), MAX_PAGES);
    const current = () =>
      generation === this.generation &&
      scope === this.host.scope() &&
      run === this.runId;
    if (fresh) this.state.set({ kind: "loading" });
    try {
      const api = this.host.api();
      const rows: LogRow[] = [];
      let cursor: string | null = null;
      let read = 0;
      do {
        const page = await readRunLogs(
          api,
          run,
          runLogQuery(filters, cursor ?? undefined),
        );
        if (!current()) return "dropped";
        rows.push(...page.rows);
        cursor = page.nextCursor;
        read++;
      } while (cursor && read < wanted);
      // Load more finished meanwhile: the next refresh reads every page.
      if (this.pages > wanted) return "dropped";
      this.pages = read;
      this.cursor = cursor;
      this.state.set({
        kind: "ready",
        rows: withoutRepeats(rows),
        more: !!cursor && read < MAX_PAGES,
      });
      this.refreshError.set(null);
      return "applied";
    } catch (error) {
      if (!current()) return "dropped";
      this.fail(error, environment, fresh);
      return "failed";
    }
  }
  /** The next page of entries, after those shown. */
  async loadMore(): Promise<ReadResult> {
    const state = this.state();
    const cursor = this.cursor;
    if (this.appending() || state.kind !== "ready" || !cursor || !state.more)
      return "dropped";
    const generation = this.generation;
    const scope = this.host.scope();
    const run = this.runId;
    const environment = environmentOf(this.host);
    this.appending.set(true);
    try {
      const page = await readRunLogs(
        this.host.api(),
        run,
        runLogQuery(this.filters(), cursor),
      );
      const now = this.state();
      if (
        generation !== this.generation ||
        scope !== this.host.scope() ||
        run !== this.runId ||
        now.kind !== "ready"
      )
        return "dropped";
      this.pages++;
      this.cursor = page.nextCursor;
      this.state.set({
        kind: "ready",
        rows: withoutRepeats([...now.rows, ...page.rows]),
        more: !!page.nextCursor && this.pages < MAX_PAGES,
      });
      return "applied";
    } catch (error) {
      if (generation !== this.generation || scope !== this.host.scope())
        return "dropped";
      this.fail(error, environment, false);
      return "failed";
    } finally {
      if (generation === this.generation) this.appending.set(false);
    }
  }
  private fail(error: unknown, environment: string, fresh: boolean) {
    const failure = logFailure(error);
    if (failure.kind === "unserved") this.unserved.add(environment);
    const keep =
      !fresh &&
      this.state().kind === "ready" &&
      (failure.kind === "slow" || failure.kind === "error");
    if (keep) this.refreshError.set(failure.error);
    else this.state.set(failure);
  }
}

export type LogSearchState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready" }
  | LogFailure;
/** One chain of pages: the query its first page sent, and where it continues. */
interface Chain {
  generation: number;
  scope: string;
  environment: string;
  query: ExecutionLogQuery;
  cursor: string | null;
}
/** The most entries the Logs page keeps: ten pages. */
const MOST_ROWS = MAX_PAGES * LOG_PAGE;

/**
 * Entries across runs, newest first. A search starts a new chain; Follow
 * reads the newest page again and puts what is new on top; Load older
 * entries continues the chain with its first page's own query, the range
 * included, as the cursor requires.
 */
export class LogSearchStore {
  readonly state = signal<LogSearchState>({ kind: "idle" });
  readonly rows = signal<LogRow[]>([]);
  /** More entries follow, and Load older entries may read them. */
  readonly more = signal(false);
  /** The list reached the most entries it keeps. */
  readonly limited = signal(false);
  readonly appending = signal(false);
  private chain: Chain | null = null;
  private generation = 0;

  constructor(
    private readonly host: LogsHost,
    private readonly unserved: Set<string> = unservedPlatforms,
  ) {}

  /** New filters: the entries of the old ones go at once. */
  search(query: ExecutionLogQuery): Promise<ReadResult> {
    this.generation++;
    this.chain = null;
    this.rows.set([]);
    this.more.set(false);
    this.limited.set(false);
    this.appending.set(false);
    return this.first(query, false);
  }
  /** The same filters over a fresh range: new entries go on top. */
  follow(query: ExecutionLogQuery): Promise<ReadResult> {
    return this.first(query, !!this.chain);
  }
  /** Another environment or person: nothing read before stays. */
  forget() {
    this.generation++;
    this.chain = null;
    this.rows.set([]);
    this.more.set(false);
    this.limited.set(false);
    this.appending.set(false);
    this.state.set({ kind: "idle" });
  }
  private async first(
    query: ExecutionLogQuery,
    merge: boolean,
  ): Promise<ReadResult> {
    const environment = environmentOf(this.host);
    if (!environment) return "dropped";
    if (this.unserved.has(environment)) {
      this.state.set({ kind: "unserved" });
      return "dropped";
    }
    const generation = this.generation;
    const scope = this.host.scope();
    if (!merge) this.state.set({ kind: "loading" });
    try {
      const page = await readExecutionLogs(this.host.api(), query);
      if (generation !== this.generation || scope !== this.host.scope())
        return "dropped";
      const chain = this.chain;
      const merged = chain
        ? mergeFollowed(this.rows(), page.rows, !!page.nextCursor)
        : { rows: page.rows, replaced: true };
      if (merged.replaced || !chain)
        this.chain = {
          generation,
          scope,
          environment,
          query,
          cursor: page.nextCursor,
        };
      this.show(withoutRepeats(merged.rows));
      this.state.set({ kind: "ready" });
      return "applied";
    } catch (error) {
      if (generation !== this.generation || scope !== this.host.scope())
        return "dropped";
      this.fail(error, environment);
      return "failed";
    }
  }
  /** The next page of the chain shown, with its own query. */
  async loadOlder(): Promise<ReadResult> {
    const chain = this.chain;
    if (this.appending() || !chain?.cursor || this.limited()) return "dropped";
    this.appending.set(true);
    try {
      const page = await readExecutionLogs(this.host.api(), {
        ...chain.query,
        cursor: chain.cursor,
      });
      if (
        this.chain !== chain ||
        chain.generation !== this.generation ||
        chain.scope !== this.host.scope()
      )
        return "dropped";
      this.chain = { ...chain, cursor: page.nextCursor };
      this.show(withoutRepeats([...this.rows(), ...page.rows]));
      return "applied";
    } catch (error) {
      if (this.chain !== chain || chain.scope !== this.host.scope())
        return "dropped";
      this.fail(error, chain.environment);
      return "failed";
    } finally {
      if (this.chain?.generation === chain.generation || !this.chain)
        this.appending.set(false);
    }
  }
  private show(rows: LogRow[]) {
    const limited = rows.length > MOST_ROWS;
    if (limited && this.chain) this.chain = { ...this.chain, cursor: null };
    this.rows.set(limited ? rows.slice(0, MOST_ROWS) : rows);
    this.limited.set(limited || (this.limited() && !!this.rows().length));
    this.more.set(!!this.chain?.cursor && !this.limited());
  }
  private fail(error: unknown, environment: string) {
    const failure = logFailure(error);
    if (failure.kind === "unserved") this.unserved.add(environment);
    // Entries stay only beside a slow or failed read of the same filters.
    if (failure.kind !== "slow" && failure.kind !== "error") {
      this.chain = null;
      this.rows.set([]);
      this.more.set(false);
    }
    this.state.set(failure);
  }
}
