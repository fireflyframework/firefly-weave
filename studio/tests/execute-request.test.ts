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
import type { Workflow } from "../src/app/model";
import {
  CALLEE_LIMIT,
  calleeBlock,
  executeRequest,
  sliceStepIds,
  tooManyCallees,
  type ExecuteParts,
} from "../src/app/editor/state/execute-request";
import type { StudioTestData } from "../src/app/editor/state/test-data";

const t = (id: string) => ({ id, kind: "transform", value: { literal: id } });
const WORKFLOW = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "orders", version: "1.0.0" },
  spec: {
    steps: [
      {
        id: "check",
        kind: "action",
        uses: "crm.check@1.0.0",
        with: { literal: {} },
      },
      {
        id: "route",
        kind: "switch",
        cases: [
          {
            when: { literal: true },
            steps: [t("big"), t("after-big")],
            output: { literal: null },
          },
        ],
        default: { steps: [t("small")], output: { literal: null } },
      },
      {
        id: "each",
        kind: "forEach",
        items: { literal: [] },
        body: {
          steps: [t("item"), t("item-after")],
          output: { literal: null },
        },
      },
      {
        id: "pay",
        kind: "transaction",
        steps: [t("hold"), t("charge")],
        output: { literal: null },
      },
      {
        id: "summarize",
        kind: "llm",
        profile: "summarize",
        prompt: { literal: "x" },
      },
      {
        id: "notify",
        kind: "callWorkflow",
        uses: "notify@1.0.0",
        with: { literal: {} },
        mode: "detach",
      },
      t("last"),
    ],
  },
} as unknown as Workflow;

const PIN = {
  source: "manual",
  pinnedAt: "2026-10-26T10:00:00Z",
  configHash: "fnv1a64:0000000000000001",
  contractHash: "fnv1a64:0000000000000002",
  upstreamHash: "fnv1a64:0000000000000003",
} as const;

function parts(overrides: Partial<ExecuteParts> = {}): ExecuteParts {
  return {
    source: "{}",
    format: "json",
    workflow: WORKFLOW,
    definitions: [],
    connected: false,
    testData: {
      schemaVersion: 1,
      kind: "weave.studio/testData",
      updatedAt: "2026-10-26T10:00:00Z",
    },
    results: null,
    sampleInput: { id: "sample" },
    target: { mode: "workflow" },
    now: new Date("2026-10-26T10:00:00Z"),
    ...overrides,
  };
}

describe("the steps an execution keeps", () => {
  it("are those before the target and the groups that hold it, as the host slices", () => {
    expect(sliceStepIds(WORKFLOW, "check")).toEqual(["check"]);
    expect(sliceStepIds(WORKFLOW, "after-big")).toEqual([
      "check",
      "route",
      "big",
      "after-big",
    ]);
    expect(sliceStepIds(WORKFLOW, "item")).toEqual([
      "check",
      "route",
      "big",
      "after-big",
      "small",
      "each",
      "item",
    ]);
    expect(sliceStepIds(WORKFLOW, "hold")).toEqual([
      "check",
      "route",
      "big",
      "after-big",
      "small",
      "each",
      "item",
      "item-after",
      "pay",
      "hold",
    ]);
    expect(sliceStepIds(WORKFLOW, "nowhere")).toBeNull();
  });
});

describe("an execution request", () => {
  it("sends pins as mocks, before last results, and only for steps a mock stands for", () => {
    const testData: StudioTestData = {
      ...parts().testData,
      pins: {
        check: { ...PIN, output: { eligible: true } },
        "check[2]": { ...PIN, output: { eligible: false } },
        big: { ...PIN, output: 1 },
      },
    };
    const request = executeRequest(
      parts({
        testData,
        results: {
          schemaVersion: 1,
          at: "2026-10-26T09:00:00Z",
          status: "completed",
          steps: {
            check: {
              status: "completed",
              execution: "fnv1a64:0000000000000009",
              output: { eligible: false },
            },
            summarize: {
              status: "completed",
              execution: "fnv1a64:0000000000000009",
              output: { result: "old" },
            },
            last: {
              status: "completed",
              execution: "fnv1a64:0000000000000009",
              output: "x",
            },
            notify: {
              status: "completed",
              execution: "fnv1a64:0000000000000009",
              output: null,
            },
          },
        },
      }),
    );
    expect(request.mocks).toEqual({
      "node:check": { eligible: true },
      "node:check[2]": { eligible: false },
      "node:summarize": { result: "old" },
    });
  });

  it("uses the test input, or a sample of the input fields when there is none", () => {
    expect(executeRequest(parts()).input).toEqual({ id: "sample" });
    const withInput = executeRequest(
      parts({
        testData: {
          ...parts().testData,
          input: { value: { id: "c-1" }, source: "manual" },
        },
      }),
    );
    expect(withInput.input).toEqual({ id: "c-1" });
  });

  it("asks for the project catalog only when connected, and stubs what isn't there", () => {
    expect(executeRequest(parts()).catalog).toBe("none");
    expect(executeRequest(parts({ connected: true })).catalog).toBe("project");
    expect(executeRequest(parts()).stubUnresolved).toBe(true);
  });

  it("carries the scripts and the callee mocks, and always advances waits", () => {
    const testData: StudioTestData = {
      ...parts().testData,
      signals: { paid: { payload: { paid: true }, source: "manual" } },
      humans: { approval: { decision: "approve", data: {}, source: "manual" } },
      workflows: {
        "notify@1.0.0": {
          mocks: { send: { output: { id: "m-1" }, source: "manual" } },
        },
      },
    };
    const request = executeRequest(
      parts({ testData, target: { mode: "step", stepId: "big" } }),
    );
    expect(request.scripts).toEqual({
      signals: testData.signals,
      humans: testData.humans,
      aiTurns: {},
      autoAdvanceWaits: true,
    });
    expect(request.workflows).toEqual({
      "notify@1.0.0": { mocks: { "node:send": { id: "m-1" } } },
    });
    expect(request.target).toEqual({ mode: "step", stepId: "big" });
    expect(request.now).toBe("2026-10-26T10:00:00.000Z");
  });
});

