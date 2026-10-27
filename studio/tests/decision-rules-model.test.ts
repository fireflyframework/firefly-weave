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
  parseCell,
  type Column,
  type ColumnType,
} from "../src/app/editor/decision-grid/cell-grammar";
import {
  addInputColumn,
  addResultColumn,
  addRule,
  composeWhen,
  decomposeWhen,
  duplicateRule,
  gridOf,
  moveRule,
  removeInputColumn,
  removeResultColumn,
  removeRule,
  renameInputColumn,
  renameResultColumn,
  renameRule,
  setInputColumnType,
  setResult,
  setRuleFormula,
  setCondition,
  setHitPolicy,
  type TableSpec,
} from "../src/app/editor/decision-grid/rules-model";

const ref = (name: string) => ({ ref: `/input/${name}` });
const op = (name: string, ...args: unknown[]) => ({ op: { name, args } });
const lit = (value: unknown) => ({ literal: value });

const table = (): TableSpec => ({
  inputSchema: {
    type: "object",
    properties: {
      amount: { type: "number" },
      tier: { type: "string" },
      country: { type: "string" },
    },
    required: ["amount"],
  },
  outputSchema: {
    type: "object",
    properties: { approve: { type: "boolean" }, limit: { type: "number" } },
    required: ["approve"],
  },
  hitPolicy: "first",
  rules: [
    {
      id: "big-gold",
      when: op(
        "and",
        op("gt", ref("amount"), lit(1000)),
        op("eq", ref("tier"), lit("gold")),
      ),
      output: lit({ approve: true, limit: 5000 }),
    },
    {
      id: "eu-small",
      when: op(
        "and",
        op("lte", ref("amount"), lit(1000)),
        op("in", ref("country"), lit(["ES", "PT"])),
      ),
      output: lit({ approve: true, limit: 500 }),
    },
    {
      id: "custom-rule",
      when: op(
        "and",
        op("gt", ref("amount"), lit(0)),
        op("exists", ref("tier")),
        op("eq", lit(1), lit(1)),
      ),
      output: { object: { approve: lit(false), limit: ref("amount") } },
    },
  ],
  defaultOutput: lit({ approve: false, limit: 0 }),
});

describe("the grid view of a table", () => {
  it("shows input and result columns, cells, merged formulas and Otherwise", () => {
    const view = gridOf(table());
    expect(view.inputs.map((c) => [c.name, c.type, c.required])).toEqual([
      ["amount", "number", true],
      ["tier", "text", false],
      ["country", "text", false],
    ]);
    expect(view.results.map((c) => [c.name, c.type, c.required])).toEqual([
      ["approve", "boolean", true],
      ["limit", "number", false],
    ]);
    expect(view.rows[0].condition).toEqual({
      kind: "cells",
      cells: [
        op("gt", ref("amount"), lit(1000)),
        op("eq", ref("tier"), lit("gold")),
        null,
      ],
    });
    expect(view.rows[2].condition.kind).toBe("formula");
    expect(view.rows[0].result).toEqual({
      kind: "cells",
      cells: [lit(true), lit(5000)],
    });
    expect(view.rows[2].result).toEqual({
      kind: "cells",
      cells: [lit(false), ref("amount")],
    });
    expect(view.otherwise).toEqual({
      kind: "cells",
      cells: [lit(false), lit(0)],
    });
  });

  it("reads a scalar result as one Result column and collect as its items", () => {
    const scalar = {
      ...table(),
      outputSchema: { type: "string" },
      rules: [{ id: "a", when: lit(true), output: lit("ok") }],
    };
    delete (scalar as Partial<TableSpec>).defaultOutput;
    const view = gridOf(scalar);
    expect(view.results.map((c) => c.name)).toEqual(["Result"]);
    expect(view.rows[0].result).toEqual({ kind: "cells", cells: [lit("ok")] });
    expect(view.otherwise).toBeNull();
    const collect = setHitPolicy(table(), "collect").spec;
    expect(gridOf(collect).results.map((c) => c.name)).toEqual([
      "approve",
      "limit",
    ]);
  });
});

