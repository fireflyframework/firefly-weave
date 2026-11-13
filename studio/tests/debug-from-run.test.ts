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
  leftOutText,
  mergeQuestion,
  mergedTestData,
  testDataFromRun,
  type RunSource,
} from "../src/app/editor/runs-tab/debug-from-run";
import type { RunRecord } from "../src/app/editor/runs-tab/run-record";
import type { StepFingerprints } from "../src/app/editor/state/fingerprints";
import { readTestData } from "../src/app/editor/state/test-data-read";
import { emptyTestData } from "../src/app/editor/state/test-data-ops";
import type { StudioTestData } from "../src/app/editor/state/test-data";
import type { Step, Workflow } from "../src/app/model";
import type { StepFact } from "../src/app/operate/run-contracts";

const RUN = "1c0d7f8a-4d66-4e7c-8b62-5a2c1d3e8f12";
const ENV = "4a1b2c3d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
const NOW = "2026-10-07T12:00:00Z";

const step = (id: string, kind: string, extra: object = {}): Step =>
  ({ id, kind, ...extra }) as Step;
const workflow = (version: string, steps: Step[]): Workflow => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "invoice-approval", version },
  spec: { steps },
});
const draft = workflow("1.3.0", [
  step("fetch", "action", { uses: "sql.lookup@1.0.0" }),
  step("check", "transform"),
  step("approve", "humanTask"),
  step("wait-for-reply", "signal"),
  step("send", "action", { uses: "crm.lookup@2.0.0" }),
]);
const older = workflow("1.2.0", [
  step("fetch", "action", { uses: "sql.lookup@1.0.0" }),
  step("log", "transform"),
  step("approve", "humanTask"),
  step("wait-for-reply", "signal"),
  step("send", "action", { uses: "crm.lookup@2.0.0" }),
]);
const print = (n: number) => `fnv1a64:${String(n).padStart(16, "0")}` as const;
/** The run version's fingerprints: a different number per step. */
const runPrints = new Map<string, StepFingerprints>(
  ["fetch", "check", "log", "approve", "wait-for-reply", "send"].map(
    (id, i) => [
      id,
      {
        config: print(10 + i),
        contract: print(20 + i),
        upstream: print(30 + i),
        execution: print(40 + i),
      },
    ],
  ),
);

