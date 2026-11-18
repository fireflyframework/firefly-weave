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
// After an activation succeeds, its triggers are set up row by row, in the
// order the dialog showed them: create what's missing, move what this
// workflow ran to the new activation (with the revision read), save a moved
// schedule with its calendar and input, and stop what the person ticked. A
// failure is reported on its row and never undoes the activation; Retry reads
// the resource again before it tries once more. What was created or moved is
// kept in its trigger choice for this environment, so the next activation
// moves that exact resource.
import { ApiError } from "../../api";
import {
  canonicalJson,
  type Json,
  type JsonObject,
} from "../../forms/core/json";
import type { TriggerIntent } from "../state/canvas-sidecar";
import { settingsKey } from "../triggers/trigger-activated";

export { settingsKey };
import { title, type TriggerRow } from "./trigger-plan";
import {
  createResource,
  moveSchedule,
  readResource,
  retarget,
  stopResource,
  type ResourceKind,
  type ResourceRefusal,
  type RetargetKind,
  type ScheduleView,
  type TriggerApi,
  type TriggerResource,
} from "./trigger-resources-api";

export interface SyncContext {
  api: TriggerApi;
  /** The activation that just succeeded. */
  activationId: string;
  environmentId: string;
  /** The activated version's input schema: a new webhook's or Kafka trigger's payload. */
  inputSchema: JsonObject;
  intents: TriggerIntent[];
  /** The connection revision chosen for each new trigger that needs one, by trigger choice. */
  connections: Readonly<Record<string, string>>;
}
export type SyncStatus = "created" | "moved" | "stopped" | "failed";
export interface SyncResult {
  row: TriggerRow;
  status: SyncStatus;
  /** "Created Kafka trigger orders", "Moved webhook orders-in to 1.2.0 …", "Stopped …", or what went wrong. */
  text: string;
  resourceId?: string;
}

const NOUNS: Record<ResourceKind, string> = {
  webhook: "webhook",
  schedule: "schedule",
  broker: "Kafka trigger",
  email: "email source",
  provider: "app event source",
};

/** The connection a new trigger runs on: its connector, and what the row says needs it. */
export interface ConnectionNeed {
  /** `name@version`, or a name alone when any version of it serves. */
  connector: string;
  label: string;
}
const text = (intent: TriggerIntent, key: string) =>
  typeof intent[key] === "string" ? (intent[key] as string) : "";
/** What connection a trigger needs to be created, or null when it needs none. */
export function connectionNeed(intent: TriggerIntent): ConnectionNeed | null {
  const name = text(intent, "name");
  const label = (noun: string) => (name ? `${noun} ${name}` : noun);
  switch (intent.kind) {
    case "broker":
      return { connector: "weave-kafka", label: label("Kafka trigger") };
    case "email":
      return { connector: "weave-email", label: label("Email trigger") };
    case "provider": {
      const pkg = text(intent, "package");
      const version = text(intent, "packageVersion");
      return pkg && version
        ? { connector: `${pkg}@${version}`, label: label("App event trigger") }
        : null;
    }
    default:
      return null;
  }
}

/** Whether a row changes anything after the activation. */
export const actionable = (row: TriggerRow): boolean =>
  row.kind === "create" ||
  row.kind === "retarget" ||
  row.kind === "schedule-move" ||
  (row.kind === "left-behind" && row.stop === true);

const failed = (row: TriggerRow, text: string): SyncResult => ({
  row,
  status: "failed",
  text,
});
const reason = (refusal: ResourceRefusal) => refusal.message;

