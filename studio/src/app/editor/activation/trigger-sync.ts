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
// moves that exact resource. A platform event trigger's rows follow their own
// rules (event-bindings.ts): a duplicate is stopped only once the declared
// trigger is set up, and the stops Studio still owes are kept with the choice.
import { ApiError } from "../../api";
import { describeError } from "../../errors";
import {
  canonicalJson,
  type Json,
  type JsonObject,
} from "../../forms/core/json";
import type { TriggerIntent } from "../state/canvas-sidecar";
import { settingsKey } from "../triggers/trigger-activated";

export { settingsKey };
import {
  applyEventRow,
  eventRowActionable,
  type EventResult,
  type EventRow,
  type EventStatus,
  type PendingStop,
} from "./event-bindings";
import { declinedResult, heldResult, keptResult } from "./event-rows";
import { pendingStopsOf, withPendingStops } from "./stored-resources";
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
  type SetUpKind,
  type TriggerApi,
  type TriggerResource,
} from "./trigger-resources-api";

export interface SyncContext {
  api: TriggerApi;
  /** The activation that just succeeded. */
  activationId: string;
  environmentId: string;
  /** The environment's name, for a refusal's words. */
  environment?: string;
  /** The activated version's input schema: a new webhook's or Kafka trigger's payload. */
  inputSchema: JsonObject;
  intents: TriggerIntent[];
  /** The connection revision chosen for each new trigger that needs one, by trigger choice. */
  connections: Readonly<Record<string, string>>;
}
export type SyncStatus = EventStatus;
export interface SyncResult {
  row: TriggerRow;
  status: SyncStatus;
  /** "Created Kafka trigger orders", "Moved webhook orders-in to 1.2.0 …", "Stopped …", or what went wrong. */
  text: string;
  resourceId?: string;
  /** A platform event trigger's row: all that happened to it. */
  event?: EventResult;
}

