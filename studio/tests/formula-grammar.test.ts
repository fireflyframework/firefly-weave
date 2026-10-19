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
import {
  parseFormula,
  parseTemplate,
  type ParseOptions,
} from "../src/app/editor/formula/grammar";
import { tokenize } from "../src/app/editor/formula/tokens";

const plain: ParseOptions = { features: [], loops: [] };
const text: ParseOptions = {
  features: ["text.concat", "text.join"],
  loops: [],
};
const inLoops: ParseOptions = { features: [], loops: ["notify", "orders"] };
const tree = (source: string, options = plain) => {
  const result = parseFormula(source, options);
  if (result.kind !== "ok") throw new Error(JSON.stringify(result));
  return result.expression;
};
const problem = (source: string, options = plain) => {
  const result = parseFormula(source, options);
  if (result.kind !== "error") throw new Error(`parsed: ${source}`);
  return result.error;
};
const op = (name: string, ...args: unknown[]) => ({ op: { name, args } });
const ref = (pointer: string) => ({ ref: pointer });
const lit = (value: unknown) => ({ literal: value });

describe("formulas as text", () => {
  it("maps references, escaping keys", () => {
    expect(tree("input.amount")).toEqual(ref("/input/amount"));
    expect(tree('input["first name"]')).toEqual(ref("/input/first name"));
    expect(tree("input.items[0]")).toEqual(ref("/input/items/0"));
    expect(tree("steps.check-customer.output.score")).toEqual(
      ref("/steps/check-customer/output/score"),
    );
    expect(tree('steps["lookup.v2"].output')).toEqual(
      ref("/steps/lookup.v2/output"),
    );
    expect(tree('input["a/b"]["a~b"]')).toEqual(ref("/input/a~1b/a~0b"));
    expect(tree("input . first_name")).toEqual(ref("/input/first_name"));
    expect(tree("input.0")).toEqual(ref("/input/0"));
    expect(tree('ref("")')).toEqual(ref(""));
    expect(tree('ref("/x")')).toEqual(ref("/x"));
    expect(tree("input")).toEqual(ref("/input"));
  });
  it("reads the loop roots inside loops", () => {
    expect(tree("item.customerEmail", inLoops)).toEqual(
      ref("/item/customerEmail"),
    );
    expect(tree("index", inLoops)).toEqual(ref("/index"));
    expect(tree("loops.orders.item.id", inLoops)).toEqual(
      ref("/loops/orders/item/id"),
    );
    expect(tree("loops.orders.index", inLoops)).toEqual(
      ref("/loops/orders/index"),
    );
  });
  it("maps literals and builds lists and groups", () => {
    expect(tree("1000")).toEqual(lit(1000));
    expect(tree('"gold"')).toEqual(lit("gold"));
    expect(tree("'gold'")).toEqual(lit("gold"));
    expect(tree("true")).toEqual(lit(true));
    expect(tree("null")).toEqual(lit(null));
    expect(tree("-2.5e-3")).toEqual(lit(-0.0025));
    expect(tree("1e+21")).toEqual(lit(1e21));
    expect(tree("[1, 2,]")).toEqual(lit([1, 2]));
    expect(tree('{"a": 1, b: [true]}')).toEqual(lit({ a: 1, b: [true] }));
    expect(tree("[input.a, 1]")).toEqual({
      array: [ref("/input/a"), lit(1)],
    });
    expect(tree('{ id: input.id, channel: "email" }')).toEqual({
      object: { id: ref("/input/id"), channel: lit("email") },
    });
    expect(tree("array[1, 2]")).toEqual({ array: [lit(1), lit(2)] });
    expect(tree("object{ a: 1 }")).toEqual({ object: { a: lit(1) } });
    expect(tree("array[]")).toEqual({ array: [] });
    expect(tree("[]")).toEqual(lit([]));
    const own = tree("{__proto__: 1}") as { literal: object };
    expect(Object.keys(own.literal)).toEqual(["__proto__"]);
  });
  it("follows the precedence of or, and, not and comparisons", () => {
    expect(tree("not a == b".replace(/\b([ab])\b/g, "input.$1"))).toEqual(
      op("not", op("eq", ref("/input/a"), ref("/input/b"))),
    );
    expect(tree("input.a or input.b and input.c")).toEqual(
      op("or", ref("/input/a"), op("and", ref("/input/b"), ref("/input/c"))),
    );
    expect(tree("input.a and input.b && input.c")).toEqual(
      op("and", ref("/input/a"), ref("/input/b"), ref("/input/c")),
    );
    expect(tree("input.a and (input.b and input.c)")).toEqual(
      op("and", ref("/input/a"), op("and", ref("/input/b"), ref("/input/c"))),
    );
    expect(tree("!input.a || not not input.b")).toEqual(
      op(
        "or",
        op("not", ref("/input/a")),
        op("not", op("not", ref("/input/b"))),
      ),
    );
    // `not (` is the prefix operator: the comparison after the parenthesis belongs to it.
    expect(tree("not (input.a and input.b) == input.c")).toEqual(
      op(
        "not",
        op("eq", op("and", ref("/input/a"), ref("/input/b")), ref("/input/c")),
      ),
    );
    expect(tree("(not (input.a and input.b)) == input.c")).toEqual(
      op(
        "eq",
        op("not", op("and", ref("/input/a"), ref("/input/b"))),
        ref("/input/c"),
      ),
    );
    expect(tree("and(input.a)")).toEqual(op("and", ref("/input/a")));
    expect(tree("eq(1, 2)")).toEqual(op("eq", lit(1), lit(2)));
    for (const [symbol, name] of Object.entries({
      "==": "eq",
      "!=": "ne",
      "<": "lt",
      "<=": "lte",
      ">": "gt",
      ">=": "gte",
    }))
      expect(tree(`1 ${symbol} 2`)).toEqual(op(name, lit(1), lit(2)));
  });
  it("calls the functions", () => {
    expect(tree("exists(input.email)")).toEqual(
      op("exists", ref("/input/email")),
    );
    expect(
      tree('coalesce(steps.lookup.output.name, input.name, "Customer")'),
    ).toEqual(
      op(
        "coalesce",
        ref("/steps/lookup/output/name"),
        ref("/input/name"),
        lit("Customer"),
      ),
    );
    expect(tree('in(input.country, ["ES", "PT"])')).toEqual(
      op("in", ref("/input/country"), lit(["ES", "PT"])),
    );
    expect(tree('join(steps.lookup.output.tags, ", ")', text)).toEqual(
      op("join", ref("/steps/lookup/output/tags"), lit(", ")),
    );
    expect(tree('concat("a", input.b)', text)).toEqual(
      op("concat", lit("a"), ref("/input/b")),
    );
  });
  it("reads a backtick template as concat, keeping a lone placeholder a concat", () => {
    expect(
      tree("`Hello {{ input.name }}, you owe {{ input.amount }}`", text),
    ).toEqual(
      op(
        "concat",
        lit("Hello "),
        ref("/input/name"),
        lit(", you owe "),
        ref("/input/amount"),
      ),
    );
    expect(tree("`{{ input.name }}`", text)).toEqual(
      op("concat", ref("/input/name")),
    );
    expect(tree("`plain`", text)).toEqual(lit("plain"));
    expect(tree("`a \\` b \\{{ c`", text)).toEqual(lit("a ` b {{ c"));
  });
  it("accepts one outer pair of braces, as n8n writes", () => {
    const result = parseFormula("{{ input.amount }}", plain);
    expect(result).toMatchObject({
      kind: "ok",
      expression: ref("/input/amount"),
    });
    expect(parseFormula("  ", plain)).toEqual({ kind: "empty" });
    expect(parseFormula("{{  }}", plain)).toEqual({ kind: "empty" });
  });
  it("records where each part of the tree came from", () => {
    const result = parseFormula("input.a > 1", plain);
    if (result.kind !== "ok") throw new Error();
    const [left, right] = (result.expression as { op: { args: object[] } }).op
      .args;
    expect(result.spans.get(result.expression)).toEqual({ from: 0, to: 11 });
    expect(result.spans.get(left)).toEqual({ from: 0, to: 7 });
    expect(result.spans.get(right)).toEqual({ from: 10, to: 11 });
  });
});

