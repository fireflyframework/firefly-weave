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
import { Icon } from "../../icon";
import type { Workflow } from "../../model";
import {
  readDeclaration,
  type EventDeclaration,
} from "../triggers/event-trigger";
import type { CanvasSidecar, TriggerIntent } from "../state/canvas-sidecar";
import { triggerIntents } from "../state/canvas-sidecar";
import { missingFeatures } from "./activation-rows";
import { activationRun } from "./activation-run";
import {
  callPins,
  calledReferenceSites,
  proposePins,
  type CalleeActivation,
  type CalleeRow,
  type CalleeVersion,
} from "./callee-pins";
import {
  EventActivationRow,
  type EventRowAnswer,
} from "./event-activation-row";
import { joined } from "./event-bindings";
import { eventResultView, eventRowView, resultIcon } from "./event-rows";
import {
  triggerPlan,
  withStop,
  type PlanActivation,
  type TriggerRow,
} from "./trigger-plan";
import {
  platformConnections,
  type BindableItem,
} from "../connections/connections-api";
import {
  attention,
  chooseResult,
  connectionNeed,
  type ConnectionNeed,
  needsSync,
  placeOf,
  retryResult,
  syncHeld,
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
  type TriggerResource,
} from "./trigger-resources-api";

/** Pages read of each list the dialog checks (published versions, activations). */
const MAX_PAGES = 10;
/** Activations per page, and pages read: the list keeps no order, so the newest can be on any of them. */
const ACTIVATIONS = { limit: 100, pages: 100 };

const PLURALS: Record<ResourceKind, string> = {
  webhook: "webhooks",
  schedule: "schedules",
  broker: "Kafka triggers",
  email: "email sources",
  provider: "app event sources",
  event: "platform event triggers",
};

const record = (value: unknown): Record<string, unknown> =>
  isJsonObject(value) ? (value as Record<string, unknown>) : {};
const text = (value: unknown) => (typeof value === "string" ? value : "");

