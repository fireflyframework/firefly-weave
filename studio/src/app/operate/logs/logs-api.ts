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
// Reading log entries: a run's entries (runs.logs) and the entries across
// runs (execution_logs.list). Every item goes through readLogRow, so what the
// screen shows, the hidden-value count and the shortened mark included, comes
// only from an entry whose every field has the contract's shape. A withheld
// run's entry is a row with its ID only; anything else is unreadable.
import {
  logLevels,
  logSources,
  runViewQuery,
  type ExecutionLogQuery,
  type LogFieldValue,
  type RunLogEntry,
  type RunLogQuery,
  type RunViewsApi,
} from "../run-contracts";

/** The run an entry of the log across runs belongs to. */
export interface LogRun {
  id: string;
  workflow: string;
  version: string;
  test: boolean;
}
/** One row of a log: an entry, a withheld run's entry, or one Studio can't read. */
export type LogRow =
  | { kind: "entry"; id: string; entry: RunLogEntry; run: LogRun | null }
  | { kind: "withheld"; id: string }
  | { kind: "unreadable"; id: string };
export interface LogRows {
  rows: LogRow[];
  nextCursor: string | null;
}

type Json = Record<string, unknown>;
const isRecord = (value: unknown): value is Json =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isText = (value: unknown): value is string => typeof value === "string";
const isTextOrNull = (value: unknown) => value === null || isText(value);
const isCount = (value: unknown) =>
  Number.isSafeInteger(value) && (value as number) >= 0;
const isInstant = (value: unknown) =>
  isText(value) && Number.isFinite(Date.parse(value));
const isFieldValue = (value: unknown): value is LogFieldValue =>
  value === null ||
  typeof value === "string" ||
  typeof value === "boolean" ||
  (typeof value === "number" && Number.isFinite(value));
const cursorPattern = /^[A-Za-z0-9_-]+$/;

/** The entry, when every field has the shape runs.logs gives it. */
function readEntry(item: Json): RunLogEntry | null {
  const fields = item["fields"];
  const attempt = item["attempt"];
  if (
    !isText(item["id"]) ||
    !isInstant(item["at"]) ||
    !(logSources as readonly unknown[]).includes(item["source"]) ||
    !(logLevels as readonly unknown[]).includes(item["level"]) ||
    !isTextOrNull(item["code"]) ||
    !isText(item["message"]) ||
    !isTextOrNull(item["node_id"]) ||
    !isText(item["instance_key"]) ||
    !(
      attempt === null ||
      (Number.isSafeInteger(attempt) && (attempt as number) >= 1)
    ) ||
    !isTextOrNull(item["worker_id"]) ||
    !isRecord(fields) ||
    !Object.values(fields).every(isFieldValue) ||
    !isCount(item["redactions"]) ||
    typeof item["truncated"] !== "boolean"
  )
    return null;
  return {
    id: item["id"],
    at: item["at"] as string,
    source: item["source"] as RunLogEntry["source"],
    level: item["level"] as RunLogEntry["level"],
    code: item["code"] as string | null,
    message: item["message"],
    node_id: item["node_id"] as string | null,
    instance_key: item["instance_key"],
    attempt: attempt as number | null,
    worker_id: item["worker_id"] as string | null,
    fields: { ...(fields as Record<string, LogFieldValue>) },
    redactions: item["redactions"] as number,
    truncated: item["truncated"],
  };
}

/** The run of an entry across runs, when its fields have their shapes. */
function readRun(item: Json): LogRun | null {
  const run = {
    id: item["run_id"],
    workflow: item["workflow_name"],
    version: item["workflow_version"],
    test: item["test"],
  };
  return isText(run.id) &&
    isText(run.workflow) &&
    isText(run.version) &&
    typeof run.test === "boolean"
    ? (run as LogRun)
    : null;
}

/**
 * One item as a row; null for one without an ID, which nothing can show.
 * `across` items carry their run, and may be a withheld run's entry.
 */
export function readLogRow(item: unknown, across: boolean): LogRow | null {
  if (!isRecord(item) || !isText(item["id"]) || !item["id"]) return null;
  const id = item["id"];
  if (across && "unavailable" in item)
    return item["unavailable"] === true && Array.isArray(item["omissions"])
      ? { kind: "withheld", id }
      : { kind: "unreadable", id };
  const entry = readEntry(item);
  if (!entry) return { kind: "unreadable", id };
  if (!across) return { kind: "entry", id, entry, run: null };
  const run = readRun(item);
  return run ? { kind: "entry", id, entry, run } : { kind: "unreadable", id };
}

function readPage(value: unknown, across: boolean): LogRows {
  if (!isRecord(value) || !Array.isArray(value["items"]))
    return { rows: [], nextCursor: null };
  const rows = value["items"]
    .map((item) => readLogRow(item, across))
    .filter((row): row is LogRow => row !== null);
  const cursor = value["next_cursor"];
  return {
    rows,
    nextCursor: isText(cursor) && cursorPattern.test(cursor) ? cursor : null,
  };
}

/** A runs.logs answer: the run's entries in time order. */
export const readRunLogPage = (value: unknown): LogRows =>
  readPage(value, false);
/** An execution_logs.list answer: entries across runs, newest first. */
export const readExecutionLogPage = (value: unknown): LogRows =>
  readPage(value, true);

/** "/env/runs/ID/logs?level=warning". */
export const runLogPath = (
  environment: string,
  runId: string,
  query: RunLogQuery,
) =>
  `${environment}/runs/${encodeURIComponent(runId)}/logs${runViewQuery(query)}`;
/** "/env/execution-logs?from=…&to=…"; every value is percent-encoded. */
export const executionLogPath = (
  environment: string,
  query: ExecutionLogQuery,
) => `${environment}/execution-logs${runViewQuery(query)}`;

/** One page of a run's entries, read through the typed reader. */
export async function readRunLogs(
  api: RunViewsApi,
  runId: string,
  query: RunLogQuery,
): Promise<LogRows> {
  return readRunLogPage(
    await api.request<unknown>(runLogPath(api.environment, runId, query)),
  );
}
/** One page of the entries across runs, read through the typed reader. */
export async function readExecutionLogs(
  api: RunViewsApi,
  query: ExecutionLogQuery,
): Promise<LogRows> {
  return readExecutionLogPage(
    await api.request<unknown>(executionLogPath(api.environment, query)),
  );
}