describe("formula errors", () => {
  const cases: [string, ParseOptions, string, string, number, number][] = [
    [
      "input.amount >",
      plain,
      "WV-FORMULA-EOF",
      "The formula ends too early. Add a value after `>`.",
      14,
      14,
    ],
    [
      "input.",
      plain,
      "WV-FORMULA-EOF",
      "The formula ends too early. Add a field name after `.`.",
      6,
      6,
    ],
    [
      "(input.amount))",
      plain,
      "WV-FORMULA-TOKEN",
      "Unexpected `)` at column 15.",
      14,
      15,
    ],
    ['"😀" ]', plain, "WV-FORMULA-TOKEN", "Unexpected `]` at column 5.", 5, 6],
    [
      "amount > 1",
      plain,
      "WV-FORMULA-NAME",
      "Unknown name `amount`. Data references start with `input.` or `steps.`, and inside a loop with `item`, `index` or `loops.`.",
      0,
      6,
    ],
    [
      "sum(1)",
      plain,
      "WV-FORMULA-FUNCTION",
      "Unknown function `sum`. Formulas support exists, coalesce, contains, notContains, in, notIn, startsWith and endsWith.",
      0,
      3,
    ],
    [
      "sum(1)",
      text,
      "WV-FORMULA-FUNCTION",
      "Unknown function `sum`. Formulas support exists, coalesce, contains, notContains, in, notIn, startsWith, endsWith, concat and join.",
      0,
      3,
    ],
    [
      "item.email",
      plain,
      "WV-FORMULA-LOOP",
      "`item` is only available inside a loop.",
      0,
      4,
    ],
    [
      "index > 0",
      plain,
      "WV-FORMULA-LOOP",
      "`index` is only available inside a loop.",
      0,
      5,
    ],
    [
      "loops.batch.item",
      inLoops,
      "WV-FORMULA-LOOP",
      "`loops.batch` is only available inside the loop batch.",
      0,
      11,
    ],
    [
      "exists(1)",
      plain,
      "WV-FORMULA-EXISTS",
      "`exists` takes one data reference, like `exists(input.email)`.",
      0,
      9,
    ],
    [
      "exists(input.a, input.b)",
      plain,
      "WV-FORMULA-EXISTS",
      "`exists` takes one data reference, like `exists(input.email)`.",
      0,
      24,
    ],
    [
      "contains(input.tags)",
      plain,
      "WV-FORMULA-ARITY",
      "`contains` takes 2 values; this has 1.",
      0,
      20,
    ],
    [
      "not(true, false)",
      plain,
      "WV-FORMULA-ARITY",
      "`not` takes 1 value; this has 2.",
      0,
      16,
    ],
    [
      "and()",
      plain,
      "WV-FORMULA-ARITY",
      "`and` takes at least 1 value; this has 0.",
      0,
      5,
    ],
    [
      "1 < 2 < 3",
      plain,
      "WV-FORMULA-CHAIN",
      "Comparisons can't be chained. Use `and`: `a < b and b < c`.",
      6,
      7,
    ],
    [
      "input.a = 1",
      plain,
      "WV-FORMULA-ASSIGN",
      "Use `==` to compare. A single `=` is not an operator.",
      8,
      9,
    ],
    [
      '"abc',
      plain,
      "WV-FORMULA-STRING",
      "This text isn't closed. Add a closing `\"`.",
      0,
      4,
    ],
    [
      "'abc",
      plain,
      "WV-FORMULA-STRING",
      "This text isn't closed. Add a closing `'`.",
      0,
      4,
    ],
    [
      "9007199254740993",
      plain,
      "WV-FORMULA-NUMBER",
      "Whole numbers must be between −9,007,199,254,740,991 and 9,007,199,254,740,991.",
      0,
      16,
    ],
    ["1e400", plain, "WV-FORMULA-NUMBER", "This number is too large.", 0, 5],
    [
      'concat("a")',
      plain,
      "WV-FORMULA-TEMPLATE",
      "`concat` needs a platform that supports text templates (text.concat).",
      0,
      6,
    ],
    [
      'join(input.tags, ",")',
      plain,
      "WV-FORMULA-TEMPLATE",
      "`join` needs a platform that supports joining lists (text.join).",
      0,
      4,
    ],
    [
      "`Hi {{ input.name }}`",
      plain,
      "WV-FORMULA-TEMPLATE",
      "Text templates need a platform that supports them (text.concat).",
      0,
      1,
    ],
    [
      "`Hi {{ input.name }}",
      text,
      "WV-FORMULA-STRING",
      "This template isn't closed. Add a closing `` ` ``.",
      0,
      20,
    ],
    [
      "{a: 1, a: 2}",
      plain,
      "WV-FORMULA-TOKEN",
      "The key `a` appears twice.",
      7,
      8,
    ],
    [
      'ref("input")',
      plain,
      "WV-FORMULA-TOKEN",
      '`ref` needs a JSON Pointer, like ref("/input/name").',
      4,
      11,
    ],
    ['"\\q"', plain, "WV-FORMULA-TOKEN", "Unexpected `\\q` at column 2.", 1, 3],
    [
      "a-b",
      plain,
      "WV-FORMULA-NAME",
      "Unknown name `a`. Data references start with `input.` or `steps.`, and inside a loop with `item`, `index` or `loops.`.",
      0,
      1,
    ],
  ];
  for (const [source, options, code, message, from, to] of cases)
    it(`${code}: ${source}`, () => {
      expect(problem(source, options)).toEqual({ code, message, from, to });
    });
});

