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
import { locateDiagnostic } from "../src/app/designer/diagnostic-location";
import { WORKFLOW } from "../src/app/forms/core/scope";

const step = (id: string, kind = "transform", extra = {}) => ({
  id,
  kind,
  value: { literal: {} },
  ...extra,
});
const branch = (steps: unknown[]) => ({ steps, output: { literal: {} } });
const definition = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "probe", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    connections: { crm: { connector: "weave-http@2.0.0" } },
    steps: [
      step("check", "action", { uses: "crm.lookup@1.0.0", with: {} }),
      step("route", "switch", {
        cases: [
          { when: { literal: true }, ...branch([step("a1"), step("a2")]) },
          {
            when: { literal: false },
            ...branch([
              step("fan", "parallel", {
                concurrency: 2,
                branches: {
                  left: branch([step("l1")]),
                  "with/slash": branch([step("s1"), step("s2")]),
                },
              }),
            ]),
          },
        ],
        default: branch([step("d1"), step("d2"), step("d3")]),
      }),
      step("done"),
    ],
    output: { ref: "/steps/done/output" },
  },
};
const at = (pointer: string | undefined) =>
  locateDiagnostic(pointer, definition);

describe("diagnostic pointers resolve to the step and field to open", () => {
  const cases: [
    string | undefined,
    {
      stepId: string;
      fieldPath: string;
      field: (string | number)[];
      dataPath?: string[];
      exact?: boolean;
      stepPointer?: string;
    },
  ][] = [
    [
      "/spec/steps/0",
      {
        stepId: "check",
        fieldPath: "",
        field: [],
        stepPointer: "/spec/steps/0",
      },
    ],
    [
      "/spec/steps/0/uses",
      { stepId: "check", fieldPath: "/uses", field: ["uses"] },
    ],
    [
      "/spec/steps/0/with/object/parameters/object/customerId/ref",
      {
        stepId: "check",
        fieldPath: "/with/object/parameters/object/customerId/ref",
        field: ["with"],
        dataPath: ["parameters", "customerId"],
      },
    ],
    [
      "/spec/steps/0/with/literal/tags/2",
      {
        stepId: "check",
        fieldPath: "/with/literal/tags/2",
        field: ["with"],
        dataPath: ["tags", "2"],
      },
    ],
    [
      "/spec/steps/0/with/object/items/array/1/op/args/0",
      {
        stepId: "check",
        fieldPath: "/with/object/items/array/1/op/args/0",
        field: ["with"],
        dataPath: ["items", "1"],
      },
    ],
    [
      "/spec/steps/1/cases/0/when",
      {
        stepId: "route",
        fieldPath: "/cases/0/when",
        field: ["cases", 0, "when"],
      },
    ],
    [
      "/spec/steps/1/cases/0/steps/1/value",
      {
        stepId: "a2",
        fieldPath: "/value",
        field: ["value"],
        stepPointer: "/spec/steps/1/cases/0/steps/1",
      },
    ],
    [
      "/spec/steps/1/cases/1/output/object/total",
      {
        stepId: "route",
        fieldPath: "/cases/1/output/object/total",
        field: ["cases", 1, "output"],
        dataPath: ["total"],
      },
    ],
    [
      "/spec/steps/1/default/steps/2/value/literal",
      {
        stepId: "d3",
        fieldPath: "/value/literal",
        field: ["value"],
        dataPath: [],
      },
    ],
    [
      "/spec/steps/1/default/output",
      {
        stepId: "route",
        fieldPath: "/default/output",
        field: ["default", "output"],
      },
    ],
    [
      // A parallel group nested inside a decision case.
      "/spec/steps/1/cases/1/steps/0/branches/left/steps/0/value/object/x/ref",
      {
        stepId: "l1",
        fieldPath: "/value/object/x/ref",
        field: ["value"],
        dataPath: ["x"],
        stepPointer: "/spec/steps/1/cases/1/steps/0/branches/left/steps/0",
      },
    ],
    [
      "/spec/steps/1/cases/1/steps/0/branches/with~1slash/steps/1/value",
      { stepId: "s2", fieldPath: "/value", field: ["value"] },
    ],
    [
      "/spec/steps/1/cases/1/steps/0/branches/with~1slash/output",
      {
        stepId: "fan",
        fieldPath: "/branches/with~1slash/output",
        field: ["branches", "with/slash", "output"],
      },
    ],
    [
      "/spec/steps/1/cases/1/steps/0/concurrency",
      { stepId: "fan", fieldPath: "/concurrency", field: ["concurrency"] },
    ],
    [
      "/spec/inputSchema/properties/amount/type",
      {
        stepId: WORKFLOW,
        fieldPath: "/spec/inputSchema/properties/amount/type",
        field: ["spec", "inputSchema"],
      },
    ],
    [
      "/spec/outputSchema",
      {
        stepId: WORKFLOW,
        fieldPath: "/spec/outputSchema",
        field: ["spec", "outputSchema"],
      },
    ],
    [
      "/spec/output/ref",
      {
        stepId: WORKFLOW,
        fieldPath: "/spec/output/ref",
        field: ["spec", "output"],
        dataPath: [],
      },
    ],
    [
      "/spec/connections/crm/connector",
      {
        stepId: WORKFLOW,
        fieldPath: "/spec/connections/crm/connector",
        field: ["spec", "connections"],
        dataPath: ["crm"],
      },
    ],
    [
      "/metadata/version",
      {
        stepId: WORKFLOW,
        fieldPath: "/metadata/version",
        field: ["metadata", "version"],
      },
    ],
    ["", { stepId: WORKFLOW, fieldPath: "", field: [] }],
    [undefined, { stepId: WORKFLOW, fieldPath: "", field: [] }],
    [
      // A stale pointer lands on the nearest step that still exists.
      "/spec/steps/1/cases/7/steps/0/value",
      {
        stepId: "route",
        fieldPath: "/cases/7/steps/0/value",
        field: ["cases", 7],
        exact: false,
      },
    ],
    [
      // The step list itself (e.g. "no steps") is a workflow setting.
      "/spec/steps",
      {
        stepId: WORKFLOW,
        fieldPath: "/spec/steps",
        field: ["spec", "steps"],
      },
    ],
    [
      "/spec/steps/99/value",
      {
        stepId: WORKFLOW,
        fieldPath: "/spec/steps/99/value",
        field: ["spec", "steps"],
        exact: false,
      },
    ],
  ];
  for (const [pointer, expected] of cases)
    it(`${pointer === undefined ? "(no path)" : pointer || "(root)"}`, () => {
      expect(at(pointer)).toMatchObject({
        dataPath: [],
        exact: true,
        ...expected,
      });
    });
  it("works without a definition by pointing at the workflow", () => {
    expect(locateDiagnostic("/spec/steps/0/value", null)).toMatchObject({
      stepId: WORKFLOW,
      exact: false,
    });
    expect(locateDiagnostic("not a pointer", definition)).toMatchObject({
      stepId: WORKFLOW,
      fieldPath: "",
      exact: false,
    });
  });
});
