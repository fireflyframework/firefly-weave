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
  COLUMN_TYPE_LABELS,
  columnTypeOf,
  parseCell,
  printCell,
  schemaOfColumnType,
  type Column,
} from "../src/app/editor/decision-grid/cell-grammar";

const amount: Column = { name: "amount", type: "number" };
const count: Column = { name: "count", type: "integer" };
const tier: Column = { name: "tier", type: "text" };
const vip: Column = { name: "vip", type: "boolean" };
const since: Column = { name: "since", type: "dateTime" };
const tags: Column = { name: "tags", type: "textList" };
const ref = (name: string) => ({ ref: `/input/${name}` });
const op = (name: string, ...args: unknown[]) => ({ op: { name, args } });
const lit = (value: unknown) => ({ literal: value });
const test = (text: string, column: Column) => {
  const parsed = parseCell(text, column);
  if (parsed.kind !== "test") throw Error(JSON.stringify(parsed));
  return parsed.expression;
};

describe("input cells", () => {
  it("read empty and - as no condition", () => {
    expect(parseCell("", amount)).toEqual({ kind: "empty" });
    expect(parseCell("  -  ", tier)).toEqual({ kind: "empty" });
  });

  it("read each test of the grammar as its expression", () => {
    expect(test("> 1000", amount)).toEqual(op("gt", ref("amount"), lit(1000)));
    expect(test(">= 1000", amount)).toEqual(
      op("gte", ref("amount"), lit(1000)),
    );
    expect(test("< 5", count)).toEqual(op("lt", ref("count"), lit(5)));
    expect(test("<=5", count)).toEqual(op("lte", ref("count"), lit(5)));
    expect(test('"gold"', tier)).toEqual(op("eq", ref("tier"), lit("gold")));
    expect(test('= "gold"', tier)).toEqual(op("eq", ref("tier"), lit("gold")));
    expect(test("1000", amount)).toEqual(op("eq", ref("amount"), lit(1000)));
    expect(test("true", vip)).toEqual(op("eq", ref("vip"), lit(true)));
    expect(test('!= "gold"', tier)).toEqual(op("ne", ref("tier"), lit("gold")));
    expect(test("[10..20]", amount)).toEqual(
      op(
        "and",
        op("gte", ref("amount"), lit(10)),
        op("lte", ref("amount"), lit(20)),
      ),
    );
    expect(test("(10..20)", amount)).toEqual(
      op(
        "and",
        op("gt", ref("amount"), lit(10)),
        op("lt", ref("amount"), lit(20)),
      ),
    );
    expect(test("[10..20)", amount)).toEqual(
      op(
        "and",
        op("gte", ref("amount"), lit(10)),
        op("lt", ref("amount"), lit(20)),
      ),
    );
    expect(test("(10..20]", amount)).toEqual(
      op(
        "and",
        op("gt", ref("amount"), lit(10)),
        op("lte", ref("amount"), lit(20)),
      ),
    );
    expect(test('"ES", "PT"', tier)).toEqual(
      op("in", ref("tier"), lit(["ES", "PT"])),
    );
    expect(test('not("ES", "PT")', tier)).toEqual(
      op("notIn", ref("tier"), lit(["ES", "PT"])),
    );
    expect(test('contains "x"', tier)).toEqual(
      op("contains", ref("tier"), lit("x")),
    );
    expect(test('starts with "x"', tier)).toEqual(
      op("startsWith", ref("tier"), lit("x")),
    );
    expect(test('ends with "x"', tier)).toEqual(
      op("endsWith", ref("tier"), lit("x")),
    );
    expect(test("present", amount)).toEqual(op("exists", ref("amount")));
    expect(test("missing", amount)).toEqual(
      op("not", op("exists", ref("amount"))),
    );
    expect(test('contains "vip"', tags)).toEqual(
      op("contains", ref("tags"), lit("vip")),
    );
    expect(test('> "2026-01-01T00:00:00Z"', since)).toEqual(
      op("gt", ref("since"), lit("2026-01-01T00:00:00Z")),
    );
  });

  it("read a bare word in a Text column as text", () => {
    expect(test("gold", tier)).toEqual(op("eq", ref("tier"), lit("gold")));
    expect(test("1000", tier)).toEqual(op("eq", ref("tier"), lit("1000")));
    expect(test("ES, PT", tier)).toEqual(
      op("in", ref("tier"), lit(["ES", "PT"])),
    );
    expect(printCell(test("gold", tier), tier)).toBe('"gold"');
  });

  it("escape column names in references", () => {
    expect(test("> 1", { name: "a/b~c", type: "number" })).toEqual(
      op("gt", { ref: "/input/a~1b~0c" }, lit(1)),
    );
  });

  it("explain parse errors and type mismatches in one sentence", () => {
    expect(parseCell(">> 1000", amount)).toEqual({
      kind: "error",
      problem: "parse",
      message: "`>>` isn't a test; try `> 1000`.",
    });
    expect(parseCell("> 1000", tier)).toEqual({
      kind: "error",
      problem: "type",
      message: "Number test in a Text column.",
    });
    expect(parseCell('"gold"', amount)).toEqual({
      kind: "error",
      problem: "type",
      message: "Text test in a Number column.",
    });
    expect(parseCell("1.5", count)).toEqual({
      kind: "error",
      problem: "type",
      message: "Use a whole number in a Whole number column.",
    });
    expect(parseCell("yes", vip)).toEqual({
      kind: "error",
      problem: "type",
      message: "Use true or false in a Yes/No column.",
    });
    expect(parseCell("[20..10]", amount)).toEqual({
      kind: "error",
      problem: "parse",
      message: "A range goes from the smaller value to the larger one.",
    });
    expect(parseCell('"gold', tier)).toEqual({
      kind: "error",
      problem: "parse",
      message: 'Close the quote: "gold".',
    });
    expect(parseCell("> 1", tags).kind).toBe("error");
  });

  it("hand a formula to the formula editor", () => {
    expect(parseCell("= input.amount > 0", amount)).toEqual({
      kind: "formula",
      text: "input.amount > 0",
    });
  });
});