describe("an execution request for one step", () => {
  it("sends only the test data of the steps it runs", () => {
    const testData: StudioTestData = {
      ...parts().testData,
      pins: {
        check: { ...PIN, output: { eligible: true } },
        summarize: { ...PIN, output: { result: "pinned" } },
      },
    };
    const request = executeRequest(
      parts({ testData, target: { mode: "step", stepId: "big" } }),
    );
    expect(request.mocks).toEqual({ "node:check": { eligible: true } });
    const before = executeRequest(
      parts({ testData, target: { mode: "before", stepId: "summarize" } }),
    );
    expect(Object.keys(before.mocks ?? {})).toEqual([
      "node:check",
      "node:summarize",
    ]);
  });
});

/** order-intake: a call to a draft on this computer that waits, and one to a published workflow. */
const CALLING = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "order-intake", version: "1.2.0" },
  spec: {
    steps: [
      {
        id: "lookup",
        kind: "callWorkflow",
        uses: "lookup-customer@0.1.0",
        mode: "wait",
        with: { literal: {} },
      },
      {
        id: "notify",
        kind: "callWorkflow",
        uses: "notify-customer@1.0.0",
        mode: "wait",
        with: { literal: {} },
      },
    ],
  },
} as unknown as Workflow;
const LOOKUP_SOURCE = "apiVersion: weave/v1alpha1\nkind: Workflow\n";

describe("an execution request with called workflows", () => {
  it("sends a draft on this computer with its source, and every other callee it reaches with its mocks", () => {
    const request = executeRequest(
      parts({
        workflow: CALLING,
        callees: {
          sources: {
            "lookup-customer@0.1.0": { source: LOOKUP_SOURCE, format: "yaml" },
          },
          references: [
            "lookup-customer@0.1.0",
            "notify-customer@1.0.0",
            "audit-trail@2.0.0",
          ],
        },
        testData: {
          ...parts().testData,
          workflows: {
            "notify-customer@1.0.0": {
              mocks: { send: { output: { id: "m-1" }, source: "manual" } },
            },
          },
        },
      }),
    );
    expect(request.workflows).toEqual({
      "lookup-customer@0.1.0": {
        source: LOOKUP_SOURCE,
        format: "yaml",
        mocks: {},
      },
      "notify-customer@1.0.0": { mocks: { "node:send": { id: "m-1" } } },
      "audit-trail@2.0.0": { mocks: {} },
    });
  });

  it("lists the workflows it calls directly when it knows of no drafts", () => {
    expect(
      Object.keys(executeRequest(parts({ workflow: CALLING })).workflows ?? {}),
    ).toEqual(["lookup-customer@0.1.0", "notify-customer@1.0.0"]);
  });

  it("sends a pin on a call step, but not its last result, so the called workflow runs again", () => {
    const request = executeRequest(
      parts({
        workflow: CALLING,
        testData: {
          ...parts().testData,
          pins: { notify: { ...PIN, output: { sent: true } } },
        },
        results: {
          schemaVersion: 1,
          at: "2026-10-26T09:00:00Z",
          status: "completed",
          steps: {
            lookup: {
              status: "completed",
              execution: "fnv1a64:0000000000000009",
              output: { name: "Ada" },
            },
          },
        },
      }),
    );
    expect(request.mocks).toEqual({ "node:notify": { sent: true } });
  });

  it("says when a test would run more called workflows than the host takes", () => {
    const references = Array.from(
      { length: CALLEE_LIMIT + 1 },
      (_, n) => `w${n}@1.0.0`,
    );
    const over = executeRequest(
      parts({ workflow: CALLING, callees: { sources: {}, references } }),
    );
    expect(tooManyCallees(over)).toBe(
      "This test would run 51 called workflows. Studio can run at most 50 in one test.",
    );
    const fits = executeRequest(
      parts({
        workflow: CALLING,
        callees: { sources: {}, references: references.slice(1) },
      }),
    );
    expect(tooManyCallees(fits)).toBeNull();
  });

  it("words the host's block on a call that waits as the call's, and leaves others", () => {
    const block = (nodeId: string, instanceKey = "") => ({
      reason: "missing_mock" as const,
      nodeId,
      instanceKey,
      message: `Step ${nodeId} needs test data.`,
    });
    expect(calleeBlock(CALLING, block("notify"))).toEqual({
      reason: "callee",
      nodeId: "notify",
      instanceKey: "",
      message: "Call notify-customer needs test data or the workflow itself.",
    });
    // Not a call, a call that doesn't wait, or one item of a loop: the host's words stay.
    expect(calleeBlock(WORKFLOW, block("check"))).toEqual(block("check"));
    expect(calleeBlock(WORKFLOW, block("notify"))).toEqual(block("notify"));
    expect(calleeBlock(CALLING, block("notify", "notify[1]"))).toEqual(
      block("notify", "notify[1]"),
    );
  });
});
