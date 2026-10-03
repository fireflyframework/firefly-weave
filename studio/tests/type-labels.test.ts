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
import { it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { typeLabels } from "../src/app/forms/core/type-labels";
import { typeLabel } from "../src/app/forms/core/scope";
it("uses one glossary for schemas, values and data suggestions", () => {
  expect(typeLabel({ type: "object" })).toBe(typeLabels["object"]);
  expect(typeLabel({ type: "boolean" })).toBe("Yes/No");
  expect(typeLabel({ enum: ["eu", "us"] })).toBe("Choice");
  for (const file of [
    "property-grid.ts",
    "forms/ui/schema-designer.ts",
    "forms/core/scope.ts",
  ]) {
    const source = readFileSync(
      new URL("../src/app/" + file, import.meta.url),
      "utf8",
    );
    expect(source).toContain("typeLabels");
    expect(source).not.toMatch(
      /label: ["'](?:Text|Whole number|Yes or no|Yes\/No|Group of fields|Group)["']/,
    );
  }
});
