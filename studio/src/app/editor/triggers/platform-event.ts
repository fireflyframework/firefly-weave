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
// Platform events: what a workflow started by a platform event trigger
// receives. The platform sends one when a transaction completes, is undone,
// is canceled, needs attention, is reconciled or is force ended. A workflow
// that reacts to them takes the platform's own schema by name as its input;
// Studio reads that schema (the platform's view) from its host once per
// connection, waiting before it asks again after a failure, and builds sample
// events for testing, since test runs start no reactions.
import { signal, type WritableSignal } from "@angular/core";
import type { StudioApi } from "../../api";
import {
  isJsonObject,
  jsonEqual,
  type Json,
  type JsonObject,
} from "../../forms/core/json";

/** The name a workflow's input schema references to take the platform event. */
export const PLATFORM_EVENT_SCHEMA = "weave.platform-event-v1";
/** A workflow input that is the platform event; frozen, so a draft gets a copy. */
export const PLATFORM_EVENT_INPUT: JsonObject = Object.freeze({
  $ref: PLATFORM_EVENT_SCHEMA,
});

/** What people call an input that is the platform event. */
export const PLATFORM_EVENT_NAME = "Platform event";

export type PlatformEventType =
  | "transaction.completed"
  | "transaction.compensated"
  | "transaction.cancelled"
  | "transaction.in_doubt"
  | "transaction.reconciled"
  | "transaction.force_ended";

/** The platform's order, which is the order Studio writes events in. */
export const EVENT_TYPES: readonly PlatformEventType[] = [
  "transaction.completed",
  "transaction.compensated",
  "transaction.cancelled",
  "transaction.in_doubt",
  "transaction.reconciled",
  "transaction.force_ended",
];
/** The order people choose events in: the one that needs attention first. */
export const EVENT_CHOICE_ORDER: readonly PlatformEventType[] = [
  "transaction.in_doubt",
  "transaction.compensated",
  "transaction.cancelled",
  "transaction.completed",
  "transaction.reconciled",
  "transaction.force_ended",
];
/** What people read for each event: a sentence, and a short form for subtitles and lists. */
export const EVENT_CHOICES: Readonly<
  Record<PlatformEventType, { label: string; short: string }>
> = {
  "transaction.completed": {
    label: "A transaction completed",
    short: "Completed",
  },
  "transaction.compensated": {
    label: "A transaction was undone",
    short: "Undone",
  },
  "transaction.cancelled": {
    label: "A transaction was canceled",
    short: "Canceled",
  },
  "transaction.in_doubt": {
    label: "A transaction needs attention",
    short: "Needs attention",
  },
  "transaction.reconciled": {
    label: "A transaction was reconciled",
    short: "Reconciled",
  },
  "transaction.force_ended": {
    label: "A transaction was force ended",
    short: "Force ended",
  },
};

/** When each event happens, under its choice: words few builders know. */
export const EVENT_DESCRIPTIONS: Readonly<Record<PlatformEventType, string>> = {
  "transaction.completed": "Every step finished.",
  "transaction.compensated":
    "It failed, and the steps it had done were undone.",
  "transaction.cancelled": "It stopped, and nothing had to be undone.",
  "transaction.in_doubt":
    "Its outcome is unclear, and a person has to resolve it.",
  "transaction.reconciled": "A person resolved one that needed attention.",
  "transaction.force_ended":
    "A person ended one that needed attention without resolving it.",
};

export const isPlatformEventType = (
  value: unknown,
): value is PlatformEventType =>
  typeof value === "string" &&
  (EVENT_TYPES as readonly string[]).includes(value);

/** Whether a schema is exactly the reference to the platform event, as the compiler requires. */
export const isPlatformEventInput = (schema: unknown): boolean =>
  isJsonObject(schema) &&
  Object.keys(schema).length === 1 &&
  schema["$ref"] === PLATFORM_EVENT_SCHEMA;

/** The pause after a failed read, doubling up to the longest. */
export const VIEW_RETRY_MS = 5_000;
export const VIEW_RETRY_MAX_MS = 60_000;

export type PlatformEventView =
  | { state: "ready"; schema: JsonObject }
  /** The read failed; Studio asks again from `retryAt` (milliseconds, on the reader's clock). */
  | { state: "unavailable"; retryAt: number };

interface ViewRead {
  ready?: JsonObject;
  loading?: Promise<PlatformEventView>;
  failure?: { retryAt: number; pause: number };
}
/** Per Studio connection: the API object stands for the paired host. */
const reads = new WeakMap<object, ViewRead>();

