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
// The rules behind the schema form (task-form.ts), free of Angular so vitest
// runs them under Node:
// - which widget a resolved field gets (forms/core/resolve.ts decides the
//   field kind; this decides how it is edited);
// - the data a form starts from: secret values removed, required booleans
//   and constants filled (a missing required boolean is rejected by the
//   platform, a `false` is not);
// - which required fields are still missing, for plain data and for an
//   expression whose fields may be bound to data or a formula.
import {
  describeField,
  fieldsOf,
  type FieldInfo,
  type ResolveOptions,
} from "./resolve";
import { decode, get, type Bound } from "./binding";
import { isPointer } from "./json";

type Data = Record<string, unknown>;
const isRecord = (value: unknown): value is Data =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** Nested groups render as fields up to this depth, then as JSON. */
export const MAX_GROUP_DEPTH = 4;

export type Widget =
  | "text"
  | "multiline"
  | "number"
  | "integer"
  | "checkbox"
  | "tristate"
  | "choice"
  | "const"
  | "datetime"
  | "email"
  | "uri"
  | "uuid"
  | "secret"
  | "group"
  | "map"
  | "list"
  | "table"
  | "any"
  | "json"
  | "never";

/** Kinds a list item, a map value or a table cell can be edited with directly. */
const SCALARS = new Set([
  "text",
  "multiline",
  "number",
  "integer",
  "boolean",
  "choice",
  "datetime",
  "email",
  "uri",
  "uuid",
]);

/** The widget that edits a field; `depth` is how deeply its group is nested. */
export function widgetFor(field: FieldInfo, depth = 0): Widget {
  switch (field.kind) {
    case "secret":
    case "never":
    case "const":
    case "choice":
    case "text":
    case "multiline":
    case "number":
    case "integer":
    case "datetime":
    case "email":
    case "uri":
    case "uuid":
    case "list":
      return field.kind;
    case "boolean":
      return field.required ? "checkbox" : "tristate";
    case "object":
      return depth < MAX_GROUP_DEPTH ? "group" : "json";
    case "table":
      return depth < MAX_GROUP_DEPTH ? "table" : "json";
    case "map":
      return "map";
    case "any":
      // `{}`: a typed value (text, number, yes/no, group or list).
      return "any";
    default:
      // union and json: the person writes JSON; the server validates.
      return "json";
  }
}

/** The schema of a map's values, or of a list's items, as a field. */
export function memberField(field: FieldInfo): FieldInfo | null {
  const schema = field.schema;
  const member =
    field.kind === "map"
      ? schema["additionalProperties"]
      : field.kind === "list" || field.kind === "table"
        ? schema["items"]
        : undefined;
  if (member === undefined || member === true)
    return field.kind === "list"
      ? null
      : describeField("value", {}, field.context);
  return describeField(field.kind === "map" ? "value" : "item", member, {
    ...field.context,
    required: true,
  });
}

/** True when map values or list items are edited as plain inputs, not JSON. */
export function scalarMember(field: FieldInfo | null): boolean {
  return !!field && SCALARS.has(field.kind);
}

/** Every field the form shows, resolved; nested groups up to the depth limit. */
export function formFields(
  schema: unknown,
  options: ResolveOptions = {},
): FieldInfo[] {
  return fieldsOf(schema, { root: schema, ...options });
}

/**
 * The data a form starts from: secret values dropped, and in every object the
 * person must fill (the root, a required group, or a group that has data),
 * required booleans set to their default or `false` and required constants
 * set. `filled` says whether anything was added or removed.
 */
