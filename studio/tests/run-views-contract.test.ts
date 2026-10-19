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
  childRunsQuery,
  isUnavailableRun,
  logLevels,
  logSources,
  runOrigins,
  runStatuses,
  runSummaryOrders,
  runViewQuery,
  stepKinds,
  stepOutput,
  stepStatuses,
  terminalRunStatuses,
  type RunSummaryItem,
  type StepFact,
} from "../src/app/operate/run-contracts";
import {
  fixtureIds,
  runViewsFixture,
} from "../src/app/operate/run-views.fixture";
import {
  pythonAvailable,
  roundTripWithPython,
  validateWithPython,
} from "./run-views-oracle";

const summaries = runViewsFixture.runs.map((run) => run.summary);
const placeholder = (id: string): RunSummaryItem => ({
  id,
  unavailable: true,
  omissions: [{ path: "", reason: "classification_unavailable" }],
});
const fact = (runId: string, key: string) =>
  runViewsFixture.steps[runId].items.find(
    (item) => (item.instance_key || item.node_id) === key,
  ) as StepFact;

describe.skipIf(!pythonAvailable)(
  "run views against the Python contract",
  () => {
    it("accepts every fixture record and shares every vocabulary", () => {
      const vocabularies = validateWithPython({
        summaries: [
          { items: summaries, next_cursor: null },
          { items: [placeholder(fixtureIds.unavailable)], next_cursor: null },
        ],
        steps: Object.values(runViewsFixture.steps).map((record) => ({
          items: record.items,
          next_cursor: null,
          complete: record.complete,
        })),
        logs: Object.values(runViewsFixture.logs).map((items) => ({
          items,
          next_cursor: null,
        })),
      });
      expect(vocabularies).toEqual({
        statuses: [...runStatuses],
        terminal: [...terminalRunStatuses].sort(),
        origins: [...runOrigins],
        orders: [...runSummaryOrders],
        kinds: [...stepKinds],
        stepStatuses: [...stepStatuses],
        sources: [...logSources],
        levels: [...logLevels],
      });
    });
  },
);

describe("run views fixture", () => {
  it("covers every origin a Runs tab shows, test runs and their children", () => {
    expect(new Set(summaries.map((run) => run.origin))).toEqual(
      new Set(["webhook", "schedule", "call", "retry", "test", "manual"]),
    );
    expect(summaries.filter((run) => run.test).map((run) => run.id)).toEqual([
      fixtureIds.test,
      fixtureIds.testChild,
    ]);
  });
  it("counts each step instance's log entries", () => {
    for (const [runId, record] of Object.entries(runViewsFixture.steps)) {
      const entries = runViewsFixture.logs[runId] ?? [];
      for (const item of record.items)
        expect(
          entries.filter(
            (entry) =>
              entry.node_id === item.node_id &&
              entry.instance_key === item.instance_key,
          ).length,
          `${runId} ${item.instance_key || item.node_id}`,
        ).toBe(item.log_entries);
    }
  });
});

describe("run view helpers", () => {
  it("encodes queries: repeated status, booleans, and offsets that keep their plus sign", () => {
    expect(
      runViewQuery({
        status: ["failed", "running"],
        include_test: true,
        started_after: "2026-10-07T12:00:00+02:00",
        workflow: undefined,
        business_key: "",
      }),
    ).toBe(
      "?status=failed&status=running&include_test=true&started_after=2026-10-07T12%3A00%3A00%2B02%3A00",
    );
    expect(runViewQuery({})).toBe("");
    expect(runViewQuery({ include: "output", step: "send" })).toBe(
      "?include=output&step=send",
    );
  });
  it("tells a null output from a withheld or missing one", () => {
    expect(stepOutput(fact(fixtureIds.failed, "send[0]"))).toEqual({
      kind: "value",
      value: null,
    });
    expect(stepOutput(fact(fixtureIds.failed, "send[2]"))).toEqual({
      kind: "omitted",
      reason: "classified_secret",
    });
    expect(stepOutput(fact(fixtureIds.failed, "send[3]"))).toEqual({
      kind: "none",
    });
  });
  it("asks for a test run's children as test runs", () => {
    const byId = Object.fromEntries(summaries.map((run) => [run.id, run]));
    expect(childRunsQuery(byId[fixtureIds.failed])).toEqual({
      caller_run_id: fixtureIds.failed,
      include_test: false,
    });
    expect(childRunsQuery(byId[fixtureIds.test])).toEqual({
      caller_run_id: fixtureIds.test,
      include_test: true,
    });
  });
  it("narrows unavailable placeholders before run fields are read", () => {
    const items = [summaries[0], placeholder(fixtureIds.unavailable)];
    expect(items.map(isUnavailableRun)).toEqual([false, true]);
    expect(
      items.flatMap((item) =>
        isUnavailableRun(item) ? [] : [item.workflow.name],
      ),
    ).toEqual(["invoice-approval"]);
  });
});

describe.skipIf(!pythonAvailable)("handled metadata compatibility", () => {
  it("retains nonzero counts and handled failures through Python JSON serialization", () => {
    const summary = summaries.find((item) => item.id === fixtureIds.retry)!;
    const step = {
      ...fact(fixtureIds.failed, "send[3]"),
      handled: "errorOutput" as const,
    };
    const result = roundTripWithPython({
      summaries: [{ items: [summary], next_cursor: null }],
      steps: [{ items: [step], next_cursor: null, complete: true }],
      logs: [],
    });
    const item = result.summaries[0].items[0];
    if (isUnavailableRun(item)) throw new Error("Expected a run summary");
    expect(item.handled_errors).toBe(1);
    expect(result.steps[0].items[0].handled).toBe("errorOutput");
    expect(item.status).toBe("succeeded");
  });
  it("keeps fields absent on older server records", () => {
    const result = roundTripWithPython({
      summaries: [{ items: [summaries[0]], next_cursor: null }],
      steps: [
        {
          items: [fact(fixtureIds.failed, "send[3]")],
          next_cursor: null,
          complete: true,
        },
      ],
      logs: [],
    });
    expect(result.summaries[0].items[0]).not.toHaveProperty("handled_errors");
    expect(result.steps[0].items[0]).not.toHaveProperty("handled");
    expect(runViewQuery({ has_handled_errors: false })).toBe(
      "?has_handled_errors=false",
    );
  });
});
