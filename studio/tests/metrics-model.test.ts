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
import type { StatisticsSeries } from "../src/app/operate/metrics/chart-model";
import {
  chartRequests,
  freshness,
  kpis,
  rangeWindow,
  sparklineRequest,
  sparklines,
  stepCsv,
  stepRows,
  workflowCsv,
  workflowRows,
  type SeriesAnswer,
  type StepAnswer,
  type WorkflowAnswer,
} from "../src/app/operate/metrics/metrics-model";

const NOW = Date.parse("2026-10-26T14:32:10.500Z");
const window24 = rangeWindow("24h", NOW);

function series(
  signal: string,
  values: Array<number | null>,
  labels: StatisticsSeries["labels"] = {},
  unit: StatisticsSeries["unit"] = "count",
): StatisticsSeries {
  return {
    signal,
    labels,
    unit,
    points: values.map((value, index) => [
      new Date(Date.parse("2026-10-26T12:00:00Z") + index * 3_600_000)
        .toISOString()
        .replace(".000", ""),
      value,
    ]),
  };
}
function answer(items: StatisticsSeries[]): SeriesAnswer {
  return {
    from: window24.from,
    to: window24.to,
    granularity: "1h",
    complete_until: "2026-10-26T14:30:00Z",
    series: items,
  };
}
const workflows: WorkflowAnswer = {
  from: window24.from,
  to: window24.to,
  granularity: "1h",
  complete_until: "2026-10-26T14:30:00Z",
  workflows: [
    {
      workflow: "orders",
      versions: ["1.2.0", "1.1.0"],
      runs: 20,
      succeeded: 15,
      failed: 4,
      cancelled: 1,
      failure_rate: 0.2,
      duration_p50_ms: 450,
      duration_p95_ms: 12_000,
      last_run_at: "2026-10-26T14:31:00Z",
    },
    {
      workflow: "=cmd",
      versions: ["1.0.0"],
      runs: 0,
      succeeded: 0,
      failed: 0,
      cancelled: 0,
      failure_rate: null,
      duration_p50_ms: null,
      duration_p95_ms: null,
      last_run_at: null,
    },
  ],
  totals: {
    runs: 25,
    succeeded: 20,
    failed: 5,
    cancelled: 0,
    failure_rate: 0.2,
    duration_p50_ms: 800,
    duration_p95_ms: 30_000,
  },
};

