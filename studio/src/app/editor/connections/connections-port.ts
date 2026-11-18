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
// What step details know about connections in the environment Studio is
// connected to, for one account: the connections each connector offers, the
// slots' kept choices on this computer, the last test of each, and the
// environment's test policy. Made on first use from the shell's profile and
// account and replaced as soon as either changes, so nothing an answer brings
// back reaches another account, environment or platform.
import { signal } from "@angular/core";
import { describeError } from "../../errors";
import { testCopy, testOrigin } from "../../integrations/connection-copy";
import type { StepDetailsHost } from "../ndv/step-details-host";
import { connectedPrefix } from "../state/browser-store";
import type { EnvironmentTestPolicy } from "../state/execution";
import { studioBackend } from "../state/studio-storage";
import {
  platformConnections,
  type BindableItem,
  type ConnectionsApi,
} from "./connections-api";
import { DevBindings, followRevision, type DevBinding } from "./dev-bindings";
import type { CreatedConnection } from "../../integrations/http-connection-form";
import type { ConnectionSheetRequest } from "./connection-sheet";
import { StepTestClient } from "../state/step-test-client";
import { StepTestService } from "../state/step-test-service";
import { allSteps } from "../ndv/step-walk";

/** What a step test needs from the editor that hosts step details. */
export type StepTestHost = Pick<
  StepDetailsHost,
  | "api"
  | "stepDataScope"
  | "draftId"
  | "draftRevision"
  | "model"
  | "flushAutosave"
  | "runCommand"
  | "error"
  | "testDataPrints"
  | "execution"
  | "realSetup"
  | "realStep"
>;

export type PortHost = Pick<
  StepDetailsHost,
  "api" | "profile" | "can" | "runAccount" | "stepDataScope" | "environmentName"
>;

/** A test policy read counts for a minute. */
export const POLICY_TTL_MS = 60_000;
/** After a failed policy read, the first pause before reading again; it doubles up to a minute. */
export const POLICY_RETRY_MS = 5_000;
export const POLICY_RETRY_MAX_MS = 60_000;

/** One connector's connections, as the platform listed them. */
export interface BindableList {
  items: BindableItem[] | null;
  error: string | null;
}
type Policy = EnvironmentTestPolicy | "unreadable";

export class ConnectionsPort {
  readonly api: ConnectionsApi;
  private bindings: DevBindings | null = null;
  /** Bindings load from this computer once the account's prefix is known. */
  readonly ready: Promise<void>;
  private readonly changed = signal(0);
  /** Changes whenever a list, binding, test or policy changes. */
  readonly version = this.changed.asReadonly();
  private alive = true;
  private readonly lists = new Map<string, BindableList>();
  private readonly loading = new Map<
    string,
    { token: object; load: Promise<void> }
  >();
  private policyKept: { at: number; value: Policy } | null = null;
  private policyLoad: Promise<Policy | null> | null = null;
  /** The last failed policy read, and how long to wait before the next one. */
  private policyFailed: { at: number; pause: number } | null = null;
  /** The last failed test's sentence, per connection revision. */
  private readonly failures = new Map<string, string>();
  /** Slots whose binding was dropped this session, with why. */
  private readonly dropped = new Map<string, string>();
  /** Slots whose connection is being tested now. */
  private readonly testing = new Set<string>();
  /** Create connection or Edit, open in the side sheet over step details. */
  readonly sheet = signal<{
    request: ConnectionSheetRequest;
    saved(created: CreatedConnection): void;
  } | null>(null);
  /** Fix and bind(): the Connection field of this slot opens its choices. */
  readonly opening = signal<{ slot: string; seq: number } | null>(null);

  constructor(
    readonly key: string,
    private readonly host: PortHost,
    readonly environmentId: string,
    private readonly now: () => number = () => Date.now(),
  ) {
    // Requests go to the environment this port was made for, never a later one.
    this.api = platformConnections(host.api, environmentPath(host.api));
    const account = host.runAccount;
    this.ready = (async () => {
      const prefix = account
        ? await connectedPrefix(account).catch(() => null)
        : null;
      if (!this.alive) return;
      const bindings = new DevBindings(studioBackend(), prefix);
      await bindings.load();
      if (!this.alive) return;
      this.bindings = bindings;
      this.bump();
    })();
  }

  private bump() {
    this.changed.update((n) => n + 1);
  }
  /** The environment as sentences name it: "local", or "this environment" before its name is known. */
  get environment(): { id: string; name: string } {
    return {
      id: this.environmentId,
      name: this.host.environmentName || "this environment",
    };
  }
  get canManage(): boolean {
    return this.host.can("connection.manage");
  }
  get live(): boolean {
    return this.alive;
  }

