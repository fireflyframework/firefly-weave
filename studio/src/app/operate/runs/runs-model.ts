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
// Operate › Runs: the filters a person sets, the address that keeps the
// shareable ones, and the run_summaries.list query each page of results
// uses. A relative time window ("the last 24 hours") stays a window in the
// address and in the page; each chain of pages resolves it to fixed bounds
// once, so Load more continues exactly the list that was first read. Business
// and correlation keys may carry business data: they reach the platform but
// never the address. Pure: the page renders it, tests check it.
import { durationText } from "../../format";
import { statusLabel, statusTone, type Tone } from "../../status-labels";
import {
  runOrigins,
  runStatuses,
  runSummaryOrders,
  runViewQuery,
  terminalRunStatuses,
  type RunOrigin,
  type RunStatus,
  type RunSummary,
  type RunSummaryOrder,
  type RunSummaryQuery,
} from "../run-contracts";

export type RunTimeWindow =
  | { kind: "relative"; milliseconds: number }
  | { kind: "absolute"; started_after?: string; started_before?: string };
export type RunsFilters = Omit<
  RunSummaryQuery,
  "limit" | "cursor" | "started_after" | "started_before"
> & { timeWindow: RunTimeWindow };

/** Runs read per page. */
export const RUNS_PAGE_SIZE = 50;
/** The longest window the platform accepts. */
export const MAX_RANGE_MS = 400 * 86_400_000;
const DEFAULT_RANGE_MS = 86_400_000;
/** The time ranges a person picks from, besides a custom range. */
export const rangePresets: {
  id: string;
  label: string;
  milliseconds: number;
}[] = [
  { id: "15m", label: "Last 15 minutes", milliseconds: 15 * 60_000 },
  { id: "1h", label: "Last hour", milliseconds: 3_600_000 },
  { id: "24h", label: "Last 24 hours", milliseconds: DEFAULT_RANGE_MS },
  { id: "7d", label: "Last 7 days", milliseconds: 7 * 86_400_000 },
  { id: "30d", label: "Last 30 days", milliseconds: 30 * 86_400_000 },
];

export function defaultRunsFilters(): RunsFilters {
  return {
    timeWindow: { kind: "relative", milliseconds: DEFAULT_RANGE_MS },
    order: "started_desc",
    include_test: false,
    include_archived: false,
    status: [],
    top_level_only: false,
  };
}

/**
 * The first page's query: a relative window ends now and starts its length
 * before; a custom range keeps its exact bounds.
 */
export function resolveRunsQuery(
  filters: RunsFilters,
  now: Date,
): RunSummaryQuery {
  const { timeWindow, ...query } = filters;
  if (timeWindow.kind === "absolute") {
    return {
      ...query,
      started_after: timeWindow.started_after,
      started_before: timeWindow.started_before,
      limit: RUNS_PAGE_SIZE,
    };
  }
  return {
    ...query,
    limit: RUNS_PAGE_SIZE,
    started_before: now.toISOString(),
    started_after: new Date(
      now.getTime() - timeWindow.milliseconds,
    ).toISOString(),
  };
}

/** The request for one page of a chain: its frozen query, and its cursor after the first page. */
export function runsQuery(
  query: RunSummaryQuery,
  cursor: string | null,
): string {
  return runViewQuery({ ...query, ...(cursor ? { cursor } : {}) });
}

const units: Record<string, number> = {
  m: 60_000,
  h: 3_600_000,
  d: 86_400_000,
};
/** "7d" or "90m": a positive whole duration of 400 days or less. */
function rangeMilliseconds(value: string): number | null {
  const match = /^([1-9][0-9]{0,5})([mhd])$/.exec(value);
  if (!match) return null;
  const milliseconds = Number(match[1]) * units[match[2]];
  return Number.isFinite(milliseconds) &&
    milliseconds > 0 &&
    milliseconds <= MAX_RANGE_MS
    ? milliseconds
    : null;
}
function rangeId(milliseconds: number): string {
  const preset = rangePresets.find(
    (item) => item.milliseconds === milliseconds,
  );
  if (preset) return preset.id;
  for (const unit of ["d", "h", "m"])
    if (milliseconds % units[unit] === 0)
      return `${milliseconds / units[unit]}${unit}`;
  return `${Math.max(1, Math.round(milliseconds / 60_000))}m`;
}
const instant =
  /^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$/;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const name = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$/;
const version = /^[0-9A-Za-z][0-9A-Za-z.+-]{0,199}$/;

/** A custom range from the address, when its bounds are times in order and at most 400 days apart. */
function absoluteWindow(
  after: string | null,
  before: string | null,
): RunTimeWindow | null {
  if (!after && !before) return null;
  if ((after && !instant.test(after)) || (before && !instant.test(before)))
    return null;
  const from = after ? Date.parse(after) : null;
  const to = before ? Date.parse(before) : null;
  if (
    (from !== null && !Number.isFinite(from)) ||
    (to !== null && !Number.isFinite(to))
  )
    return null;
  if (from !== null && to !== null && (from >= to || to - from > MAX_RANGE_MS))
    return null;
  return {
    kind: "absolute",
    ...(after ? { started_after: after } : {}),
    ...(before ? { started_before: before } : {}),
  };
}

/**
 * The filters an address shares. Business and correlation keys are never
 * read from it, and values the platform would refuse are left out.
 */
