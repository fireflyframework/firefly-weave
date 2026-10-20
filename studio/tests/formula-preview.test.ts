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
  formulaPreviews,
  previewFormula,
  referencesOf,
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
  it("never evaluates a formula that reads a secret in a step's output", () => {
    const scope = referenceScope(workflow, "use", "/value");
    const at = {
      scope: {
        ...scope,
        steps: scope.steps.map((step) =>
          step.id === "check"
            ? {
                ...step,
                schema: {
                  type: "object",
                  properties: { pin: { type: "string", "x-secret": true } },
                },
              }
            : step,
        ),
      },
      inputSchema: workflow.spec.inputSchema,
      data: data(input, { check: { pin: "1234", score: 1 } }),
    };
    expect(previewFormula({ ref: "/steps/check/output/pin" }, at)).toEqual({
      ok: false,
      error: {
        code: SECRET,
        message:
          "This formula reads a secret value, so Studio doesn't preview it.",
      },
    });
    expect(previewFormula({ ref: "/steps/check/output/score" }, at)).toEqual({
      ok: true,
      value: 1,
    });
  });
  it("reads an object field named literal as a field, not as a literal", () => {
    expect(
      referencesOf({ object: { literal: { ref: "/steps/check/output" } } }),
    ).toEqual(["/steps/check/output"]);
  });
  it("reads a literal that looks like a reference as data, not as a read", () => {
    const literal = { literal: { ref: "/input/password" } };
    expect(referencesOf(literal)).toEqual([]);
    expect(
      previewFormula(literal, {
        scope: referenceScope(workflow, "use", "/value"),
        inputSchema: workflow.spec.inputSchema,
        data: data(input),
      }),
    ).toEqual({ ok: true, value: { ref: "/input/password" } });
  });
  it("waits for the loop to run before previewing the current item", () => {
    for (const pointer of ["/item/name", "/index", "/loops/each/item"])
      expect(
        previewFormula(
          { ref: pointer },
          {
            scope: referenceScope(workflow, "use", "/value"),
            inputSchema: workflow.spec.inputSchema,
            data: data(input),
          },
        ),
        pointer,
      ).toEqual({
        ok: false,
        error: {
          code: WAITING,
          message:
            "Can't compute yet: the current item exists only while the loop runs.",
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
  it("previews a tree nested far beyond what workflows allow through step details' hook without crashing", () => {
    let deep: unknown = { ref: "/input/amount" };
    for (let i = 0; i < 20_000; i++)
      deep = { op: { name: "not", args: [deep] } };
    const hook = formulaPreviews(() => data(input));
    const ctx = {
      step: { id: "use" },
      workflow,
      scope: () => referenceScope(workflow, "use", "/value"),
    } as never;
    const first = hook.preview(deep as never, ["value"], ctx);
    expect(first.ok).toBe(false);
    // The same deep tree again: computed again, not looked up by a key.
    expect(hook.preview(deep as never, ["value"], ctx)).toEqual(first);
    // Ordinary trees are still cached.
    const amount = { ref: "/input/amount" } as never;
    expect(hook.preview(amount, ["value"], ctx)).toEqual({
      ok: true,
      value: 1500,
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
    const pinned = { ref: "/steps/lookup/output/tier" };
    expect(sourceLabel(pinned, data(input, { lookup: { tier: "a" } }))).toBe(
      "From pinned output",
    );
    expect(sourceLabel(pinned, data(input))).toBe("");
  });
});
