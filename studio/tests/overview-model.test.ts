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
import { execFileSync } from "node:child_process";
import { delimiter, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  attentionKinds,
  attentionLink,
  clockOffset,
  countText,
  dayHeadline,
  dayQuery,
  healthComponents,
  healthStates,
  healthTiles,
  isQuiet,
  keepsStatistics,
  loadFigures,
  loadSeriesQuery,
  loadSparklines,
  readSummary,
  sectionOf,
  slotMeter,
  statisticsMissing,
  uptimeSeconds,
  type OperationsSummary,
} from "../src/app/operate/overview/overview-model";
import { statusLabel, statusTone } from "../src/app/status-labels";
import { pythonAvailable } from "./run-views-oracle";

const observed = "2026-10-12T12:00:00Z";
const at = (seconds: number) =>
  new Date(Date.parse(observed) + seconds * 1000).toISOString();
const id = (n: number) =>
  `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;

/** An answer with every section readable, as the platform sends it. */
const full: OperationsSummary = {
  observed_at: observed,
  api: {
    version: "0.1.0a15",
    build: null,
    instance: id(1),
    uptime_seconds: 7_200,
    compatibility: { mode: "ready", findings: 0 },
  },
  health: [
    { component: "database", state: "ok", latency_ms: 4.25 },
    {
      component: "recovery_loop",
      state: "ok",
      last_started_at: at(-3),
      last_ok_at: at(-3),
      consecutive_failures: 0,
      recent_failures: 0,
    },
    {
      component: "schedules",
      state: "ok",
      last_ok_at: at(-3),
      consecutive_failures: 0,
      recent_failures: 0,
    },
    {
      component: "outbox_loop",
      state: "degraded",
      last_started_at: at(-1),
      last_ok_at: at(-90),
      consecutive_failures: 4,
      recent_failures: 4,
    },
    {
      component: "provider_loop",
      state: "disabled",
      consecutive_failures: 0,
      recent_failures: 0,
    },
    {
      component: "kafka_loop",
      state: "disabled",
      consecutive_failures: 0,
      recent_failures: 0,
    },
    {
      component: "operations_loop",
      state: "ok",
      last_ok_at: at(-20),
      consecutive_failures: 0,
      recent_failures: 0,
    },
    {
      component: "alert_evaluator",
      state: "disabled",
      consecutive_failures: 0,
      recent_failures: 0,
    },
    { component: "native_executors", state: "ok" },
    { component: "telemetry", state: "disabled" },
  ],
  load: {
    runs_active: 1204,
    tasks_ready: 3,
    tasks_leased: 5,
    oldest_ready_seconds: 41,
    waits_active: 7,
    waits_overdue: 1,
    oldest_overdue_seconds: 95,
    outbox_pending: 2,
    outbox_oldest_seconds: 3,
    provider_pending: 0,
    sources_enabled: 4,
  },
  workers: {
    online: 3,
    offline: 1,
    draining: 0,
    capacity: 12,
    active_leases: 5,
  },
  targets: {
    total: 2,
    runner_online: 1,
    busy: 0,
    reconciliation_required: 1,
    awaiting_approval: 1,
  },
  incidents: { active: 1, opened_24h: 3 },
  attention: [
    {
      kind: "job",
      id: id(2),
      title: "Deployment job on alpha needs reconciliation",
    },
    {
      kind: "approval",
      id: id(3),
      title: "Approve the plan to update on alpha",
      expires_at: at(240),
    },
    {
      kind: "worker",
      id: id(4),
      title: "Offline worker still holds 2 active leases",
    },
    {
      kind: "incident",
      id: id(5),
      title: "Active incident in invoice-approval",
    },
    { kind: "runner", id: id(6), title: "No runner is online for bravo" },
    {
      kind: "compatibility",
      id: "compatibility",
      title: "Execution compatibility is restricted",
    },
    { kind: "run", id: id(7), title: "A run of invoice-approval failed" },
  ],
};
/** The same environment read by someone without incident.read or deployment.read, while two sections timed out. */
const partial: OperationsSummary = {
  ...full,
  load: { state: "unavailable" },
  targets: { state: "forbidden" },
  incidents: { state: "forbidden" },
  attention: { state: "unavailable" },
};

const root = resolve(import.meta.dirname, "../..");
const python = resolve(
  root,
  process.platform === "win32"
    ? ".venv/Scripts/python.exe"
    : ".venv/bin/python",
);
/** Validates answers with the platform's own model and returns its vocabularies. */
function validateWithPython(answers: OperationsSummary[]) {
  const script = `
