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
// A platform event a person enters as test data: what is wrong with it, the
// first problem by field, and the event with its real IDs replaced. Test data
// is read by everyone who can test the workflow, so an event copied from a
// real transaction never keeps the IDs of its event, run or incident.
import {
  canonicalJson,
  hasOwn,
  isJsonObject,
  jsonEqual,
  type JsonObject,
} from "../../forms/core/json";
import { safeSample } from "../ndv/panes/sample";
import { countLabel } from "../ndv/panes/views";
import type { Json } from "../ndv/registry";
import type { DataSource } from "../state/test-data";
import {
  EVENT_CHOICE_ORDER,
  EVENT_CHOICES,
  EVENT_TYPES,
  isPlatformEventInput,
  isPlatformEventType,
  isSampleId,
  sampleId,
} from "./platform-event";

export const SAMPLE_FIELD_PROBLEM =
  "Paste a platform event: a JSON object with its type and transaction.";
/** Who reads a pasted event, and so what it must not hold. */
export const SHARED_NOTE =
  "Test data is shared with everyone who can test this workflow. Use a made-up business key.";
/** Where a test event came from, in a word or two. */
export const EVENT_ORIGINS: Readonly<Record<DataSource, string>> = {
  schema: "Sample",
  simulated: "Sample",
  manual: "Pasted",
  run: "From a run",
  "test-call": "From a test call",
};
export type ReplacedId = "event" | "run" | "incident";

const VERSION = "weave/platform-event-v1";
const STATES = EVENT_CHOICE_ORDER.map((type) =>
  type.slice("transaction.".length),
);

const TEXT: JsonObject = { type: "string", minLength: 1 };
const ID: JsonObject = { type: "string", format: "uuid" };
const orNull = (schema: JsonObject): JsonObject => ({
  anyOf: [schema, { type: "null" }],
});
const group = (properties: JsonObject, optional: string[] = []) => ({
  type: "object",
  properties,
  required: Object.keys(properties).filter((key) => !optional.includes(key)),
});
const STEPS: JsonObject = {
  type: "array",
  items: group(
    {
      step: TEXT,
      instance_key: TEXT,
      how: TEXT,
      phase: orNull(TEXT),
      attempts: orNull({ type: "integer" }),
    },
    ["phase", "attempts"],
  ),
};
/**
 * The fields a platform event has and what each holds, without its limits:
 * what Studio checks while its host can't serve the platform's view. A field
 * that isn't here is refused, because it could hold a real ID.
 */
const KNOWN: JsonObject = group({
  type: { enum: [...EVENT_TYPES] },
  emitted_at: { type: "string", format: "date-time" },
  transaction: group(
    {
      workflow: group({ name: TEXT, version: TEXT }),
      business_key: orNull(TEXT),
      state: TEXT,
      cause: orNull(
        group({ code: TEXT, step: orNull(TEXT), origin_code: orNull(TEXT) }, [
          "step",
          "origin_code",
        ]),
      ),
      steps: group({ done: STEPS, undone: STEPS, pending: STEPS }, [
        "done",
        "undone",
        "pending",
      ]),
      incident_id: orNull(ID),
      run_id: ID,
      instance_key: TEXT,
      test: { type: "boolean" },
    },
    ["cause", "incident_id"],
  ),
  event_id: ID,
  version: { const: VERSION },
});

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DATE_TIME =
  /^(\d{4})-(\d{2})-(\d{2})T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i;