export function prepareData(
  schema: unknown,
  data: unknown,
  options: ResolveOptions = {},
): { data: Data; changed: boolean } {
  const copy: Data = isRecord(data) ? structuredClone(data) : {};
  let changed = !isRecord(data);
  const visit = (fields: FieldInfo[], target: Data, depth: number) => {
    for (const field of fields) {
      const present = Object.hasOwn(target, field.key);
      if (field.secret) {
        if (present) {
          delete target[field.key];
          changed = true;
        }
        continue;
      }
      if (!present && field.required) {
        if (field.kind === "boolean") {
          target[field.key] =
            typeof field.default === "boolean" ? field.default : false;
          changed = true;
        } else if (field.kind === "const") {
          target[field.key] = structuredClone(field.constValue ?? null);
          changed = true;
        }
      }
      if (field.kind !== "object" || depth + 1 >= MAX_GROUP_DEPTH) continue;
      const child = target[field.key];
      if (child === undefined && !field.required) continue;
      if (child !== undefined && !isRecord(child)) continue;
      const group: Data = isRecord(child) ? child : {};
      const before = Object.keys(group).length;
      visit(fieldsOf(field.schema, field.context), group, depth + 1);
      if (child === undefined && Object.keys(group).length > before)
        target[field.key] = group;
    }
  };
  visit(formFields(schema, options), copy, 0);
  return { data: copy, changed };
}

const emptyValue = (value: unknown) => value === undefined || value === "";

/** Labels of required fields without a value; secrets and constants never count. */
export function missingData(
  schema: unknown,
  data: unknown,
  options: ResolveOptions = {},
): string[] {
  const missing: string[] = [];
  const visit = (fields: FieldInfo[], values: unknown, prefix: string) => {
    const record = isRecord(values) ? values : {};
    for (const field of fields) {
      if (field.secret || field.kind === "const" || field.kind === "never")
        continue;
      const value = record[field.key];
      const label = prefix + field.label;
      if (field.required && emptyValue(value)) missing.push(label);
      else if (field.kind === "object" && isRecord(value))
        visit(fieldsOf(field.schema, field.context), value, label + " › ");
    }
  };
  visit(formFields(schema, options), data, "");
  return missing;
}

/**
 * Labels of required fields an input expression leaves unset. A field bound to
 * data or a formula counts as set: the compiler checks what it produces.
 */
export function missingBound(
  schema: unknown,
  expression: unknown,
  options: ResolveOptions = {},
): string[] {
  const bound = decode(expression ?? { literal: {} }, schema);
  if (bound.kind === "ref" || bound.kind === "formula") return [];
  const missing: string[] = [];
  const visit = (
    fields: FieldInfo[],
    node: Bound | undefined,
    prefix: string,
  ) => {
    for (const field of fields) {
      if (field.secret || field.kind === "const" || field.kind === "never")
        continue;
      const child = node ? get(node, [field.key]) : undefined;
      const label = prefix + field.label;
      const empty =
        !child || (child.kind === "value" && emptyValue(child.value));
      if (field.required && empty) missing.push(label);
      else if (field.kind === "object" && child?.kind === "object")
        visit(fieldsOf(field.schema, field.context), child, label + " › ");
    }
  };
  visit(formFields(schema, options), bound, "");
  return missing;
}

/** True when an input expression can be edited field by field. */
export function fieldable(expression: unknown): boolean {
  if (expression === undefined) return true;
  if (!isRecord(expression) || Object.keys(expression).length !== 1)
    return false;
  return isRecord(expression["literal"]) || isRecord(expression["object"]);
}

/** Datetime-local text ("2026-10-02T09:30") from a stored RFC 3339 value. */
export function localDateTime(value: unknown): string {
  if (typeof value !== "string") return "";
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return "";
  const date = new Date(time);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** RFC 3339 (UTC) from datetime-local text; undefined when empty or invalid. */
export function storedDateTime(text: string): string | undefined {
  if (!text.trim()) return undefined;
  const time = Date.parse(text);
  return Number.isFinite(time) ? new Date(time).toISOString() : undefined;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
/** A plain explanation when text doesn't fit the field's format, else "". */
export function formatProblem(widget: Widget, text: string, label: string) {
  if (!text) return "";
  if (widget === "uuid" && !UUID.test(text))
    return `${label} must be an ID such as 3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e.`;
  if (widget === "email" && !/^[^\s@]+@[^\s@]+$/.test(text))
    return `${label} must be an email address.`;
  if (widget === "uri" && !/^[A-Za-z][A-Za-z0-9+.-]*:\S*$/.test(text))
    return `${label} must be a full address, such as https://example.com/page.`;
  return "";
}

/** A data reference must be a JSON pointer; the compiler decides what it may read. */
export const validReference = (text: string) =>
  text.startsWith("/") && isPointer(text);
