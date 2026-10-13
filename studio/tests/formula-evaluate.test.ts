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
// Studio previews formulas in the browser; every result must be the one the
// platform computes. The corpus is written by scripts/studio_expression_corpus.py
// from the platform's evaluator.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  AMBIGUOUS,
  DEFAULT_LIMITS,
  comparePoints,
  evaluate,
  pythonFloatText,
  textOf,
  type Limits,
} from "../src/app/editor/formula/evaluate";

type Case = {
  name: string;
  expression: unknown;
  context: number | unknown;
  limits?: Record<string, number>;
  result: { value: unknown } | { error: { code: string; path: string } };
};
const corpus = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/expression-corpus.json"),
    "utf8",
  ),
) as {
  limits: Record<string, number>;
  contexts: unknown[];
  floatText: { value: number; text: string }[];
  ambiguous: Omit<Case, "result">[];
  cases: Case[];
};
const camel: Record<string, keyof Limits> = {
  max_depth: "maxDepth",
  max_expression_nodes: "maxExpressionNodes",
  max_document_nodes: "maxDocumentNodes",
  max_payload_bytes: "maxPayloadBytes",
};
const limitsOf = (overrides: Record<string, number> = {}): Limits => {
  const limits = { ...DEFAULT_LIMITS };
  for (const [key, value] of Object.entries(overrides))
    limits[camel[key]] = value;
  return limits;
};

describe("the formula evaluator", () => {
  it("uses the platform's default limits", () => {
    expect(limitsOf()).toEqual(limitsOf(corpus.limits));
    expect(DEFAULT_LIMITS).toEqual({
      maxDepth: 32,
      maxExpressionNodes: 10_000,
      maxDocumentNodes: 100_000,
      maxPayloadBytes: 1_048_576,
    });
  });

  it("evaluates every case of the corpus exactly as the platform does", () => {
    expect(corpus.cases.length).toBeGreaterThanOrEqual(2000);
    const wrong: string[] = [];
    for (const item of corpus.cases) {
      const context =
        typeof item.context === "number"
          ? corpus.contexts[item.context]
          : item.context;
      const result = evaluate(item.expression, context, limitsOf(item.limits));
      const expected =
        "value" in item.result
          ? { ok: true, value: item.result.value }
          : { ok: false, ...item.result.error };
      const actual = result.ok
        ? result
        : { ok: false, code: result.code, path: result.path };
      try {
        expect(actual).toEqual(expected);
      } catch {
        wrong.push(
          `${item.name}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`,
        );
      }
    }
    expect(wrong).toEqual([]);
  });

  it("refuses every case whose result depends on how numbers are read", () => {
    expect(corpus.ambiguous.length).toBeGreaterThanOrEqual(20);
    const answered: string[] = [];
    for (const item of corpus.ambiguous) {
      const context =
        typeof item.context === "number"
          ? corpus.contexts[item.context]
          : item.context;
      const result = evaluate(item.expression, context, limitsOf(item.limits));
      if (result.ok || result.code !== AMBIGUOUS)
        answered.push(`${item.name}: ${JSON.stringify(result)}`);
    }
    expect(answered).toEqual([]);
  });

  it("writes floats as Python does, for the byte budget", () => {
    for (const { value, text } of corpus.floatText)
      expect(pythonFloatText(value), String(value)).toBe(text);
  });

  it("orders text by code point, not by UTF-16 unit", () => {
    expect(comparePoints("￿", "😀")).toBeLessThan(0);
    expect("￿" < "😀").toBe(false); // what plain JavaScript would answer
    expect(comparePoints("a", "ab")).toBeLessThan(0);
    expect(comparePoints("😀", "😀")).toBe(0);
  });

  it("formats numbers and Yes/No values for concat and join", () => {
    expect(
      [2.5, 1e21, 0.1, -0, true, false, "x"].map((v) => textOf(v)),
    ).toEqual(["2.5", "1e+21", "0.1", "0", "true", "false", "x"]);
    expect(textOf(null)).toBeNull();
    expect(textOf([1])).toBeNull();
  });

  it("refuses to preview numbers Studio can't read exactly", () => {
    // 2**53 may be a whole number (the platform rejects it) or a float (the
    // platform accepts it); JavaScript can't tell which.
    const big = evaluate({ ref: "/input/n" }, { input: { n: 2 ** 53 } });
    expect(big).toMatchObject({ ok: false, code: AMBIGUOUS });
    // 1500 is four bytes as a whole number and six as 1500.0.
    const near = evaluate(
      { array: [{ literal: 1500 }] },
      { input: {} },
      { ...DEFAULT_LIMITS, maxPayloadBytes: 7 },
    );
    expect(near).toMatchObject({ ok: false, code: AMBIGUOUS });
  });

  it("refuses near the budget wherever a float reading could change the answer", () => {
    const tight = (bytes: number) => ({
      ...DEFAULT_LIMITS,
      maxPayloadBytes: bytes,
    });
    // The literal alone: "[1500]" is six bytes, "[1500.0]" eight.
    expect(evaluate({ literal: [1500] }, {}, tight(7))).toMatchObject({
      ok: false,
      code: AMBIGUOUS,
    });
    expect(evaluate({ literal: [1500] }, {}, tight(8))).toEqual({
      ok: true,
      value: [1500],
    });
    // A negative zero is "0" or "-0.0": three bytes apart.
    expect(evaluate({ literal: -0 }, {}, tight(3))).toMatchObject({
      ok: false,
      code: AMBIGUOUS,
    });
    expect(evaluate({ literal: -0 }, {}, tight(4))).toMatchObject({
      ok: true,
    });
    // As floats, the first item already fills the budget; read as whole
    // numbers, the second would fail on its own, with another error.
    expect(
      evaluate({ array: [{ ref: "/l" }, { ref: "/m" }] }, { l: [1] }, tight(6)),
    ).toMatchObject({ ok: false, code: AMBIGUOUS });
    expect(
      evaluate(
        { object: { a: { ref: "/l" }, b: { ref: "/m" } } },
        { l: [1] },
        tight(10),
      ),
    ).toMatchObject({ ok: false, code: AMBIGUOUS });
  });

  it("reads __proto__ and constructor as plain data and never runs code", () => {
    const context = JSON.parse(
      '{"input":{"__proto__":{"polluted":true},"constructor":"c"}}',
    );
    expect(evaluate({ ref: "/input/__proto__/polluted" }, context)).toEqual({
      ok: true,
      value: true,
    });
    expect(evaluate({ ref: "/input/toString" }, context)).toMatchObject({
      ok: false,
      code: "WV-EXPR-MISSING",
    });
    const built = evaluate(
      { object: { __proto__: { literal: 1 } } } as unknown,
      { input: {} },
    );
    // An object literal's __proto__ key sets the prototype in JavaScript: the
    // evaluator only sees an empty group, as JSON.parse would not.
    expect(built).toEqual({ ok: true, value: {} });
    expect(({} as Record<string, unknown>)["polluted"]).toBeUndefined();
  });

  it("names the operand types of a type error", () => {
    expect(
      evaluate(
        { op: { name: "gt", args: [{ literal: "a" }, { literal: 1 }] } },
        { input: {} },
      ),
    ).toEqual({
      ok: false,
      code: "WV-EXPR-TYPE",
      path: "",
      types: ["string", "integer"],
    });
  });
});
