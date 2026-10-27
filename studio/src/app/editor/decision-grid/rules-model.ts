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
// A decision table as the grid shows it, and the edits the grid makes. The
// table document stays the source of truth: the view is read from it, and each
// edit returns a new document in which every rule it didn't touch is the same
// object, so an unedited rule keeps its exact shape. A rule's condition shows
// as cells only when it is exactly the form the cells write (`composeWhen`);
// otherwise the row shows one merged formula cell.
import { escapeSegment, type Json } from "../../forms/core/json";
import type { Expression } from "../ndv/registry";
import {
  columnTypeOf,
  printCell,
  schemaOfColumnType,
  type Column,
  type ColumnType,
} from "./cell-grammar";

export type HitPolicy = "first" | "unique" | "collect";
export interface TableRule {
  id: string;
  when: Expression;
  output: Expression;
}
export interface TableSpec {
  inputSchema: Record<string, Json>;
  outputSchema: Record<string, Json>;
  hitPolicy: HitPolicy;
  rules: TableRule[];
  defaultOutput?: Expression;
}
export interface GridColumn {
  name: string;
  /** null: a schema the grid can't test or fill cell by cell ("Other"). */
  type: ColumnType | null;
  required: boolean;
}
export type ConditionView =
  | { kind: "cells"; cells: (Expression | null)[] }
  | { kind: "formula"; when: Expression };
export type ResultView =
  | { kind: "cells"; cells: (Expression | undefined)[] }
  | { kind: "formula"; output: Expression };
export interface RowView {
  id: string;
  condition: ConditionView;
  result: ResultView;
}
export interface GridView {
  hitPolicy: HitPolicy;
  inputs: GridColumn[];
  /** The result fields, or one "Result" column for a result that isn't a set of fields. */
  results: GridColumn[];
  scalar: boolean;
  rows: RowView[];
  /** The Otherwise row; null without `defaultOutput` (always for collect). */
  otherwise: ResultView | null;
}

export const FIELD_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;
const RULE_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const propertiesOf = (schema: unknown): Record<string, Json> =>
  isRecord(schema) && isRecord(schema["properties"])
    ? (schema["properties"] as Record<string, Json>)
    : {};
const requiredOf = (schema: unknown): string[] =>
  isRecord(schema) && Array.isArray(schema["required"])
    ? schema["required"].filter((key): key is string => typeof key === "string")
    : [];
const isLiteralTrue = (node: unknown): boolean =>
  isRecord(node) && Object.keys(node).length === 1 && node["literal"] === true;
const andArgs = (node: unknown): unknown[] | null => {
  if (
    !isRecord(node) ||
    Object.keys(node).length !== 1 ||
    !isRecord(node["op"])
  )
    return null;
  const inner = node["op"];
  return inner["name"] === "and" &&
    Object.keys(inner).length === 2 &&
    Array.isArray(inner["args"])
    ? inner["args"]
    : null;
};

/** The schema of one result row: the output schema, or its items for collect. */
export function rowSchema(
  spec: Pick<TableSpec, "hitPolicy" | "outputSchema">,
): Record<string, Json> {
  const items = spec.outputSchema["items"];
  return spec.hitPolicy === "collect" && isRecord(items)
    ? (items as Record<string, Json>)
    : spec.outputSchema;
}
const isFields = (schema: Record<string, Json>): boolean =>
  schema["type"] === "object" ||
  (schema["type"] === undefined && isRecord(schema["properties"]));

function columnsOf(schema: unknown): GridColumn[] {
  const required = requiredOf(schema);
  return Object.entries(propertiesOf(schema)).map(([name, property]) => ({
    name,
    type: columnTypeOf(property),
    required: required.includes(name),
  }));
}
/** Input columns the cell grammar can read; the others take part only in formulas. */
const testable = (columns: GridColumn[]): Column[] =>
  columns.map((column) => ({ name: column.name, type: column.type ?? "text" }));

/** A rule's condition from its cells: literal true, the one cell, or and() of the cells in column order. */
export function composeWhen(cells: readonly (Expression | null)[]): Expression {
  const set = cells.filter((cell): cell is Expression => cell !== null);
  if (!set.length) return { literal: true };
  if (set.length === 1) return set[0];
  return { op: { name: "and", args: set } };
}

