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
// Reads a test data document that came from outside this session (a file,
// this computer's storage, an older Studio) and accepts exactly what the
// platform accepts: the same fields, keys, sizes and provenance rules. A
// shared fixture written by the platform's own models holds the two to the
// same answers. A document Studio can't read is never half-used.
//
// Values follow the platform's JSON rules too: integers within ±(2^53 - 1),
// finite numbers, text and keys without lone surrogates, and nesting at most
// 200 levels below the document. Two differences are one-way and fail
// closed, because a parsed value has lost its spelling: Studio refuses an
// integer-valued number beyond 2^53 - 1 written as `1e20` (the platform reads
// that as a float), and accepts `afterSeconds: 1.0` (the platform wants an
// integer spelling). The 1 MB bound is checked on Studio's own compact JSON,
// which is what Studio sends; the platform measures the bytes it receives.
import { isInstanceKey } from "./instance-keys";
import { TEST_DATA_MAX_BYTES, type StudioTestData } from "./test-data";

export type TestDataReading =
  | { ok: true; data: StudioTestData }
  | { ok: false; reason: string };

const UNREADABLE = "This test data isn't in a format Studio can read.";
const TOO_LARGE = "This test data is larger than 1 MB.";
const MAX_SAFE = Number.MAX_SAFE_INTEGER;
const UUID =
  /^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$/;
const DATE_TIME =
  /^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$/;
const HASH = /^fnv1a64:[0-9a-f]{16}$/;
const FRAME =
  /^[A-Za-z0-9][A-Za-z0-9_.-]*(\[(0|[1-9][0-9]*)\])*(\/[A-Za-z0-9][A-Za-z0-9_.-]*(\[(0|[1-9][0-9]*)\])*)*$/;
const REFERENCE =
  /^[A-Za-z0-9][A-Za-z0-9_.-]*@(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$/;
const SOURCES = new Set(["manual", "schema", "simulated", "test-call", "run"]);
const TURN_SOURCES = new Set(["manual", "simulated", "test-call", "run"]);

class Refused extends Error {}
const refuse = (): never => {
  throw new Refused();
};
type Record_ = Record<string, unknown>;
const isRecord = (value: unknown): value is Record_ =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** A JSON object whose keys are all known; present fields are never null. */
function fields(
  value: unknown,
  required: readonly string[],
  optional: readonly string[],
): Record_ {
  if (!isRecord(value)) return refuse();
  const known = new Set([...required, ...optional]);
  for (const key of Object.keys(value)) if (!known.has(key)) refuse();
  for (const key of required) if (!Object.hasOwn(value, key)) refuse();
  return value;
}
/** An optional field: absent is fine, null never is (data values aside). */
const present = (value: Record_, key: string) => {
  if (!Object.hasOwn(value, key)) return false;
  if (value[key] === null) refuse();
  return true;
};
/** Text without lone surrogates, within `max` UTF-16 units, matching `pattern`. */
const text = (value: unknown, pattern?: RegExp, max = Infinity): boolean =>
  typeof value === "string" &&
  value.length <= max &&
  !/[\uD800-\uDFFF]/.test(
    value.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]/g, ""),
  ) &&
  (!pattern || pattern.test(value));
const check = (ok: boolean) => {
  if (!ok) refuse();
};
const uuid = (value: Record_, key: string) => {
  if (present(value, key)) check(text(value[key], UUID));
};
/** Data from a run or a test call names both; a run ID always comes with a source and its environment. */
function provenance(value: Record_) {
  uuid(value, "runId");
  uuid(value, "runEnvironmentId");
  const source = value["source"];
  if (
    (source === "run" || source === "test-call") &&
    (!Object.hasOwn(value, "runId") ||
      !Object.hasOwn(value, "runEnvironmentId"))
  )
    refuse();
  if (
    Object.hasOwn(value, "runId") &&
    (!Object.hasOwn(value, "source") ||
      !Object.hasOwn(value, "runEnvironmentId"))
  )
    refuse();
}
const RUN = ["runId", "runEnvironmentId"] as const;
function keyed(
  value: unknown,
  max: number,
  entry: (item: unknown) => void,
  key: (name: string) => boolean = isInstanceKey,
) {
  if (!isRecord(value)) refuse();
  const names = Object.keys(value as Record_);
  check(names.length <= max);
  for (const name of names) {
    check(key(name));
    entry((value as Record_)[name]);
  }
}
const sourceOf = (value: Record_, allowed: Set<string>, required: boolean) => {
  if (required || present(value, "source"))
    check(typeof value["source"] === "string" && allowed.has(value["source"]));
};
const frame = (value: Record_) => {
  if (present(value, "frame")) check(text(value["frame"], FRAME, 1024));
};
const hash = (value: Record_, key: string, required: boolean) => {
  if (required || present(value, key)) check(text(value[key], HASH));
};

