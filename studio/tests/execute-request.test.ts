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
  executeRequest,
  sliceStepIds,
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
