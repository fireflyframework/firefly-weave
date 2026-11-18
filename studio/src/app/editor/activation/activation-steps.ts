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
// The activation dialog's checks and its last two steps, loaded with the
// dialog: whether the platform runs the workflow's language features, the
// activation each called workflow pins (`workflow_activation_ids`), and what
// activating does to each trigger. The dialog reads what to send, what to do
// after, and why Activate can't run, from here.
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
  type OnInit,
} from "@angular/core";
import { ApiError, type Diagnostic, type StudioApi } from "../../api";
import { describeError } from "../../errors";
import { isJsonObject, type JsonObject } from "../../forms/core/json";
import type { Workflow } from "../../model";
import type { CanvasSidecar, TriggerIntent } from "../state/canvas-sidecar";
import { triggerIntents } from "../state/canvas-sidecar";
import { missingFeatures } from "./activation-rows";
import {
  callPins,
  calledReferenceSites,
  proposePins,
  type CalleeActivation,
  type CalleeRow,
  type CalleeVersion,
} from "./callee-pins";
import {
  triggerPlan,
  type PlanActivation,
  type TriggerRow,
} from "./trigger-plan";
import {
  platformConnections,
  type BindableItem,
} from "../connections/connections-api";
import {
  actionable,
  attention,
  connectionNeed,
  type ConnectionNeed,
  retryResult,
  syncTriggers,
  withStoredResources,
  type SyncContext,
  type SyncResult,
} from "./trigger-sync";
import {
  RESOURCE_KINDS,
  listTriggerResources,
  type ResourceKind,
  type ResourceList,
} from "./trigger-resources-api";

/** Pages read of each list the dialog checks (published versions, activations). */
const MAX_PAGES = 10;

const PLURALS: Record<ResourceKind, string> = {
  webhook: "webhooks",
  schedule: "schedules",
  broker: "Kafka triggers",
  email: "email sources",
  provider: "app event sources",
};

const record = (value: unknown): Record<string, unknown> =>
  isJsonObject(value) ? (value as Record<string, unknown>) : {};
const text = (value: unknown) => (typeof value === "string" ? value : "");

@Component({
  selector: "weave-activation-steps",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  template: `@if (state() === "loading") {
      <p class="dialog-status" role="status">
        <span class="loading-spinner small"></span>Checking what this workflow
        needs…
      </p>
    } @else if (stop(); as stopped) {
      <p class="notice error-notice activation-stop" role="alert">
        {{ stopped }}
      </p>
    } @else {
      @if (callees().length) {
        <h3>Called workflows</h3>
        <ul class="activation-rows" aria-label="Called workflows">
          @for (row of callees(); track row.reference) {
            <li class="activation-row" [attr.data-kind]="row.kind">
              {{ row.text }}
            </li>
          }
        </ul>
      }
      @if (triggers().length || failures().length) {
        <h3>Triggers</h3>
        <ul class="activation-rows" aria-label="Triggers">
          @for (row of triggers(); track $index) {
            @let result = results().get($index);
            <li
              class="activation-row"
              [attr.data-kind]="result ? result.status : row.kind"
            >
              <span [id]="'trigger-row-' + $index">{{
                result ? result.text : row.text
              }}</span>
              @if (result?.status === "failed") {
                <button
                  type="button"
                  class="sm"
                  [disabled]="retrying() === $index"
                  (click)="retry($index)"
                >
                  Retry
                </button>
              }
              <!-- An email source has no stop operation: it keeps running. -->
              @if (
                !synced() &&
                row.kind === "left-behind" &&
                row.resourceKind !== "email"
              ) {
                <label class="activation-stop-it">
                  <input
                    type="checkbox"
                    [checked]="row.stop"
                    (change)="stopIt($index, $any($event.target).checked)"
                  />Stop it
                </label>
              }
              @if (!synced() && connectionsFor().get($index); as need) {
                <!-- A new Kafka, email or app event trigger runs on a connection of this environment. -->
                <div class="activation-connection">
                  <label
                    [id]="'trigger-connection-label-' + $index"
                    [for]="'trigger-connection-' + $index"
                    >Connection</label
                  >
                  <select
                    [id]="'trigger-connection-' + $index"
                    [attr.aria-labelledby]="
                      'trigger-row-' +
                      $index +
                      ' trigger-connection-label-' +
                      $index
                    "
                    required
                    (change)="
                      chooseConnection(need.intentId, $any($event.target).value)
                    "
                  >
                    <option
                      value=""
                      [selected]="!connectionChoices()[need.intentId]"
                    >
                      Choose a {{ need.words }} connection
                    </option>
                    @for (item of need.candidates; track item.revisionId) {
                      <option
                        [value]="item.revisionId"
                        [selected]="
                          connectionChoices()[need.intentId] === item.revisionId
                        "
                      >
                        {{ item.name }}
                      </option>
                    }
                  </select>
                  @if (need.message) {
                    <p class="hint">{{ need.message }}</p>
                  }
                </div>
              }
            </li>
          }
          @for (failure of failures(); track failure) {
            <li class="activation-row" data-kind="failed">{{ failure }}</li>
          }
        </ul>
      }
    }`,
  styles: `
    :host {
      display: block;
    }
    .activation-rows {
      display: flex;
      flex-direction: column;
      gap: var(--space-2);
      margin: 0 0 var(--space-4);
      padding: 0;
      list-style: none;
    }
    .activation-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-2);
      min-height: var(--control-touch, 44px);
      padding: var(--space-2) var(--space-3);
      background: var(--sunken);
      border-radius: var(--radius-sm);
      overflow-wrap: anywhere;
    }
    .activation-row[data-kind="blocked"],
    .activation-row[data-kind="schedule-input"],
    .activation-row[data-kind="missing"],
    .activation-row[data-kind="not-callable"],
    .activation-row[data-kind="failed"] {
      color: var(--danger-ink);
    }
    .activation-row[data-kind="created"],
    .activation-row[data-kind="moved"],
    .activation-row[data-kind="stopped"] {
      color: var(--success-ink, var(--text));
    }
    .activation-connection {
      display: grid;
      flex-basis: 100%;
      gap: var(--space-1);
    }
    .activation-stop-it {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
      min-height: var(--control-touch, 44px);
    }
  `,
})
export class ActivationSteps implements OnInit {
  readonly api = input.required<StudioApi>();
  readonly definition = input.required<Workflow>();
  readonly canvas = input.required<CanvasSidecar>();
  /** The published version being activated. */
  readonly versionId = input("");
  readonly environment = input<{ id: string; name: string } | null>(null);
  /** The editor's last check of the workflow, for a callee that refuses this caller. */
  readonly diagnostics = input<readonly Diagnostic[]>([]);