function isDateTime(text: string): boolean {
  const parts = DATE_TIME.exec(text);
  if (!parts || Number.isNaN(Date.parse(text))) return false;
  const [, year, month, day] = parts.map(Number);
  // Dates roll over: February 30 would be March 2.
  return new Date(Date.UTC(year!, month! - 1, day)).getUTCDate() === day;
}
/** What a field in a form of its own must be, in words, with an example after "such as". */
const FORMS: Readonly<Record<string, [what: string, example: string]>> = {
  uuid: ["an ID", "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e"],
  "date-time": ["a date and time", "2026-11-18T16:05:11Z"],
  name: ["a workflow name", "move-between-systems"],
  version: ["a version", "1.0.0"],
  step: ["a step ID", "add"],
  code: ["a code", "WV-TXN-UNDO-STUCK"],
  origin_code: ["a code", "WV-TXN-UNDO-STUCK"],
};
const TYPE_WORDS: Readonly<Record<string, string>> = {
  object: "a group of fields",
  array: "a list",
  string: "text",
  boolean: "true or false",
  integer: "a whole number",
  number: "a number",
};
const ofType = (value: unknown, type: unknown): boolean =>
  type === "object"
    ? isJsonObject(value)
    : type === "array"
      ? Array.isArray(value)
      : type === "integer"
        ? Number.isInteger(value)
        : typeof value === type;
/** The longest part of an unknown field's name a sentence repeats. */
const NAME_SHOWN = 60;

/** The first thing wrong with `value` at `path`; undefined is a field that is missing. */
function problemAt(
  path: string,
  value: unknown,
  schema: JsonObject,
  event: JsonObject,
  empty = false,
): string | null {
  if (path === "transaction.state") return stateProblem(value, event);
  if (Array.isArray(schema["anyOf"])) {
    const options = schema["anyOf"].filter(isJsonObject);
    const isNull = (option: JsonObject) => option["type"] === "null";
    const allowed = empty || options.some(isNull);
    const found = options
      .filter((option) => !isNull(option))
      .map((option) => problemAt(path, value, option, event, allowed));
    return found.includes(null) ? null : (found[0] ?? null);
  }
  const named = schema["type"];
  const types = Array.isArray(named) ? named : named ? [named] : [];
  const allowed = empty || types.includes("null");
  if (value === null && allowed) return null;
  const type = types.find((each) => each !== "null");
  const key = /[^.[\]]+$/.exec(path)?.[0] ?? "";
  const pattern = schema["pattern"];
  const form =
    type === "string"
      ? FORMS[String(schema["format"] ?? (pattern ? key : ""))]
      : undefined;
  const must = (what: string, example = "") =>
    `${path} must be ${what}${allowed ? " or null" : ""}${example && `, such as ${example}`}.`;
  const wrong = () =>
    form ? must(...form) : must(TYPE_WORDS[String(type)] ?? "filled in");
  if ("const" in schema && !jsonEqual(value, schema["const"]))
    return must(String(schema["const"]));
  const choices = schema["enum"];
  if (
    Array.isArray(choices) &&
    !choices.some((choice) => jsonEqual(value, choice))
  )
    return must(`one of ${choices.join(", ")}`);
  if (value === undefined || (type && !ofType(value, type))) return wrong();
  const limit = (name: string) => Number(schema[name] ?? Infinity);
  if (typeof value === "string") {
    const length = [...value].length;
    if (!length && Number(schema["minLength"] ?? 0)) return wrong();
    if (length > limit("maxLength"))
      return `${path} can have up to ${schema["maxLength"]} characters.`;
    if (schema["format"] === "uuid" && !UUID.test(value)) return wrong();
    if (schema["format"] === "date-time" && !isDateTime(value)) return wrong();
    if (typeof pattern === "string" && !matches(pattern, value))
      return form ? wrong() : must("in the form the platform sends");
  }
  if (
    typeof value === "number" &&
    value < Number(schema["minimum"] ?? -Infinity)
  )
    return `${path} must be ${schema["minimum"]} or more.`;
  if (Array.isArray(value)) {
    if (value.length > limit("maxItems"))
      return `${path} can have up to ${schema["maxItems"]} items.`;
    const items = isJsonObject(schema["items"]) ? schema["items"] : {};
    for (const [index, item] of value.entries()) {
      const problem = problemAt(`${path}[${index}]`, item, items, event);
      if (problem) return problem;
    }
  }
  return isJsonObject(value) ? fieldProblem(path, value, schema, event) : null;
}
/** The first field of a group that is wrong; then one it hasn't, which could hold a real ID. */
function fieldProblem(
  path: string,
  value: JsonObject,
  schema: JsonObject,
  event: JsonObject,
): string | null {
  const at = (key: string) => (path ? `${path}.${key}` : key);
  const fields = isJsonObject(schema["properties"]) ? schema["properties"] : {};
  const required = Array.isArray(schema["required"]) ? schema["required"] : [];
  for (const [key, field] of Object.entries(fields)) {
    const held = hasOwn(value, key);
    if (!held && !required.includes(key)) continue;
    const problem = problemAt(
      at(key),
      held ? value[key] : undefined,
      isJsonObject(field) ? field : {},
      event,
    );
    if (problem) return problem;
  }
  const unknown = Object.keys(value).find((key) => !hasOwn(fields, key));
  return unknown === undefined
    ? null
    : `${at(unknown.slice(0, NAME_SHOWN))} isn't one of the platform event's fields.`;
}
/** A transaction's state goes with its event: the platform sends no other. */
function stateProblem(value: unknown, event: JsonObject): string | null {
  if (typeof value !== "string" || !STATES.includes(value))
    return `transaction.state must be one of ${STATES.join(", ")}.`;
  const type = String(event["type"]);
  return type === `transaction.${value}`
    ? null
    : `transaction.state must be ${type.slice("transaction.".length)} for ${type}.`;
}
/** Whether a text fits the view's pattern; one this browser can't read is the platform's to check. */
function matches(pattern: string, text: string): boolean {
  try {
    return new RegExp(pattern, "u").test(text);
  } catch {
    return true;
  }
}

