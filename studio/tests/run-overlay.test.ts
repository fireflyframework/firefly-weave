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
import { edgeRun, pulses, tileRun } from "../src/app/editor/canvas/run-state";
import {
  overlayOf,
  versionBanner,
  versionDiff,
} from "../src/app/editor/runs-tab/run-overlay";
import {
  readRunRecord,
  type RunRecord,
} from "../src/app/editor/runs-tab/run-record";
import type { StepFact } from "../src/app/operate/run-contracts";
import type { Step, Workflow } from "../src/app/model";

const RUN = "3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b";
const VERSION = "9e8f5b0c-cfee-4af4-8dea-d2ae9fb06b9a";

/** A runs.read answer, as the platform sends it. */
function runView(state: Record<string, unknown>): Record<string, unknown> {
  return {
    id: RUN,
    activation: {
      id: "a09f6c1d-d0ff-4b05-9efb-e3bfa0c17cab",
      revision: 4,
      name: "invoice-approval",
      request: {
        version_id: VERSION,
        artifact_digest: `sha256:${"a".repeat(64)}`,
        scope: {},
        connection_revision_ids: {},
        worker_release_ids: {},
      },
      retired: false,
    },
    artifact_digest: `sha256:${"a".repeat(64)}`,
    state: {
      status: "running",
      active: [],
      input: { total: 120.5 },
      steps: {},
      waits: {},
      output: null,
      accepted_sequence: 3,
      incident: null,
      branches: {},
      joins: {},
      incidents: {},
      deferred_results: [],
      ...state,
    },
    correlation_key: null,
    business_key: "INV-1041",
    parent_run_id: null,
    external_effects_may_continue: false,
  };
}
const record = (state: Record<string, unknown>): RunRecord =>
  readRunRecord(runView(state))!;

