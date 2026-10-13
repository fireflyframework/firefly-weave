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
import type { Json } from "../src/app/forms/core/json";
import { referenceScope } from "../src/app/forms/core/scope";
import {
  SECRET,
  UNAVAILABLE,
  WAITING,
  previewFormula,
  previewText,
  sourceLabel,
  valueTypeLabel,
  type PreviewData,
} from "../src/app/editor/formula/preview";
import { parseFormula } from "../src/app/editor/formula/grammar";

const workflow = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "previews", version: "1.0.0" },
  spec: {
    inputSchema: {
      type: "object",
      properties: {
        amount: { type: "number" },
        email: { type: "string" },
        name: { type: "string" },
        password: { type: "string", "x-secret": true },
        profile: {
          type: "object",
          properties: { token: { type: "string", writeOnly: true } },
        },
      },
    },
    outputSchema: {},
    steps: [
      { id: "check", kind: "transform", value: { literal: { score: 0.5 } } },
      { id: "use", kind: "transform", value: { literal: null } },
      { id: "later", kind: "transform", value: { literal: 1 } },
    ],
    output: { literal: null },
  },
};
const data = (
  input: Json | undefined,
  outputs: Record<string, Json> = {},
): PreviewData => ({
  input: () => input,
  output: (id) => outputs[id],
  stamp: () => [input, outputs],
});
const preview = (text: string, source: PreviewData) => {
  const result = parseFormula(text, {
    features: ["text.concat", "text.join"],
    loops: [],
  });
  if (result.kind !== "ok") throw new Error(text);
  return previewFormula(result.expression, {
    scope: referenceScope(workflow, "use", "/value"),
    inputSchema: workflow.spec.inputSchema,
    data: source,
  });
};
const input = { amount: 1500, email: "ada@example.com", name: "Ada" };

describe("formula previews", () => {
  it("computes the value from the test input", () => {
    expect(
      preview("input.amount > 1000 and exists(input.email)", data(input)),
    ).toEqual({ ok: true, value: true });
    expect(preview("input.amount > 2000", data(input))).toEqual({
      ok: true,
      value: false,
    });
    expect(preview('concat("Order of ", input.amount)', data(input))).toEqual({
      ok: true,
      value: "Order of 1500",
    });
  });
  it("waits for data it doesn't have, and says which", () => {
    expect(preview("input.amount", data(undefined))).toEqual({
      ok: false,
      error: {
        code: WAITING,
        message:
          "Can't compute yet: there's no test input. Add it in the trigger's Test event.",
      },
    });
    expect(preview("steps.check.output.score", data(input))).toEqual({
      ok: false,
      error: {
        code: WAITING,
        message: "Can't compute yet: steps.check has no data.",
      },
    });
    expect(
      preview(
        "steps.check.output.score",
        data(input, { check: { score: 0.9 } }),
      ),
    ).toEqual({ ok: true, value: 0.9 });
    expect(preview("steps.later.output", data(input))).toEqual({
      ok: false,
      error: {
        code: UNAVAILABLE,
        message: "`steps.later` isn't available here.",
      },
    });
  });
  it("never evaluates a formula that reads a secret value", () => {
    for (const text of [
      "input.password",
      'concat(input.password, "x")',
      "input.profile.token",
      "exists(input.password)",
    ])
      expect(
        preview(text, data({ ...input, password: "s3cret" })),
        text,
      ).toEqual({
        ok: false,
        error: {
          code: SECRET,
          message:
            "This formula reads a secret value, so Studio doesn't preview it.",
        },
      });
  });
  it("puts evaluation errors in plain words", () => {
    expect(preview("input.name > 1000", data(input))).toEqual({
      ok: false,
      error: {
        code: "WV-EXPR-TYPE",
        message: "Text can't be compared with a number.",
      },
    });
    expect(preview("input.missing", data(input))).toEqual({
      ok: false,
      error: {
        code: "WV-EXPR-MISSING",
        message: "`input.missing` is missing in the test data.",
      },
    });
    expect(preview("input.amount and true", data(input))).toEqual({
      ok: false,
      error: {
        code: "WV-EXPR-TYPE",
        message: "`and` needs Yes/No values; this one is a number.",
      },
    });
    expect(preview('join(input.name, ",")', data(input))).toEqual({
      ok: false,
      error: { code: "WV-EXPR-TYPE", message: "`join` needs a list first." },
    });
  });
  it("never crashes on a tree nested beyond what workflows allow, and still finds secrets", () => {
    const wrap = (inner: unknown) => {
      let node = inner;
      for (let i = 0; i < 20_000; i++)
        node = { op: { name: "not", args: [node] } };
      return node as never;
    };
    const at = {
      scope: referenceScope(workflow, "use", "/value"),
      inputSchema: workflow.spec.inputSchema,
      data: data({ ...input, password: "s3cret" }),
    };
    expect(previewFormula(wrap({ ref: "/input/password" }), at)).toMatchObject({
      ok: false,
      error: { code: SECRET },
    });
    expect(previewFormula(wrap({ ref: "/input/amount" }), at)).toMatchObject({
      ok: false,
    });
  });
  it("reads __proto__ and constructor as plain data", () => {
    const tricky = JSON.parse(
      '{"__proto__":{"polluted":true},"constructor":"c"}',
    ) as Json;
    expect(preview('ref("/input/__proto__/polluted")', data(tricky))).toEqual({
      ok: true,
      value: true,
    });
    expect(preview('ref("/input/constructor")', data(tricky))).toEqual({
      ok: true,
      value: "c",
    });
    expect(({} as Record<string, unknown>)["polluted"]).toBeUndefined();
  });
  it("shows values in one line, with their type and source", () => {
    expect(previewText("a\nb")).toEqual({ text: "a\nb", truncated: false });
    expect(previewText({ a: [1, true] })).toEqual({
      text: '{"a":[1,true]}',
      truncated: false,
    });
    expect(previewText("x".repeat(250))).toEqual({
      text: "x".repeat(200),
      truncated: true,
    });
    expect(
      [true, 1, 1.5, "a", null, [], {}].map((v) => valueTypeLabel(v as Json)),
    ).toEqual([
      "Yes/No",
      "Whole number",
      "Number",
      "Text",
      "Empty",
      "List",
      "Group",
    ]);
    const parsed = parseFormula("input.amount > 1", {
      features: [],
      loops: [],
    });
    if (parsed.kind !== "ok") throw new Error();
    expect(sourceLabel(parsed.expression, data(input))).toBe("From test input");
    expect(sourceLabel({ literal: 1 }, data(input))).toBe("");
  });
});