  readonly state = signal<"loading" | "ready">("loading");
  readonly stop = signal<string | null>(null);
  readonly callees = signal<CalleeRow[]>([]);
  readonly triggers = signal<TriggerRow[]>([]);
  readonly failures = signal<string[]>([]);
  /** After the activation: what happened to each trigger row, by its place in the list. */
  readonly results = signal<ReadonlyMap<number, SyncResult>>(new Map());
  readonly synced = signal(false);
  readonly retrying = signal<number | null>(null);
  private context: SyncContext | null = null;
  /** For each new trigger that runs on a connection, by its place in the list: what it can run on. */
  readonly connectionsFor = signal<ReadonlyMap<number, TriggerConnection>>(
    new Map(),
  );
  /** The connection revision chosen for each new trigger, by trigger choice. */
  readonly connectionChoices = signal<Readonly<Record<string, string>>>({});
  /** Called after each Retry, so the dialog can keep what it made. */
  retried: () => void = () => undefined;

  /** Why Activate can't run, or null. */
  readonly blocked = computed(() => {
    if (this.state() === "loading") return "Checking what this workflow needs…";
    const stopped = this.stop();
    if (stopped) return stopped;
    const callee = this.callees().find((row) => row.kind !== "proposed");
    return callee?.text ?? null;
  });

  ngOnInit() {
    void this.load();
  }

