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
// Run summaries, run steps and run logs: run_summaries.list, runs.steps and
// runs.logs. Mirrors src/firefly_weave/contracts/run_views.py. Wire fields are
// snake_case; every key is present unless it is marked optional here.
import type { StudioApi } from "../api";

export const runStatuses = [
  "queued",
  "running",
  "waiting",
  "suspended",
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
] as const;
export type RunStatus = (typeof runStatuses)[number];
export const terminalRunStatuses: readonly RunStatus[] = [
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
];
export const runOrigins = [
  "manual",
  "webhook",
  "schedule",
  "broker",
  "email",
  "provider",
  "retry",
  "call",
  "test",
] as const;
export type RunOrigin = (typeof runOrigins)[number];
export const runSummaryOrders = [
  "started_desc",
  "started_asc",
  "updated_desc",
] as const;
export type RunSummaryOrder = (typeof runSummaryOrders)[number];
export const stepKinds = [
  "action",
  "llm",
  "transform",
  "decisionTable",
  "switch",
  "parallel",
  "wait",
  "signal",
  "humanTask",
  "fail",
  "forEach",
  "callWorkflow",
  "agent",
] as const;
export type StepKind = (typeof stepKinds)[number];
export const stepStatuses = [
  "scheduled",
  "running",
  "waiting",
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
] as const;
export type StepStatus = (typeof stepStatuses)[number];
export const logSources = ["engine", "worker", "connector", "ai"] as const;
export type LogSource = (typeof logSources)[number];
/** Lowest first: a `level` filter returns that level and every level after it. */
export const logLevels = ["debug", "info", "warning", "error"] as const;
export type LogLevel = (typeof logLevels)[number];
export type OmissionReason =
  | "classified_secret"
  | "classification_unavailable"
  | "uncertain_derived"
  | "confidential_text"
  | "resource_limit";
export interface Omission {
  path: string;
  reason: OmissionReason;
}
/** `node_id` is the static step ID; `instance_key` is "" when it equals it. */
export interface RunSummaryCaller {
  run_id: string;
  node_id: string;
  instance_key: string;
}
export interface FailedStep {
  node_id: string;
  instance_key: string;
  error_code: string | null;
}
export interface RunSummary {
  id: string;
  workflow: { name: string; version: string; definition_version_id: string };
  activation_id: string;
  activation: { name: string; revision: number };
  status: RunStatus;
  paused: boolean;
  test: boolean;
  /** null only for older runs whose start path is unknown. */
  origin: RunOrigin | null;
  caller: RunSummaryCaller | null;
  retried_from_run_id: string | null;
  started_at: string;
  updated_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  business_key: string | null;
  correlation_key: string | null;
  failed_step: FailedStep | null;
  active_incidents: number;
  archived: boolean;
}
/** A run whose classification is unavailable: only its ID is shown. */
export interface UnavailableRun {
  id: string;
  unavailable: true;
  omissions: Omission[];
}
export type RunSummaryItem = RunSummary | UnavailableRun;
export interface RunSummaryPage {
  items: RunSummaryItem[];
  next_cursor: string | null;
}
export interface StepFact {
  node_id: string;
  instance_key: string;
  /** Loop indexes of the instance key, outermost first. */
  iteration: number[];
  kind: StepKind;
  status: StepStatus;
  scheduled_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number | null;
  queue_wait_ms: number | null;
  attempts: number;
  worker_id: string | null;
  error_code: string | null;
  child_run_id: string | null;
  log_entries: number;
  /** Only with include=output: why the output was not returned. */
  omissions?: Omission[];
  /** Only with include=output, when the recorded output may be shown (it can be null). */
  output?: unknown;
}
export interface StepFactPage {
  items: StepFact[];
  next_cursor: string | null;
  /** false when older step instances have no timings. */
  complete: boolean;
}
export type LogFieldValue = string | number | boolean | null;
export interface RunLogEntry {
  id: string;
  at: string;
  source: LogSource;
  level: LogLevel;
  code: string | null;
  message: string;
  /** null for run-level entries. */
  node_id: string | null;
  instance_key: string;
  attempt: number | null;
  worker_id: string | null;
  fields: Record<string, LogFieldValue>;
  redactions: number;
  truncated: boolean;
}
export interface RunLogPage {
  items: RunLogEntry[];
  next_cursor: string | null;
}
export interface RunSummaryQuery {
  workflow?: string;
  /** Needs `workflow`. */
  version?: string;
  status?: RunStatus[];
  origin?: RunOrigin;
  /** RFC 3339; inclusive. */
  started_after?: string;
  /** RFC 3339; exclusive. At most 400 days after started_after. */
  started_before?: string;
  include_test?: boolean;
  caller_run_id?: string;
  top_level_only?: boolean;
  retried_from_run_id?: string;
  business_key?: string;
  correlation_key?: string;
  has_active_incident?: boolean;
  include_archived?: boolean;
  activation_id?: string;
  order?: RunSummaryOrder;
  /** 1 to 100; 50 by default. */
  limit?: number;
  cursor?: string;
}
export interface RunStepQuery {
  /** A static step ID; every instance of the step is returned. */
  step?: string;
  include?: "output";
  /** 1 to 500; 200 by default. */
  limit?: number;
  cursor?: string;
}
export interface RunLogQuery {
  /** The lowest level returned. */
  level?: LogLevel;
  source?: LogSource;
  /** A static step ID; every instance of the step is matched. */
  node_id?: string;
  /** 1 to 500; 200 by default. */
  limit?: number;
  cursor?: string;
}