/** The cells of a condition written by `composeWhen` over these columns; null for any other condition. */
export function decomposeWhen(
  when: unknown,
  columns: readonly Column[],
): (Expression | null)[] | null {
  const cells: (Expression | null)[] = columns.map(() => null);
  if (isLiteralTrue(when)) return cells;
  const single = columns.findIndex(
    (column) => printCell(when as Expression, column) !== null,
  );
  if (single >= 0) {
    cells[single] = when as Expression;
    return cells;
  }
  const args = andArgs(when);
  if (!args || args.length < 2) return null;
  let last = -1;
  for (const arg of args) {
    const at = columns.findIndex(
      (column, index) =>
        index > last && printCell(arg as Expression, column) !== null,
    );
    if (at < 0) return null;
    cells[at] = arg as Expression;
    last = at;
  }
  return cells;
}

function resultView(
  output: unknown,
  results: GridColumn[],
  scalar: boolean,
): ResultView {
  if (scalar) return { kind: "cells", cells: [output as Expression] };
  const names = results.map((column) => column.name);
  if (isRecord(output) && Object.keys(output).length === 1) {
    const fields = isRecord(output["literal"])
      ? Object.fromEntries(
          Object.entries(output["literal"]).map(([k, v]) => [
            k,
            { literal: v as Json },
          ]),
        )
      : isRecord(output["object"])
        ? (output["object"] as Record<string, Expression>)
        : null;
    if (fields && Object.keys(fields).every((key) => names.includes(key)))
      return {
        kind: "cells",
        cells: names.map((name) =>
          Object.hasOwn(fields, name)
            ? (fields[name] as Expression)
            : undefined,
        ),
      };
  }
  return { kind: "formula", output: output as Expression };
}

/** The table as the grid shows it. */
export function gridOf(spec: TableSpec): GridView {
  const inputs = columnsOf(spec.inputSchema);
  const row = rowSchema(spec);
  const scalar = !isFields(row);
  const results = scalar
    ? [{ name: "Result", type: columnTypeOf(row), required: true }]
    : columnsOf(row);
  const cellsOf = testable(inputs);
  return {
    hitPolicy: spec.hitPolicy,
    inputs,
    results,
    scalar,
    rows: spec.rules.map((rule) => {
      const cells = decomposeWhen(rule.when, cellsOf);
      return {
        id: rule.id,
        condition: cells
          ? { kind: "cells", cells }
          : { kind: "formula", when: rule.when },
        result: resultView(rule.output, results, scalar),
      };
    }),
    otherwise:
      spec.hitPolicy !== "collect" && spec.defaultOutput !== undefined
        ? resultView(spec.defaultOutput, results, scalar)
        : null,
  };
}

const withRule = (
  spec: TableSpec,
  index: number,
  rule: TableRule,
): TableSpec => ({
  ...spec,
  rules: spec.rules.map((current, i) => (i === index ? rule : current)),
});

/** The table with one cell of a rule's condition set (null clears it). */
export function setCondition(
  spec: TableSpec,
  rowIndex: number,
  columnIndex: number,
  cell: Expression | null,
): TableSpec {
  const rule = spec.rules[rowIndex];
  const columns = testable(columnsOf(spec.inputSchema));
  const cells = rule ? decomposeWhen(rule.when, columns) : null;
  if (!rule || !cells)
    throw Error(`Rule ${rowIndex + 1} is a formula; edit it as a formula.`);
  cells[columnIndex] = cell;
  return withRule(spec, rowIndex, { ...rule, when: composeWhen(cells) });
}

/** The table under another hit policy; collect turns results into a list and drops Otherwise. */
export function setHitPolicy(
  spec: TableSpec,
  policy: HitPolicy,
): { spec: TableSpec; removedOtherwise: boolean } {
  if (spec.hitPolicy === policy) return { spec, removedOtherwise: false };
  const row = rowSchema(spec);
  const next: TableSpec = {
    ...spec,
    hitPolicy: policy,
    outputSchema: policy === "collect" ? { type: "array", items: row } : row,
  };
  const removedOtherwise =
    policy === "collect" && spec.defaultOutput !== undefined;
  if (policy === "collect") delete next.defaultOutput;
  return { spec: next, removedOtherwise };
}