  /** `workflow_activation_ids` for the activation request. */
  pins(): Record<string, string> {
    return callPins(this.callees());
  }
  /** What to do to the triggers once the activation succeeds, with the person's Stop it choices. */
  plan(): TriggerRow[] {
    return this.triggers();
  }
  /** Whether any trigger row changes something once the activation succeeds. */
  needsSync(): boolean {
    return this.triggers().some(actionable);
  }
  chooseConnection(intentId: string, revisionId: string) {
    this.connectionChoices.update((choices) => ({
      ...choices,
      [intentId]: revisionId,
    }));
  }
  /** Sets the triggers up for the activation that just succeeded, row by row. */
  async sync(activationId: string): Promise<void> {
    const definition = this.definition();
    const inputSchema = definition.spec["inputSchema"];
    this.context = {
      api: this.api(),
      activationId,
      environmentId: this.environment()?.id ?? "",
      inputSchema: isJsonObject(inputSchema) ? (inputSchema as JsonObject) : {},
      intents: triggerIntents(this.canvas()),
      connections: { ...this.connectionChoices() },
    };
    this.synced.set(true);
    const rows = this.triggers();
    for (const [index, row] of rows.entries()) {
      if (!actionable(row)) continue;
      const [result] = await syncTriggers([row], this.context);
      this.results.update((map) => new Map(map).set(index, result));
    }
  }
  async retry(index: number) {
    const result = this.results().get(index);
    if (!result || !this.context) return;
    this.retrying.set(index);
    try {
      const next = await retryResult(result, this.context);
      this.results.update((map) => new Map(map).set(index, next));
      this.retried();
    } finally {
      this.retrying.set(null);
    }
  }
  /** The trigger choices with what was created or moved kept for this environment. */
  storedIntents(): TriggerIntent[] {
    return withStoredResources(
      triggerIntents(this.canvas()),
      this.environment()?.id ?? "",
      [...this.results().values()],
    );
  }
  /** "1 trigger needs attention.", or null. */
  attention(): string | null {
    return attention([...this.results().values()]);
  }
  stopIt(index: number, stop: boolean) {
    this.triggers.update((rows) =>
      rows.map((row, at) => (at === index ? { ...row, stop } : row)),
    );
  }

  private async pages(
    collection: string,
    environment: boolean,
  ): Promise<Record<string, unknown>[]> {
    const items: Record<string, unknown>[] = [];
    let cursor: string | undefined;
    for (let page = 0; page < MAX_PAGES; page++) {
      const answer = await this.api().page(collection, environment, cursor);
      items.push(...answer.items);
      if (!answer.next_cursor) break;
      cursor = answer.next_cursor;
    }
    return items;
  }

  /** The published version's export: its definition and compiled artifact; null when it can't be read. */
  private exported: unknown = null;
  /** The platform's features, and the workflow's from its compiled export; null when either can't be read. */
  private async features(): Promise<string | null> {
    const api = this.api();
    try {
      const [manifest, exported] = await Promise.all([
        api.request<unknown>(`${api.project}/language`),
        this.versionId()
          ? api.request<unknown>(
              `${api.project}/workflows/${encodeURIComponent(this.versionId())}/export`,
            )
          : Promise.resolve(null),
      ]);
      this.exported = exported;
      const platform = record(manifest)["features"];
      const used = record(record(record(exported)["artifact"])["executable"])[
        "features"
      ];
      if (!Array.isArray(platform) || !Array.isArray(used)) return null;
      return missingFeatures(
        used.filter((item): item is string => typeof item === "string"),
        platform.filter((item): item is string => typeof item === "string"),
      );
    } catch {
      // The activation itself still refuses an unsupported workflow.
      return null;
    }
  }

