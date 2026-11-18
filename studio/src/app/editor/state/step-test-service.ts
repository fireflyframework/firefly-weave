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
// Executes one step of the open draft for real in the current environment:
// saves the draft, starts the step test with one key per click, follows it
// and hands its view to the editor. One step test at a time per draft. Every
// answer is applied only while the account, environment, draft and step it
// was asked for still hold: each await is followed by that check.
import { signal } from "@angular/core";
import type { Json } from "../../forms/core/json";
import type { SideEffect, StepTestRequest, StepTestView } from "./execution";
import type {
  FollowAnswer,
  StartAnswer,
  StepTestRefusal,
} from "./step-test-client";

const SIDE_EFFECTS: ReadonlySet<string> = new Set([
  "read_only",
  "idempotent",
  "idempotency_key",
  "non_idempotent",
]);
const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** An action contract's declared side effect, from its typed `spec.sideEffect` only. */
export function sideEffectOf(contract: Json | null): SideEffect | null {
  if (!isRecord(contract) || !isRecord(contract["spec"])) return null;
  const effect = contract["spec"]["sideEffect"];
  return typeof effect === "string" && SIDE_EFFECTS.has(effect)
    ? (effect as SideEffect)
    : null;
}

/** What the service needs from the step test client. */
export interface StepTestCalls {
  start(
    body: StepTestRequest,
    key: string,
    checking?: () => void,
  ): Promise<StartAnswer>;
  follow(
    id: string,
    timeoutSeconds: number,
    onView: (view: StepTestView) => void,
    signal: AbortSignal,
  ): Promise<FollowAnswer>;
  cancel(id: string): Promise<StepTestView | null>;
}

export interface StepTestServiceHost {
  client: StepTestCalls;
  /** The account and workspace the editor's data belongs to right now. */
  scope(): string;
  environment(): { id: string; name: string } | null;
  draft(): { id: string; revision: number } | null;
  /** Whether the open draft still has this step. */
  hasStep(stepId: string): boolean;
  /** Saves the draft before it runs; the saved revision is the one executed. */
  saveFirst(): Promise<
    { ok: true; revision: number } | { ok: false; message: string }
  >;
  /** Hands a step test's ended view to the editor as the step's result. */
  record(stepId: string, view: StepTestView): void;
  /** Releases chosen for dependencies in this environment, this session. */
  releases: Map<string, string>;
  /** A new idempotency key; one per click. */
  newKey?(): string;
}

export type StepTestBody = Omit<StepTestRequest, "draft_id" | "draft_revision">;
export type StepTestOutcome =
  | { kind: "done"; view: StepTestView }
  | { kind: "refused"; refusal: StepTestRefusal }
  | {
      kind: "stopped";
      why: "timeout" | "aborted" | "lost";
      runId: string | null;
    }
  | { kind: "stale" };

export interface RunningStepTest {
  stepId: string;
  environment: { id: string; name: string };
  /** Set once the platform accepted it. */
  id: string | null;
  runId: string | null;
  /** True while Studio asks again whether a start whose answer was lost went through. */
  checking: boolean;
  /** The latest view while it runs. */
  view: StepTestView | null;
  /** When Studio sent it, in milliseconds since the epoch. */
  since: number;
}

interface Fence {
  scope: string;
  environment: string;
  draft: string;
  stepId: string;
  generation: number;
}

export class StepTestService {
  private readonly current = signal<RunningStepTest | null>(null);
  /** The step test Studio is starting or following, if any. */
  readonly running = this.current.asReadonly();
  private generation = 0;
  private busy = false;
  private following: AbortController | null = null;
  private stopRequested = false;
  private cancelled: StepTestView | null = null;

  constructor(private readonly host: StepTestServiceHost) {}

  private holds(fence: Fence): boolean {
    return (
      fence.generation === this.generation &&
      this.host.scope() === fence.scope &&
      this.host.environment()?.id === fence.environment &&
      this.host.draft()?.id === fence.draft &&
      this.host.hasStep(fence.stepId)
    );
  }

