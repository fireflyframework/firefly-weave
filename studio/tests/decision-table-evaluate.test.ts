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
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { tryTable } from "../src/app/editor/decision-grid/table-evaluate";
import type { TableSpec } from "../src/app/editor/decision-grid/rules-model";
import type { Json } from "../src/app/forms/core/json";

interface DecisionCase {
  name: string;
  table: TableSpec;
  input: Json;
  answer:
    | { output: Json; matched: string[]; usedDefault: boolean }
    | { code: string; path: string };
}
const cases = (
  JSON.parse(
    readFileSync(
      resolve(import.meta.dirname, "fixtures/decision-cases.json"),
      "utf8",
    ),
  ) as { cases: DecisionCase[] }
).cases;

const ref = (name: string) => ({ ref: `/input/${name}` });
const op = (name: string, ...args: unknown[]) => ({ op: { name, args } });
const lit = (value: unknown) => ({ literal: value });

describe("Try an input in Studio", () => {
  it("answers every case as Weave does", () => {
    expect(cases.length).toBeGreaterThan(400);
    for (const { name, table, input, answer } of cases) {
      const { answer: ours } = tryTable(table, input);
      const expected =
        "code" in answer
          ? { ok: false, code: answer.code, path: answer.path }
          : {
              ok: true,
              output: answer.output,
              matched: answer.matched,
              usedDefault: answer.usedDefault,
            };
      expect({ name, ...ours }).toEqual({ name, ...expected });
    }
  });

  it("marks every rule as a match, no match or an error, whatever the policy stops at", () => {
    const table = {
      inputSchema: { type: "object" },
      outputSchema: {},
      hitPolicy: "first",
      rules: [
        {
          id: "big",
          when: op("gt", ref("amount"), lit(1000)),
          output: lit("a"),
        },
        {
          id: "gold",
          when: op("eq", ref("tier"), lit("gold")),
          output: lit("b"),
        },
        { id: "vip", when: op("eq", ref("vip"), lit(true)), output: lit("c") },
      ],
    } as TableSpec;
    const { marks, answer } = tryTable(table, { amount: 2000, tier: "gold" });
    expect(marks).toEqual(["match", "match", "error"]);
    expect(answer).toEqual({
      ok: true,
      output: "a",
      matched: ["big"],
      usedDefault: false,
    });
  });
});