  async load() {
    this.state.set("loading");
    const stopped = await this.features();
    if (stopped) {
      this.stop.set(stopped);
      this.state.set("ready");
      return;
    }
    const definition = this.definition();
    // The calls the published version makes are the ones its activation pins.
    const document = record(this.exported)["document"];
    const sites = calledReferenceSites(
      record(record(document)["spec"])["steps"] ? document : definition,
    );
    const intents = triggerIntents(this.canvas());
    const resourceful = intents.some(
      (intent) => intent.kind !== "manual" && intent.kind !== "called",
    );
    let versions: Record<string, unknown>[] = [];
    let activations: Record<string, unknown>[] = [];
    const failures: string[] = [];
    try {
      [versions, activations] = await Promise.all([
        sites.length || resourceful
          ? this.pages("workflows", false)
          : Promise.resolve([]),
        sites.length || resourceful
          ? this.pages("activations", true)
          : Promise.resolve([]),
      ]);
    } catch (error) {
      failures.push(
        `Couldn't check the called workflows and triggers: ${describeError(error).message}`,
      );
    }
    const published: CalleeVersion[] = versions
      .filter((item) => text(item["id"]) && text(item["version"]))
      .map((item) => ({
        id: text(item["id"]),
        name: text(item["name"]),
        version: text(item["version"]),
      }));
    const byVersionId = new Map(published.map((item) => [item.id, item]));
    const known: CalleeActivation[] = activations.map((item) => ({
      id: text(item["id"]),
      name: text(item["name"]),
      revision: Number(item["revision"]) || 0,
      retired: item["retired"] === true,
      versionId: text(record(item["request"])["version_id"]),
      ...(text(item["created_at"])
        ? { createdAt: text(item["created_at"]) }
        : {}),
    }));
    this.callees.set(
      proposePins(sites, published, known, [...this.diagnostics()]),
    );
    if (resourceful && !failures.length) {
      const resources: Partial<Record<ResourceKind, ResourceList>> = {};
      const unread = new Set<ResourceKind>();
      await Promise.all(
        RESOURCE_KINDS.map(async (kind) => {
          try {
            resources[kind] = await listTriggerResources(this.api(), kind);
          } catch (error) {
            // A list that can't be read can't be checked: its triggers aren't planned.
            resources[kind] = { items: [], truncated: true };
            unread.add(kind);
            failures.push(
              error instanceof ApiError && error.status === 403
                ? `You can't check the ${PLURALS[kind]} here, so Studio leaves them as they are.`
                : `Couldn't check the ${PLURALS[kind]}: ${describeError(error).message}`,
            );
          }
        }),
      );
      const tooLong = RESOURCE_KINDS.some(
        (kind) => resources[kind]?.truncated && !unread.has(kind),
      );
      const planActivations: PlanActivation[] = known.map((item) => ({
        id: item.id,
        workflow: item.name,
        version: byVersionId.get(item.versionId)?.version ?? "",
      }));
      const inputSchema = definition.spec["inputSchema"];
      this.triggers.set(
        triggerPlan({
          intents,
          resources,
          activations: planActivations,
          workflow: definition.metadata.name,
          version: {
            version: definition.metadata.version,
            inputSchema: isJsonObject(inputSchema)
              ? (inputSchema as JsonObject)
              : {},
          },
          environmentId: this.environment()?.id ?? "",
        }).filter((row) => row.kind !== "truncated" || tooLong),
      );
    } else
      this.triggers.set(
        intents
          .filter((intent) => intent.kind === "called")
          .map((intent) => ({
            kind: "called",
            intentId: intent.id,
            text: "Callers pin this version when they activate",
          })),
      );
    await this.loadConnections();
    this.failures.set(failures);
    this.state.set("ready");
  }
  /** The connections each new Kafka, email or app event trigger can run on, the only one chosen. */
  private async loadConnections() {
    const intents = triggerIntents(this.canvas());
    const needs = new Map<number, ConnectionNeed & { intentId: string }>();
    this.triggers().forEach((row, index) => {
      if (row.kind !== "create") return;
      const intent = intents.find((item) => item.id === row.intentId);
      const need = intent ? connectionNeed(intent) : null;
      if (need && intent) needs.set(index, { ...need, intentId: intent.id });
    });
    if (!needs.size) return;
    const client = platformConnections(this.api());
    const lists = new Map<string, BindableItem[] | { message: string }>();
    const environment = this.environment()?.name || "this environment";
    await Promise.all(
      [...new Set([...needs.values()].map((need) => need.connector))].map(
        async (connector) => {
          try {
            const items = connector.includes("@")
              ? await client.bindable(connector)
              : await client.bindableNamed(connector);
            lists.set(
              connector,
              items.filter((item) => item.enabled),
            );
          } catch (error) {
            lists.set(connector, {
              message:
                error instanceof ApiError && error.status === 403
                  ? `You can't bind connections in ${environment}. Ask an administrator for the connection binder role.`
                  : `Couldn't list the connections: ${describeError(error).message}`,
            });
          }
        },
      ),
    );
    const rows = new Map<number, TriggerConnection>();
    const choices: Record<string, string> = {};
    for (const [index, need] of needs) {
      const listed = lists.get(need.connector) ?? [];
      const candidates = Array.isArray(listed) ? listed : [];
      const words = need.connector.replace("@", " ");
      rows.set(index, {
        intentId: need.intentId,
        words,
        candidates,
        message: !Array.isArray(listed)
          ? listed.message
          : candidates.length
            ? null
            : `No connection for ${words} in ${environment}. Create one in Connections.`,
      });
      if (candidates.length === 1)
        choices[need.intentId] = candidates[0].revisionId;
    }
    this.connectionsFor.set(rows);
    this.connectionChoices.set(choices);
  }
}

/** A new trigger's connection row: what it can run on, or why it can't be chosen here. */
interface TriggerConnection {
  intentId: string;
  /** "weave-kafka", or "weave-whatsapp 1.2.0". */
  words: string;
  candidates: BindableItem[];
  message: string | null;
}