describe("templates", () => {
  const template = (source: string, options = text) => {
    const result = parseTemplate(source, options);
    if (result.kind !== "ok") throw new Error(JSON.stringify(result));
    return result.expression;
  };
  it("keeps text, reads placeholders and saves a lone reference as itself", () => {
    expect(template("Hi {{ input.name }}!")).toEqual(
      op("concat", lit("Hi "), ref("/input/name"), lit("!")),
    );
    expect(template("{{ input.name }}")).toEqual(ref("/input/name"));
    expect(template("{{ 42 }}")).toEqual(op("concat", lit(42)));
    expect(template("plain")).toEqual(lit("plain"));
    expect(template("")).toEqual(lit(""));
    expect(template("Keep \\{{ braces }}")).toEqual(lit("Keep {{ braces }}"));
    expect(template('{{ coalesce(input.name, "you") }}')).toEqual(
      op("concat", op("coalesce", ref("/input/name"), lit("you"))),
    );
    expect(template("{{ {a: {b: 1}} }} done")).toEqual(
      op("concat", lit({ a: { b: 1 } }), lit(" done")),
    );
    expect(template('{{ "a" }}b')).toEqual(lit("ab"));
  });
  it("opens the placeholder at the last two braces of a run", () => {
    expect(template("{{{ input.name }}")).toEqual(
      op("concat", lit("{"), ref("/input/name")),
    );
    expect(template("a { b }} c")).toEqual(lit("a { b }} c"));
  });
  it("says when a placeholder isn't closed", () => {
    expect(parseTemplate("Hi {{ input.name ", text)).toEqual({
      kind: "error",
      error: {
        code: "WV-FORMULA-EOF",
        message: "The formula ends too early. Close the placeholder with `}}`.",
        from: 17,
        to: 17,
      },
    });
  });
});

