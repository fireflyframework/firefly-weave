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
// The decision table grid's result cells: a fixed value typed as text by
// its column's type, or a formula over the table's input.
import type { Expression } from "../ndv/registry";
import type { GridColumn } from "./rules-model";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** A fixed result as the cell shows it; "" when unset, null for a formula. */
export function printResult(
  expression: Expression | undefined,
  column: GridColumn,
): string | null {
  if (expression === undefined) return "";
  if (
    !isRecord(expression) ||
    Object.keys(expression).length !== 1 ||
    !("literal" in expression)
  )
    return null;
  const value = expression["literal"];
  if (column.type === "boolean" && typeof value === "boolean")
    return String(value);
  if (
    (column.type === "number" || column.type === "integer") &&
    typeof value === "number"
  )
    return String(value);
  return JSON.stringify(value);
}