  /** The connections a connector offers here; loads them on first ask. */
  list(connector: string): BindableList | null {
    this.version();
    const kept = this.lists.get(connector);
    if (!kept && !this.loading.has(connector)) void this.reload(connector);
    return kept ?? null;
  }
  /** The connector's list once the platform answered. */
  async listed(connector: string): Promise<BindableList> {
    const kept = this.lists.get(connector);
    if (kept) return kept;
    await (this.loading.get(connector)?.load ?? this.reload(connector));
    return this.lists.get(connector) ?? { items: null, error: null };
  }
  /** Asks the platform again (after a connection was created or edited). */
  reload(connector: string): Promise<void> {
    const token = {};
    const load = this.fetchList(connector, token);
    this.loading.set(connector, { token, load });
    return load;
  }
  private async fetchList(connector: string, token: object): Promise<void> {
    let next: BindableList;
    try {
      next = { items: await this.api.bindable(connector), error: null };
    } catch (error) {
      next = { items: null, error: describeError(error).message };
    }
    if (!this.alive || this.loading.get(connector)?.token !== token) return;
    this.loading.delete(connector);
    this.lists.set(connector, next);
    this.bump();
  }

  binding(workflow: string, slot: string): DevBinding | null {
    this.version();
    return this.bindings?.get(this.environmentId, workflow, slot) ?? null;
  }
  async setBinding(
    workflow: string,
    slot: string,
    value: DevBinding | null,
  ): Promise<void> {
    await this.ready;
    if (!this.alive || !this.bindings) return;
    if (value) this.dropped.delete(`${workflow}\u0000${slot}`);
    await this.bindings.set(this.environmentId, workflow, slot, value);
    this.bump();
  }
  async renameSlot(workflow: string, from: string, to: string): Promise<void> {
    await this.ready;
    if (!this.alive || !this.bindings) return;
    await this.bindings.renameSlot(this.environmentId, workflow, from, to);
    this.bump();
  }
  /** A kept binding follows its connection's newest revision in the list. */
  async follow(workflow: string, slot: string, item: BindableItem) {
    const kept = this.binding(workflow, slot);
    if (!kept || kept.connectionId !== item.connectionId) return;
    const next = followRevision(kept, item);
    if (next !== kept) await this.setBinding(workflow, slot, next);
  }

  /**
   * Forgets a slot's binding to a connection that's no longer listed, and
   * keeps saying why for the rest of the session.
   */
  async drop(workflow: string, slot: string, why: string): Promise<void> {
    this.dropped.set(`${workflow}\u0000${slot}`, why);
    await this.setBinding(workflow, slot, null);
  }
  /** Why a slot's binding was dropped this session, until another is chosen. */
  droppedWhy(workflow: string, slot: string): string | null {
    this.version();
    return this.dropped.get(`${workflow}\u0000${slot}`) ?? null;
  }
  /** Why the last test of this revision failed, or null. */
  failure(revisionId: string): string | null {
    this.version();
    return this.failures.get(revisionId) ?? null;
  }
  isTesting(workflow: string, slot: string): boolean {
    this.version();
    return this.testing.has(`${workflow}\u0000${slot}`);
  }
  /**
   * Tests the connection bound to a slot and keeps how it went. The answer is
   * kept only while the slot is still bound to the revision that was tested.
   */
  async test(
    workflow: string,
    slot: string,
    origin: string | null,
  ): Promise<void> {
    const bound = this.binding(workflow, slot);
    const key = `${workflow}\u0000${slot}`;
    if (!this.alive || !bound || this.testing.has(key)) return;
    this.testing.add(key);
    this.bump();
    let ok = false;
    let message: string | null = null;
    try {
      const result = await this.api.test(bound.revisionId);
      ok = result.ok;
      message = ok
        ? null
        : (result.message ??
          testCopy.failed(testOrigin(origin ?? ""), result.reason));
    } catch (error) {
      message = describeError(error).message;
    } finally {
      this.testing.delete(key);
    }
    if (!this.alive) return;
    const current = this.binding(workflow, slot);
    if (!current || current.revisionId !== bound.revisionId) return this.bump();
    if (message) this.failures.set(bound.revisionId, message);
    else this.failures.delete(bound.revisionId);
    await this.setBinding(workflow, slot, {
      ...current,
      tested: { ok, at: new Date(this.now()).toISOString() },
    });
  }

  /**
   * The test policy as last read (for a minute), loading it when there is none
   * or it's old. Rendering calls this, so a read that failed isn't asked again
   * until its pause is over: 5 seconds, doubling to a minute while it keeps failing.
   */
  policyNow(): Policy | null {
    this.version();
    const kept = this.policyKept;
    const stale = !kept || this.now() - kept.at > POLICY_TTL_MS;
    const failed = this.policyFailed;
    const waiting = !!failed && this.now() - failed.at < failed.pause;
    if (stale && !waiting) void this.policy();
    return kept?.value ?? null;
  }
  /** The environment's test policy; "unreadable" when this account can't read it, null when it couldn't be read. */
  policy(): Promise<Policy | null> {
    const kept = this.policyKept;
    if (kept && this.now() - kept.at <= POLICY_TTL_MS)
      return Promise.resolve(kept.value);
    this.policyLoad ??= (async () => {
      let value: Policy | null = null;
      try {
        value = await this.api.policy();
      } catch {
        value = null;
      }
      if (!this.alive) return null;
      this.policyLoad = null;
      if (value) {
        this.policyKept = { at: this.now(), value };
        this.policyFailed = null;
      } else {
        const pause = this.policyFailed
          ? Math.min(this.policyFailed.pause * 2, POLICY_RETRY_MAX_MS)
          : POLICY_RETRY_MS;
        this.policyFailed = { at: this.now(), pause };
      }
      this.bump();
      return value;
    })();
    return this.policyLoad;
  }