describe("the tokenizer", () => {
  it("reads a segment after a dot and an identifier elsewhere", () => {
    expect(
      tokenize("steps.check-customer.output").map((t) => [t.type, t.text]),
    ).toEqual([
      ["name", "steps"],
      ["punct", "."],
      ["segment", "check-customer"],
      ["punct", "."],
      ["segment", "output"],
    ]);
  });
  it("tracks braces inside a placeholder", () => {
    expect(tokenize("a {{ {x: 1} }} b", "template").map((t) => t.type)).toEqual(
      [
        "text",
        "open",
        "punct",
        "name",
        "punct",
        "number",
        "punct",
        "close",
        "text",
      ],
    );
  });
});

describe("formulas nested too deeply", () => {
  const deep = "This formula nests too deeply.";
  const shapes: [string, string, ParseOptions][] = [
    ["1,000 lists", "[".repeat(1000) + "1" + "]".repeat(1000), plain],
    ["1,000 groups", "{a: ".repeat(1000) + "1" + "}".repeat(1000), plain],
    ["1,000 calls", "coalesce(".repeat(1000) + "1" + ")".repeat(1000), plain],
    [
      "1,000 backtick templates",
      "`{{ ".repeat(1000) + "1" + " }}`".repeat(1000),
      text,
    ],
    ["2,000 parentheses", "(".repeat(2000) + "1" + ")".repeat(2000), plain],
    ["20,000 not", "not ".repeat(20_000) + "true", plain],
    ["20,000 !", "!".repeat(20_000) + "true", plain],
    ["a pasted run of backtick placeholders", "`{{".repeat(22_000), text],
  ];
  for (const [name, source, options] of shapes)
    it(`answers an error, never a crash, for ${name}`, () => {
      const started = performance.now();
      const result = parseFormula(source, options);
      expect(performance.now() - started).toBeLessThan(1000);
      expect(result.kind).toBe("error");
      if (result.kind !== "error") return;
      expect(result.error.code).toBe("WV-FORMULA-TOKEN");
      expect(result.error.message).toBe(deep);
      expect(result.error.from).toBeGreaterThan(0);
      expect(result.error.to).toBeGreaterThan(result.error.from);
    });
  it("answers an error for a template nested too deeply", () => {
    const result = parseTemplate(
      "{{ " + "[".repeat(1000) + "1" + "]".repeat(1000) + " }}",
      text,
    );
    expect(result).toMatchObject({
      kind: "error",
      error: { code: "WV-FORMULA-TOKEN", message: deep },
    });
  });
  it("still reads formulas nested more deeply than a workflow allows", () => {
    expect(tree("[".repeat(100) + "1" + "]".repeat(100))).toBeTruthy();
    expect(tree("not ".repeat(100) + "true")).toBeTruthy();
    expect(tree("(".repeat(100) + "1" + ")".repeat(100))).toEqual(lit(1));
  });
});

