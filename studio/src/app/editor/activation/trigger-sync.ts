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
import type { TriggerRow } from "./trigger-plan";
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
  /** The connection revision the dialog chose for each kind that needs one. */
  connections: Partial<Record<"broker" | "email" | "provider", string>>;
}
export type SyncStatus = "created" | "moved" | "stopped" | "failed";
export interface SyncResult {
  row: TriggerRow;
  status: SyncStatus;
  /** "Created", "Moved", "Stopped", or what went wrong. */
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
        ...(kind === "broker" || kind === "email" || kind === "provider"
          ? context.connections[kind]
            ? { connectionRevisionId: context.connections[kind] }
            : {}
          : {}),
      },
    );
    const name =
      typeof intent["name"] === "string" && intent["name"]
        ? intent["name"]
        : `the ${NOUNS[kind]}`;
    return outcome.ok
      ? {
          row,
          status: "created",
          text: "Created",
          resourceId: outcome.resource.id,
        }
      : failed(row, `Couldn't create ${name}: ${reason(outcome.refusal)}`);
  }
  if (!resource) return failed(row, "Couldn't set up this trigger.");
  const label = `${NOUNS[kind]} ${resource.name}`;
  if (row.kind === "retarget") {
    const outcome = await retarget(
      api,
      kind as RetargetKind,
      resource.id,
      activationId,
      resource.revision,
    );
    return outcome.ok
      ? { row, status: "moved", text: "Moved", resourceId: resource.id }
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
      ? { row, status: "moved", text: "Moved", resourceId: resource.id }
      : failed(row, `Couldn't move the schedule: ${reason(outcome.refusal)}`);
  }
  const stopped = await stopResource(api, kind, resource.id, resource.revision);
  return stopped.ok
    ? { row, status: "stopped", text: "Stopped", resourceId: resource.id }
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

/** A trigger choice's settings, without what Studio stores about its resources. */
export function settingsKey(intent: TriggerIntent): string {
  const {
    id: _id,
    resources: _resources,
    activated: _activated,
    ...settings
  } = intent;
  return canonicalJson(settings);
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
