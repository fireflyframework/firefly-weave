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
  partsToText,
  refToPath,
  templateParts,
  textToExpression,
} from "../src/app/editor/ndv/params/template-text";

describe("template text", () => {
  it("reads adjacent text pieces as one part, as the text shows them", () => {
    expect(
      templateParts({
        op: {
          name: "concat",
          args: [
            { literal: "a" },
            { literal: "" },
            { literal: "b" },
            { ref: "/input/x" },
          ],
        },
      }),
    ).toEqual([{ text: "ab" }, { ref: "/input/x" }]);
  });
  it("keeps formulas with mixed or extended parts out of editable template parts", () => {
    expect(templateParts({ literal: "Hello", ref: "/input/name" })).toBeNull();
    expect(
      templateParts({
        op: {
          name: "concat",
          args: [{ ref: "/input/name", literal: "Hello" }],
        },
      }),
    ).toBeNull();
    expect(
      templateParts({ op: { name: "concat", args: [], option: true } }),
    ).toBeNull();
  });
  it("reads a concat of text and references as parts", () => {
    const expression = {
      op: {
        name: "concat",
        args: [{ literal: "Hello " }, { ref: "/input/name" }, { literal: "!" }],
      },
    };
    expect(templateParts(expression)).toEqual([
      { text: "Hello " },
      { ref: "/input/name" },
      { text: "!" },
    ]);
    expect(partsToText(templateParts(expression)!)).toBe(
      "Hello {{ input.name }}!",
    );
    expect(templateParts({ literal: "plain" })).toEqual([{ text: "plain" }]);
    expect(templateParts({ ref: "/steps/check/output/amount" })).toEqual([
      { ref: "/steps/check/output/amount" },
    ]);
    expect(templateParts({ op: { name: "gt", args: [] } })).toBeNull();
  });
  it("keeps extended outer expression trees opaque", () => {
    expect(
      templateParts({
        op: {
          name: "concat",
          args: [{ literal: "Hi " }, { ref: "/input/name" }],
        },
        literal: "hidden",
      }),
    ).toBeNull();
    expect(templateParts({ ref: "/input/name", literal: "hidden" })).toBeNull();
  });
  it("keeps references Studio can't write as paths in a template's parts", () => {
    for (const ref of ["input/name", "/input/bad~2escape"]) {
      expect(refToPath(ref)).toBe(`ref(${JSON.stringify(ref)})`);
      expect(
        templateParts({
          op: { name: "concat", args: [{ literal: "Value: " }, { ref }] },
        }),
      ).toEqual([{ text: "Value: " }, { ref }]);
    }
  });
  it("writes every reference as formula text", () => {
    expect(refToPath("/input/customerId")).toBe("input.customerId");
    expect(refToPath("/steps/check.v2/output/amount")).toBe(
      'steps["check.v2"].output.amount',
    );
    expect(refToPath("/input/with space")).toBe('input["with space"]');
    expect(refToPath("/input/with~1slash")).toBe('input["with/slash"]');
    expect(refToPath("/custom")).toBe('ref("/custom")');
  });
  it("turns typed text into a literal, a reference or a concat", () => {
    expect(textToExpression("plain")).toEqual({
      ok: true,
      value: { mode: "fixed", value: "plain" },
    });
    expect(textToExpression("{{ input.name }}")).toEqual({
      ok: true,
      value: { mode: "mapped", expression: { ref: "/input/name" } },
    });
    expect(
      textToExpression(
        "Hi {{input.name}}, {{ steps.check.output.amount }} due",
      ),
    ).toEqual({
      ok: true,
      value: {
        mode: "mapped",
        expression: {
          op: {
            name: "concat",
            args: [
              { literal: "Hi " },
              { ref: "/input/name" },
              { literal: ", " },
              { ref: "/steps/check/output/amount" },
              { literal: " due" },
            ],
          },
        },
      },
    });
    expect(textToExpression("Hi {{ input.name ")).toEqual({
      ok: false,
      message: "The formula ends too early. Close the placeholder with `}}`.",
    });
    expect(textToExpression("Hi {{ 1 + 2 }}")).toEqual({
      ok: false,
      message: "Unexpected `+` at column 9.",
    });
    expect(textToExpression("Keep \\{{ braces }}")).toEqual({
      ok: true,
      value: { mode: "fixed", value: "Keep {{ braces }}" },
    });
    expect(
      textToExpression("Total {{ coalesce(input.total, 0) }}"),
    ).toMatchObject({
      ok: true,
      value: { mode: "mapped" },
    });
  });
});
