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
import { checkFormula, type CheckScope } from "../src/app/editor/formula/check";
import { parseFormula } from "../src/app/editor/formula/grammar";
import { referenceScope, schemaChild } from "../src/app/forms/core/scope";

const workflow = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "checks", version: "1.0.0" },
  spec: {
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string" },
        amount: { type: "number" },
        flag: { type: "boolean" },
        status: { type: "string", enum: ["open", "closed"] },
        details: { type: "object" },
      },
      required: ["name"],
      additionalProperties: false,
    },
    outputSchema: {},
    steps: [
      {
        id: "check",
        kind: "transform",
        value: { literal: { score: 0.5, tier: "gold" } },
      },
      { id: "use", kind: "transform", value: { literal: null } },
      { id: "later", kind: "transform", value: { literal: 1 } },
    ],
    output: { literal: null },
  },
};
const scope: CheckScope = {
  scope: referenceScope(workflow, "use", "/value"),
  stepIds: new Set(["check", "use", "later"]),
  inputSchema: workflow.spec.inputSchema,
};
const warnings = (text: string, at: CheckScope = scope) => {
  const result = parseFormula(text, {
    features: ["text.concat", "text.join"],
    loops: ["orders"],
  });
  if (result.kind !== "ok") throw new Error(text);
  return checkFormula(result.expression, at).map(({ code, message }) => ({
    code,
    message,
  }));
};

