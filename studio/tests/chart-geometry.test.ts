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
import { describe, expect, it } from "vitest";
import {
  CHAR_PX,
  axisLabels,
  axisTicks,
  chartAxis,
  chartFrame,
  columnLayout,
  metricText,
  tickCount,
  ticks,
  timeLabel,
  xLabelIndices,
  yAxisWidth,
} from "../src/app/operate/metrics/chart-model";

const COUNTS = [12, 24, 72, 31];
const WIDTHS = [320, 600, 1100];

describe("Metrics chart geometry", () => {
  for (const width of WIDTHS)
    for (const count of COUNTS)
      it(`fits ${count} columns in ${width} px with gaps of at least 1 px`, () => {
        const frame = chartFrame(width, ["0", "250", "500", "750", "1,000"]);
        const layout = columnLayout(count, frame.plotWidth);
        expect(layout.gap).toBeGreaterThanOrEqual(1);
        expect(layout.column).toBeGreaterThan(0);
        // The last column ends inside the plot; nothing spills into the margin.
        const last = layout.x(count - 1) + layout.column;
        expect(last).toBeLessThanOrEqual(frame.plotWidth + 1e-9);
        expect(layout.x(0)).toBeGreaterThanOrEqual(0);
        for (let index = 1; index < count; index++)
          expect(
            layout.x(index) - (layout.x(index - 1) + layout.column),
          ).toBeGreaterThanOrEqual(1 - 1e-9);
        // Lines sit on column centers, so a line over stacked columns meets each one in its middle.
        expect(layout.center(3)).toBeCloseTo(layout.x(3) + layout.column / 2);
        expect(frame.left + frame.plotWidth + frame.right).toBe(width);
      });

  for (const width of WIDTHS)
    for (const count of COUNTS)
      it(`never overlaps time labels for ${count} buckets at ${width} px`, () => {
        const frame = chartFrame(width, ["0", "1", "2"]);
        const layout = columnLayout(count, frame.plotWidth);
        // The longest time label: "Oct 26" for daily buckets ("14:30" otherwise).
        const label = "Oct 26";
        const indices = xLabelIndices(count, frame.plotWidth, label.length);
        expect(indices[0]).toBe(0);
        expect(indices.length).toBeGreaterThanOrEqual(2);
        const half = (label.length * CHAR_PX) / 2;
        for (let at = 1; at < indices.length; at++)
          expect(
            layout.center(indices[at]) - layout.center(indices[at - 1]),
          ).toBeGreaterThanOrEqual(label.length * CHAR_PX + 8);
        // Every label, centered on its column, stays inside the chart.
        for (const index of indices) {
          expect(index).toBeLessThan(count);
          const center = frame.left + layout.center(index);
          expect(center - half).toBeGreaterThanOrEqual(0);
          expect(center + half).toBeLessThanOrEqual(width);
        }
      });

  it("leaves room on the left for the widest tick label", () => {
    expect(yAxisWidth(["0", "5"])).toBe(1 * CHAR_PX + 8);
    expect(yAxisWidth(["0", "2.5 min", "5 min"])).toBe(7 * CHAR_PX + 8);
    const frame = chartFrame(320, ["0", "100%"]);
    expect(frame.left).toBe(yAxisWidth(["0", "100%"]));
    expect(frame.plotWidth).toBe(320 - frame.left - frame.right);
  });

  it("divides every readable axis maximum into readable steps", () => {
    for (const max of [
      1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 0.25, 15_000, 120,
    ])
      for (const value of ticks(max, tickCount(max)).slice(1)) {
        const step = value / (ticks(max, tickCount(max))[1] ?? 1);
        expect(Number.isInteger(Math.round(step * 1e9) / 1e9)).toBe(true);
      }
    expect(tickCount(0.25)).toBe(5);
    expect(ticks(0.25, tickCount(0.25))).toEqual([
      0, 0.05, 0.1, 0.15, 0.2, 0.25,
    ]);
    expect(ticks(6, tickCount(6))).toEqual([0, 2, 4, 6]);
    expect(ticks(15_000, tickCount(15_000))).toEqual([0, 5000, 10000, 15000]);
    expect(ticks(1, tickCount(1))).toEqual([0, 0.25, 0.5, 0.75, 1]);
  });

  it("steps small whole counts by one and fractional averages by readable steps", () => {
    expect(axisTicks(1, "count")).toEqual([0, 1]);
    expect(axisTicks(5, "count")).toEqual([0, 1, 2, 3, 4, 5]);
    // Sampled counts are averages: a fractional maximum still gets a scale.
    expect(axisTicks(0.4, "count")).toEqual([0, 0.1, 0.2, 0.3, 0.4]);
    expect(axisTicks(2.5, "count")).toEqual([0, 0.5, 1, 1.5, 2, 2.5]);
    expect(axisTicks(1, "ratio")).toEqual([0, 0.25, 0.5, 0.75, 1]);
  });

  it("reads durations under a second in milliseconds", () => {
    expect(metricText("ms", 0.4)).toBe("0.4 ms");
    expect(metricText("ms", 12.34)).toBe("12 ms");
    expect(metricText("ms", 999)).toBe("999 ms");
    expect(metricText("ms", 1_500)).toBe("2 s");
    expect(metricText("seconds", 90)).toBe("1 min 30 s");
    expect(metricText("ratio", 0.2)).toBe("20%");
    expect(metricText("count", 1_200)).toBe("1,200");
    expect(metricText("count", null)).toBe("—");
  });

  it("labels buckets by time of day, or by day for daily buckets", () => {
    const at = "2026-10-26T14:30:00Z";
    expect(timeLabel(at, "5m")).toMatch(/^\d{2}:\d{2}$/);
    expect(timeLabel(at, "1h")).toMatch(/^\d{2}:\d{2}$/);
    expect(timeLabel(at, "1d")).toMatch(/^[A-Z][a-z]{2} \d{1,2}$/);
  });
});

describe("Metrics chart axes", () => {
  it("steps whole counts by whole numbers, also past six", () => {
    expect(axisTicks(10, "count")).toEqual([0, 2, 4, 6, 8, 10]);
    expect(axisTicks(8, "count")).toEqual([0, 2, 4, 6, 8]);
    expect(axisTicks(100, "count")).toEqual([0, 25, 50, 75, 100]);
  });

  it("writes zero in the unit of the rest of the axis", () => {
    expect(axisLabels([0, 5_000, 10_000, 15_000], "ms")).toEqual([
      "0 s",
      "5 s",
      "10 s",
      "15 s",
    ]);
    expect(axisLabels([0, 250, 500], "ms")).toEqual([
      "0 ms",
      "250 ms",
      "500 ms",
    ]);
  });

  it("gives every chart of a page one gutter, wide enough for the widest axis", () => {
    const series = (signal: string, values: number[]) => ({
      signal,
      labels: {},
      unit: "count" as const,
      points: values.map(
        (value, index) =>
          [`2026-10-26T1${index}:00:00Z`, value] as [string, number | null],
      ),
    });
    const small = chartAxis(
      [series("outbox_pending", [1, 2])],
      [],
      [],
      "count",
    );
    const large = chartAxis(
      [series("runs_started", [900, 1_200])],
      [],
      [],
      "count",
    );
    expect(small.labels.at(-1)).toBe("2");
    expect(large.labels.at(-1)).toBe("1,200");
    const gutter = yAxisWidth([...small.labels, ...large.labels]);
    expect(chartFrame(600, small.labels, gutter).left).toBe(
      chartFrame(600, large.labels, gutter).left,
    );
  });
});
