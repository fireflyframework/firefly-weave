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
import { stepOutputSchema } from "../src/app/forms/core/scope";

const workflow = (steps: unknown[]) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "w", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object", properties: { amount: { type: "number" } } },
    outputSchema: { type: "object" },
    steps,
    output: { literal: {} },
  },
});
const closed = (properties: Record<string, unknown>) => ({
  type: "object",
  properties,
  required: Object.keys(properties),
  additionalProperties: false,
});

describe("stepOutputSchema", () => {
  it("infers a transform's output from its value", () => {
    const definition = workflow([
      { id: "shape", kind: "transform", value: { literal: { ok: true } } },
    ]);
    expect(stepOutputSchema(definition, "shape")).toEqual(
      closed({ ok: { type: "boolean" } }),
    );
  });

  it("reads an action's output from its contract and is any value without one", () => {
    const definition = workflow([
      {
        id: "lookup",
        kind: "action",
        uses: "crm.lookup@1.0.0",
        with: { literal: {} },
      },
    ]);
    const output = { type: "object", properties: { id: { type: "string" } } };
    expect(
      stepOutputSchema(definition, "lookup", { actionOutput: () => output }),
    ).toEqual(output);
    expect(stepOutputSchema(definition, "lookup")).toEqual({});
  });

  it("finds steps inside decision paths, even after a step that fails", () => {
    const definition = workflow([
      {
        id: "route",
        kind: "switch",
        cases: [
          {
            when: { literal: true },
            steps: [
              { id: "stop", kind: "fail", code: "no", message: "No" },
              { id: "after", kind: "transform", value: { literal: { n: 1 } } },
            ],
            output: { literal: {} },
          },
        ],
        default: { steps: [], output: { literal: {} } },
      },
    ]);
    expect(stepOutputSchema(definition, "after")).toEqual(
      closed({ n: { type: "integer" } }),
    );
  });

  it("is null for a step the workflow doesn't have", () => {
    expect(stepOutputSchema(workflow([]), "missing")).toBeNull();
  });
});