describe("more errors in plain words", () => {
  it("asks for an escape instead of a raw line break or tab in text", () => {
    expect(problem('"a\tb"')).toEqual({
      code: "WV-FORMULA-STRING",
      message: "Text can't hold a tab here; write `\\t` instead.",
      from: 2,
      to: 3,
    });
    expect(problem("'a\nb'")).toEqual({
      code: "WV-FORMULA-STRING",
      message: "Text can't hold a line break here; write `\\n` instead.",
      from: 2,
      to: 3,
    });
    expect(problem('"a\u0001"')).toEqual({
      code: "WV-FORMULA-STRING",
      message:
        "Text can't hold this control character; write `\\u0001` instead.",
      from: 2,
      to: 3,
    });
  });
  it("refuses a big number that would be saved as a whole number workflows can't hold", () => {
    const words =
      "This number would be saved as a whole number beyond 9,007,199,254,740,991, which workflows can't hold. Use a smaller number, or 1e21 or more.";
    for (const source of ["1e16", "9007199254740993.0", "-2.5e17", "9.99e20"])
      expect(problem(source), source).toEqual({
        code: "WV-FORMULA-NUMBER",
        message: words,
        from: 0,
        to: source.length,
      });
    expect(tree("1e21")).toEqual(lit(1e21));
    expect(tree("1e15")).toEqual(lit(1e15));
    expect(tree("9007199254740991.0")).toEqual(lit(9007199254740991));
  });
  it("counts the values of not()", () => {
    expect(problem("not()")).toEqual({
      code: "WV-FORMULA-ARITY",
      message: "`not` takes 1 value; this has 0.",
      from: 0,
      to: 5,
    });
  });
  it("refuses text with a broken character escape", () => {
    expect(problem('"\\ud800"')).toEqual({
      code: "WV-FORMULA-STRING",
      message: "This text has a broken character escape.",
      from: 0,
      to: 8,
    });
  });
  it("reads an escaped {{ as the two braces", () => {
    expect(parseTemplate("a \\{{ b", text)).toMatchObject({
      kind: "ok",
      expression: lit("a {{ b"),
    });
    expect(parseTemplate("\\{{{ input.a }}", text)).toMatchObject({
      kind: "ok",
      expression: lit("{{{ input.a }}"),
    });
  });
});

describe("the tokenizer within a range", () => {
  it("stops at the end of the range after braces", () => {
    expect(
      tokenize("{{{", "template", 0, 2).map((t) => [t.type, t.text]),
    ).toEqual([["open", "{{"]]);
    expect(tokenize("`{{{", "formula", 0, 3).length).toBeLessThan(5);
  });
});
