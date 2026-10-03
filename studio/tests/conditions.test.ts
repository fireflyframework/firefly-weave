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
// Decision conditions as rule rows (W3-4): rows read and write the
// expression tree the definition language already has.
import { describe, expect, it } from "vitest";
import {
  branchName,
  buildCondition,
  conditionSummary,
  missingConditionText,
  missingConditions,
  parseCondition,
  referenceTitle,
  shortLabel,
  withConditionDiagnostics,
} from "../src/app/designer/conditions";
import { typedValue, valueInput } from "../src/app/designer/condition-editor";

const definition = {
  spec: {
    inputSchema: {
      type: "object",
      properties: {
        amount: { type: "number", title: "Amount" },
        customer: {
          type: "object",
          properties: { tier: { type: "string", title: "Tier" } },
        },
      },
    },
  },
};
const gt = {
  op: { name: "gt", args: [{ ref: "/input/amount" }, { literal: 1000 }] },
};

describe("condition rows", () => {
  it("builds 'amount is greater than 1000' as the serializer writes it", () => {
    expect(
      buildCondition({
        join: "and",
        rows: [{ ref: "/input/amount", operator: "gt", value: 1000 }],
      }),
    ).toEqual({
      op: { name: "gt", args: [{ ref: "/input/amount" }, { literal: 1000 }] },
    });
  });
  it("round-trips every row operator through the expression tree", () => {
    for (const operator of ["eq", "ne", "gt", "gte", "lt", "lte"] as const) {
      const row = { ref: "/input/amount", operator, value: 5 };
      expect(
        parseCondition(buildCondition({ join: "and", rows: [row] })),
      ).toEqual({ join: "and", rows: [row] });
    }
    expect(
      buildCondition({
        join: "and",
        rows: [{ ref: "/input/amount", operator: "exists" }],
      }),
    ).toEqual({ op: { name: "exists", args: [{ ref: "/input/amount" }] } });
    const missing = buildCondition({
      join: "and",
      rows: [{ ref: "/input/amount", operator: "missing" }],
    });
    expect(missing).toEqual({
      op: {
        name: "not",
        args: [{ op: { name: "exists", args: [{ ref: "/input/amount" }] } }],
      },
    });
    expect(parseCondition(missing)).toEqual({
      join: "and",
      rows: [{ ref: "/input/amount", operator: "missing" }],
    });
  });
  it("joins two or more rows with all of (and) or any of (or)", () => {
    const rows = [
      { ref: "/input/amount", operator: "gt" as const, value: 10 },
      { ref: "/input/customer/tier", operator: "eq" as const, value: "gold" },
    ];
    const any = buildCondition({ join: "or", rows });
    expect(any?.["op"]).toMatchObject({ name: "or" });
    expect(parseCondition(any)).toEqual({ join: "or", rows });
  });
  it("leaves out rows that aren't complete, and an empty condition unset", () => {
    expect(buildCondition({ join: "and", rows: [] })).toBeUndefined();
    expect(
      buildCondition({
        join: "and",
        rows: [{ ref: "", operator: "eq", value: "x" }],
      }),
    ).toBeUndefined();
    expect(
      buildCondition({
        join: "and",
        rows: [{ ref: "/input/amount", operator: "gt" }],
      }),
    ).toBeUndefined();
  });
  it("hands anything else to the formula editor", () => {
    expect(parseCondition(undefined)).toEqual({ join: "and", rows: [] });
    expect(parseCondition({ literal: true })).toBeNull();
    expect(
      parseCondition({
        op: { name: "coalesce", args: [{ ref: "/input/a" }] },
      }),
    ).toBeNull();
    expect(
      parseCondition({
        op: {
          name: "eq",
          args: [{ ref: "/input/a" }, { ref: "/input/b" }],
        },
      }),
    ).toBeNull();
  });
});