/** Rewrites references under /input/<from> in an expression; the same object when none changes. */
function renameRefs(node: unknown, from: string, to: string): unknown {
  if (Array.isArray(node)) {
    const items = node.map((item) => renameRefs(item, from, to));
    return items.some((item, i) => item !== node[i]) ? items : node;
  }
  if (!isRecord(node) || "literal" in node) return node;
  if (typeof node["ref"] === "string") {
    const ref = node["ref"];
    return ref === from || ref.startsWith(from + "/")
      ? { ...node, ref: to + ref.slice(from.length) }
      : node;
  }
  let changed = false;
  const copy: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(node)) {
    copy[key] = renameRefs(value, from, to);
    changed ||= copy[key] !== value;
  }
  return changed ? copy : node;
}

/** The table with an input column renamed in its schema and in every rule's references. */
export function renameInputColumn(
  spec: TableSpec,
  from: string,
  to: string,
): TableSpec {
  if (!FIELD_NAME.test(to))
    throw Error("Use a field name with letters, numbers or underscores.");
  const properties = propertiesOf(spec.inputSchema);
  if (!Object.hasOwn(properties, from))
    throw Error(`There is no column named ${from}.`);
  if (from === to) return spec;
  if (Object.hasOwn(properties, to))
    throw Error(`A column named ${to} already exists.`);
  const required = requiredOf(spec.inputSchema);
  const inputSchema: Record<string, Json> = {
    ...spec.inputSchema,
    properties: Object.fromEntries(
      Object.entries(properties).map(([key, value]) => [
        key === from ? to : key,
        value,
      ]),
    ),
  };
  if (Array.isArray(spec.inputSchema["required"]))
    inputSchema["required"] = required.map((key) => (key === from ? to : key));
  const fromRef = `/input/${escapeSegment(from)}`;
  const toRef = `/input/${escapeSegment(to)}`;
  const next: TableSpec = {
    ...spec,
    inputSchema,
    rules: spec.rules.map(
      (rule) => renameRefs(rule, fromRef, toRef) as TableRule,
    ),
  };
  if (spec.defaultOutput !== undefined)
    next.defaultOutput = renameRefs(
      spec.defaultOutput,
      fromRef,
      toRef,
    ) as Expression;
  return next;
}

const freeName = (
  taken: ReadonlySet<string>,
  make: (n: number) => string,
  first: number,
): string => {
  let n = first;
  while (taken.has(make(n))) n++;
  return make(n);
};

/** The table with an always-matching rule after `after` (-1 for the top). */
export function addRule(spec: TableSpec, after: number): TableSpec {
  const taken = new Set(spec.rules.map((rule) => rule.id));
  const id = freeName(taken, (n) => `rule-${n}`, spec.rules.length + 1);
  const output: Expression = { literal: isFields(rowSchema(spec)) ? {} : null };
  const rules = [...spec.rules];
  rules.splice(after + 1, 0, { id, when: { literal: true }, output });
  return { ...spec, rules };
}

/** The table with a copy of a rule right after it. */
export function duplicateRule(spec: TableSpec, index: number): TableSpec {
  const rule = spec.rules[index];
  if (!rule) return spec;
  const taken = new Set(spec.rules.map((item) => item.id));
  const base = RULE_NAME.test(rule.id) ? rule.id : "rule";
  const id = freeName(taken, (n) => `${base}-${n}`, 2);
  const rules = [...spec.rules];
  rules.splice(index + 1, 0, { ...structuredClone(rule), id });
  return { ...spec, rules };
}

/** The table with a rule moved up (-1) or down (1); the same table at either end. */
export function moveRule(
  spec: TableSpec,
  index: number,
  delta: -1 | 1,
): TableSpec {
  const target = index + delta;
  if (!spec.rules[index] || target < 0 || target >= spec.rules.length)
    return spec;
  const rules = [...spec.rules];
  [rules[index], rules[target]] = [rules[target], rules[index]];
  return { ...spec, rules };
}