export const isUnavailableRun = (
  item: RunSummaryItem,
): item is UnavailableRun => "unavailable" in item;

/** What runs.steps?include=output says about one step's recorded output. */
export type StepOutput =
  | { kind: "value"; value: unknown }
  | { kind: "omitted"; reason: OmissionReason }
  | { kind: "none" };
export function stepOutput(fact: StepFact): StepOutput {
  if ("output" in fact) return { kind: "value", value: fact.output };
  const omitted = fact.omissions?.find((item) => item.path === "/output");
  return omitted
    ? { kind: "omitted", reason: omitted.reason }
    : { kind: "none" };
}

/** The runs a run called. A test run's children are test runs too. */
export const childRunsQuery = (parent: RunSummary): RunSummaryQuery => ({
  caller_run_id: parent.id,
  include_test: parent.test,
});

/**
 * Query string for a run view. Names stay snake_case, `status` repeats, and
 * every value is percent-encoded: an unencoded "+02:00" offset would reach the
 * server as a space. Absent and empty values are left out.
 */
export function runViewQuery(
  query: RunSummaryQuery | RunStepQuery | RunLogQuery,
): string {
  const parts: string[] = [];
  for (const [name, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    for (const item of Array.isArray(value) ? value : [value])
      parts.push(
        `${encodeURIComponent(name)}=${encodeURIComponent(String(item))}`,
      );
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

/** The part of StudioApi the run views need, so tests can pass the fake. */
export interface RunViewsApi {
  readonly environment: string;
  request<T>(path: string): Promise<T>;
}
export const listRunSummaries = (
  api: RunViewsApi,
  query: RunSummaryQuery = {},
) =>
  api.request<RunSummaryPage>(
    `${api.environment}/run-summaries${runViewQuery(query)}`,
  );
export const listRunSteps = (
  api: RunViewsApi,
  runId: string,
  query: RunStepQuery = {},
) =>
  api.request<StepFactPage>(
    `${api.environment}/runs/${encodeURIComponent(runId)}/steps${runViewQuery(query)}`,
  );
export const listRunLogs = (
  api: RunViewsApi,
  runId: string,
  query: RunLogQuery = {},
) =>
  api.request<RunLogPage>(
    `${api.environment}/runs/${encodeURIComponent(runId)}/logs${runViewQuery(query)}`,
  );
/** StudioApi is a RunViewsApi; this adapter keeps that checked by the compiler. */
export const asRunViewsApi = (api: StudioApi): RunViewsApi => api;