describe("a rule's condition and its cells", () => {
  const columns: Column[] = [
    { name: "amount", type: "number" },
    { name: "tier", type: "text" },
  ];

  it("is literal true when every cell is empty, the cell itself, or and() in column order", () => {
    expect(composeWhen([null, null])).toEqual(lit(true));
    const big = op("gt", ref("amount"), lit(1000));
    const gold = op("eq", ref("tier"), lit("gold"));
    expect(composeWhen([big, null])).toEqual(big);
    expect(composeWhen([big, gold])).toEqual(op("and", big, gold));
    expect(decomposeWhen(lit(true), columns)).toEqual([null, null]);
    expect(decomposeWhen(op("and", big, gold), columns)).toEqual([big, gold]);
    expect(decomposeWhen(gold, columns)).toEqual([null, gold]);
  });

  it("stays a merged formula when it isn't exactly that form", () => {
    const big = op("gt", ref("amount"), lit(1000));
    const gold = op("eq", ref("tier"), lit("gold"));
    expect(decomposeWhen(op("and", gold, big), columns)).toBeNull();
    expect(decomposeWhen(op("and", big), columns)).toBeNull();
    expect(decomposeWhen(op("or", big, gold), columns)).toBeNull();
    expect(decomposeWhen(op("and", big, big), columns)).toBeNull();
    expect(decomposeWhen(lit(false), columns)).toBeNull();
    expect(decomposeWhen({ ...lit(true), extra: 1 }, columns)).toBeNull();
    expect(
      decomposeWhen(
        op(
          "and",
          op("gte", ref("amount"), lit(20)),
          op("lte", ref("amount"), lit(10)),
        ),
        columns,
      ),
    ).toBeNull();
  });

  it("round-trips 2,000 random rows exactly", () => {
    const pools: Record<ColumnType, string[]> = {
      number: [
        "",
        "> 1000",
        ">= 1",
        "< 5",
        "<= 5",
        "1000",
        "!= 3",
        "[10..20]",
        "(1..2)",
        "[1..2)",
        "1, 2, 3",
        "not(4)",
        "present",
        "missing",
      ],
      integer: ["", "> 10", "7", "[1..9]", "not(1, 2)", "present"],
      text: [
        "",
        '"gold"',
        "gold",
        '!= "x"',
        '"ES", "PT"',
        'not("ES")',
        'contains "a"',
        'starts with "b"',
        'ends with "c"',
        "missing",
      ],
      boolean: ["", "true", "false", "!= true", "present"],
      dateTime: [
        "",
        '> "2026-01-01T00:00:00Z"',
        '["2026-01-01T00:00:00Z".."2026-02-01T00:00:00Z"]',
        "present",
      ],
      textList: ["", 'contains "vip"', "missing"],
    };
    const types = Object.keys(pools) as ColumnType[];
    let seed = 20261026;
    const next = (n: number) => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed % n;
    };
    for (let round = 0; round < 2000; round++) {
      const width = 1 + next(5);
      const cols: Column[] = Array.from({ length: width }, (_, i) => ({
        name: `c${i}`,
        type: types[next(types.length)],
      }));
      const cells = cols.map((column) => {
        const pool = pools[column.type];
        const reading = parseCell(pool[next(pool.length)], column);
        if (reading.kind === "error" || reading.kind === "formula")
          throw Error(JSON.stringify(reading));
        return reading.kind === "empty" ? null : reading.expression;
      });
      const when = composeWhen(cells);
      expect(decomposeWhen(when, cols)).toEqual(cells);
      expect(
        composeWhen(decomposeWhen(JSON.parse(JSON.stringify(when)), cols)!),
      ).toEqual(when);
    }
  });
});

