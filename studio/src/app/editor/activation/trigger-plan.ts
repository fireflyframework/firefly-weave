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
import { describeCalendar } from "../triggers/schedule";
import {
  RESOURCE_KINDS,
  type ResourceKind,
  type ResourceList,
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
  environmentId: string;
  /** The email connection chosen in the dialog, when there is one. */
  emailConnection?: { revisionId: string; name: string; mailbox?: string };
}

export type TriggerRowKind =
  | "create"
  | "retarget"
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
  /** Left behind only: whether to stop it after the activation; off until the person ticks it. */
  stop?: boolean;
}

export const TOO_MANY_TRIGGERS =
  "Too many triggers to check here. Use the CLI.";

const NOUNS: Record<ResourceKind, string> = {
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

/** The resource ID a trigger choice stored for this environment after creating it. */
function storedId(intent: TriggerIntent, environmentId: string) {
  const resources = intent["resources"];
  if (typeof resources !== "object" || resources === null) return undefined;
  const id = (resources as Record<string, unknown>)[environmentId];
  return typeof id === "string" ? id : undefined;
}

const scheduleName = (cron: string | undefined) =>
  cron ? `'${describeCalendar(cron)}'` : "without a calendar yet";

/** How people know a resource: its name, an email source's mailbox, a schedule's calendar. */
function title(resource: TriggerResource, cron?: string) {
  return resource.kind === "schedule"
    ? scheduleName(cron ?? resource.name)
    : resource.name;
}

const capitalized = (words: string) =>
  words.charAt(0).toUpperCase() + words.slice(1);

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

  const match = (intent: TriggerIntent, kind: ResourceKind) => {
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
    if (intent.kind === "called") {
      rows.push({
        kind: "called",
        intentId: intent.id,
        text: "Callers pin this version when they activate",
      });
      continue;
    }
    const kind: ResourceKind = intent.kind;
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

  for (const kind of RESOURCE_KINDS) {
    if (truncated.has(kind)) continue;
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
  if (truncated.size) rows.push({ kind: "truncated", text: TOO_MANY_TRIGGERS });
  return rows;
}
