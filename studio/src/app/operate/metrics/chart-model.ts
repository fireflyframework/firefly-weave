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
import { durationText } from "../../format";

/** One series as `run_statistics.series` answers it. */
export type Granularity = "5m" | "1h" | "1d";
export type Unit = "count" | "ratio" | "ms" | "seconds";
export type Point = [string, number | null];
export interface StatisticsSeries {
  signal: string;
  labels: Partial<Record<"workflow" | "status" | "task_type", string>>;
  unit: Unit;
  points: Point[];
}

const HOUR = 3_600_000;
/** Five minutes up to 6 hours, one hour up to 7 days, one day beyond, as the server chooses. */
export function automaticGranularity(rangeMs: number): Granularity {
  if (rangeMs <= 6 * HOUR) return "5m";
  return rangeMs <= 7 * 24 * HOUR ? "1h" : "1d";
}

const STEPS = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
const exact = (value: number) => Number(value.toPrecision(12));

/** The smallest readable axis maximum at or above a value (1 for nothing). */
export function niceMax(value: number): number {
  if (!(value > 0)) return 1;
  const exponent = Math.floor(Math.log10(value));
  const scale = (step: number) =>
    exponent < 0 ? step / 10 ** -exponent : step * 10 ** exponent;
  for (const step of STEPS) {
    const candidate = exact(scale(step));
    if (candidate >= value) return candidate;
  }
  return exact(scale(10));
}

/** Evenly spaced tick values from 0 to max. */
export function ticks(max: number, count: number): number[] {
  return Array.from({ length: count + 1 }, (_, index) =>
    exact((index * max) / count),
  );
}

export interface LineFrame {
  width: number;
  height: number;
  max: number;
  /** How many buckets the axis holds; x steps evenly across the width. */
  count: number;
}

const round = (value: number) => Math.round(value * 100) / 100;

/** An SVG path through [bucket index, value] points; an unknown value breaks the line. */
export function linePath(
  points: Array<[number, number | null]>,
  frame: LineFrame,
): string {
  const step = frame.count > 1 ? frame.width / (frame.count - 1) : 0;
  const parts: string[] = [];
  let drawing = false;
  for (const [index, value] of points) {
    if (value === null) {
      drawing = false;
      continue;
    }
    const x = round(index * step);
    const y = round(frame.height - (value / frame.max) * frame.height);
    parts.push(`${drawing ? "L" : "M"}${x} ${y}`);
    drawing = true;
  }
  return parts.join(" ");
}

export interface Segment {
  key: string;
  y: number;
  height: number;
}

/** Stacked columns, bottom first; unknown and zero values add nothing. */
export function stackColumns(
  series: Array<{ key: string; values: Array<number | null> }>,
  frame: { height: number; max: number },
): Segment[][] {
  const length = Math.max(0, ...series.map((item) => item.values.length));
  return Array.from({ length }, (_, index) => {
    let top = frame.height;
    const column: Segment[] = [];
    for (const item of series) {
      const value = item.values[index];
      if (value === null || value === undefined || value <= 0) continue;
      const height = round((value / frame.max) * frame.height);
      top = round(top - height);
      column.push({ key: item.key, y: top, height });
    }
    return column;
  });
}

const OUTCOMES: Record<string, string> = {
  runs_succeeded: "var(--success)",
  runs_failed: "var(--danger)",
  runs_cancelled: "var(--neutral-ink)",
};
const OTHER = "(other)";

/** Outcome series take status colors; others take --series-1..4, then --series-other; p95 is the p50 hue, dashed. */
export function seriesStyle(
  signal: string,
  labels: StatisticsSeries["labels"],
  index: number,
): { color: string; dashed: boolean } {
  if (OUTCOMES[signal]) return { color: OUTCOMES[signal], dashed: false };
  if (signal === "duration_p50" || signal === "duration_p95")
    return {
      color: "var(--series-1)",
      dashed: signal === "duration_p95",
    };
  const label = labels.workflow ?? labels.task_type ?? labels.status;
  if (label === OTHER || index >= 4)
    return { color: "var(--series-other)", dashed: false };
  return { color: `var(--series-${index + 1})`, dashed: false };
}

const percent = new Intl.NumberFormat("en-US", {
  style: "percent",
  maximumFractionDigits: 1,
});
/** A fraction from the API as a percent; nothing as a dash. */
export function percentText(fraction: number | null): string {
  return fraction === null ? "—" : percent.format(fraction);
}

const SIGNAL_NAMES: Record<string, string> = {
  runs_started: "Runs started",
  runs_succeeded: "Succeeded",
  runs_failed: "Failed",
  runs_cancelled: "Canceled",
  failure_rate: "Failure rate",
  duration_p50: "p50 duration",
  duration_p95: "p95 duration",
  tasks_ready: "Ready tasks",
  oldest_ready_seconds: "Oldest ready task",
  outbox_pending: "Outbox backlog",
  worker_utilization: "Worker utilization",
  runs_active: "Active runs",
  waits_active: "Waits",
  provider_pending: "Provider backlog",
};

function seriesName(series: StatisticsSeries): string {
  const label =
    series.labels.workflow ?? series.labels.task_type ?? series.labels.status;
  const name = SIGNAL_NAMES[series.signal] ?? series.signal;
  return label ? `${name} · ${label}` : name;
}

function valueText(unit: Unit, value: number | null): string {
  if (value === null) return "—";
  if (unit === "ratio") return percentText(value);
  if (unit === "ms") return durationText(value);
  if (unit === "seconds") return durationText(value * 1000);
  return String(value);
}

/** The table a chart toggles to: one row per bucket, one column per series. */
export function chartRows(series: StatisticsSeries[]): string[][] {
  const header = ["Time", ...series.map(seriesName)];
  const times = series[0]?.points.map(([time]) => time) ?? [];
  return [
    header,
    ...times.map((time, index) => [
      time,
      ...series.map((item) =>
        valueText(item.unit, item.points[index]?.[1] ?? null),
      ),
    ]),
  ];
}

const FORMULA = /^[=+\-@\t\r]/;
/** RFC 4180 CSV; a cell a spreadsheet would run as a formula is kept as text. */
export function csv(rows: string[][]): string {
  const cell = (value: string) => {
    const text = FORMULA.test(value) ? `'${value}` : value;
    return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  return rows.map((row) => row.map(cell).join(",") + "\r\n").join("");
}
