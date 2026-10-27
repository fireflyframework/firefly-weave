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
  automaticGranularity,
  chartRows,
  csv,
  linePath,
  niceMax,
  percentText,
  seriesStyle,
  stackColumns,
  ticks,
} from "../src/app/operate/metrics/chart-model";

const hour = 3_600_000;

describe("Metrics chart model", () => {
  it("picks the granularity the server picks for a range", () => {
    expect(automaticGranularity(hour)).toBe("5m");
    expect(automaticGranularity(6 * hour)).toBe("5m");
    expect(automaticGranularity(6 * hour + 1)).toBe("1h");
    expect(automaticGranularity(7 * 24 * hour)).toBe("1h");
    expect(automaticGranularity(30 * 24 * hour)).toBe("1d");
  });

  it("rounds the axis up to a readable maximum with even ticks", () => {
    expect(niceMax(0)).toBe(1);
    expect(niceMax(7)).toBe(8);
    expect(niceMax(231)).toBe(250);
    expect(niceMax(0.026)).toBe(0.03);
    expect(ticks(250, 5)).toEqual([0, 50, 100, 150, 200, 250]);
    expect(ticks(1, 4)).toEqual([0, 0.25, 0.5, 0.75, 1]);
  });

  it("draws lines that break where a point is unknown", () => {
    const path = linePath(
      [
        [0, 1],
        [1, null],
        [2, 3],
        [3, 4],
      ],
      { width: 300, height: 100, max: 4, count: 4 },
    );
    expect(path).toBe("M0 75 M200 25 L300 0");
    expect(linePath([], { width: 300, height: 100, max: 1, count: 0 })).toBe(
      "",
    );
  });

  it("stacks outcomes per bucket from the bottom, skipping unknown points", () => {
    const columns = stackColumns(
      [
        { key: "runs_succeeded", values: [3, null, 1] },
        { key: "runs_failed", values: [1, 2, 0] },
      ],
      { height: 100, max: 4 },
    );
    expect(columns[0]).toEqual([
      { key: "runs_succeeded", y: 25, height: 75 },
      { key: "runs_failed", y: 0, height: 25 },
    ]);
    expect(columns[1]).toEqual([{ key: "runs_failed", y: 50, height: 50 }]);
    expect(columns[2]).toEqual([{ key: "runs_succeeded", y: 75, height: 25 }]);
  });

  it("colors outcomes by status and other series by category, p95 dashed in the p50 hue", () => {
    expect(seriesStyle("runs_succeeded", {}, 0).color).toBe("var(--success)");
    expect(seriesStyle("runs_failed", {}, 1).color).toBe("var(--danger)");
    expect(seriesStyle("runs_cancelled", {}, 2).color).toBe(
      "var(--neutral-ink)",
    );
    expect(seriesStyle("tasks_ready", { task_type: "a" }, 0).color).toBe(
      "var(--series-1)",
    );
    expect(seriesStyle("tasks_ready", { task_type: "d" }, 3).color).toBe(
      "var(--series-4)",
    );
    expect(seriesStyle("tasks_ready", { task_type: "(other)" }, 4).color).toBe(
      "var(--series-other)",
    );
    const p50 = seriesStyle("duration_p50", {}, 0);
    const p95 = seriesStyle("duration_p95", {}, 1);
    expect(p95.color).toBe(p50.color);
    expect([p50.dashed, p95.dashed]).toEqual([false, true]);
  });

  it("shows rates as percents and nothing as a dash", () => {
    expect(percentText(0.2)).toBe("20%");
    expect(percentText(0.025)).toBe("2.5%");
    expect(percentText(1)).toBe("100%");
    expect(percentText(null)).toBe("—");
  });

  it("turns series into table rows and exports them as CSV", () => {
    const rows = chartRows([
      {
        signal: "runs_started",
        labels: {},
        unit: "count",
        points: [
          ["2026-10-26T14:00:00Z", 4],
          ["2026-10-26T15:00:00Z", null],
        ],
      },
      {
        signal: "failure_rate",
        labels: {},
        unit: "ratio",
        points: [
          ["2026-10-26T14:00:00Z", 0.25],
          ["2026-10-26T15:00:00Z", null],
        ],
      },
    ]);
    expect(rows).toEqual([
      ["Time", "Runs started", "Failure rate"],
      ["2026-10-26T14:00:00Z", "4", "25%"],
      ["2026-10-26T15:00:00Z", "—", "—"],
    ]);
    expect(
      csv([
        ["Workflow", "Runs"],
        ['say "hi", now', "3"],
      ]),
    ).toBe('Workflow,Runs\r\n"say ""hi"", now",3\r\n');
    // A cell that a spreadsheet would run as a formula is quoted as text.
    expect(csv([["=SUM(A1)", "+1", "-2", "@x"]])).toBe(
      "'=SUM(A1),'+1,'-2,'@x\r\n",
    );
  });
});
