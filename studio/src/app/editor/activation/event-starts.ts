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
// What a platform event trigger started. Every event offered to a trigger
// leaves one start: the run it started, a start still waiting, or why it
// started none, never the event's values. Studio words each start with the
// platform's own sentences and reads the list page by page, newest first.
// Rendering may ask for it, so a failed read waits before it is tried again
// (5 s, doubling to a minute, from 5 s again after an answer or a refusal), a
// read with no environment chosen fails without asking the platform, and an
// answer that arrives after the person chose another environment or trigger
// is dropped.
import { ApiError } from "../../api";
import { describeError } from "../../errors";
import {
  EVENT_CHOICES,
  isPlatformEventType,
  type PlatformEventType,
} from "../triggers/platform-event";
import type { TriggerApi } from "./trigger-resources-api";

/** Why a start started no run. */
export type EventStartCode =
  | "WV-TRIGGER-START"
  | "WV-TRIGGER-DISABLED"
  | "WV-TRIGGER-EVENT-DEPTH"
  | "WV-TRIGGER-CHAIN-BUDGET"
  | "WV-TRIGGER-START-EXPIRED";

/** `event_triggers.starts`: one event offered to one trigger. */
export interface EventStart {
  id: string;
  trigger_id: string;
  event_id: string;
  event_type: PlatformEventType;
  source_run_id: string;
  root_event_id: string;
  depth: number;
  status: "requested" | "started" | "rejected" | "skipped";
  code?: EventStartCode;
  /** The refusal's own code, such as WV-FORBIDDEN. */
  cause?: string;
  run_id?: string;
  requested_at: string;
  started_at?: string;
}

/** The platform's sentence for each reason a start started no run. */
export const START_COPY: Readonly<Record<EventStartCode, string>> = {
  "WV-TRIGGER-START": "The triggered workflow couldn't start.",
  "WV-TRIGGER-DISABLED":
    "The trigger was disabled before the workflow started.",
  "WV-TRIGGER-EVENT-DEPTH":
    "Platform events from a run this deep in a chain of reactions start no workflows.",
  "WV-TRIGGER-CHAIN-BUDGET":
    "This chain of reactions already has 64 workflows started or waiting to start, so its events start no more.",
  "WV-TRIGGER-START-EXPIRED":
    "The workflow couldn't start within a day of the event, so the event no longer starts it.",
};
/** What the refusal behind a start that couldn't start means. */
export const CAUSE_COPY: Readonly<Record<string, string>> = {
  "WV-FORBIDDEN": "Its owner can no longer start or read runs here.",
  "WV-READINESS":
    "Its version can't run here yet: a worker or connection it needs isn't ready.",
  "WV-RUNTIME-INPUT":
    "The event it should start with is missing or isn't a platform event.",
};

export interface StartRow {
  id: string;
  tone: "ok" | "waiting" | "skipped" | "failed";
  /** "Started run 9b1c4e2a", "Waiting to start", or why it started none. */
  title: string;
  /** "Needs attention in run 3fa2c1d0": the event and the run it came from. */
  detail: string;
  /** The run it started, for Open run. */
  runId?: string;
  sourceRunId: string;
  /** When it started, or when it was asked for. */
  at: string;
}

const shortId = (id: string) => id.slice(0, 8);
const reason = (code: string | undefined) =>
  code === undefined
    ? ""
    : ((START_COPY as Readonly<Record<string, string>>)[code] ?? code);

/** One line of the Starts list. */
export function startRow(start: EventStart): StartRow {
  const event = isPlatformEventType(start.event_type)
    ? EVENT_CHOICES[start.event_type].short
    : String(start.event_type);
  const row = {
    id: start.id,
    detail: `${event} in run ${shortId(start.source_run_id)}`,
    sourceRunId: start.source_run_id,
    at: start.requested_at,
  };
  switch (start.status) {
    case "started":
      return {
        ...row,
        tone: "ok",
        title: start.run_id
          ? `Started run ${shortId(start.run_id)}`
          : "Started",
        ...(start.run_id ? { runId: start.run_id } : {}),
        at: start.started_at ?? start.requested_at,
      };
    case "requested":
      return { ...row, tone: "waiting", title: "Waiting to start" };
    case "skipped":
      return { ...row, tone: "skipped", title: reason(start.code) };
    case "rejected": {
      const cause = start.cause;
      const why =
        cause === undefined ? "" : ` ${CAUSE_COPY[cause] ?? `(${cause})`}`;
      return { ...row, tone: "failed", title: `${reason(start.code)}${why}` };
    }
  }
}

/** The starts still waiting among the rows read. */
export const waitingCount = (rows: readonly StartRow[]): number =>
  rows.filter((row) => row.tone === "waiting").length;

export type StartsAnswer =
  | { rows: StartRow[]; next: string | null }
  /** The person can't read them here, or the trigger no longer exists. */
  | { refused: "forbidden" | "missing" }
  /** The read failed, or failed recently: it is tried again from `retryAt`. */
  | { failed: string; retryAt: number }
  /** The person chose another environment or trigger while the read ran. */
  | { dropped: true };

export interface StartsOptions {
  now?: () => number;
  /** The trigger the person looks at now; an answer for another one is dropped. */
  current?: () => string | null | undefined;
}

export const STARTS_RETRY_MS = 5_000;
export const STARTS_RETRY_MAX_MS = 60_000;
/** Failed reads per Studio connection, by environment and trigger. */
const failures = new WeakMap<
  object,
  Map<string, { retryAt: number; pause: number; message: string }>
>();

const environmentOf = (api: TriggerApi) => {
  try {
    return api.environment;
  } catch {
    return null;
  }
};

/** One page of what a trigger started, newest first; `cursor` continues from the page before. */
export async function readStarts(
  api: TriggerApi,
  triggerId: string,
  cursor?: string,
  options: StartsOptions = {},
): Promise<StartsAnswer> {
  const now = options.now ?? (() => Date.now());
  let environment: string;
  try {
    environment = api.environment;
  } catch (error) {
    // No environment chosen: there is nothing to ask the platform yet.
    return {
      failed: describeError(error).message,
      retryAt: now() + STARTS_RETRY_MS,
    };
  }
  const key = `${environment}\n${triggerId}`;
  let failed = failures.get(api);
  if (!failed) failures.set(api, (failed = new Map()));
  const kept = failed.get(key);
  if (kept && now() < kept.retryAt)
    return { failed: kept.message, retryAt: kept.retryAt };
  const path = `${environment}/event-triggers/${encodeURIComponent(triggerId)}/starts?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
  let answer: { items: unknown[]; next_cursor: string | null } | null = null;
  let error: unknown = null;
  try {
    answer = await api.request(path);
  } catch (caught) {
    error = caught;
  }
  if (
    environmentOf(api) !== environment ||
    (options.current && options.current() !== triggerId)
  )
    return { dropped: true };
  const refused =
    error instanceof ApiError
      ? error.status === 403
        ? "forbidden"
        : error.status === 404
          ? "missing"
          : null
      : null;
  // An answer or a refusal ends the pause: the next failure waits 5 s again.
  if (answer || refused) failed.delete(key);
  if (answer)
    return {
      rows: answer.items.map((item) => startRow(item as EventStart)),
      next: answer.next_cursor,
    };
  if (refused) return { refused };
  const pause = kept
    ? Math.min(kept.pause * 2, STARTS_RETRY_MAX_MS)
    : STARTS_RETRY_MS;
  const message = describeError(error).message;
  const retryAt = now() + pause;
  failed.set(key, { retryAt, pause, message });
  return { failed: message, retryAt };
}
