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
  splitIntoCells,
  tableCompletions,
  tableFormula,
} from "../src/app/editor/decision-grid/formula-cells";

const schema = {
  type: "object",
  properties: { amount: { type: "number" }, tier: { type: "string" } },
};
const ref = (name: string) => ({ ref: `/input/${name}` });
const op = (name: string, ...args: unknown[]) => ({ op: { name, args } });
const lit = (value: unknown) => ({ literal: value });

describe("formulas in the decision grid", () => {
  it("reads a formula over the table's input", () => {
    expect(tableFormula("input.amount", schema)).toEqual({
      ok: true,
      expression: ref("amount"),
    });
    expect(
      tableFormula("input.amount > 5 or exists(input.tier)", schema),
    ).toEqual({
      ok: true,
      expression: op(
        "or",
        op("gt", ref("amount"), lit(5)),
        op("exists", ref("tier")),
      ),
    });
  });

  it("refuses what a rule can't read or compute", () => {
    expect(tableFormula("steps.lookup.output", schema)).toEqual({
      ok: false,
      message: "Rules can read only the table's input.",
    });
    expect(tableFormula('concat(input.tier, "x") == "goldx"', schema)).toEqual({
      ok: false,
      message: "Decision rules cannot use text operators yet.",
    });
    expect(tableFormula("input.amount * 1", schema).ok).toBe(false);
    expect(tableFormula("", schema)).toEqual({
      ok: false,
      message: "Write a formula.",
    });
  });

  it("offers only the table's input in autocomplete", () => {
    const list = tableCompletions(schema)("input.", 6);
    const labels = list?.options.map((option) => option.label) ?? [];
    expect(labels).toEqual(expect.arrayContaining(["amount", "tier"]));
    const roots =
      tableCompletions(schema)("", 0)?.options.map((o) => o.apply) ?? [];
    expect(roots.some((apply) => apply.startsWith("steps"))).toBe(false);
  });

  it("splits a formula into cells when it fits them", () => {
    const columns = [
      { name: "amount", type: "number" as const },
      { name: "tier", type: "text" as const },
    ];
    // Written in another column order: the cells' form puts amount first.
    const fits = op(
      "and",
      op("eq", ref("tier"), lit("gold")),
      op("gt", ref("amount"), lit(1)),
    );
    expect(splitIntoCells(fits, columns)).toEqual(
      op(
        "and",
        op("gt", ref("amount"), lit(1)),
        op("eq", ref("tier"), lit("gold")),
      ),
    );
    expect(
      splitIntoCells(
        op("or", op("gt", ref("amount"), lit(1)), lit(true)),
        columns,
      ),
    ).toBeNull();
  });
});
