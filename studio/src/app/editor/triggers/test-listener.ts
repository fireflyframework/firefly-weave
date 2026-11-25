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
// Listening for a webhook's test event. Studio asks the platform for a test
// URL on a test activation of the saved draft, shows it with a signed cURL,
// and watches that activation's test runs: the first run a signed request
// starts brings its payload back as the workflow's test input, and the run
// itself is the workflow executing from it. The URL expires with the
// activation; Stop listening retires the activation, and the URL with it.
import { signal } from "@angular/core";
import type { StudioApi } from "../../api";
import { describeError } from "../../errors";
import type { Json } from "../../forms/core/json";
import type { StepDetailsHost } from "../ndv/step-details-host";
import { handOverLinkedRun } from "../runs-tab/run-links";
import { readRunRecord } from "../runs-tab/run-record";
import { withInput } from "../state/test-data-ops";
import type { TriggerIntent } from "../state/canvas-sidecar";
import type {
  DraftTestActivation,
  DraftTestBindings,
  DraftTestListen,
} from "../state/execution";
import type { ServiceOutcome } from "../state/test-activation-service";
import { keptInput } from "./event-sample";
import { eventFields } from "./platform-event";
import { signedCurl } from "./webhook-help";

/**
 * Why a webhook can't listen for a test event here, as the Test event pane
 * says it; null when it can: Studio connected to an environment with test
 * calls on, and the step tester and trigger manager roles there.
 */
export function listenBlocked(
  environment: { name: string; testCalls: "enabled" | "disabled" } | null,
  can: (capability: string) => boolean,
): string | null {
  if (!environment)
    return "Listening needs a platform. Paste a sample payload instead.";
  if (environment.testCalls !== "enabled")
    return `Test calls are off in ${environment.name}. Paste a sample payload instead.`;
  if (!can("step.test") || !can("trigger.manage"))
    return `Your account can't listen for test events in ${environment.name}. Paste a sample payload instead.`;
  return null;
}

/** A read of the activation's runs every this often. */
export const LISTEN_POLL_MS = 2_000;
/** Failed reads in a row before Studio stops listening. */
const MAX_FAILURES = 3;
/** The runs read start this long before this computer's clock, which may run ahead of the platform's. */
export const CLOCK_SKEW_MS = 120_000;
/** Runs read at a time: the test activation's own Execute runs may be among them. */
const PAGE = 10;

/** What listening needs of the test activations: the listen call, and retiring. */
export interface ListenPort {
  listen(
    trigger: TriggerIntent,
    bindings: DraftTestBindings,
  ): Promise<
    ServiceOutcome<{
      listening: DraftTestListen;
      activation: DraftTestActivation;
    }>
  >;
  forget(): Promise<void>;
}
export type RunReader = Pick<StudioApi, "environment" | "request">;

export type ListenState =
  | { kind: "starting"; triggerId: string }
  | {
      kind: "listening";
      triggerId: string;
      url: string;
      curl: string;
      expiresAt: string;
    }
  | { kind: "received"; triggerId: string; runId: string }
  | { kind: "failed"; triggerId: string; message: string };

/** A test event the platform received: the run it started, and its payload as the run's input. */
export interface ReceivedEvent {
  triggerId: string;
  runId: string;
  input: Json;
}

export interface ListenerOptions {
  port: ListenPort;
  api: RunReader;
  now?: () => number;
  wait?: () => Promise<void>;
  received(event: ReceivedEvent): void;
  /**
   * Runs this editor started itself on the test activation (Execute workflow
   * in the environment): they share it, but no test URL started them.
   */
  ownRuns?: () => ReadonlySet<string>;
}

export class TestEventListener {
  readonly state = signal<ListenState | null>(null);
  /** Bumped by every start and stop: a watch of an earlier one ends. */
  private ticket = 0;
  /** Runs already taken as test events: listening again never takes them twice. */
  private readonly taken = new Set<string>();
  private readonly now: () => number;
  private readonly wait: () => Promise<void>;

  constructor(private readonly options: ListenerOptions) {
    this.now = options.now ?? (() => Date.now());
    this.wait =
      options.wait ??
      (() => new Promise((resolve) => setTimeout(resolve, LISTEN_POLL_MS)));
  }

  /** The trigger listened for now (starting, listening, or just received), or null. */
  listeningTo(): string | null {
    const state = this.state();
    return state && state.kind !== "failed" ? state.triggerId : null;
  }

