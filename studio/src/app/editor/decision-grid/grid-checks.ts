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
// The problems line under the decision table grid: what the table can't do as
// written, each in one sentence, with the rule (and column) to underline.
// Overlaps of an exactly-one table are reported only when they are certain
// from equalities, lists and number ranges; anything else is left to Try an
// input and to Weave's check.
import type { Json } from "../../forms/core/json";
import type { Expression } from "../ndv/registry";
import { gridOf, type TableSpec } from "./rules-model";

export interface GridProblem {
  severity: "error" | "warning";
  message: string;
  /** The rule's position from 0; null for the table or the Otherwise row. */
  row: number | null;
  column: string | null;
}

export const MAX_RULES = 1000;
const RULE_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const count = (n: number) => n.toLocaleString("en-US");
const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const alwaysMatches = (when: unknown) =>
  isRecord(when) && Object.keys(when).length === 1 && when["literal"] === true;
const listed = (numbers: number[]) =>
  numbers.length === 2
    ? `${numbers[0]} and ${numbers[1]}`
    : `${numbers.slice(0, -1).join(", ")} and ${numbers[numbers.length - 1]}`;

type Bound = { value: number; open: boolean } | null;
/** What one cell lets through: anything, some values, a number interval, or something this check can't reason about. */
type Allowed =
  | { kind: "any" }
  | { kind: "values"; values: Json[] }
  | { kind: "interval"; low: Bound; high: Bound }
  | { kind: "unknown" };

function opOf(node: unknown): { name: string; args: unknown[] } | null {
  if (!isRecord(node) || !isRecord(node["op"])) return null;
  const { name, args } = node["op"];
  return typeof name === "string" && Array.isArray(args)
    ? { name, args }
    : null;
}
const literalOf = (node: unknown): unknown =>
  isRecord(node) && "literal" in node ? node["literal"] : undefined;

function allowed(cell: Expression | null): Allowed {
  if (cell === null) return { kind: "any" };
  const parts = opOf(cell);
  if (!parts) return { kind: "unknown" };
  const value = literalOf(parts.args[1]);
  switch (parts.name) {
    case "eq":
      return { kind: "values", values: [value as Json] };
    case "in":
      return Array.isArray(value)
        ? { kind: "values", values: value as Json[] }
        : { kind: "unknown" };
    case "gt":
    case "gte":
    case "lt":
    case "lte": {
      if (typeof value !== "number") return { kind: "unknown" };
      const bound = { value, open: parts.name === "gt" || parts.name === "lt" };
      return parts.name.startsWith("g")
        ? { kind: "interval", low: bound, high: null }
        : { kind: "interval", low: null, high: bound };
    }
    case "and": {
      const [low, high] = parts.args.map((arg) => allowed(arg as Expression));
      return low?.kind === "interval" && high?.kind === "interval"
        ? { kind: "interval", low: low.low, high: high.high }
        : { kind: "unknown" };
    }
    default:
      return { kind: "unknown" };
  }
}

const tighterLow = (a: Bound, b: Bound): Bound =>
  !a
    ? b
    : !b
      ? a
      : a.value > b.value || (a.value === b.value && a.open)
        ? a
        : b;
const tighterHigh = (a: Bound, b: Bound): Bound =>
  !a
    ? b
    : !b
      ? a
      : a.value < b.value || (a.value === b.value && a.open)
        ? a
        : b;
const inside = (value: Json, low: Bound, high: Bound): boolean =>
  typeof value === "number" &&
  (!low || value > low.value || (!low.open && value === low.value)) &&
  (!high || value < high.value || (!high.open && value === high.value));

/** One value of the interval, preferring a bound it includes. */
function sample(low: Bound, high: Bound): number | null {
  if (
    low &&
    high &&
    (low.value > high.value ||
      (low.value === high.value && (low.open || high.open)))
  )
    return null;
  if (low && !low.open) return low.value;
  if (high && !high.open) return high.value;
  if (low && high) return (low.value + high.value) / 2;
  if (low) return low.value + 1;
  if (high) return high.value - 1;
  return 0;
}