/** The schema whose properties are the result columns: the output, or each item for collect. */
function withRowSchema(spec: TableSpec, row: Record<string, Json>): TableSpec {
  return spec.hitPolicy === "collect" && isRecord(spec.outputSchema["items"])
    ? { ...spec, outputSchema: { ...spec.outputSchema, items: row } }
    : { ...spec, outputSchema: row };
}
function checkNewName(properties: Record<string, Json>, name: string): void {
  if (!FIELD_NAME.test(name))
    throw Error("Use a field name with letters, numbers or underscores.");
  if (Object.hasOwn(properties, name))
    throw Error(`A column named ${name} already exists.`);
}
const withProperties = (
  schema: Record<string, Json>,
  properties: Record<string, Json>,
): Record<string, Json> => ({ ...schema, properties });

/** The table with a new input column of this type; no rule tests it yet. */
export function addInputColumn(
  spec: TableSpec,
  name: string,
  type: ColumnType,
): TableSpec {
  const properties = propertiesOf(spec.inputSchema);
  checkNewName(properties, name);
  return {
    ...spec,
    inputSchema: withProperties(spec.inputSchema, {
      ...properties,
      [name]: schemaOfColumnType(type),
    }),
  };
}

/** The table with a new result column of this type; no rule fills it yet. */
export function addResultColumn(
  spec: TableSpec,
  name: string,
  type: ColumnType,
): TableSpec {
  const row = rowSchema(spec);
  const properties = propertiesOf(row);
  checkNewName(properties, name);
  return withRowSchema(
    spec,
    withProperties(
      { ...row, type: "object" },
      { ...properties, [name]: schemaOfColumnType(type) },
    ),
  );
}

/** The table with another type for an input column; cells that no longer read are the grid's to flag. */
export function setInputColumnType(
  spec: TableSpec,
  name: string,
  type: ColumnType,
): TableSpec {
  const properties = propertiesOf(spec.inputSchema);
  if (!Object.hasOwn(properties, name))
    throw Error(`There is no column named ${name}.`);
  return {
    ...spec,
    inputSchema: withProperties(
      spec.inputSchema,
      Object.fromEntries(
        Object.entries(properties).map(([key, value]) => [
          key,
          key === name ? schemaOfColumnType(type) : value,
        ]),
      ),
    ),
  };
}

/** The table without an input column: its cells leave every rule shown as cells; formulas stay as written. */
export function removeInputColumn(spec: TableSpec, name: string): TableSpec {
  const properties = propertiesOf(spec.inputSchema);
  if (!Object.hasOwn(properties, name))
    throw Error(`There is no column named ${name}.`);
  const columns = testable(columnsOf(spec.inputSchema));
  const at = columns.findIndex((column) => column.name === name);
  const inputSchema = withProperties(
    spec.inputSchema,
    Object.fromEntries(
      Object.entries(properties).filter(([key]) => key !== name),
    ),
  );
  if (Array.isArray(spec.inputSchema["required"]))
    inputSchema["required"] = requiredOf(spec.inputSchema).filter(
      (key) => key !== name,
    );
  return {
    ...spec,
    inputSchema,
    rules: spec.rules.map((rule) => {
      const cells = decomposeWhen(rule.when, columns);
      if (!cells || cells[at] === null) return rule;
      cells.splice(at, 1);
      return { ...rule, when: composeWhen(cells) };
    }),
  };
}