describe("formula warnings", () => {
  it("says nothing about formulas that fit", () => {
    expect(
      warnings(
        'input.amount > 1000 and exists(input.name) and input.status == "open"',
      ),
    ).toEqual([]);
    expect(warnings("steps.check.output.score >= 0.5")).toEqual([]);
    expect(warnings('startsWith(input.name, "A")')).toEqual([]);
    expect(warnings("input.details.anything")).toEqual([]);
  });
  it("warns about steps that run later, or aren't steps", () => {
    expect(warnings("steps.later.output")).toEqual([
      {
        code: "WV-FORMULA-SCOPE",
        message:
          "`steps.later` isn't available here: it runs later or on another path.",
      },
    ]);
    expect(warnings("steps.nope.output")).toEqual([
      {
        code: "WV-FORMULA-SCOPE",
        message: "`steps.nope` isn't a step of this workflow.",
      },
    ]);
  });
  it("warns about fields a closed schema doesn't have", () => {
    expect(warnings("input.amout > 1")).toEqual([
      {
        code: "WV-FORMULA-FIELD",
        message: "`input.amout` isn't a field of the workflow input.",
      },
    ]);
    expect(warnings("steps.check.foo")).toEqual([
      {
        code: "WV-FORMULA-FIELD",
        message:
          "`steps.check.foo` isn't part of a step's data; read `steps.check.output`.",
      },
    ]);
  });
  it("warns about types that don't fit an operator", () => {
    expect(warnings("input.flag and input.name")).toEqual([
      {
        code: "WV-FORMULA-TYPE",
        message: "`and` needs Yes/No values; `input.name` is Text.",
      },
    ]);
    expect(warnings("input.name > 1000")).toEqual([
      {
        code: "WV-FORMULA-TYPE",
        message:
          "`>` needs two numbers or two texts; `input.name` is Text and `1000` is Whole number.",
      },
    ]);
    expect(warnings('startsWith(input.amount, "1")')).toEqual([
      {
        code: "WV-FORMULA-TYPE",
        message: "`startsWith` needs text; `input.amount` is Number.",
      },
    ]);
    expect(warnings("not input.amount")).toEqual([
      {
        code: "WV-FORMULA-TYPE",
        message: "`not` needs Yes/No values; `input.amount` is Number.",
      },
    ]);
  });
  it("reads a loop item's schema from the loop's entries", () => {
    const inLoop: CheckScope = {
      ...scope,
      loopEntries: [
        {
          ref: "/item",
          schema: {
            type: "object",
            properties: { id: { type: "string" } },
            additionalProperties: false,
          },
        },
        { ref: "/index", schema: { type: "integer" } },
      ],
    };
    const result = parseFormula("item.idd and index", {
      features: [],
      loops: ["orders"],
    });
    if (result.kind !== "ok") throw new Error();
    expect(
      checkFormula(result.expression, inLoop).map((w) => w.message),
    ).toEqual([
      "`item.idd` isn't a field of the current item.",
      "`and` needs Yes/No values; `index` is Whole number.",
    ]);
  });
  describe("a step's error", () => {
    const handling = {
      apiVersion: "weave/v1alpha1",
      kind: "Workflow",
      metadata: { name: "handling", version: "1.0.0" },
      spec: {
        inputSchema: { type: "object" },
        outputSchema: {},
        steps: [
          {
            id: "lookup",
            kind: "transform",
            value: { literal: 1 },
            onError: "continue",
          },
          {
            id: "submit",
            kind: "transform",
            value: { literal: 1 },
            onError: "errorOutput",
            errorPath: {
              steps: [
                { id: "notify", kind: "transform", value: { literal: null } },
              ],
              output: { literal: null },
            },
          },
          { id: "after", kind: "transform", value: { literal: null } },
        ],
        output: { literal: null },
      },
    };
    const at = (stepId: string): CheckScope => ({
      scope: referenceScope(handling, stepId, "/value"),
      stepIds: new Set(["lookup", "submit", "notify", "after"]),
      inputSchema: {},
    });

    it("is readable inside the step's own error path", () => {
      expect(warnings("steps.submit.error.code", at("notify"))).toEqual([]);
    });
    it("is readable after a step that continues on failure", () => {
      expect(warnings("steps.lookup.error.code", at("after"))).toEqual([]);
    });
    it("has the fields and types of the step error object", () => {
      expect(warnings("steps.submit.error.attempts > 2", at("notify"))).toEqual(
        [],
      );
      expect(warnings("steps.lookup.error.cod", at("after"))).toEqual([
        {
          code: "WV-FORMULA-FIELD",
          message: "`steps.lookup.error.cod` isn't a field of lookup's error.",
        },
      ]);
      expect(warnings("steps.submit.error.code > 2", at("notify"))).toEqual([
        {
          code: "WV-FORMULA-TYPE",
          message:
            "`>` needs two numbers or two texts; `steps.submit.error.code` is Text and `2` is Whole number.",
        },
      ]);
    });
    it("isn't there for a step that stops the run when it fails", () => {
      expect(warnings("steps.check.error.code")).toEqual([
        {
          code: "WV-FORMULA-FIELD",
          message:
            "`steps.check` records an error only when its On error setting is Continue or Continue with error output.",
        },
      ]);
    });
    it("replaces the step's output inside its own error path", () => {
      expect(warnings("steps.submit.output", at("notify"))).toEqual([
        {
          code: "WV-FORMULA-SCOPE",
          message:
            "`steps.submit` has no output inside its own error path; read `steps.submit.error`.",
        },
      ]);
    });
  });
  it("never crashes on a tree nested beyond what workflows allow", () => {
    let deep: unknown = { ref: "/input/amout" };
    for (let i = 0; i < 20_000; i++)
      deep = { op: { name: "not", args: [deep] } };
    expect(() => checkFormula(deep, scope)).not.toThrow();
  });
  it("finds a key below a schema, or says why not", () => {
    const closed = {
      type: "object",
      properties: { a: { type: "string" } },
      additionalProperties: false,
    };
    expect(schemaChild(closed, closed, "a")).toEqual({
      kind: "found",
      schema: { type: "string" },
    });
    expect(schemaChild(closed, closed, "b")).toEqual({ kind: "absent" });
    expect(schemaChild({ type: "object" }, {}, "b")).toEqual({
      kind: "unknown",
    });
    expect(
      schemaChild({ type: "array", items: { type: "number" } }, {}, "3"),
    ).toEqual({
      kind: "found",
      schema: { type: "number" },
    });
  });
});
