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
  fieldText,
  readFieldText,
  type FieldText,
} from "../src/app/editor/formula/field-text";

const options = { features: ["text.concat", "text.join"], loops: [] };
const formula: FieldText = { mode: "formula", options, formulaOnly: false };
const template: FieldText = { mode: "template", options, formulaOnly: false };

describe("what a field's text means", () => {
  it("reads formulas, accepting one outer pair of braces", () => {
    expect(readFieldText("input.amount > 1000", formula)).toEqual({
      kind: "value",
      value: {
        mode: "mapped",
        expression: {
          op: {
            name: "gt",
            args: [{ ref: "/input/amount" }, { literal: 1000 }],
          },
        },
      },
      literal: false,
    });
    expect(readFieldText("{{ input.amount }}", formula)).toEqual({
      kind: "value",
      value: { mode: "mapped", expression: { ref: "/input/amount" } },
      literal: false,
    });
  });
  it("turns a literal into a Fixed value, unless the field takes formulas only", () => {
    expect(readFieldText('"Ready"', formula)).toEqual({
      kind: "value",
      value: { mode: "fixed", value: "Ready" },
      literal: true,
    });
    expect(readFieldText('"Ready"', { ...formula, formulaOnly: true })).toEqual(
      {
        kind: "value",
        value: { mode: "mapped", expression: { literal: "Ready" } },
        literal: true,
      },
    );
  });
  it("tells an empty formula from empty template text", () => {
    expect(readFieldText("  ", formula)).toEqual({ kind: "empty" });
    expect(readFieldText("", template)).toEqual({
      kind: "value",
      value: { mode: "fixed", value: "" },
      literal: true,
    });
  });
  it("reports what doesn't parse", () => {
    expect(readFieldText("input.amount >", formula)).toEqual({
      kind: "error",
      error: {
        code: "WV-FORMULA-EOF",
        message: "The formula ends too early. Add a value after `>`.",
        from: 14,
        to: 14,
      },
    });
  });
  it("shows stored values as text", () => {
    expect(fieldText({ mode: "fixed", value: "Ready" }, "formula")).toBe(
      '"Ready"',
    );
    expect(fieldText({ mode: "fixed", value: "Ready {{ x" }, "template")).toBe(
      "Ready \\{{ x",
    );
    expect(fieldText({ mode: "fixed", value: 25 }, "template")).toBe(
      "{{ 25 }}",
    );
    expect(fieldText({ mode: "absent" }, "formula")).toBe("");
    expect(
      fieldText(
        { mode: "mapped", expression: { op: { name: "sum", args: [] } } },
        "formula",
      ),
    ).toBeNull();
    expect(
      fieldText(
        {
          mode: "mapped",
          expression: {
            op: {
              name: "concat",
              args: [{ literal: "Hi " }, { ref: "/input/name" }],
            },
          },
        },
        "template",
      ),
    ).toBe("Hi {{ input.name }}");
  });
});