  private update(fence: Fence, change: Partial<RunningStepTest>) {
    if (!this.holds(fence)) return;
    this.current.update((running) =>
      running ? { ...running, ...change } : running,
    );
  }

  async execute(request: {
    stepId: string;
    body: StepTestBody;
    ack?: SideEffect | null;
  }): Promise<StepTestOutcome> {
    if (this.busy)
      return { kind: "refused", refusal: { kind: "busy", retryAfterMs: 0 } };
    const environment = this.host.environment();
    const draft = this.host.draft();
    if (!environment || !draft)
      return {
        kind: "refused",
        refusal: {
          kind: "failed",
          message:
            "Connect to an environment and save this workflow to the platform first.",
        },
      };
    const fence: Fence = {
      scope: this.host.scope(),
      environment: environment.id,
      draft: draft.id,
      stepId: request.stepId,
      generation: ++this.generation,
    };
    this.busy = true;
    this.stopRequested = false;
    this.cancelled = null;
    this.current.set({
      stepId: request.stepId,
      environment,
      id: null,
      runId: null,
      checking: false,
      view: null,
      since: Date.now(),
    });
    let runId: string | null = null;
    try {
      const saved = await this.host.saveFirst();
      if (!this.holds(fence)) return { kind: "stale" };
      if (!saved.ok)
        return {
          kind: "refused",
          refusal: {
            kind: "failed",
            message: `Studio couldn't save the draft, so nothing ran: ${saved.message}`,
          },
        };
      const releases: Record<string, string | null> = {
        ...(request.body.releases ?? {}),
      };
      for (const [dependency, release] of this.host.releases)
        releases[dependency] = release;
      const body: StepTestRequest = {
        ...request.body,
        releases,
        acknowledged_side_effect: request.ack ?? null,
        draft_id: draft.id,
        draft_revision: saved.revision,
      };
      if (request.ack === undefined) delete body.acknowledged_side_effect;
      const key = this.host.newKey?.() ?? crypto.randomUUID();
      const started = await this.host.client.start(body, key, () =>
        this.update(fence, { checking: true }),
      );
      if (!this.holds(fence)) return { kind: "stale" };
      if (!started.ok) return { kind: "refused", refusal: started.refusal };
      const { id } = started.accepted;
      runId = started.accepted.run_id;
      this.update(fence, { id, runId, checking: false });
      const following = new AbortController();
      this.following = following;
      if (this.stopRequested) void this.stop();
      const answer = await this.host.client.follow(
        id,
        request.body.timeout_seconds,
        (view) => {
          if (this.holds(fence)) this.update(fence, { view });
          else following.abort();
        },
        following.signal,
      );
      if (!this.holds(fence)) return { kind: "stale" };
      const view = "stopped" in answer ? this.cancelled : answer;
      if (!view)
        return {
          kind: "stopped",
          why: (answer as { stopped: "timeout" | "aborted" | "lost" }).stopped,
          runId,
        };
      this.host.record(request.stepId, view);
      return { kind: "done", view };
    } finally {
      if (fence.generation === this.generation) {
        this.busy = false;
        this.following = null;
        this.current.set(null);
      }
    }
  }

  /** Stop: cancels the step test on the platform; one still starting is canceled once it starts. */
  async stop(): Promise<void> {
    const running = this.current();
    if (!running) return;
    if (!running.id) {
      this.stopRequested = true;
      return;
    }
    const generation = this.generation;
    const view = await this.host.client.cancel(running.id);
    if (generation !== this.generation) return;
    if (view && view.status !== "running" && view.status !== "waiting") {
      this.cancelled = view;
      this.following?.abort();
    }
  }

  /**
   * Stops following and drops whatever is in flight, without canceling it on
   * the platform: on sign-out, an account or environment switch, or when the
   * workflow closes. A late answer is then stale.
   */
  forget(): void {
    this.generation++;
    this.following?.abort();
    this.following = null;
    this.busy = false;
    this.stopRequested = false;
    this.cancelled = null;
    this.current.set(null);
  }
}