export function runsFiltersFromQuery(search: string): RunsFilters {
  const query = new URLSearchParams(search);
  const filters = defaultRunsFilters();
  const flag = (key: string) => {
    const value = query.get(key);
    return value === "true" ? true : value === "false" ? false : undefined;
  };
  const statuses = [...new Set(query.getAll("status"))].filter((value) =>
    (runStatuses as readonly string[]).includes(value),
  ) as RunStatus[];
  filters.status = statuses.slice(0, runStatuses.length);
  const workflow = query.get("workflow");
  if (workflow && name.test(workflow)) {
    filters.workflow = workflow;
    const chosen = query.get("version");
    if (chosen && version.test(chosen)) filters.version = chosen;
  }
  const origin = query.get("origin");
  if (origin && (runOrigins as readonly string[]).includes(origin))
    filters.origin = origin as RunOrigin;
  for (const key of [
    "caller_run_id",
    "retried_from_run_id",
    "activation_id",
  ] as const) {
    const value = query.get(key);
    if (value && uuid.test(value)) filters[key] = value.toLowerCase();
  }
  filters.include_test = flag("include_test") ?? false;
  filters.include_archived = flag("include_archived") ?? false;
  filters.top_level_only = flag("top_level_only") ?? false;
  const incident = flag("has_active_incident");
  if (incident !== undefined) filters.has_active_incident = incident;
  const handled = flag("has_handled_errors");
  if (handled !== undefined) filters.has_handled_errors = handled;
  const order = query.get("order");
  if (order && (runSummaryOrders as readonly string[]).includes(order))
    filters.order = order as RunSummaryOrder;
  const custom = absoluteWindow(
    query.get("started_after"),
    query.get("started_before"),
  );
  const range = rangeMilliseconds(query.get("range") ?? "");
  if (custom) filters.timeWindow = custom;
  else if (range !== null)
    filters.timeWindow = { kind: "relative", milliseconds: range };
  return filters;
}

/**
 * The address for these filters: only what differs from the defaults, a
 * relative window as its length ("range=7d"), a custom range as its exact
 * bounds. Business and correlation keys are left out by name.
 */
export function runsFiltersToQuery(filters: RunsFilters): string {
  const {
    business_key: _business,
    correlation_key: _correlation,
    timeWindow,
    ...shared
  } = filters;
  const query = new URLSearchParams();
  for (const status of shared.status ?? []) query.append("status", status);
  if (shared.workflow) {
    query.set("workflow", shared.workflow);
    if (shared.version) query.set("version", shared.version);
  }
  if (shared.origin) query.set("origin", shared.origin);
  for (const key of [
    "caller_run_id",
    "retried_from_run_id",
    "activation_id",
  ] as const)
    if (shared[key]) query.set(key, shared[key]);
  for (const key of [
    "include_test",
    "include_archived",
    "top_level_only",
  ] as const)
    if (shared[key]) query.set(key, "true");
  for (const key of ["has_active_incident", "has_handled_errors"] as const)
    if (shared[key] !== undefined) query.set(key, String(shared[key]));
  if (shared.order && shared.order !== "started_desc")
    query.set("order", shared.order);
  if (timeWindow.kind === "absolute") {
    if (timeWindow.started_after)
      query.set("started_after", timeWindow.started_after);
    if (timeWindow.started_before)
      query.set("started_before", timeWindow.started_before);
  } else if (timeWindow.milliseconds !== DEFAULT_RANGE_MS)
    query.set("range", rangeId(timeWindow.milliseconds));
  const text = query.toString();
  return text ? `?${text}` : "";
}

/** How many filters differ from the defaults; private ones count too. */
export function runFilterCount(filters: RunsFilters): number {
  const defaults = defaultRunsFilters();
  const window = filters.timeWindow;
  return [
    (filters.status ?? []).length > 0,
    !!filters.workflow,
    !!filters.origin,
    window.kind !== "relative" || window.milliseconds !== DEFAULT_RANGE_MS,
    !!filters.business_key,
    !!filters.correlation_key,
    filters.include_test !== defaults.include_test,
    filters.include_archived !== defaults.include_archived,
    filters.top_level_only !== defaults.top_level_only,
    filters.has_active_incident !== undefined,
    filters.has_handled_errors !== undefined,
    !!filters.caller_run_id,
    !!filters.retried_from_run_id,
    !!filters.activation_id,
    (filters.order ?? "started_desc") !== "started_desc",
  ].filter(Boolean).length;
}

/** The run's status pill. A run that handled errors and succeeded says so. */
export function runOutcome(run: RunSummary): { label: string; tone: Tone } {
  if (run.status === "succeeded" && (run.handled_errors ?? 0) > 0)
    return { label: "Succeeded with errors", tone: "warning" };
  return {
    label: statusLabel("run", run.status),
    tone: statusTone("run", run.status),
  };
}

export function isTerminalRun(run: RunSummary): boolean {
  return terminalRunStatuses.includes(run.status);
}

/** "2 errors handled" on a run that is still going; "" otherwise. */
export function handledText(run: RunSummary): string {
  const count = run.handled_errors ?? 0;
  if (!count || isTerminalRun(run)) return "";
  return `${count} ${count === 1 ? "error" : "errors"} handled`;
}

const origins: Record<RunOrigin, string> = {
  manual: "Manual",
  webhook: "Webhook",
  schedule: "Schedule",
  broker: "Kafka",
  email: "Email",
  provider: "App event",
  retry: "Retry",
  call: "Called by a workflow",
  test: "Test",
};
/** How the run started, in plain words; older runs may not say. */
export function originLabel(origin: RunOrigin | null): string {
  return origin ? origins[origin] : "Unknown";
}

/** Its recorded duration, or how long it has run so far while active. */
export function liveDuration(run: RunSummary, now: Date): string {
  if (run.duration_ms !== null) return durationText(run.duration_ms);
  if (isTerminalRun(run)) return "—";
  const started = Date.parse(run.started_at);
  return Number.isFinite(started)
    ? durationText(Math.max(0, now.getTime() - started))
    : "—";
}
