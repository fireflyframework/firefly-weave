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
// Executing the workflow in an environment, and listening for a webhook's
// test event, both run on a test activation of the saved draft. Studio saves
// the draft first, then keeps one test activation per environment, draft,
// draft revision and bindings until a minute before it expires; a new one
// replaces it (the old one is retired). When the draft changes data, the
// platform asks for a confirmation; Studio asks the person once and creates
// the activation again with their acknowledgement. The activation is retired
// when the editor closes or the draft changes. Calls run one at a time, so
// two executions never create two activations.
import { canonicalJson, type Json } from "../../forms/core/json";
import { TEST_SECRET_REUSED } from "../triggers/trigger-forms";
import type { TriggerIntent } from "./canvas-sidecar";
import type {
  DraftTestActivation,
  DraftTestBindings,
  DraftTestListen,
  DraftTestListenRequest,
  SideEffect,
} from "./execution";
import {
  createTestActivation,
  listenForTestEvent,
  retireTestActivation,
  runTestActivation,
  type TestActivationApi,
  type TestOutcome,
  type TestRefusal,
  type TestRun,
} from "./test-activation-client";

/** A test activation is used until this long before it expires. */
export const REUSE_MARGIN_MS = 60_000;

/** What the editor gives the service. */
export interface TestActivationHost {
  /** The selected environment, or null when Studio isn't connected to one. */
  environment(): { id: string; name: string } | null;
  /** Saves the draft on the platform and answers the revision saved. */
  saveDraft(): Promise<{ draftId: string; draftRevision: number }>;
  /** Asks the person whether to run steps that change data; true runs them. */
  confirm(request: {
    sideEffect: SideEffect;
    steps: string[];
  }): Promise<boolean>;
  /** Milliseconds since the epoch. */
  now(): number;
}

export type ServiceRefusal =
  | TestRefusal
  | { kind: "cancelled"; message: string }
  | { kind: "no-environment"; message: string }
  | { kind: "not-webhook"; message: string }
  | { kind: "no-secret"; message: string };
export type ServiceOutcome<T> =
  | { ok: true; value: T }
  | { ok: false; refusal: ServiceRefusal };

interface Held {
  key: string;
  /** The saved draft it was made for. */
  draftId: string;
  draftRevision: number;
  activation: DraftTestActivation;
  /**
   * The listen settings its test URL checks, once it listens. Listening
   * again answers that URL, so other settings need another activation.
   */
  listening?: string;
}

const SAMPLE_NOUNS: Partial<Record<TriggerIntent["kind"], string>> = {
  broker: "message",
  email: "email",
  provider: "event",
};

const refused = <T>(refusal: ServiceRefusal): ServiceOutcome<T> => ({
  ok: false,
  refusal,
});

export class TestActivationService {
  private held: Held | null = null;
  private queue: Promise<unknown> = Promise.resolve();

  constructor(
    private readonly api: TestActivationApi,
    private readonly host: TestActivationHost,
  ) {}

  /** Runs `work` after every call before it has finished. */
  private serial<T>(work: () => Promise<T>): Promise<T> {
    const next = this.queue.then(work, work);
    this.queue = next.catch(() => undefined);
    return next;
  }

  /** The platform's refusal in the words of the editor. */
  private words(refusal: TestRefusal, environment: string): ServiceRefusal {
    switch (refusal.kind) {
      case "expired":
        return {
          kind: "expired",
          message:
            "The test activation expired. Execute again to start a new one.",
        };
      case "disabled":
        return {
          kind: "disabled",
          message: `Test calls are off in ${environment}. An administrator can turn them on in Settings.`,
        };
      default:
        return refusal;
    }
  }

  private async retireHeld(): Promise<void> {
    const held = this.held;
    this.held = null;
    if (!held) return;
    try {
      await retireTestActivation(this.api, held.activation.id);
    } catch {
      // The activation expires on its own; closing the editor never fails on it.
    }
  }

  /** The test activation for the saved draft and these bindings, reused while it lasts. */
  private async activation(
    environment: { id: string; name: string },
    bindings: DraftTestBindings,
    listening?: string,
  ): Promise<ServiceOutcome<DraftTestActivation>> {
    const draft = await this.host.saveDraft();
    const key = [
      environment.id,
      draft.draftId,
      draft.draftRevision,
      canonicalJson(bindings),
    ].join("\n");
    const held = this.held;
    if (
      held?.key === key &&
      this.host.now() <
        Date.parse(held.activation.expires_at) - REUSE_MARGIN_MS &&
      (listening === undefined ||
        held.listening === undefined ||
        held.listening === listening)
    )
      return { ok: true, value: held.activation };
    await this.retireHeld();
    const request = {
      draft_id: draft.draftId,
      draft_revision: draft.draftRevision,
      bindings,
    };
    let created = await createTestActivation(this.api, request);
    if (!created.ok && created.refusal.kind === "confirm") {
      const { sideEffect, steps } = created.refusal;
      if (!(await this.host.confirm({ sideEffect, steps })))
        return refused({ kind: "cancelled", message: "Nothing ran." });
      created = await createTestActivation(this.api, {
        ...request,
        acknowledged_side_effect: sideEffect,
      });
    }
    if (!created.ok)
      return refused(this.words(created.refusal, environment.name));
    this.held = {
      key,
      draftId: draft.draftId,
      draftRevision: draft.draftRevision,
      activation: created.value,
    };
    return created;
  }