describe("editing the table", () => {
  it("writes a cell into the rule's condition and leaves other rules as they were", () => {
    const before = table();
    const after = setCondition(
      before,
      0,
      2,
      op("eq", ref("country"), lit("ES")),
    );
    expect(after.rules[0].when).toEqual(
      op(
        "and",
        op("gt", ref("amount"), lit(1000)),
        op("eq", ref("tier"), lit("gold")),
        op("eq", ref("country"), lit("ES")),
      ),
    );
    expect(after.rules[1]).toBe(before.rules[1]);
    expect(after.rules[2]).toBe(before.rules[2]);
    const cleared = setCondition(setCondition(after, 0, 0, null), 0, 1, null);
    expect(cleared.rules[0].when).toEqual(op("eq", ref("country"), lit("ES")));
    expect(() => setCondition(before, 2, 0, null)).toThrow(
      "Rule 3 is a formula; edit it as a formula.",
    );
  });

  it("switches the hit policy, and collect drops the Otherwise result", () => {
    const collected = setHitPolicy(table(), "collect");
    expect(collected.removedOtherwise).toBe(true);
    expect(collected.spec.defaultOutput).toBeUndefined();
    expect(collected.spec.outputSchema).toEqual({
      type: "array",
      items: table().outputSchema,
    });
    const back = setHitPolicy(collected.spec, "unique");
    expect(back.spec.outputSchema).toEqual(table().outputSchema);
    expect(back.removedOtherwise).toBe(false);
    expect(setHitPolicy(table(), "first").spec).toEqual(table());
  });

  it("renames an input column in the schema and in every reference", () => {
    const renamed = renameInputColumn(table(), "amount", "total");
    expect(Object.keys(renamed.inputSchema["properties"] as object)).toEqual([
      "total",
      "tier",
      "country",
    ]);
    expect(renamed.inputSchema["required"]).toEqual(["total"]);
    expect(JSON.stringify(renamed.rules)).not.toContain("/input/amount");
    expect(renamed.rules[2].output).toEqual({
      object: { approve: lit(false), limit: ref("total") },
    });
    expect(() => renameInputColumn(table(), "amount", "tier")).toThrow(
      "A column named tier already exists.",
    );
    expect(() => renameInputColumn(table(), "amount", "two words")).toThrow(
      "Use a field name with letters, numbers or underscores.",
    );
  });

  it("adds, duplicates and moves rules with unique names", () => {
    const added = addRule(table(), 1);
    expect(added.rules.map((r) => r.id)).toEqual([
      "big-gold",
      "eu-small",
      "rule-4",
      "custom-rule",
    ]);
    expect(added.rules[2]).toEqual({
      id: "rule-4",
      when: lit(true),
      output: lit({}),
    });
    const copied = duplicateRule(table(), 0);
    expect(copied.rules.map((r) => r.id)).toEqual([
      "big-gold",
      "big-gold-2",
      "eu-small",
      "custom-rule",
    ]);
    expect(moveRule(table(), 2, -1).rules.map((r) => r.id)).toEqual([
      "big-gold",
      "custom-rule",
      "eu-small",
    ]);
    expect(moveRule(table(), 0, -1)).toEqual(table());
  });
});

