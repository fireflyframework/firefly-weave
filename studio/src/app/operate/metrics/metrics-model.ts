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
// Operate › Metrics: what the page asks run statistics for, and the figures,
// labels and tables it shows from the answers. Mirrors
// src/firefly_weave/contracts/run_statistics.py. Test runs never count: the
// server leaves them out.
import type { MetricsRange } from "../operate-routes";
import {
  csv,
  metricText,
  percentText,
  type Granularity,
  type StatisticsSeries,
} from "./chart-model";

interface Window {
  from: string;
  to: string;
  granularity: Granularity;
  /** Buckets ending at or before this time no longer change; null before the first rollup. */
  complete_until: string | null;
}
export interface SeriesAnswer extends Window {
  series: StatisticsSeries[];
}
export interface WorkflowStatistics {
  workflow: string;
  versions: string[];
  runs: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  failure_rate: number | null;
  duration_p50_ms: number | null;
  duration_p95_ms: number | null;
  last_run_at: string | null;
}
export interface RunTotals {
  runs: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  failure_rate: number | null;
  duration_p50_ms: number | null;
  duration_p95_ms: number | null;
}
export interface WorkflowAnswer extends Window {
  workflows: WorkflowStatistics[];
  totals?: RunTotals | null;
}
export interface StepStatistics {
  node_id: string;
  kind: string;
  executions: number;
  failures: number;
  duration_p50_ms: number | null;
  duration_p95_ms: number | null;
  queue_p50_ms: number | null;
  queue_p95_ms: number | null;
}
export interface StepAnswer extends Window {
  workflow: string;
  steps: StepStatistics[];
}

export const OTHER = "(other)";
const MINUTE = 60_000;
export const rangeMs: Record<MetricsRange, number> = {
  "1h": 60 * MINUTE,
  "6h": 6 * 60 * MINUTE,
  "24h": 24 * 60 * MINUTE,
  "7d": 7 * 24 * 60 * MINUTE,
  "30d": 30 * 24 * 60 * MINUTE,
};
export const rangeLabels: Record<MetricsRange, string> = {
  "1h": "1 h",
  "6h": "6 h",
  "24h": "24 h",
  "7d": "7 d",
  "30d": "30 d",
};

const second = (at: number) =>
  new Date(Math.floor(at / 1000) * 1000).toISOString().replace(".000Z", "Z");

/** The range back from now; `offset` moves this computer's clock to the server's. */
export function rangeWindow(
  range: MetricsRange,
  now: number,
  offset = 0,
): { from: string; to: string } {
  const to = Math.floor((now + offset) / 1000) * 1000;
  return { from: second(to - rangeMs[range]), to: second(to) };
}

export type ChartId =
  | "throughput"
  | "rate"
  | "durations"
  | "queue"
  | "outbox"
  | "workers";
interface ChartAsk {
  signals: string[];
  /** Run signals may be filtered by workflow; shared capacity can't. */
  runs: boolean;
  group?: "task_type";
}
const CHARTS: Record<ChartId, ChartAsk> = {
  throughput: {
    signals: [
      "runs_succeeded",
      "runs_failed",
      "runs_cancelled",
      "runs_started",
    ],
    runs: true,
  },
  rate: { signals: ["failure_rate"], runs: true },
  durations: { signals: ["duration_p50", "duration_p95"], runs: true },
  queue: {
    signals: ["tasks_ready", "oldest_ready_seconds"],
    runs: false,
    group: "task_type",
  },
  outbox: { signals: ["outbox_pending"], runs: false },
  workers: { signals: ["worker_utilization"], runs: false },
};
export const chartIds = Object.keys(CHARTS) as ChartId[];

function query(
  window: { from: string; to: string },
  signals: string[],
  extra: Record<string, string> = {},
): string {
  const params = new URLSearchParams({ from: window.from, to: window.to });
  for (const signal of signals) params.append("signals", signal);
  for (const [name, value] of Object.entries(extra)) params.set(name, value);
  return params.toString();
}

/** The `run_statistics.series` query of each chart; granularity is left to the server. */
export function chartRequests(
  window: { from: string; to: string },
  workflow: string,
): { chart: ChartId; query: string }[] {
  return chartIds.map((chart) => {
    const ask = CHARTS[chart];
    const extra: Record<string, string> = {};
    if (ask.runs && workflow) extra["workflow"] = workflow;
    if (ask.group) {
      extra["group_by"] = ask.group;
      extra["top"] = "4";
    }
    return { chart, query: query(window, ask.signals, extra) };
  });
}

/** One request for every visible workflow's 24-hour sparkline: hourly runs started, top 10. */
export function sparklineRequest(now: number, offset = 0): string {
  return query(rangeWindow("24h", now, offset), ["runs_started"], {
    group_by: "workflow",
    top: "10",
    granularity: "1h",
  });
}

/** Each workflow's hourly runs started; the folded "(other)" line has no row to sit in. */
export function sparklines(
  answer: SeriesAnswer | null,
): Map<string, Array<number | null>> {
  const lines = new Map<string, Array<number | null>>();
  for (const item of answer?.series ?? []) {
    const workflow = item.labels.workflow;
    if (workflow && workflow !== OTHER)
      lines.set(
        workflow,
        item.points.map(([, value]) => value),
      );
  }
  return lines;
}

const total = (answer: SeriesAnswer | undefined, signal: string) => {
  const found = answer?.series.filter((item) => item.signal === signal) ?? [];
  if (!found.length) return null;
  return found.reduce(
    (sum, item) =>
      sum + item.points.reduce((inner, [, value]) => inner + (value ?? 0), 0),
    0,
  );
};

