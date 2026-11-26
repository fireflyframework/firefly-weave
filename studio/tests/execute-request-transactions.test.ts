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
// Execute takes what a transaction's steps and their undo steps do: a
// simulated failure with its effect, a failure of an undo, a release or a
// confirm, and their test output, each under its own key (node:add#undo). The
// host refuses a whole request for a key its step doesn't have, so what no
// longer applies stays in test data and isn't sent. Real runs never see any.
import { describe, expect, it } from "vitest";
import { actionKind } from "../src/app/editor/ndv/kinds/builtin";
import type { KindContext, NdvContext } from "../src/app/editor/ndv/registry";
import {
  executeRequest,
  type ExecuteParts,
} from "../src/app/editor/state/execute-request";
import {
  ExecutionService,
  type ExecutionHost,
} from "../src/app/editor/state/execution-service";
import type { TestActivationApi } from "../src/app/editor/state/test-activation-client";
import { TestActivationService } from "../src/app/editor/state/test-activation-service";
import type { StudioTestData } from "../src/app/editor/state/test-data";
import type { Step, Workflow } from "../src/app/model";

const AT = "2026-11-25T10:00:00Z";
const PIN = {
  source: "manual",
  pinnedAt: AT,
  configHash: "fnv1a64:0000000000000001",
  contractHash: "fnv1a64:0000000000000002",
  upstreamHash: "fnv1a64:0000000000000003",
} as const;
const operation = (uses: string) => ({
  kind: "action",
  uses,
  with: { literal: {} },
});
const action = (id: string, fields: Record<string, unknown> = {}) => ({
  id,
  kind: "action",
  uses: `${id}.run@1.0.0`,
  with: { literal: {} },
  ...fields,
});
const HOLD = action("hold", {
  confirm: operation("hold.confirm@1.0.0"),
  cancel: operation("hold.release@1.0.0"),
});
const ADD = action("add", { compensate: operation("add.remove@1.0.0") });
const SUBMIT = action("submit", { irreversible: true });
const SHAPE = { id: "shape", kind: "transform", value: { literal: 1 } };
const workflowOf = (inside: unknown[], after: unknown[] = []) =>
  ({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "move-between-systems", version: "1.0.0" },
    spec: {
      steps: [
        {
          id: "move",
          kind: "transaction",
          steps: inside,
          output: { literal: null },
        },
        ...after,
      ],
    },
  }) as unknown as Workflow;
const MOVE = workflowOf(
  [HOLD, ADD, SHAPE, SUBMIT],
  [
    action("after"),
    { ...ADD, id: "still-undone" },
    { ...SHAPE, id: "end-shape" },
  ],
);

function parts(
  testData: Partial<StudioTestData>,
  workflow: Workflow = MOVE,
): ExecuteParts {
  return {
    source: "{}",
    format: "json",
    workflow,
    definitions: [],
    connected: false,
    testData: {
      schemaVersion: 1,
      kind: "weave.studio/testData",
      updatedAt: AT,
      ...testData,
    },
    results: null,
    sampleInput: {},
    target: { mode: "workflow" },
    now: new Date(AT),
  };
}