async function run(
  row: TriggerRow,
  context: SyncContext,
  fresh?: TriggerResource,
): Promise<SyncResult> {
  const { api, activationId } = context;
  const kind = row.resourceKind;
  const intent = context.intents.find((item) => item.id === row.intentId);
  const resource = fresh ?? row.resource;
  if (!kind) return failed(row, "Couldn't set up this trigger.");
  if (row.kind === "create") {
    if (!intent)
      return failed(
        row,
        "Couldn't create this trigger: its trigger choice is no longer in the workflow.",
      );
    const outcome = await createResource(
      api,
      intent as TriggerIntent & { kind: ResourceKind },
      {
        activationId,
        inputSchema: context.inputSchema,
        ...(context.connections[intent.id]
          ? { connectionRevisionId: context.connections[intent.id] }
          : {}),
      },
    );
    // The row names the kind, as the plan's rows do: "Kafka trigger orders".
    const name =
      typeof intent["name"] === "string" && intent["name"]
        ? `${NOUNS[kind]} ${intent["name"]}`
        : `the ${NOUNS[kind]}`;
    return outcome.ok
      ? {
          row,
          status: "created",
          text: `Created ${name}`,
          resourceId: outcome.resource.id,
        }
      : failed(row, `Couldn't create ${name}: ${reason(outcome.refusal)}`);
  }
  if (!resource) return failed(row, "Couldn't set up this trigger.");
  const label = `${NOUNS[kind]} ${resource.name}`;
  // "to 1.2.0": the version activated, as the row said before it ran.
  const to = row.to ? ` to ${row.to}` : "";
  if (row.kind === "retarget") {
    const outcome = await retarget(
      api,
      kind as RetargetKind,
      resource.id,
      activationId,
      resource.revision,
    );
    return outcome.ok
      ? {
          row,
          status: "moved",
          text: `Moved ${label}${to}${kind === "webhook" ? " (the URL stays the same)" : ""}`,
          resourceId: resource.id,
        }
      : failed(row, `Couldn't move ${label}: ${reason(outcome.refusal)}`);
  }
  if (row.kind === "schedule-move") {
    const outcome = await moveSchedule(
      api,
      resource.view as ScheduleView,
      activationId,
      {
        ...(typeof intent?.["cron"] === "string"
          ? { cron: intent["cron"] }
          : {}),
        ...(intent && "input" in intent
          ? { input: (intent["input"] ?? null) as Json }
          : {}),
      },
    );
    return outcome.ok
      ? {
          row,
          status: "moved",
          text: `Moved schedule ${title(resource, typeof intent?.["cron"] === "string" ? intent["cron"] : undefined)}${to}`,
          resourceId: resource.id,
        }
      : failed(row, `Couldn't move the schedule: ${reason(outcome.refusal)}`);
  }
  const stopped = await stopResource(api, kind, resource.id, resource.revision);
  return stopped.ok
    ? {
        row,
        status: "stopped",
        text: `Stopped ${label}`,
        resourceId: resource.id,
      }
    : failed(row, `Couldn't stop ${label}: ${reason(stopped.refusal)}`);
}

/** Sets up every row that changes something, in order; a failure never stops the rest. */
export async function syncTriggers(
  rows: readonly TriggerRow[],
  context: SyncContext,
): Promise<SyncResult[]> {
  const results: SyncResult[] = [];
  for (const row of rows)
    if (actionable(row)) results.push(await run(row, context));
  return results;
}

/** Tries a failed row once more, reading its resource again first. */
export async function retryResult(
  result: SyncResult,
  context: SyncContext,
): Promise<SyncResult> {
  const { row } = result;
  if (row.kind === "create" || !row.resource || !row.resourceKind)
    return run(row, context);
  try {
    const fresh = await readResource(
      context.api,
      row.resourceKind,
      row.resource.id,
    );
    return run(row, context, fresh);
  } catch (error) {
    if (error instanceof ApiError)
      return failed(
        row,
        `Couldn't read the ${NOUNS[row.resourceKind]} again: ${error.message}`,
      );
    throw error;
  }
}

/** The trigger choices with each created or moved resource kept for this environment. */
export function withStoredResources(
  intents: readonly TriggerIntent[],
  environmentId: string,
  results: readonly SyncResult[],
): TriggerIntent[] {
  const made = new Map(
    results
      .filter(
        (result) =>
          (result.status === "created" || result.status === "moved") &&
          result.row.intentId &&
          result.resourceId,
      )
      .map((result) => [result.row.intentId!, result.resourceId!]),
  );
  return intents.map((intent) => {
    const id = made.get(intent.id);
    if (!id) return intent;
    const record = (value: unknown) =>
      value && typeof value === "object" && !Array.isArray(value)
        ? (value as Record<string, string>)
        : {};
    return {
      ...intent,
      resources: { ...record(intent["resources"]), [environmentId]: id },
      activated: {
        ...record(intent["activated"]),
        [environmentId]: settingsKey(intent),
      },
    };
  });
}

/** "1 trigger needs attention.", or null when every row went through. */
export function attention(results: readonly SyncResult[]): string | null {
  const count = results.filter((result) => result.status === "failed").length;
  if (!count) return null;
  return count === 1
    ? "1 trigger needs attention."
    : `${count} triggers need attention.`;
}
