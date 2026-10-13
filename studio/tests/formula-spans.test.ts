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
  highlightSpans,
  referenceSpans,
} from "../src/app/editor/formula/spans";
import { tokenize } from "../src/app/editor/formula/tokens";

describe("what the editor draws", () => {
  it("finds every reference, with its pointer", () => {
    const text =
      'input.amount > 1 and exists(steps["lookup.v2"].output.items[0]) or ref("/x") == {input: 1}';
    expect(referenceSpans(tokenize(text))).toEqual([
      { from: 0, to: 12, pointer: "/input/amount" },
      { from: 28, to: 62, pointer: "/steps/lookup.v2/output/items/0" },
      { from: 67, to: 76, pointer: "/x" },
    ]);
  });
  it("stops a reference before an unfinished accessor", () => {
    expect(referenceSpans(tokenize("input.amount."))).toEqual([
      { from: 0, to: 12, pointer: "/input/amount" },
    ]);
  });
  it("finds references inside template placeholders", () => {
    expect(
      referenceSpans(tokenize("Hi {{ input.name }}!", "template")),
    ).toEqual([{ from: 6, to: 16, pointer: "/input/name" }]);
  });
  it("colors keywords, operators, text, numbers, functions and braces", () => {
    expect(
      highlightSpans(
        tokenize('not exists(input.a) && "x" != 2', "formula"),
      ).map((span) => span.kind),
    ).toEqual([
      "keyword",
      "function",
      "operator",
      "string",
      "operator",
      "number",
    ]);
    expect(
      highlightSpans(tokenize("a {{ b }}", "template")).map(
        (span) => span.kind,
      ),
    ).toEqual(["brace", "brace"]);
  });
});
