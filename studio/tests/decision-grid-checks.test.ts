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
} from "../src/app/editor/decision-grid/cell-grammar";
import { gridProblems } from "../src/app/editor/decision-grid/grid-checks";
import { composeWhen } from "../src/app/editor/decision-grid/rules-model";
import { tryTable } from "../src/app/editor/decision-grid/table-evaluate";
import type { Json } from "../src/app/forms/core/json";
import type {
  TableRule,
  TableSpec,
} from "../src/app/editor/decision-grid/rules-model";

const ref = (name: string) => ({ ref: `/input/${name}` });
const op = (name: string, ...args: unknown[]) => ({ op: { name, args } });
const lit = (value: unknown) => ({ literal: value });
const rule = (
  id: string,
  when: unknown,
  output: unknown = lit({ approve: true }),
): TableRule => ({ id, when, output }) as TableRule;
const spec = (
  rules: TableRule[],
  hitPolicy: TableSpec["hitPolicy"] = "first",
): TableSpec => ({
  inputSchema: {
    type: "object",
    properties: { amount: { type: "number" }, tier: { type: "string" } },
  },
  outputSchema: {
    type: "object",
    properties: { approve: { type: "boolean" }, limit: { type: "number" } },
    required: ["approve"],
  },
  hitPolicy,
  rules,
});
const messages = (table: TableSpec) =>
  gridProblems(table).map((p) => `${p.severity}: ${p.message}`);

describe("the problems under the grid", () => {
  it("names duplicate and invalid rule names", () => {
    expect(
      messages(
        spec([
          rule("a", op("eq", ref("tier"), lit("x"))),
          rule("a", lit(true)),
          rule("bad name", lit(true)),
        ]),
      ),
    ).toEqual([
      "error: Rule names must be unique: a is used by rules 1 and 2.",
      "error: Rule 3: use letters, numbers, dots, underscores or hyphens in its name.",
      "error: Rule 3 can never match: rule 2 always matches.",
    ]);
  });

  it("asks for required results, and finds rules after one that always matches", () => {
    expect(
      messages(
        spec([
          rule("a", lit(true), lit({})),
          rule("b", op("gt", ref("amount"), lit(1))),
        ]),
      ),
    ).toEqual([
      "error: Rule 1 needs a result for approve.",
      "error: Rule 2 can never match: rule 1 always matches.",
    ]);
  });

  it("warns when two rules of an exactly-one table can match the same input", () => {
    const table = spec(
      [
        rule("low", op("lte", ref("amount"), lit(1000))),
        rule(
          "mid",
          op(
            "and",
            op("gte", ref("amount"), lit(500)),
            op("lte", ref("amount"), lit(2000)),
          ),
        ),
        rule(
          "gold",
          op(
            "and",
            op("gt", ref("amount"), lit(3000)),
            op("eq", ref("tier"), lit("gold")),
          ),
        ),
        rule(
          "silver",
          op(
            "and",
            op("gt", ref("amount"), lit(3000)),
            op("eq", ref("tier"), lit("silver")),
          ),
        ),
        rule("es", op("in", ref("tier"), lit(["ES", "PT"]))),
      ],
      "unique",
    );
    expect(messages(table)).toEqual([
      "warning: Rules 1 and 2 can both match amount = 500.",
      'warning: Rules 1 and 5 can both match amount = 1000 and tier = "ES".',
      'warning: Rules 2 and 5 can both match amount = 500 and tier = "ES".',
    ]);
  });

  it("says nothing it can't prove, and nothing for a first-match table", () => {
    const table = spec(
      [
        rule("a", op("ne", ref("tier"), lit("x"))),
        rule("b", op("eq", ref("tier"), lit("y"))),
        rule("c", op("or", lit(true))),
      ],
      "unique",
    );
    expect(messages(table)).toEqual([]);
    expect(messages({ ...table, hitPolicy: "first" })).toEqual([]);
  });

  it("every overlap it reports names an input that both rules really match", () => {
    // The cell pools of the round-trip test, for a Number and a Text column.
    const pools: Record<"amount" | "tier", string[]> = {
      amount: [
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
      tier: [
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
    };
    const columns: Column[] = [
      { name: "amount", type: "number" },
      { name: "tier", type: "text" },
    ];
    let seed = 20261027;
    const next = (n: number) => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed % n;
    };
    let witnesses = 0;
    for (let round = 0; round < 1000; round++) {
      const rules = Array.from({ length: 2 + next(4) }, (_, i) => {
        const cells = columns.map((column) => {
          const pool = pools[column.name as "amount" | "tier"];
          const reading = parseCell(pool[next(pool.length)], column);
          if (reading.kind === "error" || reading.kind === "formula")
            throw Error(JSON.stringify(reading));
          return reading.kind === "empty" ? null : reading.expression;
        });
        return rule(`r${i}`, composeWhen(cells));
      });
      const table = spec(rules, "unique");
      for (const problem of gridProblems(table)) {
        if (problem.severity !== "warning") continue;
        const found = /^Rules (\d+) and (\d+) can both match (.+)\.$/.exec(
          problem.message,
        );
        expect(found, problem.message).not.toBeNull();
        const input: Record<string, unknown> = {};
        if (found![3] !== "any input")
          for (const pair of found![3].split(" and ")) {
            const at = pair.indexOf(" = ");
            input[pair.slice(0, at)] = JSON.parse(pair.slice(at + 3));
          }
        const { marks } = tryTable(table, input as Json);
        expect(
          [marks[Number(found![1]) - 1], marks[Number(found![2]) - 1]],
          `${problem.message} ${JSON.stringify(rules)}`,
        ).toEqual(["match", "match"]);
        witnesses++;
      }
    }
    expect(witnesses).toBeGreaterThan(100);
  });

  it("caps a table at 1,000 rules", () => {
    const many = Array.from({ length: 1001 }, (_, i) =>
      rule(`r${i}`, op("eq", ref("amount"), lit(i))),
    );
    expect(messages(spec(many))).toEqual([
      "error: A table holds at most 1,000 rules; this one has 1,001.",
    ]);
  });
});
