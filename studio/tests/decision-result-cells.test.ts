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
import { printResult } from "../src/app/editor/decision-grid/result-cells";
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