describe("columns and rules the grid adds, renames and removes", () => {
  it("adds an input column with its type, and refuses a bad or taken name", () => {
    const before = table();
    const after = addInputColumn(before, "since", "dateTime");
    expect(after.inputSchema["properties"]).toMatchObject({
      since: { type: "string", format: "date-time" },
    });
    expect(after.rules[0]).toBe(before.rules[0]);
    expect(gridOf(after).inputs.at(-1)).toEqual({
      name: "since",
      type: "dateTime",
      required: false,
    });
    expect(() => addInputColumn(before, "two words", "text")).toThrow(
      "Use a field name with letters, numbers or underscores.",
    );
    expect(() => addInputColumn(before, "tier", "text")).toThrow(
      "A column named tier already exists.",
    );
  });

  it("adds a result column to the result fields, or to each item of Collect all", () => {
    const after = addResultColumn(table(), "reason", "text");
    expect(after.outputSchema["properties"]).toMatchObject({
      reason: { type: "string" },
    });
    const collect = setHitPolicy(table(), "collect").spec;
    const added = addResultColumn(collect, "reason", "text");
    expect(
      (added.outputSchema["items"] as Record<string, unknown>)["properties"],
    ).toMatchObject({ reason: { type: "string" } });
    expect(() => addResultColumn(table(), "limit", "number")).toThrow(
      "A column named limit already exists.",
    );
  });

  it("removes an input column from its schema and from every rule's cells", () => {
    const before = table();
    const after = removeInputColumn(before, "tier");
    expect(Object.keys(after.inputSchema["properties"] as object)).toEqual([
      "amount",
      "country",
    ]);
    expect(after.rules[0].when).toEqual(op("gt", ref("amount"), lit(1000)));
    expect(after.rules[1]).toBe(before.rules[1]);
    // A merged formula keeps what it says; the problems line explains it.
    expect(after.rules[2]).toBe(before.rules[2]);
    const required = removeInputColumn(before, "amount");
    expect(required.inputSchema["required"]).toEqual([]);
  });

  it("renames and removes result columns in every rule and in Otherwise", () => {
    const before = table();
    const renamed = renameResultColumn(before, "limit", "cap");
    expect(renamed.rules[0].output).toEqual(lit({ approve: true, cap: 5000 }));
    expect(renamed.rules[2].output).toEqual({
      object: { approve: lit(false), cap: ref("amount") },
    });
    expect(renamed.defaultOutput).toEqual(lit({ approve: false, cap: 0 }));
    expect(Object.keys(renamed.outputSchema["properties"] as object)).toEqual([
      "approve",
      "cap",
    ]);
    const removed = removeResultColumn(before, "limit");
    expect(removed.rules[0].output).toEqual(lit({ approve: true }));
    expect(removed.defaultOutput).toEqual(lit({ approve: false }));
  });

  it("changes a column's type in its schema", () => {
    const after = setInputColumnType(table(), "country", "textList");
    expect(
      (after.inputSchema["properties"] as Record<string, unknown>)["country"],
    ).toEqual({ type: "array", items: { type: "string" } });
  });

  it("renames a rule and keeps at least one", () => {
    const before = table();
    expect(renameRule(before, 1, "eu").rules[1].id).toBe("eu");
    expect(removeRule(before, 0).rules.map((rule) => rule.id)).toEqual([
      "eu-small",
      "custom-rule",
    ]);
    const one = { ...before, rules: [before.rules[0]] };
    expect(() => removeRule(one, 0)).toThrow(
      "A table keeps at least one rule.",
    );
  });
});

describe("results the grid writes", () => {
  it("writes literal results while every cell is fixed, and object results once one is mapped", () => {
    const before = table();
    const fixed = setResult(before, 0, 1, lit(9000));
    expect(fixed.rules[0].output).toEqual(lit({ approve: true, limit: 9000 }));
    expect(fixed.rules[1]).toBe(before.rules[1]);
    const mapped = setResult(before, 0, 1, ref("amount"));
    expect(mapped.rules[0].output).toEqual({
      object: { approve: lit(true), limit: ref("amount") },
    });
    const cleared = setResult(before, 0, 1, undefined);
    expect(cleared.rules[0].output).toEqual(lit({ approve: true }));
    const otherwise = setResult(before, "otherwise", 0, lit(true));
    expect(otherwise.defaultOutput).toEqual(lit({ approve: true, limit: 0 }));
  });

  it("writes a scalar result as the expression itself", () => {
    const scalar: TableSpec = {
      ...table(),
      outputSchema: { type: "string" },
      rules: [{ id: "a", when: lit(true), output: lit("x") }],
      defaultOutput: lit("y"),
    };
    expect(setResult(scalar, 0, 0, lit("z")).rules[0].output).toEqual(lit("z"));
    expect(
      setResult(scalar, "otherwise", 0, ref("tier")).defaultOutput,
    ).toEqual(ref("tier"));
  });

  it("writes a merged condition as the formula given", () => {
    const before = table();
    const when = op(
      "or",
      op("gt", ref("amount"), lit(1)),
      op("exists", ref("tier")),
    );
    const after = setRuleFormula(before, 2, when);
    expect(after.rules[2].when).toEqual(when);
    expect(after.rules[0]).toBe(before.rules[0]);
  });
});
