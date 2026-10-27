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
// Formulas in the decision grid: a rule's whole condition, or a result
// mapped from the input. Rules read only the table's input (`input.*`) and,
// for now, can't use the text operators.
import { escapeSegment } from "../../forms/core/json";
import type { Schema, ScopeEntry } from "../../forms/core/scope";
import { completeAt, type CompletionList } from "../formula/complete";
import { parseFormula } from "../formula/grammar";
import type { Expression } from "../ndv/registry";
import {
  COLUMN_TYPE_LABELS,
  columnTypeOf,
  printCell,
  type Column,
} from "./cell-grammar";
import { composeWhen } from "./rules-model";

export const ONLY_INPUT = "Rules can read only the table's input.";
export const NO_TEXT_OPERATORS =
  "Decision rules cannot use text operators yet.";
const TEXT_OPERATORS = new Set(["concat", "join"]);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** The first thing a rule can't hold, or null. */
function refusal(node: unknown): string | null {
  if (Array.isArray(node)) {
    for (const item of node) {
      const found = refusal(item);
      if (found) return found;
    }
    return null;
  }
  if (!isRecord(node) || "literal" in node) return null;
  if (typeof node["ref"] === "string")
    return node["ref"] === "/input" || node["ref"].startsWith("/input/")
      ? null
      : ONLY_INPUT;
  if (isRecord(node["op"]) && TEXT_OPERATORS.has(String(node["op"]["name"])))
    return NO_TEXT_OPERATORS;
  for (const value of Object.values(node)) {
    const found = refusal(value);
    if (found) return found;
  }
  return null;
}

/** A formula typed in the grid: its expression, or why a rule can't hold it. */
export function tableFormula(
  text: string,
  // The input schema types the formula once the check reads it.
  _inputSchema: unknown,
): { ok: true; expression: Expression } | { ok: false; message: string } {
  // The text operators parse, so the refusal can say why a rule can't use them.
  const parsed = parseFormula(text, {
    features: ["text.concat", "text.join"],
    loops: [],
  });
  if (parsed.kind === "empty")
    return { ok: false, message: "Write a formula." };
  if (parsed.kind === "error")
    return { ok: false, message: parsed.error.message };
  const problem = refusal(parsed.expression);
  return problem
    ? { ok: false, message: problem }
    : { ok: true, expression: parsed.expression };
}

const OTHER_ROOTS = /^(?:steps|item|index|loops)\b/;

/** The table's input fields as autocomplete reads them. */
function inputEntries(inputSchema: unknown): ScopeEntry[] {
  const properties =
    isRecord(inputSchema) && isRecord(inputSchema["properties"])
      ? inputSchema["properties"]
      : {};
  const required =
    isRecord(inputSchema) && Array.isArray(inputSchema["required"])
      ? (inputSchema["required"] as unknown[])
      : [];
  return Object.entries(properties).map(([key, schema]) => {
    const type = columnTypeOf(schema);
    return {
      ref: `/input/${escapeSegment(key)}`,
      source: "input" as const,
      path: [key],
      label: key,
      breadcrumb: `Input › ${key}`,
      schema: (isRecord(schema) ? schema : {}) as Schema,
      types: [],
      typeLabel: type ? COLUMN_TYPE_LABELS[type] : "Any value",
      optional: !required.includes(key),
    };
  });
}

/** Autocomplete for a rule's formula: the table's input only. */
export function tableCompletions(
  inputSchema: unknown,
): (text: string, caret: number) => CompletionList | null {
  return (text, caret) => {
    const list = completeAt(text, caret, "formula", {
      entries: inputEntries(inputSchema),
      steps: [],
      loops: [],
      loopEntries: [],
      features: [],
      inputSchema: (inputSchema ?? {}) as Schema,
    });
    if (!list) return null;
    const options = list.options.filter(
      (option) => !OTHER_ROOTS.test(option.apply),
    );
    return options.length ? { ...list, options } : null;
  };
}

/** The condition as the cells write it, when each part tests one column; null otherwise. */
export function splitIntoCells(
  when: Expression,
  columns: readonly Column[],
): Expression | null {
  const parts =
    isRecord(when) &&
    isRecord(when["op"]) &&
    when["op"]["name"] === "and" &&
    Array.isArray(when["op"]["args"])
      ? (when["op"]["args"] as Expression[])
      : [when];
  const cells: (Expression | null)[] = columns.map(() => null);
  for (const part of parts) {
    const at = columns.findIndex(
      (column, index) =>
        cells[index] === null && printCell(part, column) !== null,
    );
    if (at < 0) return null;
    cells[at] = part;
  }
  return composeWhen(cells);
}