function entry(value: unknown) {
  const item = fields(value, ["value", "source"], RUN);
  sourceOf(item, SOURCES, true);
  provenance(item);
}
function pin(value: unknown) {
  const item = fields(
    value,
    [
      "output",
      "source",
      "pinnedAt",
      "configHash",
      "contractHash",
      "upstreamHash",
    ],
    RUN,
  );
  sourceOf(item, SOURCES, true);
  check(text(item["pinnedAt"], DATE_TIME));
  for (const key of ["configHash", "contractHash", "upstreamHash"])
    hash(item, key, true);
  provenance(item);
}
function mock(value: unknown) {
  const item = fields(value, ["output", "source"], RUN);
  sourceOf(item, SOURCES, true);
  provenance(item);
}
function signal(value: unknown) {
  if (isRecord(value) && Object.hasOwn(value, "outcome")) {
    const item = fields(value, ["outcome"], ["frame"]);
    check(item["outcome"] === "timeout");
    return frame(item);
  }
  const item = fields(
    value,
    ["payload"],
    ["afterSeconds", "frame", "source", ...RUN],
  );
  if (present(item, "afterSeconds")) {
    const seconds = item["afterSeconds"];
    check(
      typeof seconds === "number" &&
        Number.isInteger(seconds) &&
        seconds >= 0 &&
        seconds <= MAX_SAFE,
    );
  }
  frame(item);
  sourceOf(item, SOURCES, false);
  provenance(item);
}
function human(value: unknown) {
  if (isRecord(value) && Object.hasOwn(value, "outcome")) {
    const item = fields(value, ["outcome"], ["frame"]);
    check(item["outcome"] === "expire");
    return frame(item);
  }
  const item = fields(value, ["decision", "data"], ["frame", "source", ...RUN]);
  check(text(item["decision"]));
  frame(item);
  sourceOf(item, SOURCES, false);
  provenance(item);
}
function aiTurns(value: unknown) {
  const item = fields(
    value,
    ["turns"],
    ["memory", "autoApprove", "source", "configHash", "upstreamHash", ...RUN],
  );
  const turns = item["turns"];
  check(Array.isArray(turns) && turns.length <= 100 && turns.every(isRecord));
  if (present(item, "memory")) {
    const memory = item["memory"];
    check(Array.isArray(memory) && memory.length <= 200);
    for (const message of memory as unknown[]) {
      const said = fields(message, ["role", "content"], []);
      check(said["role"] === "user" || said["role"] === "assistant");
    }
  }
  if (present(item, "autoApprove"))
    check(typeof item["autoApprove"] === "boolean");
  sourceOf(item, TURN_SOURCES, false);
  hash(item, "configHash", false);
  hash(item, "upstreamHash", false);
  provenance(item);
}

/** Deepest level the platform's JSON parser reads, the document being level 0. */
const MAX_DEPTH = 200;
/** Whether text holds a lone surrogate (which UTF-8 can't encode). */
const lonely = (value: string) =>
  /[\uD800-\uDFFF]/.test(value.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]/g, ""));
/** Every value of the document within the platform's JSON domain, iteratively (no stack to overflow). */
function jsonDomain(document: unknown) {
  const stack: [unknown, number][] = [[document, 0]];
  while (stack.length > 0) {
    const [value, depth] = stack.pop()!;
    if (depth > MAX_DEPTH) refuse();
    if (typeof value === "number")
      check(
        Number.isFinite(value) &&
          (!Number.isInteger(value) || Math.abs(value) <= MAX_SAFE),
      );
    else if (typeof value === "string") check(!lonely(value));
    else if (Array.isArray(value))
      for (const item of value) stack.push([item, depth + 1]);
    else if (isRecord(value))
      for (const [key, item] of Object.entries(value)) {
        check(!lonely(key));
        stack.push([item, depth + 1]);
      }
  }
}

/** The document, checked field by field, or why Studio can't use it. */
export function readTestData(value: unknown): TestDataReading {
  try {
    // First, and without recursion: a deeply nested value never reaches JSON.stringify.
    jsonDomain(value);
    const json = JSON.stringify(value) ?? "";
    // UTF-8 takes at most three bytes per UTF-16 unit: encode only when it could matter.
    if (
      json.length * 3 > TEST_DATA_MAX_BYTES &&
      new TextEncoder().encode(json).length > TEST_DATA_MAX_BYTES
    )
      return { ok: false, reason: TOO_LARGE };
    const top = fields(
      value,
      ["schemaVersion", "kind", "updatedAt"],
      ["input", "pins", "signals", "humans", "aiTurns", "workflows", "waits"],
    );
    check(top["schemaVersion"] === 1);
    check(top["kind"] === "weave.studio/testData");
    check(text(top["updatedAt"], DATE_TIME));
    if (present(top, "input")) entry(top["input"]);
    if (present(top, "pins")) keyed(top["pins"], 1000, pin);
    if (present(top, "signals")) keyed(top["signals"], 1000, signal);
    if (present(top, "humans")) keyed(top["humans"], 1000, human);
    if (present(top, "aiTurns")) keyed(top["aiTurns"], 1000, aiTurns);
    if (present(top, "workflows"))
      keyed(
        top["workflows"],
        50,
        (callee) => keyed(fields(callee, ["mocks"], [])["mocks"], 1000, mock),
        (name) => text(name, REFERENCE, 300),
      );
    if (present(top, "waits")) {
      const waits = fields(top["waits"], [], ["autoAdvance"]);
      if (present(waits, "autoAdvance"))
        check(typeof waits["autoAdvance"] === "boolean");
    }
    return { ok: true, data: value as StudioTestData };
  } catch (error) {
    if (error instanceof Refused) return { ok: false, reason: UNREADABLE };
    throw error;
  }
}
