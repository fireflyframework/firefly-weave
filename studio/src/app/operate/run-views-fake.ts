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
const name = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const semver =
  /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$/;
const instant =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
const maxRange = 400 * 86_400_000;

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
const contradictory = () =>
  new Rejected(
    problem(422, "WV-FILTER", "Invalid filter combination or time range"),
  );

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
    const kind = kinds[key];
    if (!kind) throw invalid("Unknown query parameter");
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
function timeOf(values: Values, key: string): number | undefined {
  const value = text(values, key, instant);
  return value === undefined ? undefined : Date.parse(value);
}

function encodeCursor(key: string, offset: number): string {
  return btoa(encodeURIComponent(JSON.stringify([key, offset])))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}
function offsetOf(cursor: string | undefined, key: string): number {
  if (cursor === undefined) return 0;
  try {
    const [bound, offset] = JSON.parse(
      decodeURIComponent(atob(cursor.replace(/-/g, "+").replace(/_/g, "/"))),
    ) as [unknown, unknown];
    if (
      bound === key &&
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
function page<T>(items: T[], values: Values, limit: number, key: string) {
  const offset = offsetOf(
    text(values, "cursor", /^[A-Za-z0-9_-]+$/, 2048),
    key,
  );
  return {
    items: items.slice(offset, offset + limit),
    next_cursor:
      offset + limit < items.length ? encodeCursor(key, offset + limit) : null,
  };
}
const compare = (left: string, right: string) =>
  left < right ? -1 : left > right ? 1 : 0;

function summaries(params: URLSearchParams, data: RunViewsFixture): FakeAnswer {
  const values = read(params, summaryParameters);
  const status = [
    ...new Set((values["status"] as string[] | undefined) ?? []),
  ].sort();
  if (
    status.length > 8 ||
    status.some((item) => !runStatuses.includes(item as never))
  )
    throw invalid("Invalid status");
  const filters = {
    workflow: text(values, "workflow", name),
    version: text(values, "version", semver),
    status,
    origin: choice(values, "origin", runOrigins),
    started_after: timeOf(values, "started_after"),
    started_before: timeOf(values, "started_before"),
    include_test: values["include_test"] === true,
    caller_run_id: text(values, "caller_run_id", uuid),
    top_level_only: values["top_level_only"] === true,
    retried_from_run_id: text(values, "retried_from_run_id", uuid),
    business_key: text(values, "business_key"),
    correlation_key: text(values, "correlation_key"),
    has_active_incident: values["has_active_incident"] as boolean | undefined,
    include_archived: values["include_archived"] === true,
    activation_id: text(values, "activation_id", uuid),
  };
  const order = choice(values, "order", runSummaryOrders) ?? "started_desc";
  const limit = limitOf(values, 100, 50);
  const { started_after: after, started_before: before } = filters;
  if (
    (filters.version !== undefined && filters.workflow === undefined) ||
    (filters.top_level_only &&
      (filters.caller_run_id !== undefined || filters.origin === "call")) ||
    (filters.origin === "test" && !filters.include_test) ||
    (after !== undefined &&
      before !== undefined &&
      (after >= before || before - after > maxRange))
  )
    throw contradictory();
  const time = (value: string) => Date.parse(value);
  const keep = (run: RunSummary) =>
    (filters.include_test || !run.test) &&
    (filters.include_archived || !run.archived) &&
    (filters.workflow === undefined ||
      run.workflow.name === filters.workflow) &&
    (filters.version === undefined ||
      run.workflow.version === filters.version) &&
    (status.length === 0 || status.includes(run.status)) &&
    (filters.origin === undefined || run.origin === filters.origin) &&
    (after === undefined || time(run.started_at) >= after) &&
    (before === undefined || time(run.started_at) < before) &&
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
    (filters.activation_id === undefined ||
      run.activation_id === filters.activation_id);
  const field = order === "updated_desc" ? "updated_at" : "started_at";
  const direction = order === "started_asc" ? 1 : -1;
  const runs = data.runs
    .filter((run) => keep(run.summary))
    .sort(
      (left, right) =>
        direction *
        (time(left.summary[field]) - time(right.summary[field]) ||
          compare(left.summary.id, right.summary.id)),
    );
  const result = page(runs, values, limit, JSON.stringify({ filters, order }));
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
      ...page(items, values, limit, JSON.stringify({ runId, step })),
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
    body: page(
      items,
      values,
      limit,
      JSON.stringify({ runId, level, source, node }),
    ),
  };
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
    const runId = decodeURIComponent(match[1]);
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
