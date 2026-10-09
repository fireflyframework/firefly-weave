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
  pathToRef,
  refToPath,
  templateParts,
  textToExpression,
} from "../src/app/editor/ndv/params/template-text";

describe("template text", () => {
  it("keeps formulas with mixed or extended parts out of editable template text", () => {
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
  it("writes references as dotted paths and back", () => {
    expect(refToPath("/input/customerId")).toBe("input.customerId");
    expect(refToPath("/steps/check.v2/output/amount")).toBe(
      "steps.check.v2.output.amount",
    );
    expect(refToPath("/input/with space")).toBeNull();
    expect(pathToRef("steps.check.v2.output.amount")).toBe(
      "/steps/check.v2/output/amount",
    );
    expect(pathToRef("input")).toBe("/input");
    expect(pathToRef("nope.x")).toBeNull();
  });
  it("rejects ambiguous step paths and unrepresentable keys without losing the original parts", () => {
    for (const ref of [
      "/steps/check.output/output/amount",
      "/steps/check/output/with.dot",
      "/input/with~1slash",
      "/input/with~0tilde",
      "/input/",
      "input/name",
      "/input/bad~2escape",
    ]) {
      expect(refToPath(ref)).toBeNull();
      expect(
        templateParts({
          op: { name: "concat", args: [{ literal: "Value: " }, { ref }] },
        }),
      ).toEqual([{ text: "Value: " }, { ref }]);
    }
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
      message: "Close the braces: {{ input.field }}.",
    });
    expect(textToExpression("Hi {{ 1 + 2 }}")).toEqual({
      ok: false,
      message:
        "Between the braces, name a field such as input.name or steps.check.output.amount.",
    });
    expect(textToExpression("Keep \\{{ braces }}")).toEqual({
      ok: true,
      value: { mode: "fixed", value: "Keep {{ braces }}" },
    });
  });
});
