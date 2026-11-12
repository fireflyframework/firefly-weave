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
  parseResult,
  printResult,
} from "../src/app/editor/decision-grid/result-cells";
import type { GridColumn } from "../src/app/editor/decision-grid/rules-model";

const column = (type: GridColumn["type"]): GridColumn => ({
  name: "value",
  type,
  required: false,
});

describe("result cells", () => {
  it("prints fixed results as typed text, and nothing for a formula", () => {
    expect(printResult({ literal: true }, column("boolean"))).toBe("true");
    expect(printResult({ literal: 5000 }, column("number"))).toBe("5000");
    expect(printResult({ literal: 7 }, column("integer"))).toBe("7");
    expect(printResult({ literal: "gold" }, column("text"))).toBe('"gold"');
    expect(printResult({ literal: ["a", "b"] }, column("textList"))).toBe(
      '["a","b"]',
    );
    expect(printResult({ literal: { a: 1 } }, column(null))).toBe('{"a":1}');
    expect(printResult(undefined, column("number"))).toBe("");
    expect(printResult({ ref: "/input/amount" }, column("number"))).toBeNull();
  });
});

describe("typing a result cell", () => {
  it("reads fixed text by the column's type, both ways", () => {
    const cases: [GridColumn["type"], string, unknown][] = [
      ["boolean", "true", true],
      ["boolean", "false", false],
      ["number", "5000", 5000],
      ["number", "-1.5", -1.5],
      ["integer", "7", 7],
      ["text", '"gold"', "gold"],
      ["text", "gold", "gold"],
      ["dateTime", '"2026-01-01T00:00:00Z"', "2026-01-01T00:00:00Z"],
      ["textList", '["a","b"]', ["a", "b"]],
      [null, '{"a":1}', { a: 1 }],
    ];
    for (const [type, text, value] of cases) {
      const reading = parseResult(text, column(type));
      expect(reading, `${type} ${text}`).toEqual({
        kind: "value",
        expression: { literal: value },
      });
      const printed = printResult({ literal: value as never }, column(type))!;
      expect(parseResult(printed, column(type))).toEqual(reading);
    }
  });

  it("hands a formula over and explains what doesn't read", () => {
    expect(parseResult("= input.amount", column("number"))).toEqual({
      kind: "formula",
      text: "input.amount",
    });
    expect(parseResult("", column("number"))).toEqual({ kind: "empty" });
    expect(parseResult("yes", column("boolean"))).toEqual({
      kind: "error",
      message: "Use true or false in a Yes/No column.",
    });
    expect(parseResult("abc", column("number"))).toEqual({
      kind: "error",
      message: "Use a number in a Number column.",
    });
    expect(parseResult("1.5", column("integer"))).toEqual({
      kind: "error",
      message: "Use a whole number in a Whole number column.",
    });
    expect(parseResult("a, b", column("textList")).kind).toBe("error");
    expect(parseResult("{a", column(null)).kind).toBe("error");
  });
});
