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
// The decision table grid's input cells. Each cell tests its own column
// (`/input/<column>`) with a short text ("> 1000", "ES, PT", "missing") that
// reads as exactly one expression of the language and prints back as the same
// text, so a rule's condition can be shown as cells only when it is one of
// these. Pure: the grid, its checks and its tests use it.
import { escapeSegment, type Json } from "../../forms/core/json";
import type { Expression } from "../ndv/registry";

export type ColumnType =
  | "text"
  | "number"
  | "integer"
  | "boolean"
  | "dateTime"
  | "textList";
export interface Column {
  name: string;
  type: ColumnType;
}
export type CellReading =
  | { kind: "empty" }
  | { kind: "test"; expression: Expression }
  | { kind: "formula"; text: string }
  | { kind: "error"; problem: "parse" | "type"; message: string };

export const COLUMN_TYPE_LABELS: Readonly<Record<ColumnType, string>> = {
  text: "Text",
  number: "Number",
  integer: "Whole number",
  boolean: "Yes/No",
  dateTime: "Date-time text",
  textList: "List of text",
};

const ORDERED: ReadonlySet<ColumnType> = new Set([
  "number",
  "integer",
  "dateTime",
]);
const TEXTUAL: ReadonlySet<ColumnType> = new Set(["text", "dateTime"]);
const ORDER_OPS: Readonly<Record<string, string>> = {
  ">": "gt",
  ">=": "gte",
  "<": "lt",
  "<=": "lte",
};
const ORDER_TEXT: Readonly<Record<string, string>> = Object.fromEntries(
  Object.entries(ORDER_OPS).map(([text, name]) => [name, text]),
);
const TEXT_TESTS: readonly [string, string][] = [
  ["contains ", "contains"],
  ["starts with ", "startsWith"],
  ["ends with ", "endsWith"],
];
const NUMBER = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$/;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const refOf = (column: Column): Expression => ({
  ref: `/input/${escapeSegment(column.name)}`,
});
const op = (name: string, ...args: Expression[]): Expression => ({
  op: { name, args },
});
const literal = (value: Json): Expression => ({ literal: value });
const error = (problem: "parse" | "type", message: string): CellReading => ({
  kind: "error",
  problem,
  message,
});
const typeError = (test: string, column: Column) =>
  error("type", `${test} test in a ${COLUMN_TYPE_LABELS[column.type]} column.`);

/** The column type a property's schema describes; null for any other schema. */
export function columnTypeOf(schema: unknown): ColumnType | null {
  if (!isRecord(schema)) return null;
  if (schema["type"] === "string")
    return schema["format"] === "date-time" ? "dateTime" : "text";
  if (schema["type"] === "number") return "number";
  if (schema["type"] === "integer") return "integer";
  if (schema["type"] === "boolean") return "boolean";
  if (
    schema["type"] === "array" &&
    isRecord(schema["items"]) &&
    schema["items"]["type"] === "string" &&
    Object.keys(schema["items"]).length === 1
  )
    return "textList";
  return null;
}

/** The schema a new column of this type writes. */
export function schemaOfColumnType(type: ColumnType): Record<string, Json> {
  switch (type) {
    case "text":
      return { type: "string" };
    case "dateTime":
      return { type: "string", format: "date-time" };
    case "textList":
      return { type: "array", items: { type: "string" } };
    default:
      return { type };
  }
}

type Literal = { ok: true; value: Json } | { ok: false; reading: CellReading };

/** One value as the column reads it: quoted text, a number, true or false, or a bare word in a text column. */
function readValue(text: string, column: Column, bare = true): Literal {
  const fail = (reading: CellReading): Literal => ({ ok: false, reading });
  if (text.startsWith('"')) {
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch {
      return fail(
        text.length > 1 && text.endsWith('"')
          ? error("parse", 'Write text in double quotes, like "gold".')
          : error("parse", `Close the quote: ${text}".`),
      );
    }
    if (typeof value !== "string")
      return fail(error("parse", 'Write text in double quotes, like "gold".'));
    return TEXTUAL.has(column.type) || column.type === "textList"
      ? { ok: true, value }
      : fail(typeError("Text", column));
  }
  if (column.type === "boolean") {
    if (text === "true" || text === "false")
      return { ok: true, value: text === "true" };
    return fail(error("type", "Use true or false in a Yes/No column."));
  }
  if (column.type === "number" || column.type === "integer") {
    if (NUMBER.test(text)) {
      const value = Number(text);
      if (!Number.isFinite(value))
        return fail(error("parse", "This number is too large."));
      if (column.type === "integer" && !Number.isInteger(value))
        return fail(
          error("type", "Use a whole number in a Whole number column."),
        );
      return { ok: true, value };
    }
    if (text === "true" || text === "false")
      return fail(typeError("Yes/No", column));
    return fail(typeError("Text", column));
  }
  if (!bare || !text || /["]/.test(text))
    return fail(error("parse", 'Write text in double quotes, like "gold".'));
  return { ok: true, value: text };
}

/** Splits on commas outside double quotes. */
function splitList(text: string): string[] {
  const parts: string[] = [];
  let current = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === "\\" && quoted) {
      current += char + (text[i + 1] ?? "");
      i++;
      continue;
    }
    if (char === '"') quoted = !quoted;
    if (char === "," && !quoted) {
      parts.push(current.trim());
      current = "";
    } else current += char;
  }
  parts.push(current.trim());
  return parts;
}

