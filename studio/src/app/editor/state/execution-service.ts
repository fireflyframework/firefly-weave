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
// Runs Execute step, Execute previous steps and Execute workflow on the
// Studio host, one at a time, and keeps the last results. Loaded with the
// designer (the shell never imports it by value).
//
// Every answer is fenced: what an execution reads (the draft, its revision,
// the account's place, the steps' fingerprints, the test data) is taken
// before its first await, and the answer lands only when the same draft, at
// the same revision, with the same steps, is still open. Results live in
// memory, and on this computer only under the account's prefix, at most
// 1 MiB.
import { signal } from "@angular/core";
import type { StudioApi } from "../../api";
import { ApiError } from "../../api";
import type { DebugCommandBody } from "../../designer/simulation/simulation-plan";
import type { Workflow } from "../../model";
import { sampleFromSchema } from "../ndv/panes/sample";
import type { Json } from "../ndv/registry";
import type { StoreBackend } from "./browser-store";
import { executeRequest } from "./execute-request";
import type {
  ExecuteRequest,
  ExecuteResponse,
  ExecuteTarget,
  SimulationRequest,
} from "./execution";
import type { StepFingerprints } from "./fingerprints";
import {
  keptText,
  lastResultsKey,
  pruneResults,
  readResults,
  withExecution,
  type LastResults,
} from "./last-results";
import type { StudioTestData } from "./test-data";

type JsonObject = { [key: string]: Json };

export interface ExecutionDraft {
  id: string;
  source: string;
  format: "yaml" | "json";
  /** Bumped by every change to the workflow. */
  revision: number;
  workflow: Workflow;
}

export interface ExecutionHost {
  api: Pick<StudioApi, "request">;
  /** Where results are kept; null keeps them in memory only. */
  backend: StoreBackend | null;
  now(): Date;
  draft(): ExecutionDraft | null;
  /** The account's storage prefix, or null when no account can own results. */
  place(): { prefix: string } | null;
  connected(): boolean;
  /** Documents of the actions the workflow owns and local definitions it uses. */
  definitions(): JsonObject[];
  testData(): StudioTestData;
  prints(): ReadonlyMap<string, StepFingerprints> | null;
  /** The workflow's input fields, sampled when test data has no input. */
  inputSchema(): Json;
}

export type ExecuteOutcome =
  | { kind: "done"; response: ExecuteResponse }
  /** The host's one slot is taken; `stopping`: by the execution Stop left running there. */
  | { kind: "busy"; stopping?: boolean }
  | { kind: "stale" }
  | { kind: "refused"; code: string; message: string; paths: string[] }
  | { kind: "conflict" }
  | { kind: "catalog" }
  | { kind: "failed"; message: string };

/** A step-through session of the host, as the run log reads it. */
export interface HostSession {
  id: string;
  revision: number;
  view: Record<string, unknown>;
}

const EXECUTE = "/studio/local/debug/execute";
const SESSIONS = "/studio/local/debug/sessions";
/** The host's slot is busy for a moment after an execution Studio itself sent. */
const BUSY_RETRY_MS = 500;
/** An execution may take the host's full 30 s, plus the way there and back. */
const EXECUTE_TIMEOUT_MS = 35_000;

const sameSteps = (
  before: ReadonlyMap<string, unknown> | null,
  after: ReadonlyMap<string, unknown> | null,
): boolean =>
  !before ||
  !after ||
  (before.size === after.size &&
    [...before.keys()].every((id) => after.has(id)));

export class ExecutionService {
  /** The execution running now, or null. */
  readonly running = signal<ExecuteTarget | null>(null);
  /** The open draft's last results. */
  readonly results = signal<LastResults | null>(null);
  /** The last answer, for the run log and a decision's paths. */
  readonly last = signal<ExecuteResponse | null>(null);
  /** Bumped by open and forget: an answer asked for before never lands. */
  private generation = 0;
  /** Bumped by every execution and by Stop: only the newest answer lands. */
  private ticket = 0;
  private session: HostSession | null = null;
  /** The draft `results` belong to: another draft's are never sent or merged. */
  private resultsDraft: string | null = null;
  /** Stop leaves the host's slot busy until that execution ends there. */
  private stopped = false;

  constructor(private readonly host: ExecutionHost) {}

  /** Opens a draft's kept results (this computer, this account), or none. */
  async open(draftId: string): Promise<void> {
    const generation = ++this.generation;
    this.running.set(null);
    this.results.set(null);
    this.last.set(null);
    this.resultsDraft = draftId;
    const place = this.host.place();
    const backend = this.host.backend;
    if (!place || !backend) return;
    const text = await backend
      .get(lastResultsKey(place.prefix, draftId))
      .catch(() => null);
    if (generation === this.generation && this.resultsDraft === draftId)
      this.results.set(readResults(text));
  }

  /** Sign-out, an account switch or a closed workflow: nothing stays in memory. */
  forget(): void {
    this.generation++;
    this.ticket++;
    this.running.set(null);
    this.results.set(null);
    this.last.set(null);
    this.session = null;
  }

  /** Stops waiting for the running execution; its answer is dropped. */
  stop(): void {
    if (this.running()) this.stopped = true;
    this.ticket++;
    this.running.set(null);
  }