/**
 * The first problem, by field, with a value a person entered as a platform
 * event; null when it is one. `fields` is the platform's view as the data
 * picker lists it; without it, the fields Studio knows.
 */
export function eventProblem(
  value: unknown,
  fields: JsonObject | null,
): string | null {
  if (!isJsonObject(value)) return SAMPLE_FIELD_PROBLEM;
  const known = fields && isJsonObject(fields["properties"]) ? fields : KNOWN;
  return fieldProblem("", value, known, value);
}

/** The event pasted as `source`, or what is wrong with it (see eventProblem). */
export function pastedEvent(
  source: string,
  fields: JsonObject | null,
): { value: Json } | { problem: string } {
  let event: unknown;
  try {
    event = JSON.parse(source);
  } catch {
    return { problem: SAMPLE_FIELD_PROBLEM };
  }
  const problem = eventProblem(event, fields);
  return problem ? { problem } : { value: event as Json };
}

/** What a transaction in an event always has: an event of any version is known by it. */
const FACTS = ["run_id", "state", "instance_key"];
/** Whether an object is a platform event: it says so, or its transaction, or one it lists, is an event's. */
function isEvent(value: JsonObject): boolean {
  const held = value["transaction"];
  return (
    value["version"] === VERSION ||
    (Array.isArray(held) ? held : [held]).some(
      (facts) =>
        isJsonObject(facts) && FACTS.every((key) => hasOwn(facts, key)),
    )
  );
}
const EVENT_IDS: Readonly<Record<string, ReplacedId>> = { event_id: "event" };
const FACTS_IDS: Readonly<Record<string, ReplacedId>> = {
  run_id: "run",
  incident_id: "incident",
};
const REPLACED_ORDER: readonly ReplacedId[] = ["event", "run", "incident"];

/** The value with each ID of every platform event in it, at any depth, as `map` gives it. */
function mapIds(
  value: Json,
  map: (name: ReplacedId, id: Json) => Json,
  inEvent = false,
): Json {
  if (Array.isArray(value))
    return value.map((item) => mapIds(item, map, inEvent));
  if (!isJsonObject(value)) return value;
  const event = isEvent(value);
  const ids = { ...(event ? EVENT_IDS : {}), ...(inEvent ? FACTS_IDS : {}) };
  return Object.fromEntries(
    Object.entries(value).map(([key, held]) => [
      key,
      !hasOwn(ids, key)
        ? mapIds(held, map, event && key === "transaction")
        : held === null
          ? held
          : map(ids[key]!, held),
    ]),
  );
}