const transform = (id: string, value: unknown = { literal: {} }): Step => ({
  id,
  kind: "transform",
  value,
});
function workflow(version: string, steps: Step[]): Workflow {
  return {
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "invoice-approval", version },
    spec: { steps },
  };
}
const draft = workflow("1.3.0", [
  { id: "fetch", kind: "action", action: "sql.lookup@1.0.0" },
  { id: "approve", kind: "humanTask", decisions: ["approve", "reject"] },
  {
    id: "send",
    kind: "action",
    action: "crm.lookup@2.0.0",
    with: { ref: "/steps/fetch/output" },
  },
  transform("check"),
]);
const versions = {
  run: null,
  draft,
  runVersion: "1.3.0",
  test: false,
};
function fact(
  key: string,
  status: StepFact["status"],
  error_code: string | null = null,
): StepFact {
  const index = /\[(\d+)\]/.exec(key);
  return {
    node_id: key.replace(/\[.*$/, ""),
    instance_key: key,
    iteration: index ? [Number(index[1])] : [],
    kind: "action",
    status,
    scheduled_at: null,
    started_at: null,
    ended_at: null,
    duration_ms: null,
    queue_wait_ms: null,
    attempts: 1,
    worker_id: null,
    error_code,
    child_run_id: null,
    log_entries: 0,
  };
}

describe("a run's typed record", () => {
  it("reads only the run's typed fields", () => {
    expect(
      readRunRecord(
        runView({
          status: "failed",
          steps: { fetch: { output: { rows: [{ name: "Ada" }] } } },
          active: ["send"],
          waits: { "wait-for-reply": "2026-10-07T10:05:00Z" },
          incidents: {
            "incident-1": {
              node_id: "send",
              generation: 0,
              code: "SMTP_REJECTED",
            },
          },
          branches: {
            "route/approve": {
              owner: "route",
              name: "approve",
              target: "approve",
              status: "completed",
              visible: {},
              steps: { approve: { output: { decision: "approve", data: {} } } },
              output: null,
            },
          },
        }),
      ),
    ).toStrictEqual({
      id: RUN,
      versionId: VERSION,
      status: "failed",
      input: { total: 120.5 },
      completed: {
        fetch: { output: { rows: [{ name: "Ada" }] } },
        approve: { output: { decision: "approve", data: {} } },
      },
      active: ["send"],
      waiting: ["wait-for-reply"],
      incidents: [{ key: "send", code: "SMTP_REJECTED" }],
    });
  });

  it("reads nothing from an answer whose facts aren't typed", () => {
    const bad = [
      { ...runView({}), id: "not-a-uuid" },
      {
        ...runView({}),
        activation: { request: { version_id: "1.2.0" } },
      },
      runView({ status: "paused" }),
      runView({ incidents: { first: { node_id: "send" } } }),
      runView({ steps: [{ fetch: {} }] }),
      runView({ active: "send" }),
      runView({ active: [7] }),
      runView({ waits: ["approve"] }),
      runView({ branches: { a: { steps: [] } } }),
      runView({ input: undefined, steps: undefined }),
      null,
      "run",
    ];
    for (const value of bad) expect(readRunRecord(value)).toBeNull();
  });
});

describe("the run over the draft", () => {
  it("draws a failed run", () => {
    const run = record({
      status: "failed",
      steps: { fetch: { rows: [] }, approve: { decision: "approve" } },
      incidents: {
        i1: { node_id: "send", generation: 0, code: "SMTP_REJECTED" },
      },
    });
    const overlay = overlayOf(run, null, versions);
    expect(Object.fromEntries(overlay.states)).toEqual({
      fetch: "completed",
      approve: "completed",
      send: "failed",
      check: "not-reached",
    });
    expect(overlay.canvas).toMatchObject({
      mode: "real",
      status: "failed",
      current: ["send"],
      active: [],
      done: ["approve", "fetch"],
      failures: { send: "SMTP_REJECTED" },
      changed: [],
    });
    expect(tileRun("send", overlay.canvas)).toBe("failed");
    expect(tileRun("check", overlay.canvas)).toBeNull();
    expect(
      edgeRun({ leaves: "fetch", enters: "approve" }, overlay.canvas),
    ).toBe("taken");
    expect(edgeRun({ leaves: "send", enters: "check" }, overlay.canvas)).toBe(
      "skipped",
    );
  });

  it("draws a running run and a waiting one", () => {
    const running = overlayOf(
      record({ status: "running", steps: { fetch: {} }, active: ["send"] }),
      null,
      versions,
    );
    expect(running.canvas.current).toEqual(["send"]);
    expect(running.canvas.done).toEqual(["fetch"]);
    expect(pulses(tileRun("send", running.canvas), running.canvas)).toBe(true);
    expect(running.states.get("send")).toBe("running");

    const waiting = overlayOf(
      record({
        status: "waiting",
        steps: { fetch: {} },
        waits: { approve: "2026-10-07T10:05:00Z" },
      }),
      null,
      versions,
    );
    expect(waiting.canvas.active).toEqual(["approve"]);
    expect(waiting.canvas.current).toEqual([]);
    expect(tileRun("approve", waiting.canvas)).toBe("waiting");
    expect(waiting.states.get("approve")).toBe("waiting");
  });

  it("reads the steps finished on a decision's path", () => {
    const run = record({
      status: "running",
      steps: { fetch: {} },
      branches: {
        "route/0": {
          owner: "route",
          name: "0",
          target: "approve",
          status: "running",
          visible: {},
          steps: { approve: { decision: "approve" } },
          output: null,
        },
      },
    });
    expect(overlayOf(run, null, versions).canvas.done).toEqual([
      "approve",
      "fetch",
    ]);
  });

  it("a step with several instances is failed when one failed", () => {
    const run = record({ status: "failed", steps: { fetch: {} } });
    const facts = [
      fact("send[0]", "succeeded"),
      fact("send[1]", "failed", "SMTP_REJECTED"),
    ];
    const failed = overlayOf(run, facts, versions);
    expect(failed.states.get("send")).toBe("failed");
    expect(failed.canvas.failures).toEqual({ send: "SMTP_REJECTED" });
    const done = overlayOf(
      record({ status: "succeeded", steps: { fetch: {} } }),
      [fact("send[0]", "succeeded"), fact("send[1]", "succeeded")],
      versions,
    );
    expect(done.states.get("send")).toBe("completed");
    expect(done.canvas.done).toEqual(["fetch", "send"]);
    // One instance still running makes the step running.
    const going = overlayOf(
      record({ status: "running", steps: { fetch: {} } }),
      [fact("send[0]", "succeeded"), fact("send[1]", "running")],
      versions,
    );
    expect(going.states.get("send")).toBe("running");
  });

  it("a run that ended runs nothing, whatever older step facts say", () => {
    // Step facts read just before the run's state said it succeeded.
    const done = overlayOf(
      record({ status: "succeeded", steps: { fetch: {}, send: {} } }),
      [fact("send", "running"), fact("approve", "waiting")],
      versions,
    );
    expect(done.states.get("send")).toBe("completed");
    expect(done.states.has("approve")).toBe(true);
    expect(done.states.get("approve")).toBe("not-reached");
    expect(done.canvas.current).toEqual([]);
    expect(done.canvas.active).toEqual([]);
    expect(done.canvas.done).toEqual(["fetch", "send"]);
  });
});

describe("what changed since the run", () => {
  const runVersion = workflow("1.2.0", [
    { id: "fetch", kind: "action", action: "sql.lookup@1.0.0" },
    transform("log"),
    {
      id: "send",
      kind: "action",
      action: "crm.lookup@2.0.0",
      with: { ref: "/input" },
    },
  ]);
  const edited = workflow("1.3.0", [
    { id: "fetch", kind: "action", action: "sql.lookup@1.0.0" },
    transform("check"),
    {
      id: "send",
      kind: "action",
      action: "crm.lookup@2.0.0",
      with: { ref: "/steps/fetch/output" },
    },
  ]);

  it("lists what changed since the run", () => {
    expect(versionDiff(runVersion, edited)).toEqual({
      changed: ["send"],
      missing: ["log"],
      added: ["check"],
    });
    expect(versionDiff(edited, structuredClone(edited))).toEqual({
      changed: [],
      missing: [],
      added: [],
    });
    // The same step with its keys in another order is the same step.
    const reordered = workflow("1.3.0", [
      { action: "sql.lookup@1.0.0", kind: "action", id: "fetch" },
    ]);
    const original = workflow("1.3.0", [
      { id: "fetch", kind: "action", action: "sql.lookup@1.0.0" },
    ]);
    expect(versionDiff(original, reordered).changed).toEqual([]);
  });

  it("a step changed inside a group leaves the group unchanged", () => {
    const group = (inner: unknown) =>
      workflow("1.0.0", [
        {
          id: "route",
          kind: "switch",
          cases: [
            {
              when: { literal: true },
              steps: [transform("inside", inner)],
              output: { literal: {} },
            },
          ],
        },
      ]);
    expect(versionDiff(group({ literal: 1 }), group({ literal: 2 }))).toEqual({
      changed: ["inside"],
      missing: [],
      added: [],
    });
  });

  it("the overlay marks the steps changed since the run", () => {
    const overlay = overlayOf(
      record({ status: "succeeded", steps: { fetch: {}, send: {} } }),
      null,
      { run: runVersion, draft: edited, runVersion: "1.2.0", test: false },
    );
    expect(overlay.diff).toEqual({
      changed: ["send"],
      missing: ["log"],
      added: ["check"],
    });
    expect(overlay.canvas.changed).toEqual(["send"]);
    // A step new in the draft isn't one the run skipped.
    expect(overlay.canvas.added).toEqual(["check"]);
    expect(overlay.banner).toBe(
      "This run used 1.2.0; you're editing the draft of 1.3.0.",
    );
  });

  it("names the versions", () => {
    expect(versionBanner("1.2.0", "1.3.0", false)).toBe(
      "This run used 1.2.0; you're editing the draft of 1.3.0.",
    );
    expect(versionBanner("1.3.0", "1.3.0", false)).toBeNull();
    expect(versionBanner("1.3.0+test.d3c29f400322", "1.3.0", true)).toBe(
      "This test run used a draft of 1.3.0.",
    );
  });
});