function readList(text: string, column: Column): Literal {
  const values: Json[] = [];
  for (const part of splitList(text)) {
    if (!part)
      return {
        ok: false,
        reading: error("parse", "Remove the empty value between commas."),
      };
    const value = readValue(part, column);
    if (!value.ok) return value;
    values.push(value.value);
  }
  return { ok: true, value: values };
}

const lessThan = (a: Json, b: Json): boolean =>
  typeof a === "number" && typeof b === "number"
    ? a < b
    : typeof a === "string" && typeof b === "string" && a < b;

/** Reads a cell's text for its column. */
export function parseCell(input: string, column: Column): CellReading {
  const text = input.trim();
  if (text === "" || text === "-") return { kind: "empty" };
  const ref = refOf(column);
  const word = text.toLowerCase();
  if (word === "present")
    return { kind: "test", expression: op("exists", ref) };
  if (word === "missing")
    return { kind: "test", expression: op("not", op("exists", ref)) };
  if (column.type === "textList") {
    const contains = /^contains\s+/i.exec(text);
    if (!contains)
      return error(
        "type",
        "Use contains, present or missing in a List of text column.",
      );
    const value = readValue(text.slice(contains[0].length).trim(), column);
    return value.ok
      ? { kind: "test", expression: op("contains", ref, literal(value.value)) }
      : value.reading;
  }
  const operator = /^[<>=!]+/.exec(text)?.[0];
  if (operator) {
    const rest = text.slice(operator.length).trim();
    if (operator === "=") {
      if (!rest) return error("parse", "Write a value or a formula after =.");
      const value = readValue(rest, column, false);
      return value.ok
        ? { kind: "test", expression: op("eq", ref, literal(value.value)) }
        : { kind: "formula", text: rest };
    }
    if (operator === "!=") {
      const value = readValue(rest, column);
      return value.ok
        ? { kind: "test", expression: op("ne", ref, literal(value.value)) }
        : value.reading;
    }
    const name = ORDER_OPS[operator];
    if (!name) {
      const hint = operator.startsWith("<")
        ? "<"
        : operator.startsWith("!")
          ? "!="
          : operator.startsWith(">")
            ? ">"
            : "=";
      return error(
        "parse",
        `\`${operator}\` isn't a test; try \`${hint} ${rest || "1000"}\`.`,
      );
    }
    if (!ORDERED.has(column.type))
      return NUMBER.test(rest)
        ? typeError("Number", column)
        : error(
            "type",
            "Use >, >=, < and <= in Number, Whole number or Date-time text columns.",
          );
    const value = readValue(rest, column);
    return value.ok
      ? { kind: "test", expression: op(name, ref, literal(value.value)) }
      : value.reading;
  }
  const range = /^([[(])(.*)\.\.(.*)([\])])$/.exec(text);
  if (range) {
    if (!ORDERED.has(column.type))
      return error(
        "type",
        "Use ranges in Number, Whole number or Date-time text columns.",
      );
    const low = readValue(range[2].trim(), column);
    if (!low.ok) return low.reading;
    const high = readValue(range[3].trim(), column);
    if (!high.ok) return high.reading;
    if (lessThan(high.value, low.value))
      return error(
        "parse",
        "A range goes from the smaller value to the larger one.",
      );
    return {
      kind: "test",
      expression: op(
        "and",
        op(range[1] === "[" ? "gte" : "gt", ref, literal(low.value)),
        op(range[4] === "]" ? "lte" : "lt", ref, literal(high.value)),
      ),
    };
  }
  const not = /^not\s*\((.*)\)$/i.exec(text);
  if (not) {
    const values = readList(not[1], column);
    return values.ok
      ? { kind: "test", expression: op("notIn", ref, literal(values.value)) }
      : values.reading;
  }
  for (const [prefix, name] of TEXT_TESTS)
    if (word.startsWith(prefix)) {
      if (!TEXTUAL.has(column.type)) return typeError("Text", column);
      const value = readValue(text.slice(prefix.length).trim(), column);
      return value.ok
        ? { kind: "test", expression: op(name, ref, literal(value.value)) }
        : value.reading;
    }
  const parts = splitList(text);
  if (parts.length > 1) {
    const values = readList(text, column);
    return values.ok
      ? { kind: "test", expression: op("in", ref, literal(values.value)) }
      : values.reading;
  }
  const value = readValue(text, column);
  return value.ok
    ? { kind: "test", expression: op("eq", ref, literal(value.value)) }
    : value.reading;
}