/** The latest bucket any series knows, and the largest value there. */
function latestMax(answer: SeriesAnswer | undefined, signal: string) {
  const found = answer?.series.filter((item) => item.signal === signal) ?? [];
  const length = Math.max(0, ...found.map((item) => item.points.length));
  for (let index = length - 1; index >= 0; index--) {
    const values = found
      .map((item) => item.points[index]?.[1] ?? null)
      .filter((value): value is number => value !== null);
    if (values.length) return Math.max(...values);
  }
  return null;
}

export interface Kpi {
  id: "started" | "success" | "failure" | "p50" | "p95" | "oldest";
  label: string;
  value: string;
}

/**
 * The figures above the charts: runs started, success and failure rates over
 * ended runs, p50 and p95 run duration from the merged range (the chosen
 * workflow's row, or the totals), and the oldest ready task now.
 */
export function kpis(answers: {
  throughput?: SeriesAnswer;
  queue?: SeriesAnswer;
  workflows?: WorkflowAnswer;
  workflow: string;
}): Kpi[] {
  const started = total(answers.throughput, "runs_started");
  const succeeded = total(answers.throughput, "runs_succeeded") ?? 0;
  const failed = total(answers.throughput, "runs_failed") ?? 0;
  const cancelled = total(answers.throughput, "runs_cancelled") ?? 0;
  const ended = succeeded + failed + cancelled;
  const merged = answers.workflow
    ? answers.workflows?.workflows.find(
        (row) => row.workflow === answers.workflow,
      )
    : answers.workflows?.totals;
  return [
    {
      id: "started",
      label: "Runs started",
      value: metricText("count", started),
    },
    {
      id: "success",
      label: "Success rate",
      value: percentText(ended ? succeeded / ended : null),
    },
    {
      id: "failure",
      label: "Failure rate",
      value: percentText(ended ? failed / ended : null),
    },
    {
      id: "p50",
      label: "p50 run duration",
      value: metricText("ms", merged?.duration_p50_ms ?? null),
    },
    {
      id: "p95",
      label: "p95 run duration",
      value: metricText("ms", merged?.duration_p95_ms ?? null),
    },
    {
      id: "oldest",
      label: "Oldest ready task",
      value: metricText(
        "seconds",
        latestMax(answers.queue, "oldest_ready_seconds"),
      ),
    },
  ];
}

const clock = new Intl.DateTimeFormat("en-US", {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
/** "Data complete until 14:32" in local time; the page's tooltip gives the full date. */
export function freshness(completeUntil: string | null): string {
  if (!completeUntil) return "No figures rolled up yet";
  return `Data complete until ${clock.format(new Date(completeUntil))}`;
}

export interface WorkflowRow {
  workflow: string;
  versions: string;
  runs: string;
  failureRate: string;
  p50: string;
  p95: string;
  lastRunAt: string | null;
  /** "(other)" folds workflows past the table's limit: it has no steps to open. */
  other: boolean;
}

export function workflowRows(answer: WorkflowAnswer | null): WorkflowRow[] {
  return (answer?.workflows ?? []).map((row) => ({
    workflow: row.workflow,
    versions: row.versions.join(", "),
    runs: metricText("count", row.runs),
    failureRate: percentText(row.failure_rate),
    p50: metricText("ms", row.duration_p50_ms),
    p95: metricText("ms", row.duration_p95_ms),
    lastRunAt: row.last_run_at,
    other: row.workflow === OTHER,
  }));
}

const raw = (value: number | null) => (value === null ? "" : String(value));

/** The workflow table as CSV: raw numbers, rates as fractions, durations in milliseconds. */
export function workflowCsv(answer: WorkflowAnswer): string {
  return csv([
    [
      "Workflow",
      "Versions",
      "Runs",
      "Succeeded",
      "Failed",
      "Canceled",
      "Failure rate",
      "p50 duration (ms)",
      "p95 duration (ms)",
      "Last run",
    ],
    ...answer.workflows.map((row) => [
      row.workflow,
      row.versions.join(", "),
      String(row.runs),
      String(row.succeeded),
      String(row.failed),
      String(row.cancelled),
      raw(row.failure_rate),
      raw(row.duration_p50_ms),
      raw(row.duration_p95_ms),
      row.last_run_at ?? "",
    ]),
  ]);
}

export interface StepRow {
  step: string;
  kind: string;
  executions: string;
  failures: string;
  p50: string;
  p95: string;
  queueP95: string;
}

const kindLabel = (kind: string) =>
  kind ? kind[0].toUpperCase() + kind.slice(1).replace(/_/g, " ") : "";

export function stepRows(answer: StepAnswer | null): StepRow[] {
  return (answer?.steps ?? []).map((row) => ({
    step: row.node_id,
    kind: kindLabel(row.kind),
    executions: metricText("count", row.executions),
    failures: metricText("count", row.failures),
    p50: metricText("ms", row.duration_p50_ms),
    p95: metricText("ms", row.duration_p95_ms),
    queueP95: metricText("ms", row.queue_p95_ms),
  }));
}

/** The step table as CSV, durations and queue waits in milliseconds. */
export function stepCsv(answer: StepAnswer): string {
  return csv([
    [
      "Step",
      "Kind",
      "Executions",
      "Failures",
      "p50 duration (ms)",
      "p95 duration (ms)",
      "p50 queue wait (ms)",
      "p95 queue wait (ms)",
    ],
    ...answer.steps.map((row) => [
      row.node_id,
      row.kind,
      String(row.executions),
      String(row.failures),
      raw(row.duration_p50_ms),
      raw(row.duration_p95_ms),
      raw(row.queue_p50_ms),
      raw(row.queue_p95_ms),
    ]),
  ]);
}