function fact(
  key: string,
  kind: StepFact["kind"],
  status: StepFact["status"],
  extra: Partial<StepFact> = {},
): StepFact {
  const loop = /\[(\d+)\]$/.exec(key);
  return {
    node_id: key.replace(/\[.*$/, ""),
    instance_key: loop ? key : "",
    iteration: loop ? [Number(loop[1])] : [],
    kind,
    status,
    scheduled_at: null,
    started_at: null,
    ended_at: null,
    duration_ms: null,
    queue_wait_ms: null,
    attempts: 1,
    worker_id: "w-7",
    error_code: null,
    child_run_id: null,
    log_entries: 0,
    ...extra,
  };
}
const record = (status: RunRecord["status"]): RunRecord => ({
  id: RUN,
  versionId: "9e8f5b0c-cfee-4af4-8dea-d2ae9fb06b9a",
  status,
  input: { total: 120.5 },
  completed: {},
  active: [],
  waiting: [],
  incidents: [],
});
function failedRun(facts?: StepFact[]): RunSource {
  return {
    runId: RUN,
    environmentId: ENV,
    status: "failed",
    record: record("failed"),
    definition: draft,
    failedStep: { key: "send", code: "SMTP_REJECTED" },
    facts: facts ?? [
      fact("fetch", "action", "succeeded", {
        output: { rows: [{ name: "Ada" }] },
      }),
      fact("check", "transform", "succeeded", { output: { ok: true } }),
      fact("approve", "humanTask", "succeeded", {
        output: {
          decision: "approve",
          data: { note: "fine" },
          actor_id: "u-1",
          task_id: "t-9",
        },
      }),
      fact("wait-for-reply", "signal", "succeeded", {
        output: { payload: { ok: true } },
      }),
      fact("send", "action", "failed", { error_code: "SMTP_REJECTED" }),
    ],
  };
}
const built = (source: RunSource, onto: Workflow = draft) => {
  const result = testDataFromRun(source, onto, runPrints, NOW);
  if ("refused" in result) throw Error(result.refused);
  return result;
};

describe("test data from a run", () => {
  it("copies only typed values and marks each as run data", () => {
    const { data, pointers } = built(failedRun());
    const json = JSON.stringify(data);
    for (const name of ["actor_id", "task_id", "worker_id"])
      expect(json).not.toContain(name);
    expect(readTestData(data).ok).toBe(true);
    expect(data.input).toEqual({
      value: { total: 120.5 },
      source: "run",
      runId: RUN,
      runEnvironmentId: ENV,
    });
    const entries = [
      data.input,
      ...Object.values(data.pins ?? {}),
      ...Object.values(data.humans ?? {}),
      ...Object.values(data.signals ?? {}),
    ];
    for (const entry of entries)
      expect(entry).toMatchObject({
        source: "run",
        runId: RUN,
        runEnvironmentId: ENV,
      });
    expect(pointers.sort()).toEqual([
      "/humans/approve",
      "/input",
      "/pins/fetch",
      "/signals/wait-for-reply",
    ]);
  });

  it("pins action-like steps by instance key and gives the failed step nothing", () => {
    const { data, stopAt } = built(failedRun());
    expect(Object.keys(data.pins ?? {})).toEqual(["fetch"]);
    expect(data.pins?.["fetch"]?.output).toEqual({ rows: [{ name: "Ada" }] });
    expect(data.humans?.["approve"]).toMatchObject({
      decision: "approve",
      data: { note: "fine" },
    });
    expect(data.signals?.["wait-for-reply"]).toMatchObject({
      payload: { ok: true },
    });
    // The transform is worked out again; the failed step gets nothing.
    expect(data.pins?.["check"]).toBeUndefined();
    expect(data.pins?.["send"]).toBeUndefined();
    expect(stopAt).toEqual({ key: "send", code: "SMTP_REJECTED" });

    const loop = built({
      ...failedRun([
        fact("fetch", "action", "succeeded", { output: { rows: [] } }),
        fact("send[0]", "action", "succeeded", { output: { id: 0 } }),
        fact("send[1]", "action", "failed", { error_code: "SMTP_REJECTED" }),
        fact("send[2]", "action", "succeeded", { output: { id: 2 } }),
      ]),
      failedStep: { key: "send[1]", code: "SMTP_REJECTED" },
    });
    expect(Object.keys(loop.data.pins ?? {}).sort()).toEqual([
      "fetch",
      "send[0]",
      "send[2]",
    ]);
    expect(loop.stopAt).toEqual({ key: "send[1]", code: "SMTP_REJECTED" });
  });

  it("names the finished steps Execute works out again instead of copying", () => {
    expect(built(failedRun()).recomputed).toEqual(["check"]);
    // Not a step of this draft, or not finished: nothing to work out again.
    const { recomputed } = built({
      ...failedRun([
        fact("check", "transform", "running"),
        fact("log", "transform", "succeeded", { output: "logged" }),
      ]),
      definition: older,
    });
    expect(recomputed).toEqual([]);
  });

  it("pins carry the hashes of the run's version", () => {
    const { data } = built({ ...failedRun(), definition: older });
    expect(data.pins?.["fetch"]).toMatchObject({
      configHash: runPrints.get("fetch")!.config,
      contractHash: runPrints.get("fetch")!.contract,
      upstreamHash: runPrints.get("fetch")!.upstream,
      pinnedAt: NOW,
    });
  });

  it("leaves out what isn't in the draft or wasn't returned", () => {
    const source = {
      ...failedRun([
        fact("fetch", "action", "succeeded", {
          omissions: [{ path: "/output", reason: "classified_secret" }],
        }),
        fact("log", "action", "succeeded", { output: "logged" }),
        fact("send", "action", "succeeded", {}),
      ]),
      definition: older,
      status: "succeeded" as const,
      record: record("succeeded"),
      failedStep: null,
    };
    const { data, leftOut } = built(source);
    expect(data.pins).toBeUndefined();
    expect(leftOut).toEqual([
      {
        key: "fetch",
        reason: "omitted",
        detail: "This output is classified as secret.",
      },
      { key: "log", reason: "not-in-draft", detail: "not in this draft" },
      {
        key: "send",
        reason: "not-recorded",
        detail: "the platform didn't record it",
      },
    ]);
    expect(
      leftOutText(leftOut, [
        { pointer: "/pins/approve", code: "WV-STUDIO-TESTDATA-SCHEMA" },
      ]),
    ).toBe(
      "Left out 4 values: fetch (marked secret), log (not in this draft), send (the platform didn't record it), approve (doesn't fit the step's fields).",
    );
    expect(
      leftOutText(
        [],
        [{ pointer: "/pins/fetch", code: "WV-SCHEMA-SECRET_VALUE" }],
      ),
    ).toBe("Left out 1 value: fetch (marked secret).");
    expect(leftOutText([], [])).toBe("");
  });

  it("refuses a run too large for test data, keeping nothing", () => {
    const many = Array.from({ length: 1001 }, (_, i) =>
      fact(`send[${i}]`, "action", "succeeded", { output: { i } }),
    );
    const tooMany = testDataFromRun(
      { ...failedRun(many), failedStep: null },
      draft,
      runPrints,
      NOW,
    );
    expect(tooMany).toEqual({
      refused:
        "This run's data is larger than test data holds (1 MB, 1,000 entries a section), so Studio kept nothing.",
    });
    const big = testDataFromRun(
      failedRun([
        fact("fetch", "action", "succeeded", {
          output: { blob: "x".repeat(1_200_000) },
        }),
      ]),
      draft,
      runPrints,
      NOW,
    );
    expect(big).toEqual(tooMany);
    // More step instances than Studio reads (2,000), even of steps that copy
    // nothing: what was read is only part of the run, so nothing is kept.
    const partial = testDataFromRun(
      failedRun(
        Array.from({ length: 2001 }, (_, i) =>
          fact(`check[${i}]`, "transform", "succeeded", { output: i }),
        ),
      ),
      draft,
      runPrints,
      NOW,
    );
    expect(partial).toEqual(tooMany);
  });

  it("refuses a run Studio can't place or that recorded no steps", () => {
    expect(
      testDataFromRun(
        { ...failedRun(), environmentId: "development" },
        draft,
        runPrints,
        NOW,
      ),
    ).toEqual({
      refused:
        "Studio can't tell which environment this run is from, so it can't keep its data.",
    });
    expect(
      testDataFromRun(
        {
          ...failedRun([]),
          record: { ...record("failed"), completed: { fetch: {} } },
        },
        draft,
        runPrints,
        NOW,
      ),
    ).toEqual({
      refused:
        "Studio can't copy this run: the platform didn't record its steps.",
    });
  });

  it("Merge keeps entries only for steps the run didn't reach", () => {
    const pin = (source: "manual" | "schema", output: unknown) => ({
      output: output as never,
      source,
      pinnedAt: NOW,
      configHash: print(1),
      contractHash: print(2),
      upstreamHash: print(3),
    });
    const current: StudioTestData = {
      ...emptyTestData(NOW),
      pins: {
        // Not reached by the run: kept.
        notify: pin("manual", { sent: true }),
        // Reached and failed: Execute must stop there, so it goes.
        send: pin("schema", { message_id: "sample" }),
        fetch: pin("manual", { rows: [] }),
      },
      humans: {
        approve: { decision: "reject", data: {}, source: "manual" },
      },
    };
    const merged = mergedTestData(current, built(failedRun()), NOW);
    expect(merged.pins?.["notify"]?.source).toBe("manual");
    expect(merged.pins?.["send"]).toBeUndefined();
    expect(merged.pins?.["fetch"]?.source).toBe("run");
    expect(merged.input?.source).toBe("run");
    expect(merged.humans?.["approve"]?.source).toBe("run");

    // A reached step whose value the run didn't give (left out by the
    // platform) keeps nothing of the draft's either.
    const withheld = built(
      failedRun([
        fact("fetch", "action", "succeeded", {
          omissions: [{ path: "/output", reason: "classified_secret" }],
        }),
        fact("send", "action", "failed", { error_code: "SMTP_REJECTED" }),
      ]),
    );
    const kept = mergedTestData(current, withheld, NOW);
    expect(kept.pins?.["fetch"]).toBeUndefined();
    expect(Object.keys(kept.pins ?? {})).toEqual(["notify"]);
  });

  it("asks Merge or Replace in three short paragraphs, naming each step", () => {
    expect(mergeQuestion(built(failedRun()), 4, "development")).toBe(
      "Your test data already has 4 entries. Merge keeps the ones for steps this run didn't reach. Replace removes them.\n\n" +
        "Studio copies the run's input, fetch's output, approve's answer and wait-for-reply's signal. Execute stops at send, where the run failed. Studio works out check again from the copied data.\n\n" +
        "Each entry is marked as data from a run in development, so only people who can read those runs can open it.",
    );
    // A run that didn't fail, with a loop and nothing worked out again.
    const loop = built({
      ...failedRun([
        fact("send[0]", "action", "succeeded", { output: { id: 0 } }),
        fact("send[1]", "action", "succeeded", { output: { id: 1 } }),
      ]),
      status: "succeeded",
      record: record("succeeded"),
      failedStep: null,
    });
    expect(mergeQuestion(loop, 1, "this environment").split("\n\n")).toEqual([
      "Your test data already has 1 entry. Merge keeps the ones for steps this run didn't reach. Replace removes them.",
      "Studio copies the run's input and send's outputs.",
      "Each entry is marked as data from a run in this environment, so only people who can read those runs can open it.",
    ]);
  });

  it("a running run copies what it finished", () => {
    const { data, stopAt } = built({
      ...failedRun([
        fact("fetch", "action", "succeeded", { output: { rows: [] } }),
        fact("send", "action", "running"),
      ]),
      status: "running",
      record: record("running"),
      failedStep: null,
    });
    expect(Object.keys(data.pins ?? {})).toEqual(["fetch"]);
    expect(stopAt).toBeNull();
  });
});

describe("the fingerprints of the run's version", () => {
  it("hashes the version the run used, not the draft", async () => {
    const { printsOf, runPrints: printsOfRun } = await import(
      "../src/app/editor/state/test-data-tools"
    );
    const { emptyCanvas } = await import(
      "../src/app/editor/state/canvas-sidecar"
    );
    const changed = workflow("1.3.0", [
      step("fetch", "action", {
        uses: "sql.lookup@1.0.0",
        with: { object: { id: { literal: 2 } } },
      }),
    ]);
    const before = workflow("1.2.0", [
      step("fetch", "action", {
        uses: "sql.lookup@1.0.0",
        with: { object: { id: { literal: 1 } } },
      }),
    ]);
    const model = { definition: changed, canvas: emptyCanvas() } as never;
    const ofRun = printsOfRun(before, { total: 1 }, model).get("fetch")!;
    const ofDraft = printsOf(model, { total: 1 }).get("fetch")!;
    expect(ofRun.config).not.toBe(ofDraft.config);
    // The same version gives the draft's own hashes.
    expect(printsOfRun(changed, { total: 1 }, model).get("fetch")).toEqual(
      ofDraft,
    );
  });
});
