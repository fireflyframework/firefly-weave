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
// What activating a version does to each of the workflow's triggers, worked
// out before anything changes: which resources to create, which to move to
// the new activation, which can't move and why, and which of this workflow's
// running resources no trigger choice describes any more.
//
// A trigger choice finds its resource by the ID it stored for this
// environment after creating it, then by name (an email source by its
// connection); a resource counts only while an activation of this workflow
// runs it and nobody stopped it.
import { missingData } from "../../forms/core/form-model";
import { jsonEqual, type Json, type JsonObject } from "../../forms/core/json";
import type { TriggerIntent } from "../state/canvas-sidecar";
import type { EventDeclaration } from "../triggers/event-trigger";
import { describeCalendar } from "../triggers/schedule";
import { eventBindingPlan, type EventRow } from "./event-bindings";
import { pendingStopsOf, storedId } from "./stored-resources";
import {
  RESOURCE_KINDS,
  TOO_MANY_TRIGGERS,
  type ResourceKind,
  type ResourceList,
  type SetUpKind,
  type TriggerResource,
} from "./trigger-resources-api";

/** An activation in the environment: the workflow and version it runs. */
export interface PlanActivation {
  id: string;
  workflow: string;
  version: string;
  /** The version's input schema, when Studio has it. */
  inputSchema?: JsonObject;
}
export interface TriggerPlanInput {
  intents: TriggerIntent[];
  /** Each kind's resources in the environment; a missing kind counts as none. */
  resources: Partial<Record<ResourceKind, ResourceList>>;
  activations: PlanActivation[];
  /** The workflow's name. */
  workflow: string;
  /** The version being activated. */
  version: { version: string; inputSchema: JsonObject };
  /** The platform events the version declares; null or left out when it reacts to none. */
  declaration?: EventDeclaration | null;
  environmentId: string;
  /** The email connection chosen in the dialog, when there is one. */
  emailConnection?: { revisionId: string; name: string; mailbox?: string };
}

export type TriggerRowKind =
  | "create"
  | "retarget"
  /** A platform event trigger whose events or source workflows changed. */
  | "replace"
  /** A second platform event trigger for the same events. */
  | "duplicate"
  | "schedule-move"
  | "blocked"
  | "schedule-input"
  | "called"
  | "left-behind"
  | "truncated"
  /** An app event source Studio can't describe (no package or schema digest): the CLI creates it. */
  | "cli";
export interface TriggerRow {
  kind: TriggerRowKind;
  text: string;
  intentId?: string;
  resourceKind?: ResourceKind;
  /** The resource the row moves, can't move, or leaves running. */
  resource?: TriggerResource;
  /** The version its activation runs now. */
  from?: string;
  /** The version it moves to. */
  to?: string;
  /** Whether to stop it after the activation: off until the person ticks it, on for a duplicate. */
  stop?: boolean;
  /** A platform event trigger's row: what activating does to it, and how. */
  event?: EventRow;
}

const NOUNS: Record<SetUpKind, string> = {
  webhook: "webhook",
  schedule: "schedule",
  broker: "Kafka trigger",
  email: "email source",
  provider: "app event source",
};

const text = (intent: TriggerIntent, key: string) => {
  const value = intent[key];
  return typeof value === "string" && value.trim() ? value : undefined;
};

/** An app event choice with everything creating its source needs, which only the platform's packages tell. */
const describable = (intent: TriggerIntent) =>
  ["provider", "package", "packageVersion", "schemaDigest"].every(
    (key) => text(intent, key) !== undefined,
  );

const scheduleName = (cron: string | undefined) =>
  cron ? `'${describeCalendar(cron)}'` : "without a calendar yet";

/** How people know a resource: its name, an email source's mailbox, a schedule's calendar. */
export function title(resource: TriggerResource, cron?: string) {
  return resource.kind === "schedule"
    ? scheduleName(cron ?? resource.name)
    : resource.name;
}

const capitalized = (words: string) =>
  words.charAt(0).toUpperCase() + words.slice(1);

/** The row with Stop it as the person left it. */
export const withStop = (row: TriggerRow, stop: boolean): TriggerRow => ({
  ...row,
  stop,
  ...(row.event && "stop" in row.event
    ? { event: { ...row.event, stop } }
    : {}),
});

/** The platform event trigger's rows: the declared one first when the version declares one. */
function eventRows(
  input: TriggerPlanInput,
  resources: ResourceList,
): { declared: TriggerRow[]; others: TriggerRow[] } {
  const intent = input.intents.find((item) => item.kind === "event");
  const declaration = input.declaration ?? null;
  const rows = eventBindingPlan({
    declaration,
    workflow: input.workflow,
    version: input.version.version,
    storedId: intent && storedId(intent, input.environmentId),
    pendingStops: intent ? pendingStopsOf(intent, input.environmentId) : [],
    bindings: resources.items.map(
      (item) => (item as TriggerResource<"event">).view,
    ),
    activations: input.activations,
  }).map(
    (event): TriggerRow => ({
      kind: event.kind,
      ...(intent ? { intentId: intent.id } : {}),
      resourceKind: "event",
      ...("from" in event ? { from: event.from } : {}),
      ...("to" in event ? { to: event.to } : {}),
      ...("stop" in event ? { stop: event.stop } : {}),
      event,
      text: event.text,
    }),
  );
  const first = declaration === null ? 0 : 1;
  return { declared: rows.slice(0, first), others: rows.slice(first) };
}