/** A value both cells let through; null when there is none; undefined when this check can't tell. */
function common(a: Allowed, b: Allowed): Json | null | undefined {
  if (a.kind === "unknown" || b.kind === "unknown") return undefined;
  if (a.kind === "any" && b.kind === "any") return undefined;
  if (a.kind === "any") return common(b, b);
  if (b.kind === "any") return common(a, a);
  if (a.kind === "values" && b.kind === "values")
    return (
      a.values.find((v) =>
        b.values.some((w) => JSON.stringify(w) === JSON.stringify(v)),
      ) ?? null
    );
  if (a.kind === "values" && b.kind === "interval")
    return a.values.find((v) => inside(v, b.low, b.high)) ?? null;
  if (a.kind === "interval" && b.kind === "values") return common(b, a);
  if (a.kind === "interval" && b.kind === "interval")
    return sample(tighterLow(a.low, b.low), tighterHigh(a.high, b.high));
  return undefined;
}

/** Every problem of the table, in the order the problems line lists them. */
export function gridProblems(spec: TableSpec): GridProblem[] {
  const problems: GridProblem[] = [];
  const add = (
    message: string,
    row: number | null = null,
    column: string | null = null,
    severity: GridProblem["severity"] = "error",
  ) => problems.push({ severity, message, row, column });
  if (spec.rules.length > MAX_RULES) {
    add(
      `A table holds at most ${count(MAX_RULES)} rules; this one has ${count(spec.rules.length)}.`,
    );
    return problems;
  }
  const byName = new Map<string, number[]>();
  spec.rules.forEach((rule, i) =>
    byName.set(rule.id, [...(byName.get(rule.id) ?? []), i + 1]),
  );
  for (const [name, rows] of byName)
    if (rows.length > 1)
      add(
        `Rule names must be unique: ${name} is used by rules ${listed(rows)}.`,
        rows[1] - 1,
      );
  spec.rules.forEach((rule, i) => {
    if (!RULE_NAME.test(rule.id))
      add(
        `Rule ${i + 1}: use letters, numbers, dots, underscores or hyphens in its name.`,
        i,
      );
  });
  const view = gridOf(spec);
  const missing = (
    result: (typeof view.rows)[number]["result"],
    label: string,
    row: number | null,
  ) => {
    if (result.kind !== "cells") return;
    view.results.forEach((column, c) => {
      if (column.required && result.cells[c] === undefined)
        add(`${label} needs a result for ${column.name}.`, row, column.name);
    });
  };
  view.rows.forEach((row, i) => missing(row.result, `Rule ${i + 1}`, i));
  if (view.otherwise) missing(view.otherwise, "Otherwise", null);
  if (spec.hitPolicy === "first") {
    const always = spec.rules.findIndex((rule) => alwaysMatches(rule.when));
    if (always >= 0)
      for (let i = always + 1; i < spec.rules.length; i++)
        add(
          `Rule ${i + 1} can never match: rule ${always + 1} always matches.`,
          i,
        );
  }
  if (spec.hitPolicy === "unique") {
    for (let i = 0; i < view.rows.length; i++)
      for (let j = i + 1; j < view.rows.length; j++) {
        const a = view.rows[i].condition;
        const b = view.rows[j].condition;
        if (a.kind !== "cells" || b.kind !== "cells") continue;
        const witness: string[] = [];
        let certain = true;
        for (let c = 0; c < view.inputs.length && certain; c++) {
          const left = allowed(a.cells[c]);
          const right = allowed(b.cells[c]);
          if (left.kind === "any" && right.kind === "any") continue;
          const value = common(left, right);
          if (value === undefined || value === null) certain = false;
          else
            witness.push(
              `${view.inputs[c].name} = ${typeof value === "string" ? JSON.stringify(value) : String(value)}`,
            );
        }
        if (certain)
          add(
            `Rules ${i + 1} and ${j + 1} can both match ${witness.length ? witness.join(" and ") : "any input"}.`,
            j,
            null,
            "warning",
          );
      }
  }
  return problems;
}