describe("an execution of a transaction with simulated failures", () => {
  it("sends the test output of a confirm, and a failure drops only the one at its own key", () => {
    const pins = {
      add: { ...PIN, output: { id: "a-1" } },
      "add#undo": { ...PIN, output: { undone: true } },
      "hold#confirm": { ...PIN, output: { confirmedAt: AT } },
    };
    expect(executeRequest(parts({ pins })).mocks).toEqual({
      "node:add": { id: "a-1" },
      "node:add#undo": { undone: true },
      "node:hold#confirm": { confirmedAt: AT },
    });
    const failing = executeRequest(
      parts({ pins, errors: { "add#undo": { code: "UNAVAILABLE" } } }),
    );
    expect(failing.mocks).toEqual({
      "node:add": { id: "a-1" },
      "node:hold#confirm": { confirmedAt: AT },
    });
    // The step's own failure leaves its undo's test output.
    const stepFails = executeRequest(
      parts({ pins, errors: { add: { code: "UNAVAILABLE" } } }),
    );
    expect(stepFails.mocks).toEqual({
      "node:add#undo": { undone: true },
      "node:hold#confirm": { confirmedAt: AT },
    });
  });

  it("sends the effect of a participant's failure alone, and each undo's failure under its own key", () => {
    const testData: Partial<StudioTestData> = {
      errors: {
        hold: { code: "VALIDATION", effect: "none" },
        "hold#cancel": { code: "TIMEOUT", attempts: 3 },
        "hold[2]#confirm": { code: "TIMEOUT", attempts: 3 },
        // An undo step's failure changed nothing a person says: the host takes no effect for it.
        "add#undo": {
          code: "UNAVAILABLE",
          attempts: 10,
          effect: "none",
          source: "manual",
        },
        submit: { code: "REFUSED", outcome: "unknown", effect: "unknown" },
        // It was in a transaction when its failure was set.
        after: { code: "REFUSED", effect: "none" },
        "still-undone": { code: "REFUSED", effect: "none" },
        // Nothing to undo: the host takes no effect for it.
        shape: { code: "BAD", outcome: "not_started", effect: "none" },
      },
    };
    expect(executeRequest(parts(testData)).errors).toEqual({
      "node:hold": { code: "VALIDATION", effect: "none" },
      // An undo step fails every one of the transaction's tries (10 by default).
      "node:hold#cancel": { code: "TIMEOUT", attempts: 10 },
      "node:hold[2]#confirm": { code: "TIMEOUT", attempts: 10 },
      "node:add#undo": { code: "UNAVAILABLE", attempts: 10 },
      "node:submit": { code: "REFUSED", outcome: "unknown", effect: "unknown" },
      "node:after": { code: "REFUSED" },
      "node:still-undone": { code: "REFUSED" },
      "node:shape": { code: "BAD", outcome: "not_started" },
    });
    // Execute step on hold brings hold's alone.
    expect(
      executeRequest({
        ...parts(testData),
        target: { mode: "step", stepId: "hold" },
      }).errors,
    ).toEqual({
      "node:hold": { code: "VALIDATION", effect: "none" },
      "node:hold#cancel": { code: "TIMEOUT", attempts: 10 },
      "node:hold[2]#confirm": { code: "TIMEOUT", attempts: 10 },
    });
  });

  it("fails an undo step as often as the transaction's Max tries say when it runs, 1,000 at most", () => {
    const withTries = (maxAttempts: number) => {
      const workflow = structuredClone(MOVE);
      Object.assign(workflow.spec.steps[0], {
        policy: {
          holdSeconds: 900,
          undoRetry: {
            maxAttempts,
            initialDelaySeconds: 2,
            maxDelaySeconds: 300,
          },
          undoTimeoutSeconds: 86_400,
        },
      });
      return workflow;
    };
    // Saved while Max tries was 10, and once without its tries.
    const testData: Partial<StudioTestData> = {
      errors: {
        "add#undo": { code: "UNAVAILABLE", attempts: 10, source: "manual" },
        "hold#cancel": { code: "TIMEOUT", source: "manual" },
      },
    };
    expect(executeRequest(parts(testData, withTries(3))).errors).toEqual({
      "node:add#undo": { code: "UNAVAILABLE", attempts: 3 },
      "node:hold#cancel": { code: "TIMEOUT", attempts: 3 },
    });
    // The host takes 1,000 tries at most.
    expect(executeRequest(parts(testData, withTries(5000))).errors).toEqual({
      "node:add#undo": { code: "UNAVAILABLE", attempts: 1000 },
      "node:hold#cancel": { code: "TIMEOUT", attempts: 1000 },
    });
  });

  it("says what a failure set before the step was in the transaction may have changed, when its outcome is unknown", () => {
    // The host would read a definite code as no change, whatever the outcome.
    const request = executeRequest(
      parts({
        errors: {
          add: { code: "VALIDATION", outcome: "unknown" },
          hold: { code: "VALIDATION" },
        },
      }),
    );
    expect(request.errors).toEqual({
      "node:add": { code: "VALIDATION", outcome: "unknown", effect: "unknown" },
      "node:hold": { code: "VALIDATION" },
    });
  });

  it("sends no failure or test output of a phase its step no longer has", () => {
    const pinned = { ...PIN, output: {} };
    const request = executeRequest(
      parts(
        {
          errors: {
            // add can't be undone any more, hold is only reserved, after left the transaction.
            "add#undo": { code: "UNAVAILABLE" },
            "hold#undo": { code: "UNAVAILABLE" },
            "after#undo": { code: "UNAVAILABLE" },
            "gone#undo": { code: "UNAVAILABLE" },
          },
          pins: {
            "add#undo": pinned,
            "hold#undo": pinned,
            "after#undo": pinned,
            "gone#undo": pinned,
            "hold#confirm": pinned,
          },
        },
        workflowOf(
          [HOLD, { ...ADD, compensate: undefined, irreversible: true }],
          [{ ...ADD, id: "after" }],
        ),
      ),
    );
    expect(request).not.toHaveProperty("errors");
    expect(request.mocks).toEqual({ "node:hold#confirm": {} });
  });

  it("fails a transform inside a transaction without an On error setting, and not after it", () => {
    const guarded = workflowOf([HOLD, SHAPE], [{ ...SHAPE, id: "end-shape" }]);
    Object.assign(guarded.spec.steps[0], {
      onError: "errorOutput",
      errorPath: {
        steps: [{ ...SHAPE, id: "path-shape" }],
        output: { literal: null },
      },
    });
    const request = executeRequest(
      parts(
        {
          errors: {
            shape: { code: "BAD", outcome: "not_started" },
            // The transaction's error path runs after it.
            "path-shape": { code: "BAD", outcome: "not_started" },
            "end-shape": { code: "BAD", outcome: "not_started" },
          },
        },
        guarded,
      ),
    );
    expect(request.errors).toEqual({
      "node:shape": { code: "BAD", outcome: "not_started" },
    });
  });
});