  /** Forgets the held activation when the platform says it is gone. */
  private settle<T>(
    outcome: TestOutcome<T>,
    activation: DraftTestActivation,
    environment: string,
  ): ServiceOutcome<T> {
    if (outcome.ok) return outcome;
    if (
      (outcome.refusal.kind === "expired" ||
        outcome.refusal.kind === "missing") &&
      this.held?.activation.id === activation.id
    )
      this.held = null;
    return refused(this.words(outcome.refusal, environment));
  }

  /** Saves the draft and starts a test run with `input`. */
  execute(
    input: Json,
    bindings: DraftTestBindings,
    key?: string,
  ): Promise<
    ServiceOutcome<{ run: TestRun; activation: DraftTestActivation }>
  > {
    return this.serial(async () => {
      const environment = this.host.environment();
      if (!environment)
        return refused({
          kind: "no-environment",
          message:
            "Executing in an environment needs a platform. Connect Studio to one first.",
        });
      const held = await this.activation(environment, bindings);
      if (!held.ok) return held;
      const run = this.settle(
        await runTestActivation(this.api, held.value.id, input, key),
        held.value,
        environment.name,
      );
      return run.ok
        ? { ok: true, value: { run: run.value, activation: held.value } }
        : run;
    });
  }

  /** Saves the draft and listens for a webhook's test event on a test URL. */
  listen(
    trigger: TriggerIntent,
    bindings: DraftTestBindings,
  ): Promise<
    ServiceOutcome<{
      listening: DraftTestListen;
      activation: DraftTestActivation;
    }>
  > {
    return this.serial(async () => {
      if (trigger.kind !== "webhook") {
        const noun = SAMPLE_NOUNS[trigger.kind];
        return refused({
          kind: "not-webhook",
          message: noun
            ? `Paste a sample ${noun} instead.`
            : "Only a webhook listens for a test event.",
        });
      }
      // A test URL has its own secret: a signature doesn't cover the route, so
      // an event signed for production must never start a test run.
      const secret = trigger["testSecretRef"];
      if (typeof secret !== "string" || !secret)
        return refused({
          kind: "no-secret",
          message: "Choose the webhook's test secret first.",
        });
      if (secret === trigger["secretRef"])
        return refused({ kind: "no-secret", message: TEST_SECRET_REUSED });
      const environment = this.host.environment();
      if (!environment)
        return refused({
          kind: "no-environment",
          message:
            "Listening needs a platform. Paste a sample payload instead.",
        });
      const request: DraftTestListenRequest = { secret_ref: secret };
      if (typeof trigger["maxBodyBytes"] === "number")
        request.max_body_bytes = trigger["maxBodyBytes"];
      if (typeof trigger["toleranceSeconds"] === "number")
        request.tolerance_seconds = trigger["toleranceSeconds"];
      const settings = canonicalJson(request);
      const held = await this.activation(environment, bindings, settings);
      if (!held.ok) return held;
      const listening = this.settle(
        await listenForTestEvent(this.api, held.value.id, request),
        held.value,
        environment.name,
      );
      if (!listening.ok) return listening;
      if (this.held?.activation.id === held.value.id)
        this.held.listening = settings;
      return {
        ok: true,
        value: { listening: listening.value, activation: held.value },
      };
    });
  }

  /** The draft changed: its test activation no longer runs what the person sees. */
  draftChanged(): Promise<void> {
    return this.serial(() => this.retireHeld());
  }

  /** The saved draft the held activation runs, or null when none is held. */
  heldDraft(): { draftId: string; draftRevision: number } | null {
    const held = this.held;
    return held
      ? { draftId: held.draftId, draftRevision: held.draftRevision }
      : null;
  }

  /**
   * Retires the held activation unless it runs the editor's draft as it is
   * now, read when this call's turn comes; true when it retired one.
   */
  retireUnless(
    now: () => { draftId: string; draftRevision: number | undefined },
  ): Promise<boolean> {
    return this.serial(async () => {
      const held = this.held;
      const draft = now();
      if (
        !held ||
        (held.draftId === draft.draftId &&
          held.draftRevision === draft.draftRevision)
      )
        return false;
      await this.retireHeld();
      return true;
    });
  }

  /** The editor closed. */
  forget(): Promise<void> {
    return this.serial(() => this.retireHeld());
  }
}