/**
 * The platform event's view as the Studio host serves it, read once per
 * Studio connection. Rendering may ask for it: after a failure (no session,
 * a busy host, an answer that isn't a schema) nothing is sent until the
 * pause is over, 5 s doubling to a minute.
 */
export function platformEventView(
  api: Pick<StudioApi, "request">,
  now: () => number = () => Date.now(),
): Promise<PlatformEventView> {
  let read = reads.get(api);
  if (!read) reads.set(api, (read = {}));
  const kept = read;
  if (kept.ready)
    return Promise.resolve({ state: "ready", schema: kept.ready });
  if (kept.loading) return kept.loading;
  if (kept.failure && now() < kept.failure.retryAt)
    return Promise.resolve({
      state: "unavailable",
      retryAt: kept.failure.retryAt,
    });
  const loading = api
    .request<unknown>("/studio/contracts/platform-event")
    .then(
      (value) => (isJsonObject(value) ? value : null),
      () => null,
    )
    .then((schema): PlatformEventView => {
      kept.loading = undefined;
      if (schema) {
        kept.ready = schema;
        kept.failure = undefined;
        return { state: "ready", schema };
      }
      const pause = kept.failure
        ? Math.min(kept.failure.pause * 2, VIEW_RETRY_MAX_MS)
        : VIEW_RETRY_MS;
      kept.failure = { retryAt: now() + pause, pause };
      return { state: "unavailable", retryAt: kept.failure.retryAt };
    });
  kept.loading = loading;
  return loading;
}

const shown = new WeakMap<object, WritableSignal<PlatformEventView | null>>();

/**
 * The view as a template reads it: null until the first answer, then the last
 * one, announced through a signal when the next arrives. Drawing it asks for
 * it, at most once per connection and, after a failure, once per pause.
 */
export function currentPlatformEvent(
  api: Pick<StudioApi, "request">,
  now: () => number = () => Date.now(),
): PlatformEventView | null {
  let kept = shown.get(api);
  if (!kept) shown.set(api, (kept = signal(null)));
  const view = kept;
  const known = view();
  if (
    known === null ||
    (known.state === "unavailable" && now() >= known.retryAt)
  )
    void platformEventView(api, now).then((next) => view.set(next));
  return known;
}

/** The event's fields as the data picker lists them; null until they're read. */
export function eventFields(api: Pick<StudioApi, "request">) {
  const view = currentPlatformEvent(api);
  return view?.state === "ready" ? eventFieldView(view.schema) : null;
}

/** The fields a builder looks for first; the others follow as the view lists them. */
const FIELD_ORDER = [
  "type",
  "emitted_at",
  "transaction",
  "event_id",
  "name",
  "version",
  "workflow",
  "business_key",
  "state",
  "cause",
  "steps",
  "incident_id",
  "run_id",
  "instance_key",
  "code",
  "step",
  "done",
  "undone",
  "pending",
];
const place = (key: string) => {
  const at = FIELD_ORDER.indexOf(key);
  return at < 0 ? FIELD_ORDER.length : at;
};
const fieldViews = new WeakMap<JsonObject, JsonObject>();

/**
 * The view as the data picker lists it: fields by their keys (the view's
 * titles only repeat them), what a builder looks for first, rules about one
 * value together so its type shows, and a group that may be empty as one
 * group that allows null, so what it holds is listed. It allows the same
 * values.
 */
export function eventFieldView(view: JsonObject): JsonObject {
  let kept = fieldViews.get(view);
  if (!kept) fieldViews.set(view, (kept = fieldsOf(view)));
  return kept;
}
/** `allOf` rules about one value that don't contradict each other, as one rule. */
function oneRule(schema: JsonObject): JsonObject {
  const { allOf, ...rest } = schema;
  if (!Array.isArray(allOf)) return schema;
  const merged: JsonObject = { ...rest };
  for (const part of allOf) {
    if (
      !isJsonObject(part) ||
      Object.entries(part).some(
        ([key, value]) =>
          key in merged && !jsonEqual(value as Json, merged[key] as Json),
      )
    )
      return schema;
    Object.assign(merged, part);
  }
  return merged;
}
function fieldsOf(schema: JsonObject): JsonObject {
  const { title: _title, ...node } = oneRule(schema);
  if (isJsonObject(node["properties"]))
    node["properties"] = Object.fromEntries(
      Object.entries(node["properties"])
        .sort(([a], [b]) => place(a) - place(b))
        .map(([key, value]) => [
          key,
          isJsonObject(value) ? fieldsOf(value) : value,
        ]),
    );
  if (isJsonObject(node["items"])) node["items"] = fieldsOf(node["items"]);
  if (Array.isArray(node["anyOf"]))
    node["anyOf"] = node["anyOf"].map((part) =>
      isJsonObject(part) ? fieldsOf(part) : part,
    );
  const either = node["anyOf"];
  if (!Array.isArray(either) || either.length !== 2) return node;
  const group = either.find(
    (part): part is JsonObject =>
      isJsonObject(part) && part["type"] === "object",
  );
  const empty = either.some(
    (part) => isJsonObject(part) && part["type"] === "null",
  );
  if (!group || !empty) return node;
  delete node["anyOf"];
  return { ...group, ...node, type: ["object", "null"] };
}