describe("condition summaries", () => {
  it("names data by its title and operators in words", () => {
    expect(conditionSummary(gt, definition)).toBe("Amount > 1000");
    expect(
      conditionSummary(
        {
          op: {
            name: "eq",
            args: [
              { ref: "/steps/review/output/decision" },
              { literal: "approve" },
            ],
          },
        },
        definition,
      ),
    ).toBe("decision is approve");
    expect(conditionSummary(undefined)).toBe("Condition not set");
    expect(conditionSummary({ literal: true })).toBe("Custom condition");
    expect(referenceTitle("/input/customer/tier", definition)).toBe("Tier");
    expect(referenceTitle("/steps/load", definition)).toBe("load output");
  });
  it("labels branches by condition and the default as Otherwise", () => {
    const decision = { kind: "switch", cases: [{ when: gt }, {}] };
    expect(branchName(decision, "case 1", definition)).toBe("Amount > 1000");
    expect(branchName(decision, "case 2", definition)).toBe(
      "Condition not set",
    );
    expect(branchName(decision, "default", definition)).toBe("Otherwise");
    expect(branchName({ kind: "parallel" }, "first")).toBe("first");
    for (const label of [
      branchName(decision, "case 1", definition),
      branchName(decision, "default"),
    ])
      expect(label).not.toMatch(/^Case \d+$|^Default$/);
  });
  it("shortens long labels at 28 characters", () => {
    const long = "Customer lifetime value > 1000000";
    expect(shortLabel(long)).toHaveLength(28);
    expect(shortLabel(long).endsWith("…")).toBe(true);
    expect(shortLabel("Amount > 1000")).toBe("Amount > 1000");
  });
});

describe("unset conditions", () => {
  const workflow = {
    spec: {
      steps: [
        {
          id: "route",
          kind: "switch",
          cases: [
            { when: gt, steps: [] },
            {
              steps: [
                {
                  id: "inner",
                  kind: "switch",
                  cases: [{ steps: [] }],
                  default: { steps: [] },
                },
              ],
            },
          ],
          default: { steps: [] },
        },
      ],
    },
  };
  it("finds every case without a condition, at any depth", () => {
    expect(missingConditions(workflow)).toEqual([
      {
        stepId: "route",
        caseIndex: 1,
        stepPointer: "/spec/steps/0",
        pointer: "/spec/steps/0/cases/1/when",
      },
      {
        stepId: "inner",
        caseIndex: 0,
        stepPointer: "/spec/steps/0/cases/1/steps/0",
        pointer: "/spec/steps/0/cases/1/steps/0/cases/0/when",
      },
    ]);
  });
  it("reports them by case instead of the compiler's step-wide error", () => {
    const result = withConditionDiagnostics(
      {
        validationOk: false,
        errorCount: 2,
        diagnostics: [
          {
            code: "WV-SCHEMA-INVALID_INSTANCE",
            severity: "error",
            path: "/spec/steps/0",
            message: "Value does not satisfy the declared contract.",
          },
          {
            code: "WV-COMP-UNUSED",
            severity: "warning",
            path: "/spec/steps/1",
          },
        ],
      },
      workflow,
    );
    expect(result.errorCount).toBe(3);
    expect(result.validationOk).toBe(false);
    expect(result.diagnostics.map((d) => (d as { path: string }).path)).toEqual(
      [
        "/spec/steps/0/cases/1/when",
        "/spec/steps/0/cases/1/steps/0/cases/0/when",
        "/spec/steps/1",
      ],
    );
    expect(result.diagnostics[0]).toMatchObject({
      severity: "error",
      message: missingConditionText,
    });
    // Nothing to add: the result is returned as it was.
    const clean = { validationOk: true, errorCount: 0, diagnostics: [] };
    expect(withConditionDiagnostics(clean, { spec: { steps: [] } })).toBe(
      clean,
    );
  });
});

describe("condition values", () => {
  it("enters values in the type of the data the row tests", () => {
    expect(valueInput({ type: "number" })).toEqual({
      kind: "number",
      integer: false,
    });
    expect(valueInput({ type: "integer" })).toEqual({
      kind: "number",
      integer: true,
    });
    expect(valueInput({ type: "boolean" })).toEqual({ kind: "boolean" });
    expect(valueInput({ enum: ["eu", "us"] })).toEqual({
      kind: "choice",
      options: ["eu", "us"],
    });
    expect(valueInput(undefined)).toEqual({ kind: "text" });
    const number = valueInput({ type: "number" });
    expect(typedValue("1000", number, "gt")).toBe(1000);
    expect(typedValue("", number, "gt")).toBeUndefined();
    expect(typedValue("1.5", valueInput({ type: "integer" }), "eq")).toBe(
      undefined,
    );
    expect(typedValue("true", valueInput({ type: "boolean" }), "eq")).toBe(
      true,
    );
    expect(typedValue('"us"', valueInput({ enum: ["eu", "us"] }), "eq")).toBe(
      "us",
    );
    // Unknown data: "greater than" compares numbers, "is" compares text.
    const text = valueInput(undefined);
    expect(typedValue("42", text, "gte")).toBe(42);
    expect(typedValue("42", text, "eq")).toBe("42");
    expect(typedValue("approve", text, "eq")).toBe("approve");
  });
});