/** A result written field by field, with one field renamed or removed (`to` null); the same object when untouched. */
function editFields(
  output: Expression,
  from: string,
  to: string | null,
): Expression {
  if (!isRecord(output) || Object.keys(output).length !== 1) return output;
  const key = isRecord(output["literal"])
    ? "literal"
    : isRecord(output["object"])
      ? "object"
      : null;
  if (!key) return output;
  const fields = output[key] as Record<string, Json>;
  if (!Object.hasOwn(fields, from)) return output;
  return {
    [key]: Object.fromEntries(
      Object.entries(fields).flatMap(([name, value]) =>
        name !== from ? [[name, value]] : to === null ? [] : [[to, value]],
      ),
    ),
  } as Expression;
}
function editResults(
  spec: TableSpec,
  from: string,
  to: string | null,
): TableSpec {
  const row = rowSchema(spec);
  const properties = propertiesOf(row);
  if (!Object.hasOwn(properties, from))
    throw Error(`There is no column named ${from}.`);
  if (to !== null && to !== from) checkNewName(properties, to);
  const nextRow = withProperties(
    row,
    Object.fromEntries(
      Object.entries(properties).flatMap(([name, value]) =>
        name !== from ? [[name, value]] : to === null ? [] : [[to, value]],
      ),
    ),
  );
  if (Array.isArray(row["required"]))
    nextRow["required"] = requiredOf(row).flatMap((name) =>
      name !== from ? [name] : to === null ? [] : [to],
    );
  const next: TableSpec = {
    ...withRowSchema(spec, nextRow),
    rules: spec.rules.map((rule) => {
      const output = editFields(rule.output, from, to);
      return output === rule.output ? rule : { ...rule, output };
    }),
  };
  if (spec.defaultOutput !== undefined)
    next.defaultOutput = editFields(spec.defaultOutput, from, to);
  return next;
}

/** The table with a result column renamed in its schema, every rule and Otherwise. */
export const renameResultColumn = (
  spec: TableSpec,
  from: string,
  to: string,
): TableSpec => (from === to ? spec : editResults(spec, from, to));

/** The table without a result column. */
export const removeResultColumn = (spec: TableSpec, name: string): TableSpec =>
  editResults(spec, name, null);

/** The table with a rule renamed; the problems line says when a name can't be used. */
export function renameRule(
  spec: TableSpec,
  index: number,
  id: string,
): TableSpec {
  const rule = spec.rules[index];
  return !rule || rule.id === id
    ? spec
    : withRule(spec, index, { ...rule, id });
}

/** The table without a rule; a table keeps at least one. */
export function removeRule(spec: TableSpec, index: number): TableSpec {
  if (spec.rules.length <= 1) throw Error("A table keeps at least one rule.");
  return { ...spec, rules: spec.rules.filter((_, i) => i !== index) };
}

/**
 * The table with one result cell set (undefined clears it) in a rule or in
 * Otherwise: `{literal: {...}}` while every cell is fixed, `{object: {...}}`
 * once one is a formula, the expression itself for a scalar result.
 */
export function setResult(
  spec: TableSpec,
  row: number | "otherwise",
  columnIndex: number,
  value: Expression | undefined,
): TableSpec {
  const view = gridOf(spec);
  const current =
    row === "otherwise" ? view.otherwise : (view.rows[row]?.result ?? null);
  let output: Expression;
  if (view.scalar) output = value ?? { literal: null };
  else {
    const cells =
      current?.kind === "cells"
        ? [...current.cells]
        : current === null
          ? view.results.map(() => undefined)
          : null;
    if (!cells)
      throw Error(
        `${row === "otherwise" ? "Otherwise" : `Rule ${row + 1}`}'s result is a formula; edit it as a formula.`,
      );
    cells[columnIndex] = value;
    const names = view.results.map((column) => column.name);
    const set = names
      .map((name, i) => [name, cells[i]] as const)
      .filter((entry): entry is readonly [string, Expression] => !!entry[1]);
    const fixed = set.every(
      ([, cell]) =>
        isRecord(cell) && Object.keys(cell).length === 1 && "literal" in cell,
    );
    output = fixed
      ? {
          literal: Object.fromEntries(
            set.map(([name, cell]) => [
              name,
              (cell as { literal: Json }).literal,
            ]),
          ),
        }
      : ({ object: Object.fromEntries(set) } as Expression);
  }
  if (row === "otherwise") return { ...spec, defaultOutput: output };
  const rule = spec.rules[row];
  return rule ? withRule(spec, row, { ...rule, output }) : spec;
}

/** The table with a rule's whole condition replaced by a formula. */
export function setRuleFormula(
  spec: TableSpec,
  index: number,
  when: Expression,
): TableSpec {
  const rule = spec.rules[index];
  return rule ? withRule(spec, index, { ...rule, when }) : spec;
}
