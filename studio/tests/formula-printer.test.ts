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
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parse as parseYaml } from "yaml";
import {
  ARITY,
  SYMBOLS,
  parseFormula,
  type ParseOptions,
} from "../src/app/editor/formula/grammar";
import { printFormula, printRef } from "../src/app/editor/formula/printer";
import { tokenize, type Token } from "../src/app/editor/formula/tokens";
import { workflowTemplates } from "../src/app/templates/catalog";

const everything: ParseOptions = {
  features: ["text.concat", "text.join"],
  loops: ["orders", "notify"],
};
const op = (name: string, ...args: unknown[]) => ({ op: { name, args } });
const ref = (pointer: string) => ({ ref: pointer });
const lit = (value: unknown) => ({ literal: value });
const [a, b, c] = ["/input/a", "/input/b", "/input/c"].map(ref);
const back = (text: string) => {
  const result = parseFormula(text, everything);
  if (result.kind !== "ok")
    throw new Error(`${text}: ${JSON.stringify(result)}`);
  return result.expression;
};
/** Sets a key as an own property, even "__proto__". */
const put = (target: Record<string, unknown>, key: string, value: unknown) =>
  Object.defineProperty(target, key, {
    value,
    enumerable: true,
    writable: true,
    configurable: true,
  });

/** A seeded generator (mulberry32), so failures repeat. */
function random(seed: number): () => number {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const pick = <T>(next: () => number, items: readonly T[]): T =>
  items[Math.floor(next() * items.length)];

const SCALARS = [
  0,
  1,
  -7,
  9007199254740991,
  -9007199254740991,
  2.5,
  0.1,
  1e21,
  1.5e-7,
  "",
  "a",
  "ü",
  "😀",
  'say "hi"',
  "line\nbreak",
  "{{",
  "\\",
  "\u0001",
  true,
  false,
  null,
];
const KEYS = [
  "a",
  "b",
  "1",
  "first name",
  "a/b",
  "__proto__",
  "true",
  "input",
  "ü",
  "x-y",
];
const SEGMENTS = [
  "amount",
  "first name",
  "a/b",
  "a~b",
  "0",
  "12",
  "ü",
  "😀",
  "check-customer",
  "lookup.v2",
  "__proto__",
  "1abc",
  "",
  "x_y",
  "-1",
  "constructor",
];
const POINTERS = [
  "/input",
  "/steps/check/output",
  "/steps/check-customer/output",
  "/steps/lookup.v2/output",
  "/item",
  "/index",
  "/loops/orders/item",
  "/loops/notify/index",
  "/other",
  "",
];
const LOGICAL = ["and", "or", "not", "eq", "ne", "lt", "gte"];
const OPERATORS = Object.keys(ARITY);

function pointer(next: () => number): string {
  let text = pick(next, POINTERS);
  for (let i = Math.floor(next() * 3); i > 0; i--)
    text += "/" + pick(next, SEGMENTS).replace(/~/g, "~0").replace(/\//g, "~1");
  return text;
}
function json(next: () => number, depth: number): unknown {
  const roll = next();
  if (depth >= 3 || roll < 0.6) return pick(next, SCALARS);
  if (roll < 0.8)
    return Array.from({ length: Math.floor(next() * 3) }, () =>
      json(next, depth + 1),
    );
  const value: Record<string, unknown> = {};
  for (const key of KEYS.filter(() => next() < 0.3))
    put(value, key, json(next, depth + 1));
  return value;
}
function tree(next: () => number, depth: number, logical = false): unknown {
  if (depth >= 6 || (!logical && next() < 0.25))
    return next() < 0.5 ? lit(json(next, 0)) : ref(pointer(next));
  const roll = logical ? 1 : next();
  if (roll < 0.07)
    return {
      array: Array.from({ length: Math.floor(next() * 3) }, () =>
        tree(next, depth + 1),
      ),
    };
  if (roll < 0.14) {
    const body: Record<string, unknown> = {};
    for (const key of KEYS.filter(() => next() < 0.3))
      put(body, key, tree(next, depth + 1));
    return { object: body };
  }
  if (roll < 0.2) return lit(json(next, 0));
  const name =
    logical && next() < 0.6 ? pick(next, LOGICAL) : pick(next, OPERATORS);
  if (name === "exists") return op("exists", ref(pointer(next)));
  const [min, max] = ARITY[name];
  const count = min === max ? min : min + Math.floor(next() * 4);
  return op(
    name,
    ...Array.from({ length: count }, () =>
      tree(next, depth + 1, name in SYMBOLS && next() < 0.5),
    ),
  );
}

describe("the canonical printer", () => {
  it("prints the spec's forms", () => {
    expect(printFormula(op("gt", ref("/input/amount"), lit(1000)))).toBe(
      "input.amount > 1000",
    );
    expect(printFormula(op("eq", a, op("and", b, c)))).toBe(
      "input.a == (input.b and input.c)",
    );
    expect(printFormula(op("eq", op("not", a), b))).toBe(
      "(not input.a) == input.b",
    );
    expect(printFormula(op("and", a))).toBe("and(input.a)");
    expect(printFormula(op("not", op("and", a, b)))).toBe(
      "not (input.a and input.b)",
    );
    expect(printFormula(op("not", op("eq", op("and", a, b), c)))).toBe(
      "not (input.a and input.b) == input.c",
    );
    expect(printFormula(op("or", op("and", a, b), c))).toBe(
      "input.a and input.b or input.c",
    );
    expect(printFormula(op("and", op("or", a, b), c))).toBe(
      "(input.a or input.b) and input.c",
    );
    expect(printFormula(op("and", a, op("and", b, c)))).toBe(
      "input.a and (input.b and input.c)",
    );
    expect(printFormula(op("and", a, op("not", b)))).toBe(
      "input.a and not input.b",
    );
    expect(printFormula(op("concat", lit("a"), b))).toBe(
      'concat("a", input.b)',
    );
    expect(printFormula(op("exists", ref("/input/email")))).toBe(
      "exists(input.email)",
    );
    expect(printFormula(lit({ a: 1, "first name": [true, null] }))).toBe(
      '{a: 1, "first name": [true, null]}',
    );
    expect(printFormula({ array: [lit(1), lit([2])] })).toBe("array[1, [2]]");
    expect(printFormula({ array: [a, lit(1)] })).toBe("[input.a, 1]");
    expect(printFormula({ object: { a: lit(1) } })).toBe("object{a: 1}");
    expect(printFormula({ object: { id: a, channel: lit("email") } })).toBe(
      '{id: input.a, channel: "email"}',
    );
    expect(printFormula(lit(1e21))).toBe("1e+21");
    expect(printFormula(lit(2 ** 60))).toBe("1.152921504606847e+18");
    expect(printFormula(lit(-0))).toBe("0");
  });
  it("prints references as paths when it can", () => {
    expect(printRef("/input/first name")).toBe('input["first name"]');
    expect(printRef("/steps/lookup.v2/output")).toBe(
      'steps["lookup.v2"].output',
    );
    expect(printRef("/steps/check-customer/output/score")).toBe(
      "steps.check-customer.output.score",
    );
    expect(printRef("/input/items/0")).toBe("input.items[0]");
    expect(printRef("/input/a~1b/a~0b")).toBe('input["a/b"]["a~b"]');
    expect(printRef("/input/1abc")).toBe('input["1abc"]');
    expect(printRef("/loops/orders/item/id")).toBe("loops.orders.item.id");
    expect(printRef("/x")).toBe('ref("/x")');
    expect(printRef("")).toBe('ref("")');
    expect(printRef("input/x")).toBe('ref("input/x")');
  });
  it("refuses trees outside the language", () => {
    expect(printFormula({ op: { name: "sum", args: [] } })).toBeNull();
    expect(printFormula({ literal: 1, ref: "/x" })).toBeNull();
    expect(
      printFormula({ op: { name: "eq", args: [lit(1), lit(1)], x: 1 } }),
    ).toBeNull();
    expect(printFormula({ literal: Number.NaN })).toBeNull();
    expect(printFormula({ items: [] })).toBeNull();
  });
});

describe("trees nested beyond what formulas show", () => {
  it("prints nothing for them, never a crash", () => {
    let deep: unknown = ref("/input/a");
    for (let i = 0; i < 20_000; i++) deep = op("not", deep);
    expect(printFormula(deep)).toBeNull();
    let shallow: unknown = ref("/input/a");
    for (let i = 0; i < 100; i++) shallow = op("not", shallow);
    expect(printFormula(shallow)).toBe("not ".repeat(100) + "input.a");
  });
  it("prints only what the parser reads back: up to 127 levels", () => {
    const nested = (levels: number) => {
      let tree: unknown = ref("/input/a");
      for (let i = 0; i < levels; i++) tree = op("not", tree);
      return tree;
    };
    const deepest = printFormula(nested(127));
    expect(deepest).not.toBeNull();
    expect(parseFormula(deepest!, everything)).toMatchObject({
      kind: "ok",
      expression: nested(127),
    });
    expect(printFormula(nested(140))).toBeNull();
  });
});

describe("references the printer writes", () => {
  it("writes an index beyond what numbers hold exactly as a quoted key", () => {
    const pointer = "/input/list/9007199254740993";
    expect(printRef(pointer)).toBe('input.list["9007199254740993"]');
    expect(parseFormula(printRef(pointer), everything)).toMatchObject({
      kind: "ok",
      expression: ref(pointer),
    });
    expect(printRef("/input/list/12")).toBe("input.list[12]");
  });
});

describe("round trips", () => {
  it("parses back every one of 10,000 random trees", () => {
    const next = random(20261013);
    const seen = new Set<string>();
    let logicalOperands = 0;
    const failures: string[] = [];
    for (let i = 0; i < 10_000; i++) {
      const original = tree(next, 0);
      const text = printFormula(original);
      JSON.stringify(original, (key, value) => {
        if (value?.op?.name) seen.add(value.op.name);
        if (
          value?.op?.name in SYMBOLS &&
          value.op.args.some(
            (arg: { op?: { name: string } }) =>
              arg?.op &&
              (arg.op.name in SYMBOLS ||
                ["and", "or", "not"].includes(arg.op.name)),
          )
        )
          logicalOperands++;
        return value;
      });
      const result = text === null ? null : parseFormula(text, everything);
      if (
        result?.kind !== "ok" ||
        JSON.stringify(result.expression) !== JSON.stringify(original)
      )
        failures.push(
          `${JSON.stringify(original)} → ${text} → ${JSON.stringify(result)}`,
        );
      else expect(result.expression).toEqual(original);
      if (failures.length >= 5) break;
    }
    expect(failures).toEqual([]);
    expect([...seen].sort()).toEqual([...OPERATORS].sort());
    expect(logicalOperands).toBeGreaterThan(100);
  });

  const SEEDS = [
    "input.amount > 1000 and exists(input.email)",
    'not (input.status == "closed" or input.status == "archived")',
    'coalesce(steps.lookup.output.name, input.name, "Customer")',
    'in(input.country, ["ES", "PT"])',
    '{ customerId: input.id, channel: "email", vip: input.score >= 0.8 }',
    "item.amount > 1000 and exists(item.email)",
    'join(steps.lookup.output.tags, ", ")',
    "input.a or input.b and input.c",
    "(input.a or input.b) and input.c",
    "input.a and (input.b and input.c)",
    "not input.a == input.b",
    "(not input.a) == input.b",
    "not (input.a and input.b) == input.c",
    "not not input.flag",
    "and(input.flag)",
    "or(input.flag, false)",
    "eq(input.a, input.b)",
    'contains(input.tags, "vip")',
    'notContains(input.tags, "blocked")',
    'notIn(input.status, ["closed"])',
    'startsWith(input.email, "admin@")',
    'endsWith(input.email, ".com")',
    'concat("Order ", input.id, " for ", input.name)',
    'steps["lookup.v2"].output',
    'input["first name"]',
    'input["a/b"]["a~b"]',
    "input.items[0].id",
    'ref("/custom/root")',
    'ref("")',
    "loops.orders.item.id",
    "loops.orders.index > 0",
    "index == 0",
    "[1, 2, 3]",
    '{"a": 1, "b": [true, null]}',
    "array[1, 2]",
    "object{ a: 1 }",
    "[input.a, 1]",
    "{ a: input.a, b: { c: input.c } }",
    "-2.5e-3 < input.ratio",
    "1e+21",
    'input.name != "Ada"',
    "input.score <= 0.8 and input.score >= 0.2",
    "exists(steps.check-customer.output.amount)",
    'coalesce(input.nickname, input.name) == "Ada"',
    'in(input.tier, ["gold", "silver"]) or input.vip',
    "not exists(input.email)",
    "input.count > 0 and not (input.count > 100)",
    '"line\\nbreak"',
    '"😀" < input.name',
    'join([1, 2.5, true], "-") == "1-2.5-true"',
  ];

  function respell(seed: string, next: () => number): string {
    const tokens = tokenize(seed);
    const call = (i: number) =>
      tokens[i + 1]?.type === "punct" && tokens[i + 1].text === "(";
    const wordy = (token?: Token) =>
      !!token &&
      (token.type === "name" ||
        token.type === "segment" ||
        token.type === "number");
    let out = "";
    tokens.forEach((token, i) => {
      let text = token.text;
      if (
        token.type === "name" &&
        token.text === "and" &&
        !call(i) &&
        next() < 0.5
      )
        text = "&&";
      else if (
        token.type === "name" &&
        token.text === "or" &&
        !call(i) &&
        next() < 0.5
      )
        text = "||";
      else if (token.type === "name" && token.text === "not" && next() < 0.5)
        text = "!";
      else if (
        token.type === "string" &&
        token.text.startsWith('"') &&
        !/['\\]/.test(token.text) &&
        next() < 0.5
      )
        text = `'${token.text.slice(1, -1)}'`;
      const space =
        i === 0
          ? ""
          : wordy(tokens[i - 1]) && wordy({ ...token, text })
            ? " ".repeat(1 + Math.floor(next() * 2))
            : " ".repeat(Math.floor(next() * 3));
      out += space + text;
    });
    return out;
  }

  it("prints 500 texts in one canonical form", () => {
    expect(SEEDS).toHaveLength(50);
    const next = random(7);
    let checked = 0;
    for (const seed of SEEDS) {
      const meaning = back(seed);
      for (let variant = 0; variant < 10; variant++) {
        const text = variant === 0 ? seed : respell(seed, next);
        const parsed = back(text);
        expect(parsed, text).toEqual(meaning);
        const canonical = printFormula(parsed)!;
        expect(back(canonical), canonical).toEqual(meaning);
        expect(printFormula(back(canonical)), canonical).toBe(canonical);
        checked++;
      }
    }
    expect(checked).toBe(500);
  });

  it("round-trips every expression in the examples and the templates", () => {
    const KEYS = new Set([
      "with",
      "value",
      "when",
      "output",
      "title",
      "context",
      "prompt",
      "items",
      "businessKey",
    ]);
    const TAGS = new Set(["literal", "ref", "object", "array", "op"]);
    const isExpression = (node: Record<string, unknown>) => {
      const keys = Object.keys(node);
      return keys.length === 1 && TAGS.has(keys[0]);
    };
    const collect = (node: unknown, key: string, out: unknown[]) => {
      if (Array.isArray(node)) node.forEach((item) => collect(item, key, out));
      else if (node && typeof node === "object") {
        const record = node as Record<string, unknown>;
        if (KEYS.has(key) && isExpression(record)) out.push(record);
        else
          for (const [child, value] of Object.entries(record))
            if (!/Schema$/.test(child)) collect(value, child, out);
      }
      return out;
    };
    const root = resolve(import.meta.dirname, "../../examples");
    const sources = [
      ...readdirSync(root, { recursive: true })
        .map(String)
        .filter((file) => file.endsWith(".yaml"))
        .map((file) => readFileSync(resolve(root, file), "utf8")),
      ...workflowTemplates.map((template) => template.yaml),
    ];
    let count = 0;
    for (const source of sources) {
      const document = parseYaml(source);
      if (document?.kind !== "Workflow") continue;
      for (const expression of collect(document, "", [])) {
        const text = printFormula(expression);
        expect(text, JSON.stringify(expression)).not.toBeNull();
        expect(back(text!), text!).toEqual(expression);
        count++;
      }
    }
    expect(count).toBeGreaterThan(20);
  });
});