  async execute(
    target: ExecuteTarget,
    options: { catalog?: "none" } = {},
  ): Promise<ExecuteOutcome> {
    if (this.running()) return { kind: "busy" };
    // Everything the answer is fenced on, before the first await.
    const draft = this.host.draft();
    if (!draft)
      return { kind: "failed", message: "Open a workflow to execute it." };
    const generation = this.generation;
    const ticket = ++this.ticket;
    const place = this.host.place();
    const prints = this.host.prints();
    // Results kept for another draft (this one's aren't read yet) stay out.
    if (this.resultsDraft !== draft.id) {
      this.resultsDraft = draft.id;
      this.results.set(null);
      this.last.set(null);
    }
    const previous = this.results();
    const body: ExecuteRequest = executeRequest({
      source: draft.source,
      format: draft.format,
      workflow: draft.workflow,
      definitions: this.host.definitions(),
      connected: this.host.connected(),
      testData: this.host.testData(),
      results: previous,
      sampleInput: sampleFromSchema(this.host.inputSchema()),
      target,
      now: this.host.now(),
    });
    if (options.catalog) body.catalog = options.catalog;
    this.running.set(target);
    let answer: ExecuteResponse | ExecuteOutcome;
    try {
      answer = await this.send(body);
    } finally {
      if (ticket === this.ticket) this.running.set(null);
    }
    const now = this.host.draft();
    if (
      ticket !== this.ticket ||
      generation !== this.generation ||
      now?.id !== draft.id ||
      now.revision !== draft.revision ||
      !sameSteps(prints, this.host.prints())
    )
      return { kind: "stale" };
    if ("kind" in answer)
      return answer.kind === "busy" && this.stopped
        ? { kind: "busy", stopping: true }
        : answer;
    this.stopped = false;
    let results = withExecution(
      previous,
      answer,
      prints ?? new Map(),
      this.host.now().toISOString(),
    );
    if (prints) results = pruneResults(results, (id) => prints.has(id));
    this.results.set(results);
    this.last.set(answer);
    await this.keep(place, draft.id, results);
    return { kind: "done", response: answer };
  }

  /** Step-through sessions for the run log, created from the same request as Execute workflow. */
  readonly sessions = {
    current: (): HostSession | null => this.session,
    create: async (): Promise<HostSession | null> => {
      const draft = this.host.draft();
      if (!draft) return null;
      const { target: _, ...body } = executeRequest({
        source: draft.source,
        format: draft.format,
        workflow: draft.workflow,
        definitions: this.host.definitions(),
        connected: this.host.connected(),
        testData: this.host.testData(),
        results: this.results(),
        sampleInput: sampleFromSchema(this.host.inputSchema()),
        target: { mode: "workflow" },
        now: this.host.now(),
      });
      const generation = this.generation;
      const session = await this.host.api.request<HostSession>(
        SESSIONS,
        "POST",
        body satisfies SimulationRequest,
      );
      if (generation !== this.generation) return null;
      return (this.session = session);
    },
    /** The session as the host keeps it now (after a 409, say). */
    read: async (): Promise<HostSession | null> => {
      const session = this.session;
      if (!session) return null;
      const latest = await this.host.api.request<HostSession>(
        `${SESSIONS}/${encodeURIComponent(session.id)}`,
      );
      if (this.session?.id !== session.id) return null;
      return (this.session = latest);
    },
    command: async (command: DebugCommandBody): Promise<HostSession | null> => {
      const session = this.session;
      if (!session) return null;
      const moved = await this.host.api.request<HostSession>(
        `${SESSIONS}/${encodeURIComponent(session.id)}/commands`,
        "POST",
        command,
        { "If-Match": `"${session.revision}"` },
      );
      if (this.session?.id !== session.id) return null;
      return (this.session = moved);
    },
    close: async (): Promise<void> => {
      const session = this.session;
      this.session = null;
      if (!session) return;
      await this.host.api
        .request(`${SESSIONS}/${encodeURIComponent(session.id)}`, "DELETE")
        .catch(() => undefined);
    },
  };

  /** One request, sent once more after a 503 the previous execution caused. */
  private async send(
    body: ExecuteRequest,
  ): Promise<ExecuteResponse | ExecuteOutcome> {
    for (let attempt = 1; ; attempt++) {
      try {
        return await this.host.api.request<ExecuteResponse>(
          EXECUTE,
          "POST",
          body,
          {},
          EXECUTE_TIMEOUT_MS,
        );
      } catch (error) {
        const outcome = outcomeOf(error);
        // Only a busy slot is worth one more try; a timeout would just time out again.
        if (outcome.kind !== "busy" || attempt > 1) return outcome;
        await new Promise((done) => setTimeout(done, BUSY_RETRY_MS));
      }
    }
  }

  private async keep(
    place: { prefix: string } | null,
    draftId: string,
    results: LastResults,
  ): Promise<void> {
    const backend = this.host.backend;
    if (!place || !backend) return;
    const key = lastResultsKey(place.prefix, draftId);
    const text = keptText(results);
    // Larger than 1 MiB: nothing is kept rather than half.
    await (text === null ? backend.remove(key) : backend.set(key, text)).catch(
      () => false,
    );
  }
}

/** A host refusal in plain terms: codes and pointers, never a value. */
function outcomeOf(error: unknown): ExecuteOutcome {
  if (!(error instanceof ApiError))
    return {
      kind: "failed",
      message:
        "Studio couldn't reach its host. Check that Weave Studio is running.",
    };
  if (error.status === 503 && error.code === "WV-STUDIO-BUSY")
    return { kind: "busy" };
  if (error.status === 503)
    return { kind: "failed", message: error.plain.message };
  if (error.status === 409) return { kind: "conflict" };
  if (error.status === 502) return { kind: "catalog" };
  const detail = (error.detail ?? {}) as {
    code?: unknown;
    message?: unknown;
    result?: { paths?: unknown };
  };
  const paths = Array.isArray(detail.result?.paths)
    ? detail.result.paths.filter(
        (path): path is string => typeof path === "string",
      )
    : [];
  return {
    kind: "refused",
    code: typeof detail.code === "string" ? detail.code : error.code,
    message: error.plain.message,
    paths,
  };
}