@Component({
  selector: "weave-activation-steps",
  standalone: true,
  imports: [EventActivationRow, Icon],
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
              [class.event-row]="row.event"
              [attr.data-row]="$index"
              [attr.data-kind]="result ? result.status : row.kind"
              [attr.aria-label]="row.event && !synced() ? row.text : null"
            >
              @if (eventViews().get($index); as view) {
                <weave-event-activation-row
                  [view]="view"
                  [textId]="'trigger-row-' + $index"
                  [busy]="acting()"
                  (stop)="stopIt($index, $event)"
                  (answer)="answer($index, $event)"
                />
              } @else {
                <span class="activation-said">
                  @if (result) {
                    <weave-icon [name]="resultIcon(result.status)" size="16" />
                  }
                  <span [id]="'trigger-row-' + $index">{{
                    result ? result.text : row.text
                  }}</span>
                </span>
              }
              @if (!row.event && result?.status === "failed") {
                <!-- It keeps focus while it runs. -->
                <button
                  type="button"
                  class="sm"
                  [attr.aria-disabled]="acting() || null"
                  (click)="answer($index, 'retry')"
                >
                  Retry
                </button>
              }
              @if (row.kind === "schedule-input" && row.intentId) {
                <!-- The way to fix it: the schedule's step details, where its run input is. -->
                <button
                  type="button"
                  class="sm"
                  [disabled]="busy()"
                  (click)="openTrigger(row.intentId)"
                >
                  Open schedule
                </button>
              }
              <!-- An email source has no stop operation: it keeps running. -->
              @if (
                !synced() &&
                !row.event &&
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
    /* What happened, after its icon, as a platform event trigger's row says it. */
    .activation-said {
      display: flex;
      align-items: flex-start;
      gap: var(--space-3);
      min-width: 0;
    }
    .activation-said weave-icon {
      flex: none;
      margin-top: 2px;
    }
    /* A platform event trigger's row lays its own text and controls out. */
    .activation-row.event-row {
      align-items: flex-start;
      padding: 10px var(--space-3);
    }
    .activation-row[data-kind="blocked"],
    .activation-row[data-kind="schedule-input"],
    .activation-row[data-kind="missing"],
    .activation-row[data-kind="not-callable"],
    .activation-row[data-kind="failed"] {
      color: var(--danger-ink);
    }
    .activation-row[data-kind="changed"] {
      color: var(--warning-ink);
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
    /* On a phone each control on a row is a full touch target. */
    @media (max-width: 767px) {
      .activation-connection select,
      .activation-row button {
        min-height: var(--control-touch, 44px);
      }
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
  /** The environment, as the dialog names it, and `short` as a sentence does ("development"). */
  readonly environment = input<{
    id: string;
    name: string;
    short?: string | null;
  } | null>(null);
  /** The editor's last check of the workflow, for a callee that refuses this caller. */
  readonly diagnostics = input<readonly Diagnostic[]>([]);

  readonly resultIcon = resultIcon;
  readonly state = signal<"loading" | "ready">("loading");
  readonly stop = signal<string | null>(null);
  readonly callees = signal<CalleeRow[]>([]);
  readonly triggers = signal<TriggerRow[]>([]);
  readonly failures = signal<string[]>([]);
  /**
   * The revision of the workflow's newest activation here, which the next one
   * replaces; null when it has none, undefined when Studio can't read it.
   */
  readonly replaces = signal<number | null | undefined>(undefined);
  /** The person may not read the activations here. */
  readonly replacesForbidden = signal(false);
  /** After the activation: what happened to each trigger row, by its place in the list. */
  readonly results = signal<ReadonlyMap<number, SyncResult>>(new Map());
  readonly synced = signal(false);
  /** While a row's Retry, Stop it or answer runs. */
  readonly acting = signal(false);
  private context: SyncContext | null = null;
  /** Where what the dialog shows was read, and as whom. */
  private place: string | null = null;
  /** What each platform event trigger's row shows, by its place in the list. */
  readonly eventViews = computed(
    () =>
      new Map(
        this.triggers().flatMap((row, index) => {
          if (!row.event) return [];
          return [
            [
              index,
              this.synced()
                ? eventResultView(
                    row.event,
                    this.results().get(index)?.event ?? null,
                  )
                : eventRowView(row.event),
            ] as const,
          ];
        }),
      ),
  );
  /** For each new trigger that runs on a connection, by its place in the list: what it can run on. */
  readonly connectionsFor = signal<ReadonlyMap<number, TriggerConnection>>(
    new Map(),
  );
  /** The connection revision chosen for each new trigger, by trigger choice. */
  readonly connectionChoices = signal<Readonly<Record<string, string>>>({});
  /** Called after each Retry, Stop it or answer, with its row, so the dialog can keep what it did. */
  retried: (row: number) => void = () => undefined;
  /** True while the dialog has to stay open: nothing may open behind it. */
  readonly busy = signal(false);
  /** Opens a trigger choice's step details; the dialog closes first. */
  openTrigger: (intentId: string) => void = () => undefined;

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
    return needsSync(this.triggers());
  }
  chooseConnection(intentId: string, revisionId: string) {
    this.connectionChoices.update((choices) => ({
      ...choices,
      [intentId]: revisionId,
    }));
  }
  /** Sets the triggers up for the activation that just succeeded, row by row. */
  async sync(activationId: string): Promise<void> {
    this.context = {
      api: this.api(),
      place: this.place,
      activationId,
      environmentId: this.environment()?.id ?? "",
      environment: this.environment()?.short || this.environment()?.name,
      inputSchema: this.activatedVersion().inputSchema,
      intents: triggerIntents(this.canvas()),
      connections: { ...this.connectionChoices() },
    };
    this.synced.set(true);
    await syncTriggers(this.triggers(), this.context, this.keep);
  }
  private readonly keep = (index: number, result: SyncResult) =>
    this.results.update((map) => new Map(map).set(index, result));
  /** A row's button: Retry and Stop it try it once more, the two answers say what to do with a trigger whose events wait. */
  async answer(index: number, answer: EventRowAnswer) {
    const result = this.results().get(index);
    const context = this.context;
    if (!result || !context || this.acting()) return;
    this.acting.set(true);
    try {
      this.keep(
        index,
        answer === "retry"
          ? await retryResult(result, context)
          : await chooseResult(result, answer, context),
      );
      // The duplicates that waited for the declared trigger are stopped now.
      await syncHeld(this.triggers(), this.results(), context, this.keep);
    } finally {
      this.acting.set(false);
      this.retried(index);
    }
  }
  /** "Done keeps notify-team for now.", while a trigger's waiting events are still to be chosen; null otherwise. */
  readonly doneKeeps = computed(() => {
    const names = this.triggers().flatMap(({ event }, index) =>
      this.results().get(index)?.status === "waiting" &&
      event &&
      "binding" in event
        ? [event.binding.name]
        : [],
    );
    return names.length ? `Done keeps ${joined(names)} for now.` : null;
  });
  /** The trigger choices with what was created or moved kept for this environment. */
  storedIntents(): TriggerIntent[] {
    return withStoredResources(
      triggerIntents(this.canvas()),
      this.environment()?.id ?? "",
      [...this.results().values()],
      this.triggers(),
    );
  }
  /** "1 trigger needs attention.", or null. */
  attention(): string | null {
    return attention([...this.results().values()]);
  }
  stopIt(index: number, stop: boolean) {
    this.triggers.update((rows) =>
      rows.map((row, at) => (at === index ? withStop(row, stop) : row)),
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

  /** The activations here, and whether these are all of them. */
  private async activationsHere(): Promise<{
    items: Record<string, unknown>[];
    complete: boolean;
  }> {
    const items: Record<string, unknown>[] = [];
    let cursor: string | undefined;
    for (let page = 0; page < ACTIVATIONS.pages; page++) {
      const answer = await this.api().page(
        "activations",
        true,
        cursor,
        {},
        ACTIVATIONS.limit,
      );
      items.push(...answer.items);
      if (!answer.next_cursor) return { items, complete: true };
      cursor = answer.next_cursor;
    }
    return { items, complete: false };
  }

  /** One activation as the plan knows it: the workflow and version it runs. */
  private async activation(id: string): Promise<PlanActivation> {
    const api = this.api();
    return { id, ...(await activationRun(api, api.environment, id)) };
  }

  /** The published version's export: its definition and compiled artifact; null when it can't be read. */
  private exported: unknown = null;
  /**
   * The version being activated: its version and input as published, read
   * from its export; the editor's only when the export can't be read (the
   * draft may have changed since it was published).
   */
  private activatedVersion(): {
    name: string;
    version: string;
    inputSchema: JsonObject;
    declaration: EventDeclaration | null;
  } {
    const definition = this.definition();
    const document = record(record(this.exported)["document"]);
    const published = record(document["spec"]);
    const metadata = record(document["metadata"]);
    const version = text(metadata["version"]);
    const inputSchema =
      "inputSchema" in published
        ? published["inputSchema"]
        : definition.spec["inputSchema"];
    return {
      name: text(metadata["name"]) || definition.metadata.name,
      version: version || definition.metadata.version,
      inputSchema: isJsonObject(inputSchema) ? (inputSchema as JsonObject) : {},
      // A version that says what its steps are says what events it declares.
      declaration: readDeclaration(
        "steps" in published
          ? { ...definition, spec: published as Workflow["spec"] }
          : definition,
      ),
    };
  }
  /** The platform's features; null when they can't be read. */
  private platformFeatures: readonly unknown[] | null = null;
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
      if (Array.isArray(platform)) this.platformFeatures = platform;
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
    this.place = placeOf(this.api());
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
    const version = this.activatedVersion();
    const reacts =
      version.declaration !== null ||
      intents.some((intent) => intent.kind === "event");
    const resourceful =
      reacts ||
      intents.some(
        (intent) => intent.kind !== "manual" && intent.kind !== "called",
      );
    const resources: Partial<Record<ResourceKind, ResourceList>> = {};
    const unread = new Set<ResourceKind>();
    const failures: string[] = [];
    const unreadable = (kind: ResourceKind, error: unknown) => {
      // A list that can't be read can't be checked: its triggers aren't planned.
      resources[kind] = { items: [], truncated: true };
      unread.add(kind);
      failures.push(
        error instanceof ApiError && error.status === 403
          ? `You can't check the ${PLURALS[kind]} here, so Studio leaves them as they are.`
          : `Couldn't check the ${PLURALS[kind]}: ${describeError(error).message}`,
      );
    };
    // A version that declares no platform event may still have a trigger an
    // earlier one left reacting, which only the platform knows.
    if (this.platformFeatures?.includes("flow.eventTrigger") ?? reacts)
      try {
        resources.event = await listTriggerResources(this.api(), "event");
      } catch (error) {
        if (reacts) unreadable("event", error);
      }
    const mine = (resources.event?.items ?? []).filter(
      (item) =>
        !item.disabled &&
        (item as TriggerResource<"event">).view.workflow_name === version.name,
    );
    const checked = sites.length > 0 || resourceful || mine.length > 0;
    let known = true;
    const couldnt = (error: unknown) => {
      if (known && checked)
        failures.push(
          `Couldn't check the called workflows and triggers: ${describeError(error).message}`,
        );
      known = false;
    };
    const [versions, here] = await Promise.all([
      checked
        ? this.pages("workflows", false).catch((error: unknown) => {
            couldnt(error);
            return [];
          })
        : [],
      this.activationsHere().catch((error: unknown) => {
        couldnt(error);
        this.replacesForbidden.set(
          error instanceof ApiError && error.status === 403,
        );
        return { items: [], complete: false };
      }),
    ]);
    const activations = known ? here.items : [];
    if (here.complete)
      this.replaces.set(
        here.items.reduce<number | null>(
          (newest, item) =>
            text(item["name"]) === version.name
              ? Math.max(newest ?? 0, Number(item["revision"]) || 0)
              : newest,
          null,
        ),
      );
    const published: CalleeVersion[] = versions
      .filter((item) => text(item["id"]) && text(item["version"]))
      .map((item) => ({
        id: text(item["id"]),
        name: text(item["name"]),
        version: text(item["version"]),
      }));
    const byVersionId = new Map(published.map((item) => [item.id, item]));
    const callable: CalleeActivation[] = activations.map((item) => ({
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
      proposePins(sites, published, callable, [...this.diagnostics()]),
    );
    if ((resourceful || mine.length) && known) {
      if (resourceful)
        await Promise.all(
          RESOURCE_KINDS.filter((kind) => kind !== "event").map(
            async (kind) => {
              try {
                resources[kind] = await listTriggerResources(this.api(), kind);
              } catch (error) {
                unreadable(kind, error);
              }
            },
          ),
        );
      const tooLong = RESOURCE_KINDS.some(
        (kind) => resources[kind]?.truncated && !unread.has(kind),
      );
      const planActivations: PlanActivation[] = callable.map((item) => ({
        id: item.id,
        workflow: item.name,
        version: byVersionId.get(item.versionId)?.version ?? "",
      }));
      // A trigger whose activation is past the pages read is still this
      // workflow's: without it Studio would make a second one beside it.
      const listed = new Set(callable.map((item) => item.id));
      try {
        planActivations.push(
          ...(await Promise.all(
            mine
              .filter((item) => !listed.has(item.activationId ?? ""))
              .map((item) => this.activation(item.activationId ?? "")),
          )),
        );
      } catch (error) {
        unreadable("event", error);
      }
      this.triggers.set(
        triggerPlan({
          intents,
          resources,
          activations: planActivations,
          workflow: version.name,
          version,
          declaration: version.declaration,
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