/** Schemas by name for the data picker: the platform event's name is its view, any other name nothing. */
export function namedSchemas(
  view: JsonObject | null,
): (name: string) => JsonObject | undefined {
  return (name) =>
    view !== null && name === PLATFORM_EVENT_SCHEMA ? view : undefined;
}

/**
 * What every sample's IDs start with, so Studio can tell them from the IDs of
 * a real event, run or incident, which the platform draws at random.
 */
const SAMPLE_ID_START = "00000000-0000-4000-8000-";
const SAMPLE_ID = /^00000000-0000-4000-8000-[0-9a-f]{12}$/i;
/** A sample's ID by number, the same each time. */
export const sampleIdOf = (number: number): string =>
  SAMPLE_ID_START + number.toString(16).padStart(12, "0");
/** A new ID for a sample. */
export const sampleId = (): string =>
  SAMPLE_ID_START + crypto.randomUUID().slice(-12);
export const isSampleId = (value: unknown): boolean =>
  typeof value === "string" && SAMPLE_ID.test(value);

export interface SampleOptions {
  /** The workflow whose transaction the sample comes from: the first one the trigger names. */
  source?: string;
  now: Date;
  /** A new ID for the event, the source run and, for an event that needs attention, the incident. */
  id: () => string;
}

const HOLD = { step: "hold", instance_key: "hold", how: "reserve" };
const ADD = { step: "add", instance_key: "add", how: "compensate" };
/** Each event's transaction steps: which steps went ahead, were undone, or still wait. */
const SAMPLE_STEPS: Record<PlatformEventType, () => JsonObject> = {
  "transaction.completed": () => ({
    done: [{ ...HOLD }, { ...ADD }],
    undone: [],
    pending: [],
  }),
  "transaction.compensated": () => ({
    done: [],
    undone: [{ ...HOLD }, { ...ADD }],
    pending: [],
  }),
  "transaction.cancelled": () => ({
    done: [],
    undone: [{ ...HOLD }],
    pending: [],
  }),
  "transaction.in_doubt": () => ({
    done: [],
    undone: [],
    pending: [
      { ...HOLD, phase: "cancel" },
      { ...ADD, phase: "undo", attempts: 10 },
    ],
  }),
  "transaction.reconciled": () => ({
    done: [{ ...HOLD }, { ...ADD }],
    undone: [],
    pending: [],
  }),
  "transaction.force_ended": () => ({
    done: [{ ...HOLD }],
    undone: [],
    pending: [],
  }),
};

/**
 * A platform event of `type` as the platform would send it, with made-up
 * keys: the transaction of `move-between-systems` (or `source`) with the
 * business key REF-1029, a cause and an incident when it needs attention.
 */
export function sampleEvent(
  type: PlatformEventType,
  options: SampleOptions,
): JsonObject {
  const eventId = options.id();
  const runId = options.id();
  const attention = type === "transaction.in_doubt";
  const incidentId = attention ? options.id() : undefined;
  return {
    version: "weave/platform-event-v1",
    event_id: eventId,
    type,
    emitted_at: options.now.toISOString().replace(/\.\d{3}Z$/, "Z"),
    transaction: {
      workflow: {
        name: options.source ?? "move-between-systems",
        version: "1.0.0",
      },
      run_id: runId,
      instance_key: "move",
      business_key: "REF-1029",
      state: type.slice("transaction.".length),
      ...(attention
        ? { cause: { code: "WV-TXN-UNDO-STUCK", step: "add" } }
        : {}),
      steps: SAMPLE_STEPS[type](),
      ...(incidentId === undefined ? {} : { incident_id: incidentId }),
      test: false,
    },
  };
}