describe("printing cells", () => {
  const cases: [string, Column][] = [
    ["> 1000", amount],
    [">= 1000", amount],
    ["< 5", count],
    ["<= 5", count],
    ['"gold"', tier],
    ["1000", amount],
    ["true", vip],
    ['!= "gold"', tier],
    ["[10..20]", amount],
    ["(10..20)", amount],
    ["[10..20)", amount],
    ["(10..20]", amount],
    ['"ES", "PT"', tier],
    ['not("ES", "PT")', tier],
    ['contains "x"', tier],
    ['starts with "x"', tier],
    ['ends with "x"', tier],
    ["present", amount],
    ["missing", amount],
    ['contains "vip"', tags],
    ['> "2026-01-01T00:00:00Z"', since],
  ];

  it("prints each canonical cell back as typed", () => {
    for (const [text, column] of cases)
      expect(printCell(test(text, column), column)).toBe(text);
    expect(printCell(null, amount)).toBe("-");
  });

  it("refuses what isn't a cell of that column", () => {
    expect(printCell(op("gt", ref("other"), lit(1)), amount)).toBeNull();
    expect(printCell(op("gt", ref("amount"), lit("1")), amount)).toBeNull();
    expect(printCell(op("eq", ref("amount"), ref("other")), amount)).toBeNull();
    expect(printCell(op("or", op("exists", ref("amount"))), amount)).toBeNull();
    expect(printCell(lit(true), amount)).toBeNull();
    expect(
      printCell(
        op(
          "and",
          op("gte", ref("amount"), lit(20)),
          op("lte", ref("amount"), lit(10)),
        ),
        amount,
      ),
    ).toBeNull();
  });
});

describe("column types", () => {
  it("map schemas to the six column types and back", () => {
    expect(COLUMN_TYPE_LABELS).toEqual({
      text: "Text",
      number: "Number",
      integer: "Whole number",
      boolean: "Yes/No",
      dateTime: "Date-time text",
      textList: "List of text",
    });
    for (const type of Object.keys(COLUMN_TYPE_LABELS) as Column["type"][])
      expect(columnTypeOf(schemaOfColumnType(type))).toBe(type);
    expect(columnTypeOf({ type: "object" })).toBeNull();
    expect(
      columnTypeOf({ type: "array", items: { type: "number" } }),
    ).toBeNull();
    expect(columnTypeOf({ type: "string", format: "date-time" })).toBe(
      "dateTime",
    );
  });
});

describe("cells the grid can show", () => {
  const types: ColumnType[] = [
    "text",
    "number",
    "integer",
    "boolean",
    "dateTime",
    "textList",
  ];
  const texts = [
    "true",
    "false",
    "not(true)",
    "not(false)",
    "true, false",
    "!= true",
    "> 5",
    "[1..2]",
    "1, 2",
    "not(1)",
    "gold",
    '"gold"',
    '"a", "b"',
    'not("a")',
    'contains "x"',
    'starts with "x"',
    "present",
    "missing",
    '= "gold"',
    "= 5",
    '> "2026-01-01T00:00:00Z"',
  ];

  it("prints every test it reads, and reads it back the same, in every column type", () => {
    for (const type of types)
      for (const text of texts) {
        const column = { name: "c", type };
        const reading = parseCell(text, column);
        if (reading.kind !== "test") continue;
        const printed = printCell(reading.expression, column);
        expect(printed, `${type} ${text}`).not.toBeNull();
        expect(parseCell(printed!, column), `${type} ${text}`).toEqual(reading);
      }
  });

  it("refuses lists and not() in a Yes/No column", () => {
    for (const text of ["not(true)", "not(false)", "true, false"])
      expect(parseCell(text, { name: "vip", type: "boolean" })).toEqual({
        kind: "error",
        problem: "type",
        message: "Use true or false in a Yes/No column.",
      });
  });

  it("reads a bare word after = as a formula, and quoted text as a test", () => {
    const column = { name: "tier", type: "text" as const };
    expect(parseCell("= gold", column)).toEqual({
      kind: "formula",
      text: "gold",
    });
    expect(parseCell('= "gold"', column)).toEqual({
      kind: "test",
      expression: {
        op: { name: "eq", args: [{ ref: "/input/tier" }, { literal: "gold" }] },
      },
    });
  });
});
