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
import { RunData } from "../src/app/editor/runs-tab/run-data";
import type { RunRecord } from "../src/app/editor/runs-tab/run-record";
import type { Step, Workflow } from "../src/app/model";
import type { StepFact } from "../src/app/operate/run-contracts";

const RUN = "3f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b";
const ENVIRONMENT = "00000000-0000-4000-8000-000000000003";

function record(facts: Partial<RunRecord> = {}): RunRecord {
  return {
    id: RUN,
    versionId: "9e8f5b0c-cfee-4af4-8dea-d2ae9fb06b9a",
    status: "failed",
    input: { total: 120.5 },
    // The run's state keeps each step's binding under its step ID.
    completed: { fetch: { output: { rows: [{ name: "Ada" }] } } },
    active: [],
    waiting: [],
    incidents: [],
    ...facts,
  };
}
function definition(send: Partial<Step>): Workflow {
  return {
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "invoice-approval", version: "1.2.0" },
    spec: {
      steps: [
        { id: "fetch", kind: "action", action: "sql.lookup@1.0.0" },
        { id: "send", kind: "action", action: "crm.lookup@2.0.0", ...send },
        { id: "log", kind: "transform", value: { literal: {} } },
      ],
    },
  };
}
/** The task input the platform sent to `send`, worked out by hand. */
const sendWith = {
  object: {
    customer: { ref: "/steps/fetch/output/rows/0/name" },
    total: { ref: "/input/total" },
  },
};
function fact(
  key: string,
  status: StepFact["status"],
  output?: { value: unknown } | { omitted: "classified_secret" },
): StepFact {
  const index = /\[(\d+)\]/.exec(key);
  return {
    node_id: key.replace(/\[.*$/, ""),
    instance_key: index ? key : "",
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
    error_code: status === "failed" ? "SMTP_REJECTED" : null,
    child_run_id: null,
    log_entries: 0,
    ...(output && "value" in output ? { output: output.value } : {}),
    ...(output && "omitted" in output
      ? { omissions: [{ path: "/output", reason: output.omitted }] }
      : {}),
  };
}

describe("what a step received", () => {
  it("recomputes the input the step received", () => {
    const data = new RunData(
      record(),
      ENVIRONMENT,
      definition({ with: sendWith }),
    );
    expect(data.received("send")).toEqual({
      ok: true,
      value: { customer: "Ada", total: 120.5 },
    });
    // A step with no input mapping shows none.
    expect(data.received("log")).toEqual({ ok: false, reason: "" });
  });

  it("says why it can't recompute", () => {
    const loop = new RunData(
      record(),
      ENVIRONMENT,
      definition({ with: { object: { name: { ref: "/item/name" } } } }),
    );
    expect(loop.received("send")).toEqual({
      ok: false,
      reason: "Studio can't recompute one iteration's input yet.",
    });

    const omitted = new RunData(
      record(),
      ENVIRONMENT,
      definition({ with: sendWith }),
    );
    omitted.setOutputs([
      fact("fetch", "succeeded", { omitted: "classified_secret" }),
    ]);
    expect(omitted.received("send")).toEqual({
      ok: false,
      reason:
        "This input reads data the platform left out of the run, so Studio can't recompute it.",
    });

    const unread = new RunData(record(), ENVIRONMENT, null);
    expect(unread.received("send")).toEqual({
      ok: false,
      reason:
        "Studio couldn't read the version this run used, so it can't recompute the input.",
    });

    // A value of the wrong type: the evaluator's code is named.
    const failing = new RunData(
      record(),
      ENVIRONMENT,
      definition({
        with: {
          op: {
            name: "add",
            args: [{ ref: "/steps/fetch/output/rows" }, { literal: 1 }],
          },
        },
      }),
    );
    const result = failing.received("send");
    expect(result.ok).toBe(false);
    expect(!result.ok && result.reason).toMatch(
      /^Studio couldn't recompute this input: WV-[A-Z-]+\.$/,
    );
  });

  it("reads a step's own output, never a repeated activation's", () => {
    const next = (expression: unknown): Workflow => ({
      ...definition({}),
      spec: {
        steps: [
          { id: "add", kind: "action", action: "crm.lookup@2.0.0" },
          {
            id: "next",
            kind: "action",
            action: "crm.lookup@2.0.0",
            with: expression,
          },
        ],
      },
    });
    const data = new RunData(
      record({
        completed: {
          add: { output: { id: 1 } },
          // An undo's output and a loop's yield, after the step's own.
          "add#undo": { output: { id: 99 } },
          "add~1": { output: { id: 98 } },
        },
      }),
      ENVIRONMENT,
      next({ ref: "/steps/add/output/id" }),
    );
    // The platform withholds a repeated activation's output as unclassifiable;
    // that never leaves the step's own output out.
    data.setOutputs([
      {
        ...fact("add", "succeeded"),
        instance_key: "add#undo",
        omissions: [{ path: "/output", reason: "classification_unavailable" }],
      },
    ]);
    expect(data.received("next")).toEqual({ ok: true, value: 1 });

    // A handled failure's binding is read as the run's state keeps it.
    const handled = new RunData(
      record({
        completed: { add: { output: null, error: { code: "CRM_DOWN" } } },
      }),
      ENVIRONMENT,
      next({ ref: "/steps/add/error/code" }),
    );
    expect(handled.received("next")).toEqual({ ok: true, value: "CRM_DOWN" });
  });

  it("keeps a handled failure's error when its recorded output is read", () => {
    const workflow: Workflow = {
      ...definition({}),
      spec: {
        steps: [
          {
            id: "add",
            kind: "action",
            action: "crm.lookup@2.0.0",
            onError: "continue",
          },
          {
            id: "next",
            kind: "action",
            action: "crm.lookup@2.0.0",
            with: {
              object: {
                code: { ref: "/steps/add/error/code" },
                output: { ref: "/steps/add/output" },
              },
            },
          },
        ],
      },
    };
    const data = new RunData(
      record({
        completed: { add: { output: null, error: { code: "CRM_DOWN" } } },
      }),
      ENVIRONMENT,
      workflow,
    );
    // Step details read the step's recorded output: a value, here null.
    data.setOutputs([fact("add", "succeeded", { value: null })]);
    expect(data.received("next")).toEqual({
      ok: true,
      value: { code: "CRM_DOWN", output: null },
    });
  });
});

describe("recorded outputs", () => {
  it("keeps outputs, omissions and iterations by instance key", () => {
    const data = new RunData(record(), ENVIRONMENT, null);
    const stamp = data.version();
    data.setOutputs([
      fact("send[0]", "succeeded", { value: { message_id: "m-1" } }),
      fact("send[1]", "failed", { omitted: "classified_secret" }),
      fact("send[2]", "succeeded"),
      fact("send[3]", "running"),
    ]);
    expect(data.version()).not.toBe(stamp);
    expect(data.output("send[0]")).toEqual({
      kind: "value",
      value: { message_id: "m-1" },
    });
    expect(data.output("send[1]")).toEqual({
      kind: "omitted",
      reason: "classified_secret",
    });
    expect(data.output("send[2]")).toEqual({
      kind: "none",
      why: "not-recorded",
    });
    expect(data.output("send[3]")).toEqual({ kind: "none", why: "running" });
    expect(data.output("check")).toEqual({ kind: "none", why: "not-reached" });
    expect(data.iterations("send")).toEqual([
      "send[1]",
      "send[0]",
      "send[2]",
      "send[3]",
    ]);
    expect(data.failure("send")).toBe("SMTP_REJECTED");
    expect(data.failure("fetch")).toBeNull();
  });

  it("says when a step's output is loading, failed to load or is too large", () => {
    const data = new RunData(
      record({ active: ["send"], status: "running" }),
      ENVIRONMENT,
      null,
    );
    expect(data.output("send")).toEqual({ kind: "none", why: "running" });
    data.markLoading("fetch");
    expect(data.output("fetch")).toEqual({ kind: "loading" });
    data.markError("fetch", "The platform didn't answer.");
    expect(data.output("fetch")).toEqual({
      kind: "error",
      message: "The platform didn't answer.",
    });
    data.markTooLarge("fetch");
    expect(data.output("fetch")).toEqual({ kind: "too-large" });
    // Reading the step again replaces what was said about it.
    data.setOutputs(
      [fact("fetch", "succeeded", { value: { rows: [] } })],
      "fetch",
    );
    expect(data.output("fetch")).toEqual({
      kind: "value",
      value: { rows: [] },
    });
    // A step read with no instances is no longer loading.
    data.markLoading("log");
    data.setOutputs([], "log");
    expect(data.output("log")).toEqual({ kind: "none", why: "not-reached" });
  });

  it("names the run, its input and the runs it called", () => {
    const data = new RunData(record(), ENVIRONMENT, null);
    data.setFacts([
      {
        ...fact("notify", "succeeded"),
        child_run_id: "2d1e8a9b-5e77-4f8d-9c73-6b3d2e4f9a23",
      },
    ]);
    expect(data.runId).toBe(RUN);
    expect(data.environmentId).toBe(ENVIRONMENT);
    expect(data.label()).toBe("run 3f2a9c1e");
    expect(data.input()).toEqual({ total: 120.5 });
    expect(data.childRun("notify")).toBe(
      "2d1e8a9b-5e77-4f8d-9c73-6b3d2e4f9a23",
    );
    expect(data.childRun("fetch")).toBeNull();
  });
});
