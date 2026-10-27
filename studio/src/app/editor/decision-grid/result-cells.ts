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
import type { Expression, Json } from "../ndv/registry";
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

export type ResultReading =
  | { kind: "empty" }
  | { kind: "value"; expression: Expression }
  | { kind: "formula"; text: string }
  | { kind: "error"; message: string };

const NUMBER = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/;
const value = (literal: Json): ResultReading => ({
  kind: "value",
  expression: { literal },
});
const problem = (message: string): ResultReading => ({
  kind: "error",
  message,
});
const parseJson = (text: string): { ok: true; value: Json } | { ok: false } => {
  try {
    return { ok: true, value: JSON.parse(text) as Json };
  } catch {
    return { ok: false };
  }
};

/** A result cell's text: a fixed value by the column's type, or a formula after "=". */
export function parseResult(input: string, column: GridColumn): ResultReading {
  const text = input.trim();
  if (!text) return { kind: "empty" };
  if (text.startsWith("=")) {
    const rest = text.slice(1).trim();
    return rest
      ? { kind: "formula", text: rest }
      : problem("Write a value or a formula after =.");
  }
  switch (column.type) {
    case "boolean":
      return text === "true" || text === "false"
        ? value(text === "true")
        : problem("Use true or false in a Yes/No column.");
    case "number":
      return NUMBER.test(text) && Number.isFinite(Number(text))
        ? value(Number(text))
        : problem("Use a number in a Number column.");
    case "integer":
      return NUMBER.test(text) && Number.isInteger(Number(text))
        ? value(Number(text))
        : problem("Use a whole number in a Whole number column.");
    case "text":
    case "dateTime": {
      if (!text.startsWith('"')) return value(text);
      const read = parseJson(text);
      return read.ok && typeof read.value === "string"
        ? value(read.value)
        : problem('Write text in double quotes, like "gold".');
    }
    case "textList": {
      const read = parseJson(text);
      return read.ok &&
        Array.isArray(read.value) &&
        read.value.every((item) => typeof item === "string")
        ? value(read.value)
        : problem('Write a list of text, like ["a", "b"].');
    }
    default: {
      const read = parseJson(text);
      return read.ok
        ? value(read.value)
        : problem('Write a JSON value, like {"a": 1}.');
    }
  }
}
