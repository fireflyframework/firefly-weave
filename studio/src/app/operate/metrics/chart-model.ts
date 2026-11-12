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

const READABLE_STEPS = [1, 2, 2.5, 5];
/** How many intervals divide an axis maximum into readable steps (1, 2, 2.5 or 5 × 10^k), preferring 4. */
export function tickCount(max: number): number {
  for (const count of [4, 5, 3, 2, 6]) {
    const step = max / count;
    const mantissa = exact(step / 10 ** Math.floor(Math.log10(step)));
    if (READABLE_STEPS.includes(mantissa)) return count;
  }
  return 4;
}

/** The y-axis ticks of a chart: whole counts step by whole numbers (no 2.5 runs), others by readable steps. */
export function axisTicks(max: number, unit: Unit): number[] {
  if (unit === "count" && Number.isInteger(max)) {
    if (max <= 6) return ticks(max, max);
    for (const count of [4, 5, 3, 2, 6]) {
      const step = max / count;
      const mantissa = exact(step / 10 ** Math.floor(Math.log10(step)));
      if (Number.isInteger(step) && READABLE_STEPS.includes(mantissa))
        return ticks(max, count);
    }
  }
  return ticks(max, tickCount(max));
}

/** Tick labels; zero is written in the unit of the rest of the axis ("0 s", not "0 ms", beside "5 s"). */
export function axisLabels(values: number[], unit: Unit): string[] {
  const labels = values.map((value) => metricText(unit, value));
  const top = labels[labels.length - 1] ?? "";
  if ((unit === "ms" || unit === "seconds") && !top.endsWith(" ms"))
    return labels.map((label, index) => (values[index] === 0 ? "0 s" : label));
  return labels;
}

/** The scale of a chart: its maximum, its ticks and their labels, as the chart draws them. */
export function chartAxis(
  series: StatisticsSeries[],
  stackedSignals: string[],
  lineSignals: string[],
  unit: Unit,
): { max: number; values: number[]; labels: string[] } {
  const stacked = stackedSignals.flatMap((signal) =>
    series.filter((item) => item.signal === signal),
  );
  const lines = series.filter((item) =>
    lineSignals.length
      ? lineSignals.includes(item.signal)
      : !stackedSignals.includes(item.signal),
  );
  const count = Math.max(0, ...series.map((item) => item.points.length));
  const sums = Array.from({ length: count }, (_, index) =>
    stacked.reduce((sum, item) => sum + (item.points[index]?.[1] ?? 0), 0),
  );
  const lineMax = Math.max(
    0,
    ...lines.flatMap((item) => item.points.map(([, value]) => value ?? 0)),
  );
  const max = niceMax(Math.max(lineMax, ...sums));
  const values = axisTicks(max, unit);
  return { max, values, labels: axisLabels(values, unit) };
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

/** A series' name in legends, tooltips and tables: "Ready tasks · email". */
export function seriesName(series: StatisticsSeries): string {
  const label =
    series.labels.workflow ?? series.labels.task_type ?? series.labels.status;
  const name = SIGNAL_NAMES[series.signal] ?? series.signal;
  return label ? `${name} · ${label}` : name;
}

const whole = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
/** A value in its unit: "1,200", "20%", "450 ms", "1 min 30 s"; nothing as a dash. */
export function metricText(unit: Unit, value: number | null): string {
  if (value === null) return "—";
  if (unit === "ratio") return percentText(value);
  const ms = unit === "seconds" ? value * 1000 : value;
  if (unit === "ms" || unit === "seconds") {
    if (ms >= 1000) return durationText(ms);
    return ms < 10 ? `${Number(ms.toFixed(1))} ms` : `${Math.round(ms)} ms`;
  }
  return whole.format(value);
}
const valueText = metricText;

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

const clock = new Intl.DateTimeFormat("en-US", {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
const dated = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
// Daily buckets are UTC days: named in UTC, so they never shift a day west of UTC.
const day = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});
/**
 * A bucket's label: "14:30" in local time, "Oct 25, 14:30" when the range
 * spans days (`withDate`), or the UTC day "Oct 25" for daily buckets.
 */
export function timeLabel(
  iso: string,
  granularity: Granularity,
  withDate = false,
): string {
  const at = new Date(iso);
  if (granularity === "1d") return day.format(at);
  return (withDate ? dated : clock).format(at);
}

/** Whether points reach across more than one day, so time labels need dates. */
export function spansDays(points: Point[]): boolean {
  if (points.length < 2) return false;
  const first = Date.parse(points[0][0]);
  const last = Date.parse(points[points.length - 1][0]);
  return last - first >= 24 * 3_600_000;
}

/** Estimated width of one character of tabular figures at the axis size. */
export const CHAR_PX = 8;
const LABEL_GAP = 8;
/** Room the edge labels need on either side of the plot. */
const EDGE = 24;

/** The left margin that holds the widest y-axis tick label. */
export function yAxisWidth(labels: string[]): number {
  return (
    Math.max(0, ...labels.map((label) => label.length)) * CHAR_PX + LABEL_GAP
  );
}

export interface ChartFrame {
  left: number;
  right: number;
  plotWidth: number;
}

/** Margins and plot width of a chart this wide whose y axis shows these labels. */
export function chartFrame(
  width: number,
  yLabels: string[],
  gutter = 0,
): ChartFrame {
  // A page passes one gutter to every chart, so plots in a column share their left edge.
  const left = Math.max(EDGE, yAxisWidth(yLabels), gutter);
  return { left, right: EDGE, plotWidth: Math.max(0, width - left - EDGE) };
}

export interface ColumnLayout {
  band: number;
  column: number;
  gap: number;
  /** Left edge of a bucket's column, from the plot's left. */
  x(index: number): number;
  /** Center of a bucket's column, where lines meet it. */
  center(index: number): number;
}

/** Equal bands per bucket; the column leaves a gap of a fifth of the band, at least 1 px. */
export function columnLayout(count: number, plotWidth: number): ColumnLayout {
  const band = count > 0 ? plotWidth / count : plotWidth;
  const gap = Math.max(1, band / 5);
  const column = Math.max(0, band - gap);
  return {
    band,
    column,
    gap,
    x: (index) => index * band + gap / 2,
    center: (index) => index * band + band / 2,
  };
}

/** Buckets that get a time label: every nth from the first, so labels never overlap. */
export function xLabelIndices(
  count: number,
  plotWidth: number,
  labelChars: number,
): number[] {
  if (count <= 0) return [];
  const band = plotWidth / count;
  const every = Math.max(
    1,
    Math.ceil((labelChars * CHAR_PX + LABEL_GAP) / band),
  );
  const indices: number[] = [];
  for (let index = 0; index < count; index += every) indices.push(index);
  return indices;
}

/** A statistics read this server doesn't serve (an older platform): pages say so quietly, never as an error. */
export function statisticsMissing(error: {
  status?: number;
  code: string;
}): boolean {
  return error.status === 501 || error.status === 404;
}
