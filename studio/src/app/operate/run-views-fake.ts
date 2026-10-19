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
// An in-memory platform for run_summaries.list, runs.steps and runs.logs.
// It applies the frozen query rules (filters, order, limits, filter-bound
// cursors and the 422 codes) to run-views.fixture.ts, so Studio can be built
// and tested before a platform serves these operations.
import { ApiError } from "../api";
import {
  logLevels,
  logSources,
  runOrigins,
  runStatuses,
  runSummaryOrders,
  type RunLogEntry,
  type RunSummary,
  type RunViewsApi,
  type StepFact,
} from "./run-contracts";
import { runViewsFixture, type RunViewsFixture } from "./run-views.fixture";

export interface FakeAnswer {
  status: number;
  body: unknown;
}
export const fakeEnvironment =
  "/studio/api/api/v1/tenants/00000000-0000-4000-8000-000000000001" +
  "/projects/00000000-0000-4000-8000-000000000002" +
  "/environments/00000000-0000-4000-8000-000000000003";

type Kind = "string" | "boolean" | "integer" | "array";
type Values = Record<string, string | string[] | boolean | number | undefined>;
// Never looser than the server: whatever the server refuses, the fake refuses.
// It is stricter on UUIDs written as urn:uuid:, without hyphens or in braces,
// and names, versions or keys over 200 characters (counted in UTF-16 units,
// so keys of 101 to 200 characters outside the BMP also fail here).
const name = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// Mirrors SEMVER_PATTERN in src/firefly_weave/contracts/definitions.py.
const semver = new RegExp(
  `^${[
    String.raw`(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)`,
    String.raw`(?:-(?:(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))`,
    String.raw`(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?`,
    String.raw`(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?`,
  ].join("")}$`,
);
const instant =
  /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?:[Zz]|([+-])(\d{2}):(\d{2}))$/;
const cursorPattern = /^[A-Za-z0-9_-]+$/;
/** 400 days, in microseconds: the server compares instants to the microsecond. */
const maxRange = BigInt(400 * 86_400_000) * 1000n;
/** 0001-01-01T00:00:00Z to 9999-12-31T23:59:59.999999Z: the UTC instants the server can hold. */
const firstMicros = -62_135_596_800_000_000n;
const lastMicros = 253_402_300_799_999_999n;

const problem = (
  status: number,
  code: string,
  message: string,
): FakeAnswer => ({
  status,
  body: { code, message },
});
class Rejected extends Error {
  constructor(readonly answer: FakeAnswer) {
    super(String(answer.status));
  }
}
const invalid = (message: string) =>
  new Rejected(problem(422, "WV-VALIDATION", message));
/** The message names the failed rule, as RunSummaryQuery words it. */
const contradictory = (message: string) =>
  new Rejected(problem(422, "WV-FILTER", message));

const summaryParameters: Record<string, Kind> = {
  workflow: "string",
  version: "string",
  status: "array",
  origin: "string",
  started_after: "string",
  started_before: "string",
  include_test: "boolean",
  caller_run_id: "string",
  top_level_only: "boolean",
  retried_from_run_id: "string",
  business_key: "string",
  correlation_key: "string",
  has_active_incident: "boolean",
  has_handled_errors: "boolean",
  include_archived: "boolean",
  activation_id: "string",
  order: "string",
  limit: "integer",
  cursor: "string",
};
const stepParameters: Record<string, Kind> = {
  step: "string",
  include: "string",
  limit: "integer",
  cursor: "string",
};
const logParameters: Record<string, Kind> = {
  level: "string",
  source: "string",
  node_id: "string",
  limit: "integer",
  cursor: "string",
};

function read(params: URLSearchParams, kinds: Record<string, Kind>): Values {
  const values: Values = {};
  for (const key of new Set(params.keys())) {
    if (!Object.hasOwn(kinds, key)) throw invalid("Unknown query parameter");
    const kind = kinds[key];
    const raw = params.getAll(key);
    if (kind === "array") {
      values[key] = raw;
      continue;
    }
    if (raw.length !== 1) throw invalid("Repeated query parameter");
    if (kind === "boolean") {
      if (raw[0] !== "true" && raw[0] !== "false")
        throw invalid("A boolean query parameter must be true or false");
      values[key] = raw[0] === "true";
    } else if (kind === "integer") {
      if (!/^-?[0-9]{1,9}$/.test(raw[0]))
        throw invalid("An integer query parameter is required");
      values[key] = Number(raw[0]);
    } else values[key] = raw[0];
  }
  return values;
}
function text(
  values: Values,
  key: string,
  pattern?: RegExp,
  max = 200,
): string | undefined {
  const value = values[key];
  if (value === undefined) return undefined;
  if (
    typeof value !== "string" ||
    value.length > max ||
    (pattern && !pattern.test(value))
  )
    throw invalid(`Invalid ${key}`);
  return value;
}
function choice<T extends string>(
  values: Values,
  key: string,
  allowed: readonly T[],
): T | undefined {
  const value = text(values, key);
  if (value !== undefined && !allowed.includes(value as T))
    throw invalid(`Invalid ${key}`);
  return value as T | undefined;
}
function limitOf(values: Values, max: number, fallback: number): number {
  const value = values["limit"] ?? fallback;
  if (typeof value !== "number" || value < 1 || value > max)
    throw invalid("Invalid limit");
  return value;
}
/** UUID values are compared in lower case, as the server normalizes them. */
function uuidOf(values: Values, key: string): string | undefined {
  return text(values, key, uuid)?.toLowerCase();
}
const cursorOf = (values: Values) =>
  text(values, "cursor", cursorPattern, 2048);

/**
 * Microseconds since the epoch, with the server's rules: a real calendar
 * date, a 24-hour clock, offsets up to 23:59, fractions past six digits
 * truncated, and a UTC instant between year 1 and year 9999 (an offset can
 * push a local time outside it).
 */
function instantMicros(value: string): bigint {
  const match = instant.exec(value);
  if (match === null) throw invalid("Invalid timestamp");
  const [, year, month, day, hour, minute, second, digits, sign] = match;
  const offsetHour = Number(match[9] ?? 0);
  const offsetMinute = Number(match[10] ?? 0);
  const fields = [year, month, day, hour, minute, second].map(Number);
  const [y, mo, d, h, mi, s] = fields;
  const local = new Date(0);
  local.setUTCFullYear(y, mo - 1, d);
  local.setUTCHours(h, mi, s, 0);
  const rebuilt = [
    local.getUTCFullYear(),
    local.getUTCMonth() + 1,
    local.getUTCDate(),
    local.getUTCHours(),
    local.getUTCMinutes(),
    local.getUTCSeconds(),
  ];
  if (y < 1 || rebuilt.some((field, index) => field !== fields[index]))
    throw invalid("Invalid timestamp");
  if (offsetHour > 23 || offsetMinute > 59) throw invalid("Invalid timestamp");
  const offset =
    sign === undefined
      ? 0
      : (sign === "-" ? -1 : 1) * (offsetHour * 60 + offsetMinute);
  const micros = (digits ?? "").padEnd(6, "0").slice(0, 6);
  const utc =
    BigInt(local.getTime() - offset * 60_000) * 1000n + BigInt(micros);
  if (utc < firstMicros || utc > lastMicros) throw invalid("Invalid timestamp");
  return utc;
}
function timeOf(values: Values, key: string): bigint | undefined {
  const value = text(values, key, instant, 64);
  return value === undefined ? undefined : instantMicros(value);
}
/** Run timestamps come from the fixture, which has whole-millisecond precision. */
const recordMicros = (value: string): bigint =>
  BigInt(Date.parse(value)) * 1000n;
const compareMicros = (left: bigint, right: bigint) =>
  left < right ? -1 : left > right ? 1 : 0;

/**
 * cyrb53: a synchronous 53-bit hash. A cursor carries this digest of the
 * query scope, so filter values such as business keys never appear in it.
 */
function digest(value: string): string {
  let h1 = 0xdeadbeef;
  let h2 = 0x41c6ce57;
  for (let index = 0; index < value.length; index++) {
    const code = value.charCodeAt(index);
    h1 = Math.imul(h1 ^ code, 2654435761);
    h2 = Math.imul(h2 ^ code, 1597334677);
  }
  h1 =
    Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^
    Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 =
    Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^
    Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (4294967296 * (2097151 & h2) + (h1 >>> 0))
    .toString(16)
    .padStart(14, "0");
}
function base64url(value: string): string {
  return btoa(value).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function fromBase64url(cursor: string): string {
  const base64 = cursor.replace(/-/g, "+").replace(/_/g, "/");
  return atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, "="));
}
function offsetOf(cursor: string | undefined, bound: string): number {
  if (cursor === undefined) return 0;
  try {
    const [issued, offset] = JSON.parse(fromBase64url(cursor)) as [
      unknown,
      unknown,
    ];
    if (
      issued === bound &&
      typeof offset === "number" &&
      Number.isInteger(offset) &&
      offset > 0
    )
      return offset;
  } catch {
    // A cursor this fake did not issue is rejected like a foreign one.
  }
  throw invalid("Invalid scope-bound cursor");
}
function page<T>(items: T[], offset: number, limit: number, bound: string) {
  return {
    items: items.slice(offset, offset + limit),
    next_cursor:
      offset + limit < items.length
        ? base64url(JSON.stringify([bound, offset + limit]))
        : null,
  };
}
const compare = (left: string, right: string) =>
  left < right ? -1 : left > right ? 1 : 0;

function summaries(params: URLSearchParams, data: RunViewsFixture): FakeAnswer {
  const values = read(params, summaryParameters);
  // The server limits the raw list before it de-duplicates, so repeats count.
  const raw = (values["status"] as string[] | undefined) ?? [];
  if (
    raw.length > 8 ||
    raw.some((item) => !runStatuses.includes(item as never))
  )
    throw invalid("Invalid status");
  const status = [...new Set(raw)].sort();
  const filters = {
    workflow: text(values, "workflow", name),
    version: text(values, "version", semver),
    status,
    origin: choice(values, "origin", runOrigins),
    started_after: timeOf(values, "started_after"),
    started_before: timeOf(values, "started_before"),
    include_test: values["include_test"] === true,
    caller_run_id: uuidOf(values, "caller_run_id"),
    top_level_only: values["top_level_only"] === true,
    retried_from_run_id: uuidOf(values, "retried_from_run_id"),
    business_key: text(values, "business_key"),
    correlation_key: text(values, "correlation_key"),
    has_active_incident: values["has_active_incident"] as boolean | undefined,
    has_handled_errors: values["has_handled_errors"] as boolean | undefined,
    include_archived: values["include_archived"] === true,
    activation_id: uuidOf(values, "activation_id"),
  };
  const order = choice(values, "order", runSummaryOrders) ?? "started_desc";
  const limit = limitOf(values, 100, 50);
  const cursor = cursorOf(values);
  // Every value is checked above, so a value error wins over a filter combination.
  const after = filters.started_after;
  const before = filters.started_before;
  if (filters.version !== undefined && filters.workflow === undefined)
    throw contradictory("version requires workflow");
  if (
    filters.top_level_only &&
    (filters.caller_run_id !== undefined || filters.origin === "call")
  )
    throw contradictory(
      "top_level_only excludes caller_run_id and origin call",
    );
  if (filters.origin === "test" && !filters.include_test)
    throw contradictory("origin test requires include_test");
  if (after !== undefined && before !== undefined) {
    if (after >= before)
      throw contradictory("started_after must be earlier than started_before");
    if (before - after > maxRange)
      throw contradictory("The time range is longer than 400 days");
  }
  const bound = digest(
    JSON.stringify({
      ...filters,
      started_after: after?.toString(),
      started_before: before?.toString(),
      order,
    }),
  );
  const keep = (run: RunSummary) =>
    (filters.include_test || !run.test) &&
    (filters.include_archived || !run.archived) &&
    (filters.workflow === undefined ||
      run.workflow.name === filters.workflow) &&
    (filters.version === undefined ||
      run.workflow.version === filters.version) &&
    (status.length === 0 || status.includes(run.status)) &&
    (filters.origin === undefined || run.origin === filters.origin) &&
    (after === undefined || recordMicros(run.started_at) >= after) &&
    (before === undefined || recordMicros(run.started_at) < before) &&
    (filters.caller_run_id === undefined ||
      run.caller?.run_id === filters.caller_run_id) &&
    (!filters.top_level_only || run.caller === null) &&
    (filters.retried_from_run_id === undefined ||
      run.retried_from_run_id === filters.retried_from_run_id) &&
    (filters.business_key === undefined ||
      run.business_key === filters.business_key) &&
    (filters.correlation_key === undefined ||
      run.correlation_key === filters.correlation_key) &&
    (filters.has_active_incident === undefined ||
      run.active_incidents > 0 === filters.has_active_incident) &&
    (filters.has_handled_errors === undefined ||
      (run.handled_errors ?? 0) > 0 === filters.has_handled_errors) &&
    (filters.activation_id === undefined ||
      run.activation_id === filters.activation_id);
  const field = order === "updated_desc" ? "updated_at" : "started_at";
  const direction = order === "started_asc" ? 1 : -1;
  const runs = data.runs
    .filter((run) => keep(run.summary))
    .sort(
      (left, right) =>
        direction *
        (compareMicros(
          recordMicros(left.summary[field]),
          recordMicros(right.summary[field]),
        ) || compare(left.summary.id, right.summary.id)),
    );
  const result = page(runs, offsetOf(cursor, bound), limit, bound);
  return {
    status: 200,
    body: {
      ...result,
      items: result.items.map((run) =>
        run.unavailable
          ? {
              id: run.summary.id,
              unavailable: true,
              omissions: [{ path: "", reason: "classification_unavailable" }],
            }
          : run.summary,
      ),
    },
  };
}

function steps(
  runId: string,
  params: URLSearchParams,
  data: RunViewsFixture,
): FakeAnswer {
  const values = read(params, stepParameters);
  const step = text(values, "step", name);
  const include = choice(values, "include", ["output"] as const);
  const limit = limitOf(values, 500, 200);
  // The server decodes the cursor, bound to this run and filter, before it
  // looks the run up, so a foreign cursor on an unknown run is a 422.
  const bound = digest(JSON.stringify({ runId, step }));
  const offset = offsetOf(cursorOf(values), bound);
  if (!data.runs.some((run) => run.summary.id === runId))
    return problem(404, "WV-NOT-FOUND", "Run not found");
  const record = data.steps[runId] ?? { complete: true, items: [] };
  const scheduled = (fact: StepFact) =>
    fact.scheduled_at === null
      ? Number.POSITIVE_INFINITY
      : Date.parse(fact.scheduled_at);
  const items = record.items
    .filter((fact) => step === undefined || fact.node_id === step)
    .sort(
      (left, right) =>
        scheduled(left) - scheduled(right) ||
        compare(left.node_id, right.node_id) ||
        compare(left.instance_key, right.instance_key),
    )
    .map((fact) => {
      if (include === "output") return fact;
      const { output: _output, omissions: _omissions, ...rest } = fact;
      return rest;
    });
  return {
    status: 200,
    body: {
      ...page(items, offset, limit, bound),
      complete: record.complete,
    },
  };
}

function logs(
  runId: string,
  params: URLSearchParams,
  data: RunViewsFixture,
): FakeAnswer {
  const values = read(params, logParameters);
  const level = choice(values, "level", logLevels);
  const source = choice(values, "source", logSources);
  const node = text(values, "node_id", name);
  const limit = limitOf(values, 500, 200);
  const bound = digest(JSON.stringify({ runId, level, source, node }));
  const offset = offsetOf(cursorOf(values), bound);
  if (!data.runs.some((run) => run.summary.id === runId))
    return problem(404, "WV-NOT-FOUND", "Run not found");
  const lowest = level === undefined ? 0 : logLevels.indexOf(level);
  const items = (data.logs[runId] ?? [])
    .filter(
      (entry: RunLogEntry) =>
        logLevels.indexOf(entry.level) >= lowest &&
        (source === undefined || entry.source === source) &&
        (node === undefined || entry.node_id === node),
    )
    .sort(
      (left, right) =>
        Date.parse(left.at) - Date.parse(right.at) ||
        compare(left.id, right.id),
    );
  return {
    status: 200,
    body: page(items, offset, limit, bound),
  };
}

function decodedSegment(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return "";
  }
}