const NOUNS: Record<SetUpKind, string> = {
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
  row.event
    ? eventRowActionable(row.event)
    : row.kind === "create" ||
      row.kind === "retarget" ||
      row.kind === "schedule-move" ||
      (row.kind === "left-behind" && row.stop === true);

const failed = (row: TriggerRow, text: string): SyncResult => ({
  row,
  status: "failed",
  text,
});
const reason = (refusal: ResourceRefusal) => refusal.message;

const eventResult = (row: TriggerRow, event: EventResult): SyncResult => ({
  row,
  status: event.status,
  text: event.text,
  ...(event.id ? { resourceId: event.id } : {}),
  event,
});

async function run(
  row: TriggerRow,
  context: SyncContext,
  fresh?: TriggerResource,
): Promise<SyncResult> {
  const { api, activationId } = context;
  if (row.event)
    return eventResult(row, await applyEvent(row.event, context, false));
  const kind = row.resourceKind as SetUpKind | undefined;
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
      intent as TriggerIntent & { kind: SetUpKind },
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

const applyEvent = (
  row: EventRow,
  context: SyncContext,
  stopWithWaiting: boolean,
) =>
  applyEventRow(context.api, row, context.activationId, {
    stopWithWaiting,
    ...(context.environment ? { environment: context.environment } : {}),
  });

/** A row run once; an answer lost on the way fails that row alone. */
const attempt = (
  row: TriggerRow,
  context: SyncContext,
  fresh?: TriggerResource,
) =>
  run(row, context, fresh).catch((error: unknown) =>
    failed(
      row,
      `Couldn't set up this trigger: ${describeError(error).message}`,
    ),
  );

/** The row of the platform event trigger the version declares, when the activation changes it. */
const declares = (row: TriggerRow) =>
  row.event?.kind === "create" ||
  row.event?.kind === "retarget" ||
  row.event?.kind === "replace";
/** Whether the declared trigger reacts for the version now, and nothing else waits on it. */
const settled = (status: SyncStatus) =>
  status === "created" || status === "moved" || status === "replaced";

/**
 * Sets up every row that changes something, in order, telling each as it
 * finishes; a failure never stops the rest. A duplicate platform event
 * trigger is stopped only once the declared one is set up (or beside one
 * Studio leaves as it is): until then it may be the only one that reacts.
 */
export async function syncTriggers(
  rows: readonly TriggerRow[],
  context: SyncContext,
  each: (index: number, result: SyncResult) => void = () => undefined,
): Promise<SyncResult[]> {
  const results: SyncResult[] = [];
  let declared: SyncStatus | null = null;
  for (const [index, row] of rows.entries()) {
    const duplicate = row.event?.kind === "duplicate" ? row.event : null;
    let result: SyncResult;
    if (duplicate && declared !== null && !settled(declared))
      result = eventResult(row, heldResult(duplicate));
    else if (duplicate && !duplicate.stop)
      result = eventResult(row, declinedResult(duplicate));
    else if (actionable(row)) result = await attempt(row, context);
    else continue;
    if (declares(row)) declared = result.status;
    results.push(result);
    each(index, result);
  }
  return results;
}

/** Stops the duplicates that were held, once the declared trigger is set up. */
export async function syncHeld(
  rows: readonly TriggerRow[],
  results: ReadonlyMap<number, SyncResult>,
  context: SyncContext,
  each: (index: number, result: SyncResult) => void,
): Promise<void> {
  const declared = results.get(rows.findIndex(declares));
  if (!declared || !settled(declared.status)) return;
  for (const [index, row] of rows.entries()) {
    if (!results.get(index)?.event?.held || row.event?.kind !== "duplicate")
      continue;
    each(
      index,
      row.event.stop
        ? await attempt(row, context)
        : eventResult(row, declinedResult(row.event)),
    );
  }
}

/** The person's answer about a trigger whose events wait to start. */
export async function chooseResult(
  result: SyncResult,
  choice: "keep" | "stop",
  context: SyncContext,
): Promise<SyncResult> {
  const { row, event } = result;
  if (!row.event || !event) return result;
  return eventResult(
    row,
    choice === "keep"
      ? keptResult(row.event, event)
      : await applyEvent(row.event, context, true),
  );
}

/** Tries a failed row once more, reading its resource again first. */
export async function retryResult(
  result: SyncResult,
  context: SyncContext,
): Promise<SyncResult> {
  const { row } = result;
  const kind = row.resourceKind;
  if (row.kind === "create" || !row.resource || !kind || kind === "event")
    return attempt(row, context);
  try {
    const fresh = await readResource(context.api, kind, row.resource.id);
    return attempt(row, context, fresh);
  } catch (error) {
    return failed(
      row,
      `Couldn't read the ${NOUNS[kind]} again: ${error instanceof ApiError ? error.message : describeError(error).message}`,
    );
  }
}

/** Whether a result made or moved what its trigger choice runs on in this environment. */
const keepsResource = (result: SyncResult) =>
  result.event
    ? declares(result.row)
    : result.status === "created" || result.status === "moved";

/**
 * The stops a platform event trigger owes in this environment: those the
 * dialog showed as duplicates (the rest no longer count), with what each row
 * now owes or cleared.
 */
function owedAfter(
  intent: TriggerIntent,
  environmentId: string,
  results: readonly SyncResult[],
  planned: readonly TriggerRow[],
): PendingStop[] {
  const duplicates = new Set(
    planned.flatMap((row) =>
      row.event?.kind === "duplicate" ? [row.event.binding.id] : [],
    ),
  );
  const owed = new Map(
    pendingStopsOf(intent, environmentId)
      .filter(
        (stop) =>
          !planned.some((row) => row.event) || duplicates.has(stop.triggerId),
      )
      .map((stop) => [stop.triggerId, stop]),
  );
  for (const { row, event } of results) {
    if (!row.event || !("binding" in row.event)) continue;
    if (event?.pendingStop) owed.set(row.event.binding.id, event.pendingStop);
    else if (event?.pendingStop === null) owed.delete(row.event.binding.id);
  }
  return [...owed.values()];
}

/**
 * The trigger choices with each created or moved resource, and the stops a
 * platform event trigger still owes, kept for this environment. `planned` are
 * the rows the dialog showed.
 */
export function withStoredResources(
  intents: readonly TriggerIntent[],
  environmentId: string,
  results: readonly SyncResult[],
  planned: readonly TriggerRow[] = [],
): TriggerIntent[] {
  const made = new Map(
    results
      .filter(
        (result) =>
          keepsResource(result) && result.row.intentId && result.resourceId,
      )
      .map((result) => [result.row.intentId!, result.resourceId!]),
  );
  return intents.map((choice) => {
    const intent =
      choice.kind === "event"
        ? withPendingStops(
            choice,
            environmentId,
            owedAfter(choice, environmentId, results, planned),
          )
        : choice;
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

/** "1 trigger needs attention.", "1 trigger needs your choice.", or null when every row went through. */
export function attention(results: readonly SyncResult[]): string | null {
  const needing = (what: string, ...statuses: SyncStatus[]) => {
    const count = results.filter((result) =>
      statuses.includes(result.status),
    ).length;
    return count === 0
      ? []
      : count === 1
        ? [`1 trigger needs ${what}.`]
        : [`${count} triggers need ${what}.`];
  };
  const said = [
    ...needing("attention", "failed", "changed"),
    ...needing("your choice", "waiting"),
  ];
  return said.length ? said.join(" ") : null;
}
