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
// Decision conditions as rule rows: "amount is greater than 1000". A row is a
// data reference, an operator in words and a typed value. Rows read and write
// the same expression tree the definition language already has
// ({op: {name, args}}), so nothing changes in a workflow's source; a
// condition the rows can't show stays a formula. The summaries label the
// canvas branches ("Amount > 1000", "Otherwise") and the decision's node.

type Json = Record<string, unknown>;
const isRecord = (value: unknown): value is Json =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** What a row compares; "missing" is `not(exists(ref))`. */
export type ConditionOperator =
  | "eq"
  | "ne"
  | "gt"
  | "gte"
  | "lt"
  | "lte"
  | "exists"
  | "missing"
  | "contains"
  | "notContains"
  | "in"
  | "notIn"
  | "startsWith"
  | "endsWith";
export type ConditionJoin = "and" | "or";
export type ConditionScalar = string | number | boolean | null;
export interface ConditionRow {
  /** JSON pointer of the data the row tests, such as /input/amount. */
  ref: string;
  operator: ConditionOperator;
  /** The literal compared with; unused by "exists" and "missing". */
  value?: ConditionScalar | ConditionScalar[];
  compareRef?: string;
}
export interface Condition {
  join: ConditionJoin;
  rows: ConditionRow[];
}

/** The operators a row offers, in the order the menu lists them. */
export const conditionOperators: {
  value: ConditionOperator;
  label: string;
  symbol: string;
}[] = [
  { value: "eq", label: "is", symbol: "is" },
  { value: "ne", label: "is not", symbol: "is not" },
  { value: "gt", label: "is greater than", symbol: ">" },
  { value: "gte", label: "is at least", symbol: "≥" },
  { value: "lt", label: "is less than", symbol: "<" },
  { value: "lte", label: "is at most", symbol: "≤" },
  { value: "contains", label: "contains", symbol: "contains" },
  {
    value: "notContains",
    label: "does not contain",
    symbol: "does not contain",
  },
  { value: "in", label: "is in list", symbol: "is in" },
  { value: "notIn", label: "is not in list", symbol: "is not in" },
  { value: "startsWith", label: "starts with", symbol: "starts with" },
  { value: "endsWith", label: "ends with", symbol: "ends with" },
  { value: "exists", label: "is present", symbol: "is present" },
  { value: "missing", label: "is missing", symbol: "is missing" },
];
const comparisons = new Set(
  conditionOperators
    .filter((op) => !["exists", "missing"].includes(op.value))
    .map((op) => op.value),
);
export const membership = (operator: string) =>
  operator === "in" || operator === "notIn";
/** Operators that need no value. */
export const unary = (operator: ConditionOperator) =>
  operator === "exists" || operator === "missing";

/** The error shown on a path whose condition is not chosen yet. */
export const missingConditionText = "Choose when this path applies.";
const pointer = /^(?:\/(?:[^~/]|~[01])*)+$/;

const scalar = (value: unknown): value is ConditionScalar =>
  value === null || ["string", "number", "boolean"].includes(typeof value);
const refOf = (expression: unknown) =>
  isRecord(expression) &&
  Object.keys(expression).length === 1 &&
  typeof expression["ref"] === "string"
    ? expression["ref"]
    : null;
const operation = (expression: unknown) =>
  isRecord(expression) &&
  Object.keys(expression).length === 1 &&
  isRecord(expression["op"]) &&
  Array.isArray(expression["op"]["args"])
    ? {
        name: String(expression["op"]["name"]),
        args: expression["op"]["args"] as unknown[],
      }
    : null;

/** One row from one comparison, or null when it isn't a row. */
function rowOf(expression: unknown): ConditionRow | null {
  const op = operation(expression);
  if (!op) return null;
  if (comparisons.has(op.name as ConditionOperator) && op.args.length === 2) {
    const ref = refOf(op.args[0]);
    const compareRef = refOf(op.args[1]);
    if (ref !== null && compareRef !== null)
      return { ref, operator: op.name as ConditionOperator, compareRef };
    const literal = op.args[1];
    if (
      ref === null ||
      !isRecord(literal) ||
      Object.keys(literal).length !== 1 ||
      !("literal" in literal)
    )
      return null;
    const value = literal["literal"];
    if (membership(op.name)) {
      if (!Array.isArray(value) || !value.every(scalar)) return null;
    } else if (
      !scalar(value) ||
      (["startsWith", "endsWith"].includes(op.name) &&
        typeof value !== "string")
    )
      return null;
    return {
      ref,
      operator: op.name as ConditionOperator,
      value: value as ConditionRow["value"],
    };
  }
  if (op.name === "exists" && op.args.length === 1) {
    const ref = refOf(op.args[0]);
    return ref === null ? null : { ref, operator: "exists" };
  }
  if (op.name === "not" && op.args.length === 1) {
    const inner = operation(op.args[0]);
    if (inner?.name !== "exists" || inner.args.length !== 1) return null;
    const ref = refOf(inner.args[0]);
    return ref === null ? null : { ref, operator: "missing" };
  }
  return null;
}

