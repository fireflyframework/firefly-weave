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
import type { CanvasSidecar } from "../state/canvas-sidecar";
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
            <li class="activation-row" [attr.data-kind]="row.kind">
              <span>{{ row.text }}</span>
              <!-- An email source has no stop operation: it keeps running. -->
              @if (row.kind === "left-behind" && row.resourceKind !== "email") {
                <label class="activation-stop-it">
                  <input
                    type="checkbox"
                    [checked]="row.stop"
                    (change)="stopIt($index, $any($event.target).checked)"
                  />Stop it
                </label>
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
    this.failures.set(failures);
    this.state.set("ready");
  }
}