const FAILING: StudioTestData = {
  schemaVersion: 1,
  kind: "weave.studio/testData",
  updatedAt: AT,
  input: { value: { moveId: "M-1" }, source: "manual" },
  errors: {
    add: { code: "REQUEST_REFUSED", effect: "none", source: "manual" },
    "add#undo": { code: "UNAVAILABLE", attempts: 10, source: "manual" },
  },
  pins: { "add#undo": { ...PIN, output: { undone: "PHASE-OUTPUT" } } },
};
/** What a real run must never carry. */
const LEAK = /errors|REQUEST_REFUSED|UNAVAILABLE|PHASE-OUTPUT|#|effect"/;

describe("real runs while the test data holds failures of undo steps", () => {
  it("Execute workflow in an environment starts its run with the test input alone", async () => {
    const input = new ExecutionService({
      testData: () => FAILING,
      inputSchema: () => ({ type: "object" }),
    } as unknown as ExecutionHost).workflowInput();
    const calls: unknown[] = [];
    const answers: unknown[] = [
      {
        id: "ta-1",
        activation_id: "a-ta-1",
        artifact_digest: "d".repeat(64),
        side_effect: "read_only",
        expires_at: "2026-11-25T11:00:00Z",
      },
      { id: "run-1", status: "running" },
    ];
    const api: TestActivationApi = {
      environment: "/env",
      mutate: async <T>(_path: string, _method: string, body: unknown) => {
        calls.push(body);
        return answers.shift() as T;
      },
    };
    const service = new TestActivationService(api, {
      environment: () => ({ id: "e", name: "local" }),
      saveDraft: async () => ({ draftId: "draft-1", draftRevision: 4 }),
      confirm: async () => true,
      now: () => Date.parse("2026-11-25T10:30:00Z"),
    });
    expect((await service.execute(input, {})).ok).toBe(true);
    expect(calls).toHaveLength(2);
    expect(calls[1]).toEqual({ input: { moveId: "M-1" } });
    for (const body of calls) expect(JSON.stringify(body)).not.toMatch(LEAK);
  });

  it("a step test sends the step's own input, never its undo's failure or test output", () => {
    const ctx = {
      step: ADD as unknown as Step,
      workflow: MOVE,
      sample: {
        resolvedInput: () => ({
          state: "available",
          value: { id: "r-7" },
          source: "simulated",
          stale: null,
        }),
      },
      connections: { slot: () => null, devBinding: () => null },
    } as unknown as NdvContext;
    const kind = {
      workflow: MOVE,
      features: [],
      actionContract: () => ({
        apiVersion: "weave/v1alpha1",
        kind: "Action",
        metadata: { name: "add.run", version: "1.0.0" },
        spec: { inputSchema: { type: "object" }, sideEffect: "read_only" },
      }),
      tableContract: () => null,
      workflowContract: () => null,
    } as unknown as KindContext;
    const request = actionKind.real?.request(ctx, kind);
    expect(request).toMatchObject({
      body: { step_id: "add", uses: "add.run@1.0.0", input: { id: "r-7" } },
    });
    expect(JSON.stringify(request)).not.toMatch(LEAK);
  });
});
