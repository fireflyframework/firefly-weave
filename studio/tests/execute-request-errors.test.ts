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
// Execute takes the simulated failures: each goes to the host under its
// mock key (node:<key>) with its code, outcome and attempts, and a key that
// fails sends no pin, no replayed result and no AI turns, since the host
// takes a key as a failure or a mock, never both.
import { describe, expect, it } from "vitest";
import type { Workflow } from "../src/app/model";
import {
  executeRequest,
  type ExecuteParts,
} from "../src/app/editor/state/execute-request";
import type { StudioTestData } from "../src/app/editor/state/test-data";

const WORKFLOW = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "orders", version: "1.0.0" },
  spec: {
    steps: [
      {
        id: "submit",
        kind: "action",
        uses: "crm.submit@1.0.0",
        with: { literal: {} },
        onError: "continue",
      },
      {
        id: "summarize",
        kind: "llm",
        profile: "summarize",
        prompt: { literal: "x" },
        onError: "continue",
      },
      { id: "after", kind: "transform", value: { literal: 1 } },
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
const AT = "2026-10-26T10:00:00Z";

function parts(testData: Partial<StudioTestData>): ExecuteParts {
  return {
    source: "{}",
    format: "json",
    workflow: WORKFLOW,
    definitions: [],
    connected: false,
    testData: {
      schemaVersion: 1,
      kind: "weave.studio/testData",
      updatedAt: AT,
      ...testData,
    },
    results: {
      steps: {
        submit: { output: { replayed: true } },
        after: { output: { replayed: true } },
      },
    } as never,
    sampleInput: {},
    target: { mode: "workflow" },
    now: new Date(AT),
  };
}

describe("an execution with simulated failures", () => {
  it("sends each failure under its mock key, as the platform would report it", () => {
    const request = executeRequest(
      parts({
        errors: {
          submit: {
            code: "REQUEST_REFUSED",
            outcome: "unknown",
            attempts: 3,
            source: "run",
            runId: "8f0f6b0e-6a43-4f0e-9a43-6b1f7a2f9c11",
            runEnvironmentId: "1b0f6b0e-6a43-4f0e-9a43-6b1f7a2f9c12",
          },
          "summarize[2]": { code: "LLM_TIMEOUT", source: "manual" },
        },
      }),
    );
    expect(request.errors).toEqual({
      "node:submit": {
        code: "REQUEST_REFUSED",
        outcome: "unknown",
        attempts: 3,
      },
      "node:summarize[2]": { code: "LLM_TIMEOUT" },
    });
  });

  it("sends no pin, replayed result or AI turns where a step fails", () => {
    const request = executeRequest(
      parts({
        pins: {
          submit: { ...PIN, output: { pinned: true } },
          after: { ...PIN, output: { pinned: true } },
        },
        aiTurns: { summarize: { turns: [{ answer: "kept" }] } },
        errors: {
          submit: { code: "REQUEST_REFUSED" },
          summarize: { code: "LLM_TIMEOUT" },
        },
      }),
    );
    expect(request.mocks).toEqual({});
    expect(request.scripts?.aiTurns).toEqual({});
  });

  it("sends no failure for a step that can't take one any more, or that is gone", () => {
    // after is a transform whose On error stops the run; gone left the draft.
    const request = executeRequest(
      parts({
        errors: {
          after: { code: "BAD_SHAPE", source: "manual" },
          gone: { code: "REQUEST_REFUSED", source: "manual" },
        },
      }),
    );
    // The host would refuse the whole request for them (WV-DEBUG-MOCK-KEY).
    expect(request).not.toHaveProperty("errors");
  });

  it("sends only the failures of the steps Execute step runs", () => {
    const request = executeRequest({
      ...parts({
        errors: {
          submit: { code: "REQUEST_REFUSED" },
          summarize: { code: "LLM_TIMEOUT" },
        },
      }),
      target: { mode: "step", stepId: "submit" },
    });
    expect(request.errors).toEqual({
      "node:submit": { code: "REQUEST_REFUSED" },
    });
  });

  it("sends an item's own pin with its step's failure for every item: the item's pin wins there", () => {
    const request = executeRequest(
      parts({
        pins: { "submit[2]": { ...PIN, output: { kept: true } } },
        errors: { submit: { code: "REQUEST_REFUSED" } },
      }),
    );
    expect(request.mocks).toEqual({ "node:submit[2]": { kept: true } });
    expect(request.errors).toEqual({
      "node:submit": { code: "REQUEST_REFUSED" },
    });
  });
});