describe("Metrics model", () => {
  it("reads the range back from now, by the server's clock, to the second", () => {
    expect(rangeWindow("1h", NOW)).toEqual({
      from: "2026-10-26T13:32:10Z",
      to: "2026-10-26T14:32:10Z",
    });
    expect(rangeWindow("30d", NOW, 60_000)).toEqual({
      from: "2026-09-26T14:33:10Z",
      to: "2026-10-26T14:33:10Z",
    });
  });

  it("asks for six charts within the series and point limits, run charts filtered by workflow", () => {
    for (const range of ["1h", "6h", "24h", "7d", "30d"] as const) {
      const requests = chartRequests(rangeWindow(range, NOW), "");
      expect(requests.map((request) => request.chart)).toEqual([
        "throughput",
        "rate",
        "durations",
        "queue",
        "outbox",
        "workers",
      ]);
      for (const { query } of requests) {
        const params = new URLSearchParams(query);
        const signals = params.getAll("signals");
        const grouped = ["workflow", "task_type"].includes(
          params.get("group_by") ?? "",
        );
        const top = Number(params.get("top") ?? 4);
        expect(signals.length * (grouped ? top + 1 : 1)).toBeLessThanOrEqual(
          21,
        );
        // Granularity is the server's automatic one, so no range exceeds 500 points.
        expect(params.has("granularity")).toBe(false);
      }
    }
    const filtered = chartRequests(window24, "orders");
    const queries = Object.fromEntries(
      filtered.map(({ chart, query }) => [chart, new URLSearchParams(query)]),
    );
    expect(queries["throughput"].getAll("signals")).toEqual([
      "runs_succeeded",
      "runs_failed",
      "runs_cancelled",
      "runs_started",
    ]);
    expect(queries["throughput"].get("workflow")).toBe("orders");
    expect(queries["durations"].get("workflow")).toBe("orders");
    // Shared capacity has no workflow; those charts stay environment-wide.
    expect(queries["queue"].get("workflow")).toBeNull();
    expect(queries["queue"].get("group_by")).toBe("task_type");
    expect(queries["queue"].get("top")).toBe("4");
    expect(queries["workers"].getAll("signals")).toEqual([
      "worker_utilization",
    ]);
  });

  it("asks for the workflow sparklines in one grouped hourly request", () => {
    const params = new URLSearchParams(sparklineRequest(NOW));
    expect(params.getAll("signals")).toEqual(["runs_started"]);
    expect(params.get("group_by")).toBe("workflow");
    expect(params.get("top")).toBe("10");
    expect(params.get("granularity")).toBe("1h");
    const lines = sparklines(
      answer([
        series("runs_started", [1, 2, null], { workflow: "orders" }),
        series("runs_started", [0, 1, 0], { workflow: "(other)" }),
      ]),
    );
    expect(lines.get("orders")).toEqual([1, 2, null]);
    expect(lines.has("(other)")).toBe(false);
  });

  it("sums the figures over the range and takes percentiles from the merged totals", () => {
    const figures = kpis({
      throughput: answer([
        series("runs_succeeded", [10, 10]),
        series("runs_failed", [3, 2]),
        series("runs_cancelled", [0, 0]),
        series("runs_started", [12, null]),
      ]),
      queue: answer([
        series(
          "oldest_ready_seconds",
          [30, 95, null],
          { task_type: "email" },
          "seconds",
        ),
        series(
          "oldest_ready_seconds",
          [5, 40, null],
          { task_type: "(other)" },
          "seconds",
        ),
        series("tasks_ready", [1, 2, 0], { task_type: "email" }),
      ]),
      workflows,
      workflow: "",
    });
    expect(figures.map((figure) => [figure.label, figure.value])).toEqual([
      ["Runs started", "12"],
      ["Success rate", "80%"],
      ["Failure rate", "20%"],
      ["p50 run duration", "800 ms"],
      ["p95 run duration", "30 s"],
      ["Oldest ready task", "1 min 35 s"],
    ]);
  });

  it("takes a chosen workflow's percentiles from its row, and shows dashes when nothing ended", () => {
    const chosen = kpis({
      throughput: answer([series("runs_started", [3])]),
      workflows,
      workflow: "orders",
    });
    expect(chosen.find((figure) => figure.id === "p95")?.value).toBe("12 s");
    const empty = kpis({
      throughput: answer([
        series("runs_succeeded", [0]),
        series("runs_failed", [0]),
        series("runs_cancelled", [null]),
        series("runs_started", [0]),
      ]),
      workflow: "",
    });
    expect(empty.map((figure) => figure.value)).toEqual([
      "0",
      "—",
      "—",
      "—",
      "—",
      "—",
    ]);
  });

  it("says until when the data is complete, in local time", () => {
    expect(freshness("2026-10-26T14:30:00Z")).toMatch(
      /^Data complete until \d{2}:\d{2}$/,
    );
    expect(freshness(null)).toBe("No figures rolled up yet");
  });

  it("lists workflows and steps for the tables and their CSV, formulas kept as text", () => {
    const rows = workflowRows(workflows);
    expect(rows[0]).toMatchObject({
      workflow: "orders",
      versions: "1.2.0, 1.1.0",
      runs: "20",
      failureRate: "20%",
      p50: "450 ms",
      p95: "12 s",
      lastRunAt: "2026-10-26T14:31:00Z",
    });
    expect(rows[1].failureRate).toBe("—");
    const text = workflowCsv(workflows);
    expect(text.split("\r\n")[0]).toBe(
      "Workflow,Versions,Runs,Succeeded,Failed,Canceled,Failure rate,p50 duration (ms),p95 duration (ms),Last run",
    );
    expect(text).toContain('orders,"1.2.0, 1.1.0",20,15,4,1,0.2,450,12000,');
    expect(text).toContain("'=cmd,");
    const steps: StepAnswer = {
      from: window24.from,
      to: window24.to,
      granularity: "1h",
      complete_until: null,
      workflow: "orders",
      steps: [
        {
          node_id: "charge",
          kind: "action",
          executions: 40,
          failures: 2,
          duration_p50_ms: 120,
          duration_p95_ms: 2_500,
          queue_p50_ms: 15,
          queue_p95_ms: 600,
        },
      ],
    };
    expect(stepRows(steps)[0]).toMatchObject({
      step: "charge",
      kind: "Action",
      executions: "40",
      failures: "2",
      p50: "120 ms",
      p95: "3 s",
      queueP95: "600 ms",
    });
    expect(stepCsv(steps).split("\r\n")[1]).toBe(
      "charge,action,40,2,120,2500,15,600",
    );
  });
});