  /**
   * Starts listening for the trigger's test event. `started` settles once the
   * test URL shows (or why it can't); `done` once listening ends.
   */
  start(
    trigger: TriggerIntent,
    bindings: DraftTestBindings,
    payloadSample?: Json,
  ): { started: Promise<void>; done: Promise<void> } {
    const ticket = ++this.ticket;
    const since = new Date(this.now() - CLOCK_SKEW_MS).toISOString();
    this.state.set({ kind: "starting", triggerId: trigger.id });
    const started = (async () => {
      let outcome;
      try {
        outcome = await this.options.port.listen(trigger, bindings);
      } catch (error) {
        outcome = {
          ok: false as const,
          refusal: { message: describeError(error).message },
        };
      }
      if (ticket !== this.ticket) return null;
      if (!outcome.ok) {
        this.state.set({
          kind: "failed",
          triggerId: trigger.id,
          message: outcome.refusal.message,
        });
        return null;
      }
      const { listening, activation } = outcome.value;
      if (
        typeof listening?.test_url !== "string" ||
        !listening.test_url ||
        typeof listening.expires_at !== "string"
      ) {
        this.state.set({
          kind: "failed",
          triggerId: trigger.id,
          message: "The platform didn't answer with a test URL. Listen again.",
        });
        return null;
      }
      this.state.set({
        kind: "listening",
        triggerId: trigger.id,
        url: listening.test_url,
        curl: signedCurl({
          url: listening.test_url,
          payloadSample,
          ...(typeof trigger["testSecretRef"] === "string"
            ? { secretHandle: trigger["testSecretRef"] }
            : {}),
        }),
        expiresAt: listening.expires_at,
      });
      return { activationId: activation.activation_id, listening };
    })();
    const done = started.then((held) =>
      held
        ? this.watch(
            ticket,
            trigger.id,
            held.activationId,
            since,
            held.listening,
          )
        : undefined,
    );
    return { started: started.then(() => undefined), done };
  }

  /** Ends listening because its test URL was already retired (the draft changed): says why. */
  ended(message: string): void {
    const state = this.state();
    if (!state || state.kind === "failed" || state.kind === "received") return;
    this.ticket++;
    this.state.set({ kind: "failed", triggerId: state.triggerId, message });
  }
  /** Stops listening and retires the test URL. */
  async stop(): Promise<void> {
    this.ticket++;
    this.state.set(null);
    await this.options.port.forget();
  }

  /** Watches the activation's test runs until one starts, the URL expires, or listening stops. */
  private async watch(
    ticket: number,
    triggerId: string,
    activationId: string,
    since: string,
    listening: DraftTestListen,
  ): Promise<void> {
    const { api } = this.options;
    const query = new URLSearchParams({
      activation_id: activationId,
      include_test: "true",
      started_after: since,
      order: "started_desc",
      limit: String(PAGE),
    });
    let failures = 0;
    for (;;) {
      if (ticket !== this.ticket) return;
      if (this.now() >= Date.parse(listening.expires_at)) {
        this.state.set({
          kind: "failed",
          triggerId,
          message: "The test URL expired. Listen again to get a new one.",
        });
        return;
      }
      try {
        const page = await api.request<{ items?: { id?: unknown }[] }>(
          `${api.environment}/run-summaries?${query}`,
        );
        if (ticket !== this.ticket) return;
        const own = this.options.ownRuns?.() ?? new Set<string>();
        const id = (page.items ?? [])
          .map((item) => item.id)
          .find(
            (run): run is string =>
              typeof run === "string" &&
              !!run &&
              !own.has(run) &&
              !this.taken.has(run),
          );
        if (id) {
          this.taken.add(id);
          const record = readRunRecord(
            await api.request(
              `${api.environment}/runs/${encodeURIComponent(id)}`,
            ),
          );
          if (ticket !== this.ticket) return;
          this.state.set({ kind: "received", triggerId, runId: id });
          this.options.received({
            triggerId,
            runId: id,
            input: (record?.input ?? null) as Json,
          });
          return;
        }
        failures = 0;
      } catch (error) {
        if (ticket !== this.ticket) return;
        if (++failures >= MAX_FAILURES) {
          this.state.set({
            kind: "failed",
            triggerId,
            message: `Studio stopped listening: ${describeError(error).message}`,
          });
          return;
        }
      }
      await this.wait();
    }
  }
}

/**
 * A received test event: its payload becomes the workflow's test input, as
 * one a person enters does, kept as from a test call with the run it came
 * from, and the run opens in the Runs tab, which follows it over the draft.
 */
export async function keepTestEvent(
  host: Pick<
    StepDetailsHost,
    "testData" | "model" | "draftId" | "selectTab" | "api"
  >,
  event: ReceivedEvent,
  environmentId: string,
): Promise<void> {
  const service = host.testData;
  const kept =
    service &&
    keptInput(
      host.model.definition.spec["inputSchema"],
      event.input,
      () => eventFields(host.api),
      service.sampleIds,
    );
  if (kept && "value" in kept && kept.value !== undefined) {
    const next = withInput(
      service.data(),
      kept.value,
      "test-call",
      new Date().toISOString(),
    );
    next.input = {
      ...next.input!,
      runId: event.runId,
      runEnvironmentId: environmentId,
    };
    await service.change(next, ["/input"]);
  }
  handOverLinkedRun(host.draftId ?? "", event.runId);
  await host.selectTab?.("Runs");
}