/**
 * The rows of a condition. An unset condition has no rows; null means the
 * expression is something rows can't show (it stays a formula).
 */
export function parseCondition(when: unknown): Condition | null {
  if (when === undefined || when === null) return { join: "and", rows: [] };
  const single = rowOf(when);
  if (single) return { join: "and", rows: [single] };
  const op = operation(when);
  if (!op || (op.name !== "and" && op.name !== "or") || !op.args.length)
    return null;
  const rows = op.args.map(rowOf);
  return rows.every((row): row is ConditionRow => !!row)
    ? { join: op.name, rows }
    : null;
}

/** True when a row can be written: a data pointer and, if needed, a value. */
export function rowComplete(row: ConditionRow) {
  if (!pointer.test(row.ref)) return false;
  if (unary(row.operator)) return true;
  if (row.compareRef !== undefined) return pointer.test(row.compareRef);
  if (membership(row.operator))
    return Array.isArray(row.value) && row.value.every(scalar);
  if (row.value === undefined) return false;
  return (
    typeof row.value !== "number" ||
    (Number.isFinite(row.value) && !Number.isNaN(row.value))
  );
}

function rowExpression(row: ConditionRow): Json {
  const ref = { ref: row.ref };
  if (row.operator === "exists") return { op: { name: "exists", args: [ref] } };
  if (row.operator === "missing")
    return {
      op: { name: "not", args: [{ op: { name: "exists", args: [ref] } }] },
    };
  return {
    op: {
      name: row.operator,
      args: [
        ref,
        row.compareRef !== undefined
          ? { ref: row.compareRef }
          : { literal: row.value },
      ],
    },
  };
}

/**
 * The expression for complete rows: one row as itself, more rows joined by
 * "and" or "or". Undefined when there is no row (the condition is unset).
 */
export function buildCondition(condition: Condition): Json | undefined {
  const rows = condition.rows.filter(rowComplete);
  if (!rows.length) return undefined;
  if (rows.length === 1) return rowExpression(rows[0]);
  return { op: { name: condition.join, args: rows.map(rowExpression) } };
}