/**
 * The fake platform answer for a run view URL (absolute, or a path with its
 * query), or null when the URL is not one of the three operations.
 */
export function answerRunViews(
  url: string,
  data: RunViewsFixture = runViewsFixture,
): FakeAnswer | null {
  const parsed = new URL(url, "http://studio.invalid");
  const { pathname, searchParams } = parsed;
  try {
    if (pathname.endsWith("/run-summaries"))
      return summaries(searchParams, data);
    const match = /\/runs\/([^/]+)\/(steps|logs)$/.exec(pathname);
    if (!match) return null;
    // Run IDs are compared in lower case, as the server normalizes them. An ID
    // that is not valid percent-encoding is no run ID, like any other bad one.
    const runId = decodedSegment(match[1]).toLowerCase();
    if (!uuid.test(runId))
      return problem(404, "WV-STUDIO-ROUTE", "Unknown platform operation");
    return match[2] === "steps"
      ? steps(runId, searchParams, data)
      : logs(runId, searchParams, data);
  } catch (error) {
    if (error instanceof Rejected) return error.answer;
    throw error;
  }
}

/** A RunViewsApi backed by the fake; failures throw ApiError like StudioApi. */
export function fakeRunViewsApi(
  data: RunViewsFixture = runViewsFixture,
): RunViewsApi {
  return {
    environment: fakeEnvironment,
    async request<T>(path: string): Promise<T> {
      const answer =
        answerRunViews(path, data) ??
        problem(404, "WV-STUDIO-ROUTE", "Unknown platform operation");
      if (answer.status >= 400) throw new ApiError(answer.status, answer.body);
      return structuredClone(answer.body) as T;
    },
  };
}