const fits = (value: unknown, column: Column): value is Json => {
  switch (column.type) {
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "integer":
      return typeof value === "number" && Number.isInteger(value);
    case "boolean":
      return typeof value === "boolean";
    default:
      return typeof value === "string";
  }
};
const printValue = (value: Json): string =>
  typeof value === "string" ? JSON.stringify(value) : String(value);

/** The parts of `{op: {name, args}}` with nothing else in it, or null. */
function opOf(node: unknown): { name: string; args: unknown[] } | null {
  if (
    !isRecord(node) ||
    Object.keys(node).length !== 1 ||
    !isRecord(node["op"])
  )
    return null;
  const inner = node["op"];
  if (
    Object.keys(inner).length !== 2 ||
    typeof inner["name"] !== "string" ||
    !Array.isArray(inner["args"])
  )
    return null;
  return { name: inner["name"], args: inner["args"] };
}
const literalOf = (node: unknown): { value: unknown } | null =>
  isRecord(node) && Object.keys(node).length === 1 && "literal" in node
    ? { value: node["literal"] }
    : null;
const isRefTo = (node: unknown, column: Column): boolean =>
  isRecord(node) &&
  Object.keys(node).length === 1 &&
  node["ref"] === `/input/${escapeSegment(column.name)}`;

/** A comparison of the column with one value that fits it: [operator name, value]. */
function compared(node: unknown, column: Column): [string, Json] | null {
  const parts = opOf(node);
  if (!parts || parts.args.length !== 2 || !isRefTo(parts.args[0], column))
    return null;
  const value = literalOf(parts.args[1]);
  return value && fits(value.value, column) ? [parts.name, value.value] : null;
}

/** The cell text an expression prints as in this column; null when it isn't one of the column's cells. */
export function printCell(
  expression: Expression | null,
  column: Column,
): string | null {
  if (expression === null) return "-";
  const parts = opOf(expression);
  if (!parts) return null;
  if (parts.name === "exists")
    return parts.args.length === 1 && isRefTo(parts.args[0], column)
      ? "present"
      : null;
  if (parts.name === "not") {
    const inner = parts.args.length === 1 ? opOf(parts.args[0]) : null;
    return inner?.name === "exists" &&
      inner.args.length === 1 &&
      isRefTo(inner.args[0], column)
      ? "missing"
      : null;
  }
  if (column.type === "textList") {
    const test = compared(expression, column);
    return test && test[0] === "contains"
      ? `contains ${printValue(test[1])}`
      : null;
  }
  if (parts.name === "and") {
    if (!ORDERED.has(column.type) || parts.args.length !== 2) return null;
    const low = compared(parts.args[0], column);
    const high = compared(parts.args[1], column);
    if (
      !low ||
      !high ||
      (low[0] !== "gte" && low[0] !== "gt") ||
      (high[0] !== "lte" && high[0] !== "lt") ||
      lessThan(high[1], low[1])
    )
      return null;
    return `${low[0] === "gte" ? "[" : "("}${printValue(low[1])}..${printValue(high[1])}${high[0] === "lte" ? "]" : ")"}`;
  }
  if (parts.name === "in" || parts.name === "notIn") {
    if (
      parts.args.length !== 2 ||
      !isRefTo(parts.args[0], column) ||
      column.type === "boolean"
    )
      return null;
    const list = literalOf(parts.args[1]);
    if (
      !list ||
      !Array.isArray(list.value) ||
      list.value.length < (parts.name === "in" ? 2 : 1) ||
      !list.value.every((item) => fits(item, column))
    )
      return null;
    const text = (list.value as Json[]).map(printValue).join(", ");
    return parts.name === "in" ? text : `not(${text})`;
  }
  const test = compared(expression, column);
  if (!test) return null;
  const [name, value] = test;
  if (name === "eq") return printValue(value);
  if (name === "ne") return `!= ${printValue(value)}`;
  if (ORDER_TEXT[name])
    return ORDERED.has(column.type)
      ? `${ORDER_TEXT[name]} ${printValue(value)}`
      : null;
  const prefix = TEXT_TESTS.find(([, op]) => op === name)?.[0];
  return prefix && TEXTUAL.has(column.type)
    ? `${prefix}${printValue(value)}`
    : null;
}