const words = (name: string) =>
  name
    .replace(/[_-]+/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .trim();
const decode = (segment: string) =>
  segment.replace(/~1/g, "/").replace(/~0/g, "~");

/**
 * The plain name of the data a pointer reads: the workflow input field's
 * title when the input schema has one ("Amount"), otherwise the last part of
 * the pointer ("decision").
 */
export function referenceTitle(ref: string, definition?: unknown): string {
  const segments = ref.split("/").slice(1).map(decode);
  if (segments[0] === "input" && segments.length > 1) {
    let schema: unknown = isRecord(definition)
      ? (definition["spec"] as Json | undefined)?.["inputSchema"]
      : undefined;
    for (const segment of segments.slice(1))
      schema =
        isRecord(schema) && isRecord(schema["properties"])
          ? schema["properties"][segment]
          : undefined;
    if (isRecord(schema) && typeof schema["title"] === "string")
      return schema["title"];
    return segments.at(-1)!;
  }
  if (segments[0] === "steps" && segments.length >= 2) {
    if (segments.length <= 3) return `${segments[1]} output`;
    return segments.at(-1)!;
  }
  if (segments[0] === "input") return "Workflow input";
  return segments.at(-1) ?? ref;
}

const valueText = (value: ConditionRow["value"]) =>
  Array.isArray(value)
    ? value
        .map((item) => (item === null ? "no value" : String(item)))
        .join(", ")
    : value === null
      ? "no value"
      : String(value);

/** "Amount > 1000", "decision is approve". */
export function rowSummary(row: ConditionRow, definition?: unknown) {
  const operator = conditionOperators.find((o) => o.value === row.operator)!;
  const subject = referenceTitle(row.ref, definition);
  return unary(row.operator)
    ? `${subject} ${operator.symbol}`
    : `${subject} ${operator.symbol} ${row.compareRef !== undefined ? referenceTitle(row.compareRef, definition) : valueText(row.value)}`;
}

/** A condition in a few words, for branch labels and node summaries. */
export function conditionSummary(when: unknown, definition?: unknown) {
  const condition = parseCondition(when);
  if (!condition) return "Custom condition";
  if (!condition.rows.length) return "Condition not set";
  return condition.rows
    .map((row) => rowSummary(row, definition))
    .join(condition.join === "and" ? " and " : " or ");
}

/** Labels longer than this many characters end in "…"; the title has them whole. */
export const labelLimit = 28;
export function shortLabel(text: string, limit = labelLimit) {
  return text.length > limit ? `${text.slice(0, limit - 1).trimEnd()}…` : text;
}

/**
 * The name a branch goes by on the canvas and in the outline: a decision
 * case's condition, "Otherwise" for its default, a parallel branch's name.
 */
export function branchName(step: Json, branch: string, definition?: unknown) {
  if (step["kind"] === "switch") {
    if (branch === "default") return "Otherwise";
    const index = Number(branch.replace(/^case /, "")) - 1;
    const cases = Array.isArray(step["cases"]) ? step["cases"] : [];
    const found = cases[index];
    const condition = parseCondition(
      isRecord(found) ? found["when"] : undefined,
    );
    const row = condition?.rows.length === 1 ? condition.rows[0] : undefined;
    if (
      row?.operator === "eq" &&
      /^\/steps\/[^/]+\/output\/decision$/.test(row.ref) &&
      typeof row.value === "string" &&
      row.value
    )
      return (
        row.value.charAt(0).toUpperCase() +
        row.value.slice(1).replace(/[-_]/g, " ")
      );
    return conditionSummary(
      isRecord(found) ? found["when"] : undefined,
      definition,
    );
  }
  return branch;
}

export interface MissingCondition {
  stepId: string;
  caseIndex: number;
  /** The step's pointer, such as /spec/steps/0. */
  stepPointer: string;
  /** The condition's pointer, such as /spec/steps/0/cases/1/when. */
  pointer: string;
}

/** Decision cases, at any depth, whose condition is not chosen yet. */
export function missingConditions(definition: unknown): MissingCondition[] {
  const found: MissingCondition[] = [];
  const escape = (key: string) => key.replace(/~/g, "~0").replace(/\//g, "~1");
  const visit = (steps: unknown, at: string) => {
    if (!Array.isArray(steps)) return;
    steps.forEach((step, index) => {
      if (!isRecord(step)) return;
      const here = `${at}/${index}`;
      if (step["kind"] === "switch") {
        const cases = Array.isArray(step["cases"]) ? step["cases"] : [];
        cases.forEach((branch, i) => {
          if (isRecord(branch) && branch["when"] === undefined)
            found.push({
              stepId: String(step["id"] ?? ""),
              caseIndex: i,
              stepPointer: here,
              pointer: `${here}/cases/${i}/when`,
            });
          if (isRecord(branch))
            visit(branch["steps"], `${here}/cases/${i}/steps`);
        });
        const fallback = step["default"];
        if (isRecord(fallback))
          visit(fallback["steps"], `${here}/default/steps`);
      } else if (step["kind"] === "parallel" && isRecord(step["branches"]))
        for (const [name, branch] of Object.entries(step["branches"]))
          if (isRecord(branch))
            visit(branch["steps"], `${here}/branches/${escape(name)}/steps`);
    });
  };
  if (isRecord(definition) && isRecord(definition["spec"]))
    visit(definition["spec"]["steps"], "/spec/steps");
  return found;
}

interface DiagnosticLike {
  code?: unknown;
  severity?: unknown;
  path?: unknown;
  message?: unknown;
}
interface ResultLike {
  diagnostics: readonly object[];
  errorCount?: number;
  validationOk?: boolean;
  ok?: boolean;
}

/**
 * A check result with "Choose when this path applies." on every decision
 * case whose condition isn't chosen. The compiler reports such a step only
 * as a whole ("Value does not satisfy the declared contract." on the step),
 * so that generic error is replaced by the one that names the case.
 */
export function withConditionDiagnostics<T extends ResultLike>(
  result: T,
  definition: unknown,
): T {
  const missing = missingConditions(definition);
  if (!missing.length || !Array.isArray(result.diagnostics)) return result;
  const steps = new Set(missing.map((m) => m.stepPointer));
  const generic = (d: DiagnosticLike) =>
    typeof d.code === "string" &&
    d.code.startsWith("WV-SCHEMA-") &&
    typeof d.path === "string" &&
    steps.has(d.path);
  const all = result.diagnostics as DiagnosticLike[];
  const kept = all.filter((d) => !generic(d));
  const replaced = all.filter(
    (d) => generic(d) && d.severity !== "warning" && d.severity !== "info",
  ).length;
  const added: DiagnosticLike[] = missing.map((m) => ({
    code: "WV-STUDIO-CONDITION",
    severity: "error",
    message: missingConditionText,
    path: m.pointer,
  }));
  return {
    ...result,
    diagnostics: [...added, ...kept],
    errorCount: Math.max(0, (result.errorCount ?? 0) - replaced) + added.length,
    validationOk: false,
    ...(result.ok !== undefined ? { ok: false } : {}),
  };
}