import json, sys
from typing import get_args
from firefly_weave.contracts import operations_summary as s
for answer in json.load(sys.stdin):
    parsed = s.OperationsSummary.model_validate_json(json.dumps(answer))
    assert parsed.model_dump(mode="json") == s.OperationsSummary.model_validate_json(json.dumps(answer)).model_dump(mode="json")
print(json.dumps({
    "states": get_args(s.HealthState.__value__),
    "components": get_args(s.HealthComponentName.__value__),
    "kinds": get_args(s.AttentionKind.__value__),
}))
`;
  return JSON.parse(
    execFileSync(python, ["-c", script], {
      cwd: root,
      encoding: "utf8",
      timeout: 180_000,
      input: JSON.stringify(answers),
      env: {
        ...process.env,
        PYTHONPATH: [resolve(root, "src"), process.env.PYTHONPATH]
          .filter(Boolean)
          .join(delimiter),
      },
    }),
  ) as Record<string, string[]>;
}

describe("Overview model", () => {
  it("reads the summary of the environment Studio is in", async () => {
    const paths: string[] = [];
    const api = {
      environment: "/studio/api/api/v1/tenants/t/projects/p/environments/e",
      request<T>(path: string) {
        paths.push(path);
        return Promise.resolve(full as T);
      },
    };
    expect(await readSummary(api)).toBe(full);
    expect(paths).toEqual([
      "/studio/api/api/v1/tenants/t/projects/p/environments/e/operations/summary",
    ]);
  });

  it("tells a forbidden section from an unavailable one, and neither from a zero", () => {
    expect(sectionOf(full.incidents)).toEqual({
      kind: "ready",
      value: { active: 1, opened_24h: 3 },
    });
    expect(sectionOf(partial.incidents)).toEqual({ kind: "forbidden" });
    expect(sectionOf(partial.load)).toEqual({ kind: "unavailable" });
    expect(sectionOf(partial.attention)).toEqual({ kind: "unavailable" });
    expect(sectionOf({ active: 0, opened_24h: 0 })).toEqual({
      kind: "ready",
      value: { active: 0, opened_24h: 0 },
    });
  });

  it("names health states as Operate does: ok, degraded, down and off", () => {
    expect(healthStates.map((state) => statusLabel("health", state))).toEqual([
      "OK",
      "Degraded",
      "Down",
      "Off",
    ]);
    expect(healthStates.map((state) => statusTone("health", state))).toEqual([
      "success",
      "warning",
      "danger",
      "neutral",
    ]);
  });

  it("builds six tiles, each with a state, one figure and its details", () => {
    const now = new Date(at(0));
    const tiles = healthTiles(full, now);
    expect(tiles.map((tile) => [tile.label, tile.state, tile.figure])).toEqual([
      ["API", "ok", "Up 2 h"],
      ["Database", "ok", "Latency 4 ms"],
      // The worst enabled loop decides; loops switched off do not count.
      ["Scheduler", "ok", "3 of 3 loops running"],
      ["Outbox", "degraded", "Last success 2 min ago"],
      ["Compatibility", "ok", "No findings"],
      ["Telemetry export", "disabled", "Export is off"],
    ]);
    // Details list each loop's last success and failures in a row.
    expect(
      tiles[2].rows.map((row) => [
        row.label,
        row.state,
        row.lastSuccess,
        row.failures,
      ]),
    ).toEqual([
      ["Recovery", "ok", "just now", 0],
      ["Schedules", "ok", "just now", 0],
      ["Providers", "disabled", "", 0],
      ["Kafka", "disabled", "", 0],
      ["Operations", "ok", "just now", 0],
      ["Alerts", "disabled", "", 0],
    ]);
    expect(tiles[3].rows).toEqual([
      {
        label: "Outbox",
        state: "degraded",
        lastSuccess: "2 min ago",
        lastSuccessAt: at(-90),
        failures: 4,
      },
    ]);
    expect(tiles[0].facts).toEqual([
      ["Version", "0.1.0a15"],
      ["Build", "Not reported"],
      ["Instance", "00000000"],
      ["Native executors", "OK"],
    ]);
    expect(tiles[1].facts).toEqual([["Latency", "4.25 ms"]]);
    expect(tiles[4].facts).toEqual([
      ["Mode", "Ready"],
      ["Findings in this project", "0"],
    ]);
  });

  it("degrades a tile for a down loop, restricted compatibility or dropped telemetry", () => {
    const now = new Date(at(0));
    const broken: OperationsSummary = {
      ...full,
      api: { ...full.api, compatibility: { mode: "restricted", findings: 3 } },
      health: full.health.map((component) =>
        component.component === "schedules"
          ? { ...component, state: "down" }
          : component.component === "telemetry"
            ? { component: "telemetry", state: "degraded" }
            : component.component === "database"
              ? { component: "database", state: "degraded" }
              : component.component === "native_executors"
                ? { component: "native_executors", state: "down" }
                : component,
      ),
    };
    const tiles = Object.fromEntries(
      healthTiles(broken, now).map((tile) => [tile.id, tile]),
    );
    expect([tiles.api.state, tiles.api.figure]).toEqual(["degraded", "Up 2 h"]);
    expect([tiles.scheduler.state, tiles.scheduler.figure]).toEqual([
      "down",
      "1 of 3 loops down",
    ]);
    expect([tiles.database.state, tiles.database.figure]).toEqual([
      "degraded",
      "Probe timed out",
    ]);
    expect([tiles.compatibility.state, tiles.compatibility.figure]).toEqual([
      "degraded",
      "3 findings",
    ]);
    expect([tiles.telemetry.state, tiles.telemetry.figure]).toEqual([
      "degraded",
      "Dropping telemetry",
    ]);
    // An API-only server: every scheduler loop is off.
    const apiOnly: OperationsSummary = {
      ...full,
      health: full.health.map((component) =>
        component.component.endsWith("_loop") ||
        component.component === "schedules" ||
        component.component === "alert_evaluator"
          ? { ...component, state: "disabled" }
          : component,
      ),
    };
    const quiet = healthTiles(apiOnly, now);
    expect([quiet[2].state, quiet[2].figure]).toEqual([
      "disabled",
      "Off on this server",
    ]);
    expect([quiet[3].state, quiet[3].figure]).toEqual([
      "disabled",
      "Off on this server",
    ]);
  });

  it("ages times with the server clock, not this computer's", () => {
    // This computer is five minutes ahead of the server.
    const received = Date.parse(observed) + 300_000;
    const offset = clockOffset(observed, received);
    expect(offset).toBe(-300_000);
    const now = new Date(received + 2_000 + offset);
    expect(healthTiles(full, now)[3].figure).toBe("Last success 2 min ago");
    // Uptime counts on from the answer, by the server clock.
    expect(uptimeSeconds(full, now)).toBe(7_202);
    expect(clockOffset("not a time", received)).toBe(0);
  });

  it("writes load figures with tabular counts and their ages", () => {
    const load = sectionOf(full.load);
    expect(load.kind).toBe("ready");
    if (load.kind !== "ready") return;
    expect(
      loadFigures(load.value).map((figure) => [
        figure.label,
        countText(figure.value),
        figure.detail,
      ]),
    ).toEqual([
      ["Active runs", "1,204", "Test runs left out"],
      ["Ready tasks", "3", "Oldest waits 41 s"],
      ["Waits", "7", "1 overdue, oldest by 1 min 35 s"],
      ["Outbox backlog", "2", "Oldest waits 3 s"],
      ["Provider backlog", "0", "4 sources enabled"],
    ]);
    const idle = loadFigures({
      ...load.value,
      tasks_ready: 0,
      oldest_ready_seconds: null,
      waits_overdue: 0,
      oldest_overdue_seconds: null,
      outbox_pending: 0,
      outbox_oldest_seconds: null,
      sources_enabled: 1,
    });
    expect(idle.map((figure) => figure.detail)).toEqual([
      "Test runs left out",
      "Nothing waiting",
      "None overdue",
      "Nothing due",
      "1 source enabled",
    ]);
  });

  it("measures free slots against ready tasks, and says when work exceeds them", () => {
    const workers = {
      online: 3,
      offline: 1,
      draining: 0,
      capacity: 12,
      active_leases: 5,
    };
    const load = full.load as Exclude<
      OperationsSummary["load"],
      { state: string }
    >;
    expect(slotMeter(workers, load)).toEqual({
      free: 7,
      ready: 3,
      fill: 3 / 7,
      over: false,
      text: "3 ready, 7 free slots",
    });
    expect(slotMeter(workers, { ...load, tasks_ready: 9 })).toMatchObject({
      fill: 1,
      over: true,
      text: "9 ready, 7 free slots",
    });
    // No free slot at all and nothing ready is not over capacity.
    expect(
      slotMeter({ ...workers, capacity: 5 }, { ...load, tasks_ready: 0 }),
    ).toMatchObject({
      free: 0,
      fill: 0,
      over: false,
      text: "0 ready, 0 free slots",
    });
    // Without the load figures there is nothing to compare.
    expect(slotMeter(workers, null)).toEqual({
      free: 7,
      ready: null,
      fill: 0,
      over: false,
      text: "7 free slots",
    });
  });

  it("links each attention item to its record", () => {
    const attention = full.attention as Exclude<
      OperationsSummary["attention"],
      { state: string }
    >;
    expect(attention.map(attentionLink)).toEqual([
      `/operate/clusters/jobs/${id(2)}`,
      `/operate/clusters/plans/${id(3)}`,
      `/operate/workers/${id(4)}`,
      `/operate/incidents?incident=${id(5)}`,
      "/operate/clusters?tab=runners",
      null,
      `/operate/runs/${id(7)}`,
    ]);
  });

  it("calls an environment quiet only when every figure it may read is zero", () => {
    expect(isQuiet(full)).toBe(false);
    const zero: OperationsSummary = {
      ...full,
      load: {
        runs_active: 0,
        tasks_ready: 0,
        tasks_leased: 0,
        oldest_ready_seconds: null,
        waits_active: 0,
        waits_overdue: 0,
        oldest_overdue_seconds: null,
        outbox_pending: 0,
        outbox_oldest_seconds: null,
        provider_pending: 0,
        sources_enabled: 0,
      },
      attention: [],
    };
    expect(isQuiet(zero)).toBe(true);
    // A section Studio could not read is unknown, never quiet.
    expect(isQuiet({ ...zero, attention: { state: "unavailable" } })).toBe(
      false,
    );
    expect(isQuiet({ ...zero, load: { state: "unavailable" } })).toBe(false);
  });

  it.skipIf(!pythonAvailable)(
    "mirrors the platform's summary answer exactly",
    () => {
      const vocabularies = validateWithPython([full, partial]);
      expect(vocabularies).toEqual({
        states: [...healthStates],
        components: [...healthComponents],
        kinds: [...attentionKinds],
      });
    },
  );
});

describe("Overview run statistics", () => {
  const now = Date.parse("2026-10-26T14:32:10.400Z");
  const day = { started: 240, succeeded: 231, failed: 6, failure_rate: 0.025 };
  const line = (signal: string, values: Array<number | null>) => ({
    signal,
    labels: {},
    unit: "count" as const,
    points: values.map(
      (value, index) =>
        [
          `2026-10-26T13:${String(35 + index * 5).padStart(2, "0")}:00Z`,
          value,
        ] as [string, number | null],
    ),
  });

  it("asks for the hour's load lines and the day's hourly outcomes", () => {
    const load = new URLSearchParams(loadSeriesQuery(now));
    expect(load.getAll("signals")).toEqual([
      "runs_active",
      "tasks_ready",
      "waits_active",
      "outbox_pending",
      "provider_pending",
    ]);
    expect([load.get("from"), load.get("to")]).toEqual([
      "2026-10-26T13:32:10Z",
      "2026-10-26T14:32:10Z",
    ]);
    // An hour is answered in five-minute buckets: the server picks them.
    expect(load.has("granularity")).toBe(false);
    const hours = new URLSearchParams(dayQuery(now, 1_000));
    expect(hours.getAll("signals")).toEqual([
      "runs_succeeded",
      "runs_failed",
      "runs_cancelled",
      "runs_started",
      "failure_rate",
    ]);
    expect(hours.get("granularity")).toBe("1h");
    expect([hours.get("from"), hours.get("to")]).toEqual([
      "2026-10-25T14:32:11Z",
      "2026-10-26T14:32:11Z",
    ]);
  });

  it("keys the hour's lines by the figure they sit under", () => {
    const lines = loadSparklines({
      series: [line("runs_active", [1, 2, 3]), line("tasks_ready", [0, null])],
    });
    expect(lines.get("runs_active")).toEqual([1, 2, 3]);
    expect(lines.get("tasks_ready")).toEqual([0, null]);
    expect(lines.has("waits_active")).toBe(false);
    expect(loadSparklines(null).size).toBe(0);
  });

  it("reads the last day only from a server that keeps statistics", () => {
    const base = { observed_at: observed } as OperationsSummary;
    expect(keepsStatistics(base)).toBe(false);
    expect(keepsStatistics({ ...base, runs_24h: day })).toBe(true);
    expect(
      keepsStatistics({ ...base, runs_24h: { state: "unavailable" } }),
    ).toBe(true);
    expect(dayHeadline(day)).toBe("240 runs · 6 failed · 2.5% failure rate");
    expect(
      dayHeadline({ started: 1, succeeded: 0, failed: 0, failure_rate: null }),
    ).toBe("1 run · 0 failed");
  });

  it("stays quiet about statistics a server doesn't serve, and only about those", () => {
    expect(statisticsMissing({ status: 501, code: "WV-UNAVAILABLE" })).toBe(
      true,
    );
    expect(statisticsMissing({ status: 404, code: "WV-NOT-FOUND" })).toBe(true);
    expect(statisticsMissing({ status: 503, code: "WV-UNAVAILABLE" })).toBe(
      false,
    );
    expect(statisticsMissing({ status: 403, code: "WV-DENIED" })).toBe(false);
  });
});