  /** Opens the Connection field of this slot (Fix, or another field asking to bind). */
  open(slot: string) {
    this.opening.set({ slot, seq: (this.opening()?.seq ?? 0) + 1 });
  }

  /** Releases chosen this session for dependencies here: one question per dependency. */
  readonly releases = new Map<string, string>();
  private service: StepTestService | null = null;
  /**
   * The step test service of this account and environment, made on first
   * use. It outlives step details (closing them keeps following) and goes
   * with the port.
   */
  stepTests(host: StepTestHost): StepTestService {
    if (this.service) return this.service;
    const port = this;
    this.service = new StepTestService({
      client: new StepTestClient(host.api),
      scope: () => host.stepDataScope ?? "",
      environment: () => (port.alive ? port.environment : null),
      draft: () =>
        host.draftId
          ? { id: host.draftId, revision: host.draftRevision ?? 0 }
          : null,
      hasStep: (stepId) =>
        allSteps(host.model.definition.spec.steps).some(
          (step) => step.id === stepId,
        ),
      saveFirst: async () => {
        // A draft the platform hasn't had yet is saved as Save draft does.
        if (host.draftRevision === undefined) await host.runCommand("save");
        const saved = (await host.flushAutosave?.()) ?? true;
        const revision = host.draftRevision;
        return saved && revision !== undefined
          ? { ok: true, revision }
          : {
              ok: false,
              message: host.error || "the workflow isn't saved to the platform",
            };
      },
      record: (stepId, view, timeoutSeconds) => {
        const prints = host.testDataPrints?.()?.get(stepId);
        host.execution?.recordTestCall(
          stepId,
          view,
          port.environment,
          // Without the shell's fingerprints the result reads as out of date.
          prints?.execution ?? "fnv1a64:",
          timeoutSeconds,
        );
      },
      releases: this.releases,
    });
    return this.service;
  }
  /** The service when one was made; null before the first Execute in an environment. */
  runningTests(): StepTestService | null {
    return this.service;
  }
  /** Steps whose Execute in this environment found no connection for a slot, per draft: their tile asks for one. */
  attempted(
    host: Pick<StepTestHost, "realSetup" | "draftId">,
    stepId: string,
    slot: string,
  ) {
    const draft = host.draftId ?? "";
    const kept = host.realSetup?.();
    const steps = new Map(kept?.draft === draft ? kept.steps : []);
    steps.set(stepId, slot);
      tracking: (stepId) => host.realStep?.set(stepId),
    host.realSetup?.set({ draft, steps });
  }
  /** A slot got its connection: its steps need nothing more. */
  bound(host: Pick<StepTestHost, "realSetup">, slot: string) {
    const kept = host.realSetup?.();
    if (!kept || ![...kept.steps.values()].includes(slot)) return;
    host.realSetup?.set({
      draft: kept.draft,
      steps: new Map([...kept.steps].filter(([, name]) => name !== slot)),
    });
  }

  /** Drops everything: the account, environment or platform changed. Writes no signal, so a template may call it. */
  forget() {
    this.alive = false;
    // The service's fences read this port's environment, which is gone now;
    // its own state (a signal) is cleared after the render that replaced the port.
    const service = this.service;
    if (service) queueMicrotask(() => service.forget());
  }
}

const ports = new WeakMap<object, ConnectionsPort>();

function environmentPath(api: PortHost["api"]): string | undefined {
  try {
    return api.environment;
  } catch {
    return undefined;
  }
}

/**
 * The connections port of the account, environment and platform shown now,
 * or null when Studio isn't connected to an environment. A change of any of
 * them replaces the port, and the old one ignores whatever answers it later.
 */
export function connectionsPort(host: PortHost): ConnectionsPort | null {
  const profile = host.profile;
  if (!profile?.environmentId) return null;
  const key = [
    profile.name,
    profile.baseUrl,
    profile.tenantId,
    profile.projectId,
    profile.environmentId,
    host.stepDataScope ?? "",
  ].join("\u0000");
  const kept = ports.get(host);
  if (kept?.key === key) return kept;
  kept?.forget();
  const port = new ConnectionsPort(key, host, profile.environmentId);
  ports.set(host, port);
  return port;
}
