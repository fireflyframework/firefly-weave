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
// Simulated failures go only to the Studio host's simulation: a workflow
// executed in an environment (a draft test activation) and a step test send
// the test data's input and the step's resolved input, never an `errors`
// section or a failure's code, even while the test data holds failures.
import { describe, expect, it } from "vitest";
import { actionKind } from "../src/app/editor/ndv/kinds/builtin";
import type { KindContext, NdvContext } from "../src/app/editor/ndv/registry";
import {
  ExecutionService,
  type ExecutionHost,
} from "../src/app/editor/state/execution-service";
import type { TestActivationApi } from "../src/app/editor/state/test-activation-client";
import { TestActivationService } from "../src/app/editor/state/test-activation-service";
import type { StudioTestData } from "../src/app/editor/state/test-data";
import type { Step, Workflow } from "../src/app/model";

const FAILING: StudioTestData = {
  schemaVersion: 1,
  kind: "weave.studio/testData",
  updatedAt: "2026-11-22T10:00:00Z",
  input: { value: { region: "eu" } },
  errors: {
    submit: { code: "REQUEST_REFUSED", source: "manual" },
    "submit[2]": { code: "REQUEST_REFUSED", outcome: "unknown" },
  },
};
const SUBMIT = {
  id: "submit",
  kind: "action",
  uses: "requests.submit@1.0.0",
  with: { object: {} },
  onError: "errorOutput",
  errorPath: { steps: [], output: { literal: null } },
} as unknown as Step;
/** What a real run must never carry. */
const LEAK = /errors|REQUEST_REFUSED/;

describe("real runs while the test data holds simulated failures", () => {
  it("Execute workflow in an environment starts its run with the test input alone", async () => {
    // The input Execute workflow in <environment> sends, as the editor reads it.
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
        expires_at: "2026-11-22T11:00:00Z",
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
      now: () => Date.parse("2026-11-22T10:30:00Z"),
    });
    const outcome = await service.execute(input, {});
    expect(outcome.ok).toBe(true);
    expect(calls).toHaveLength(2);
    expect(calls[1]).toEqual({ input: { region: "eu" } });
    for (const body of calls) expect(JSON.stringify(body)).not.toMatch(LEAK);
  });

  it("a step test sends the step's resolved input, never its simulated failure", () => {
    const workflow = {
      apiVersion: "weave/v1alpha1",
      kind: "Workflow",
      metadata: { name: "request-intake", version: "1.0.0" },
      spec: { steps: [SUBMIT] },
    } as unknown as Workflow;
    const ctx = {
      step: SUBMIT,
      workflow,
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
      workflow,
      features: [],
      actionContract: () => ({
        apiVersion: "weave/v1alpha1",
        kind: "Action",
        metadata: { name: "requests.submit", version: "1.0.0" },
        spec: { inputSchema: { type: "object" }, sideEffect: "read_only" },
      }),
      tableContract: () => null,
      workflowContract: () => null,
    } as unknown as KindContext;
    const request = actionKind.real?.request(ctx, kind);
    expect(request).toMatchObject({
      body: { step_id: "submit", input: { id: "r-7" } },
    });
    expect(JSON.stringify(request)).not.toMatch(LEAK);
  });
});