/**
 * The value with sample IDs in place of the event, run and incident IDs of
 * every platform event in it, and which it replaced; IDs that are samples
 * already stay, and so does the rest. A real ID gets the sample ID it has in
 * `samples`, which keeps the new ones.
 */
export function withSampleIds<T extends Json>(
  value: T,
  samples = new Map<string, string>(),
): { value: T; replaced: ReplacedId[] } {
  const replaced = new Set<ReplacedId>();
  const mapped = mapIds(value, (name, id) => {
    if (isSampleId(id)) return id;
    replaced.add(name);
    const real = canonicalJson(id);
    const sample = samples.get(real) ?? sampleId();
    samples.set(real, sample);
    return sample;
  });
  return { value: mapped as T, replaced: inOrder(replaced) };
}
const inOrder = (replaced: ReadonlySet<ReplacedId>): ReplacedId[] =>
  REPLACED_ORDER.filter((name) => replaced.has(name));

/** Which IDs of the events in a value are those Studio put in place of real ones. */
export function replacedIn(
  value: Json | undefined,
  samples: ReadonlyMap<string, string>,
): ReplacedId[] {
  if (value === undefined || !samples.size) return [];
  const known = new Set<Json>(samples.values());
  const replaced = new Set<ReplacedId>();
  mapIds(value, (name, id) => {
    if (known.has(id)) replaced.add(name);
    return id;
  });
  return inOrder(replaced);
}

/** A test input without its secret fields; the platform event has none. */
export const safeInput = (
  schema: unknown,
  value: Json | undefined,
): Json | undefined =>
  isPlatformEventInput(schema) ? value : safeSample(schema, value);

/**
 * What test data keeps of an input a person entered or a test call brought:
 * never a secret field and, because test data is shared, no ID of a real
 * event, run or incident, nor, in a workflow whose input is the platform
 * event, a field that could hold one: a `problem` then.
 */
export function keptInput(
  schema: unknown,
  value: Json | undefined,
  fields: () => JsonObject | null,
  samples: Map<string, string>,
): { value: Json | undefined } | { problem: string } {
  const safe = safeInput(schema, value);
  if (safe === undefined) return { value: safe };
  const problem = isPlatformEventInput(schema)
    ? eventProblem(safe, fields())
    : null;
  return problem ? { problem } : withSampleIds(safe, samples);
}

/** "Studio replaced the event and run IDs with sample IDs."; "" when it replaced none. */
export function replacedNote(replaced: readonly ReplacedId[]): string {
  if (!replaced.length) return "";
  if (replaced.length === 1)
    return `Studio replaced the ${replaced[0]} ID with a sample ID.`;
  const last = replaced[replaced.length - 1];
  return `Studio replaced the ${replaced.slice(0, -1).join(", ")} and ${last} IDs with sample IDs.`;
}

/** "Sample · 5 fields", "Pasted · 5 fields", "From a run · 5 fields"; "" without a test event. */
export function eventCount(
  source: DataSource | null,
  value: Json | undefined,
): string {
  if (!source || !isJsonObject(value)) return "";
  return `${EVENT_ORIGINS[source]} · ${countLabel(Object.keys(value).length)}`;
}

/** What Studio replaced in an event entered as test data, and who reads it; "" when it replaced nothing. */
export const enteredNote = (replaced: readonly ReplacedId[]): string =>
  replaced.length ? `${replacedNote(replaced)} ${SHARED_NOTE}` : "";

/** What a test event is, in a few words; null for data that isn't a platform event. */
export function eventSummary(
  value: Json | undefined,
): { label: string; text: string }[] | null {
  if (!isJsonObject(value)) return null;
  const facts = value["transaction"];
  if (!isPlatformEventType(value["type"]) || !isJsonObject(facts)) return null;
  const workflow = isJsonObject(facts["workflow"]) ? facts["workflow"] : {};
  const key = facts["business_key"];
  return [
    { label: "Event", text: EVENT_CHOICES[value["type"]].short },
    {
      label: "From",
      text: [workflow["name"], workflow["version"]].filter(Boolean).join(" "),
    },
    { label: "Business key", text: typeof key === "string" ? key : "None" },
  ];
}