/** One row per trigger choice, then the resources left behind, then the CLI hint when a list was too long. */
export function triggerPlan(input: TriggerPlanInput): TriggerRow[] {
  const byId = new Map(input.activations.map((item) => [item.id, item]));
  const to = input.version.version;
  const ours = (resource: TriggerResource) =>
    resource.activationId !== null &&
    byId.get(resource.activationId)?.workflow === input.workflow;
  const live = (kind: ResourceKind) =>
    (input.resources[kind]?.items ?? []).filter(
      (resource) => ours(resource) && !resource.disabled,
    );
  const truncated = new Set(
    RESOURCE_KINDS.filter((kind) => input.resources[kind]?.truncated),
  );
  const claimed = new Set<string>();
  const rows: TriggerRow[] = [];

  const read = input.resources.event;
  const events =
    read && !read.truncated
      ? eventRows(input, read)
      : { declared: [], others: [] };

  const match = (intent: TriggerIntent, kind: SetUpKind) => {
    const candidates = live(kind).filter((item) => !claimed.has(item.id));
    const stored = storedId(intent, input.environmentId);
    const byStored = candidates.find((item) => item.id === stored);
    if (byStored) return byStored;
    if (kind === "schedule") return undefined;
    if (kind === "email") {
      const connection = input.emailConnection?.revisionId;
      return candidates.find(
        (item) =>
          connection !== undefined &&
          (item as TriggerResource<"email">).view.connection_revision_id ===
            connection,
      );
    }
    const name = text(intent, "name");
    return candidates.find((item) => name !== undefined && item.name === name);
  };

  const fromOf = (resource: TriggerResource) =>
    byId.get(resource.activationId ?? "")?.version ?? "";

  /** A webhook or Kafka trigger moves when the new input is its payload, or the input it starts with now. */
  const payloadMoves = (resource: TriggerResource<"webhook" | "broker">) => {
    const target = input.version.inputSchema;
    if (jsonEqual(resource.view.payload_schema, target)) return true;
    const current = byId.get(resource.activationId ?? "")?.inputSchema;
    // Without the current input Studio can't tell; the platform checks the move.
    return current === undefined || jsonEqual(current, target);
  };

  for (const intent of input.intents) {
    if (intent.kind === "manual") continue;
    if (intent.kind === "event") {
      rows.push(...events.declared.splice(0));
      continue;
    }
    if (intent.kind === "called") {
      rows.push({
        kind: "called",
        intentId: intent.id,
        text: "Callers pin this version when they activate",
      });
      continue;
    }
    const kind: SetUpKind = intent.kind;
    if (truncated.has(kind)) continue;
    const resource = match(intent, kind);
    if (resource) claimed.add(resource.id);
    const row = { intentId: intent.id, resourceKind: kind };

    if (kind === "schedule") {
      const cron = text(intent, "cron");
      const runInput = (intent["input"] ?? null) as Json;
      if (missingData(input.version.inputSchema, runInput).length) {
        rows.push({
          kind: "schedule-input",
          ...row,
          ...(resource ? { resource } : {}),
          text: "The schedule's run input no longer fits this version. Update it in the trigger's step details.",
        });
        continue;
      }
      rows.push(
        resource
          ? {
              kind: "schedule-move",
              ...row,
              resource,
              from: fromOf(resource),
              to,
              text: `Move schedule ${title(resource, cron)} from ${fromOf(resource)} to ${to}`,
            }
          : {
              kind: "create",
              ...row,
              text: `Create schedule ${scheduleName(cron)}`,
            },
      );
      continue;
    }

    if (!resource && kind === "provider" && !describable(intent)) {
      rows.push({
        kind: "cli",
        ...row,
        text: `Create the app event source ${text(intent, "name") ?? ""} with the CLI. Studio can't read app event schemas yet.`.replace(
          "source  with",
          "source with",
        ),
      });
      continue;
    }
    if (!resource) {
      const connection = input.emailConnection;
      rows.push({
        kind: "create",
        ...row,
        text:
          kind !== "email"
            ? `Create ${NOUNS[kind]} ${text(intent, "name") ?? ""}`.trim()
            : connection
              ? `Start reading ${connection.mailbox ?? "mail"} on ${connection.name}`
              : "Start reading mail on the email connection you choose for it",
      });
      continue;
    }
    if (
      (kind === "webhook" || kind === "broker") &&
      !payloadMoves(resource as TriggerResource<"webhook" | "broker">)
    ) {
      rows.push({
        kind: "blocked",
        ...row,
        resource,
        text: `The ${NOUNS[kind]}'s payload changed. Create a new trigger for the changed input.`,
      });
      continue;
    }
    const from = fromOf(resource);
    rows.push({
      kind: "retarget",
      ...row,
      resource,
      from,
      to,
      text: `Move ${NOUNS[kind]} ${title(resource)} from ${from} to ${to}${kind === "webhook" ? " (the URL stays the same)" : ""}`,
    });
  }

  // Declared by the version, with no trigger choice in the draft any more.
  rows.push(...events.declared);
  for (const kind of RESOURCE_KINDS) {
    if (truncated.has(kind) || kind === "event") continue;
    for (const resource of live(kind)) {
      if (claimed.has(resource.id)) continue;
      const from = fromOf(resource);
      rows.push({
        kind: "left-behind",
        resourceKind: kind,
        resource,
        from,
        stop: false,
        text: `${capitalized(NOUNS[kind])} ${title(resource)} keeps running on ${from}`,
      });
    }
  }
  rows.push(...events.others);
  if (truncated.size) rows.push({ kind: "truncated", text: TOO_MANY_TRIGGERS });
  return rows;
}
