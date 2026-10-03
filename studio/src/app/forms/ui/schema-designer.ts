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
// Field-list editor for workflow input, output, signal payload and human-task
// form schemas. Load it lazily (`@defer` or `import()`): it is not part of
// the initial bundle.
//
// The model keeps each field's original schema object and rewrites only the
// keywords a person changed, so an unedited schema comes back identical (key
// order included) and keywords the designer cannot edit are preserved and
// listed. Sample inference goes through an injected function, normally the
// local Studio host's HTTP action builder: values are never stored, only
// field names and types come back.

import {
  ChangeDetectorRef,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  OnChanges,
  SimpleChanges,
  afterNextRender,
  computed,
  inject,
  input,
  output,
  signal,
} from "@angular/core";
import { NgTemplateOutlet } from "@angular/common";
import { Modal } from "../../dialog";
import { describeError } from "../../errors";
import { TaskForm, type Schema as TaskSchema } from "../../task-form";
import {
  canonicalJson,
  getAt,
  isJsonObject,
  parseJsonText,
  type Json,
  type JsonObject,
} from "../core/json";

// ---------------------------------------------------------------------------
// Model (pure; vitest runs it under Node)
// ---------------------------------------------------------------------------

export type RowType =
  | "string"
  | "number"
  | "integer"
  | "boolean"
  | "object"
  | "array"
  | "any"
  | "null"
  | "advanced";
export type EditableType = Exclude<RowType, "advanced">;
export type Format = "" | "date-time" | "email" | "uri" | "uuid";

export const typeOptions: { value: EditableType; label: string }[] = [
  { value: "string", label: "Text" },
  { value: "number", label: "Number" },
  { value: "integer", label: "Whole number" },
  { value: "boolean", label: "Yes or no" },
  { value: "object", label: "Group of fields" },
  { value: "array", label: "List" },
  { value: "any", label: "Any value" },
  { value: "null", label: "Empty (null)" },
];
export const formatOptions: { value: Format; label: string }[] = [
  { value: "", label: "Any text" },
  { value: "date-time", label: "Date and time" },
  { value: "email", label: "Email address" },
  { value: "uri", label: "Web address (URI)" },
  { value: "uuid", label: "UUID" },
];

export interface DesignerRow {
  /** Stable identity for the UI (ids, focus, tracking). */
  uid: number;
  name: string;
  required: boolean;
  type: RowType;
  nullable: boolean;
  title: string;
  description: string;
  /** Allowed values, one per line; empty means any value of the type. */
  choices: string;
  /** Smallest value, shortest text or fewest items, as typed. */
  min: string;
  /** Largest value, longest text or most items, as typed. */
  max: string;
  format: Format;
  /** Fields of a group (type object). */
  children: DesignerRow[];
  /** A group that rejects fields it does not list (`additionalProperties: false`). */
  closed: boolean;
  /** What each list item is (type array); its name is unused. */
  item: DesignerRow | null;
  /** Keywords kept unchanged that this designer cannot edit (shown to the person). */
  kept: string[];
  /** The keyword that makes the type uneditable here, for example "oneOf". */
  locked: string | null;
  /** UI: the details panel is open. */
  expanded: boolean;
  /** The schema this row was read from; unchanged keywords are copied from it. */
  readonly source: Json | undefined;
  /** Type and nullability as read, so an unchanged type is never rewritten. */
  readonly original: { type: RowType; nullable: boolean };
  enumKept: boolean;
  formatKept: boolean;
  closedKept: boolean;
}

export interface DesignerModel {
  /** False when the root is not a list of fields; it is then kept unchanged. */
  editable: boolean;
  reason?: string;
  rows: DesignerRow[];
  closed: boolean;
  closedKept: boolean;
  kept: string[];
  readonly source: unknown;
}

export interface DesignerIssue {
  row: number;
  field: "name" | "min" | "max" | "choices";
  message: string;
}

/** Keywords that make a field's type something the row editor cannot show. */
const LOCKING = [
  "$ref",
  "oneOf",
  "anyOf",
  "allOf",
  "not",
  "if",
  "then",
  "else",
  "const",
  "prefixItems",
  "contains",
  "patternProperties",
  "propertyNames",
  "dependentSchemas",
];
const FORMATS = new Set(["date-time", "email", "uri", "uuid"]);
const SCALAR = new Set(["string", "number", "integer", "boolean", "null"]);
/** Keywords that only apply to one JSON type; dropped when the type changes. */
const ONLY_FOR: Record<string, string[]> = {
  string: ["minLength", "maxLength", "pattern", "format"],
  number: [
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
  ],
  array: [
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minContains",
    "maxContains",
  ],
  object: [
    "properties",
    "required",
    "additionalProperties",
    "minProperties",
    "maxProperties",
    "dependentRequired",
  ],
};
const familyOf = (type: RowType) =>
  type === "integer" ? "number" : type in ONLY_FOR ? type : null;
const boundsFor = (type: RowType): [string, string] | null =>
  type === "string"
    ? ["minLength", "maxLength"]
    : type === "number" || type === "integer"
      ? ["minimum", "maximum"]
      : type === "array"
        ? ["minItems", "maxItems"]
        : null;
const ROOT_ANNOTATIONS = new Set([
  "title",
  "description",
  "$comment",
  "$schema",
  "$id",
]);

let uidSequence = 0;
const own = (object: JsonObject, key: string): Json | undefined =>
  Object.prototype.hasOwnProperty.call(object, key) ? object[key] : undefined;
function define(object: JsonObject, key: string, value: Json) {
  Object.defineProperty(object, key, {
    value,
    enumerable: true,
    writable: true,
    configurable: true,
  });
}

/** A blank optional text field. */
export function newRow(type: EditableType = "string"): DesignerRow {
  return {
    uid: ++uidSequence,
    name: "",
    required: false,
    type,
    nullable: false,
    title: "",
    description: "",
    choices: "",
    min: "",
    max: "",
    format: "",
    children: [],
    closed: false,
    item: type === "array" ? newRow() : null,
    kept: [],
    locked: null,
    expanded: false,
    source: undefined,
    original: { type: "any", nullable: false },
    enumKept: false,
    formatKept: false,
    closedKept: false,
  };
}

function choicesText(values: Json[], type: RowType): string | null {
  if (!values.length) return null;
  if (type === "string") {
    const strings = values.filter(
      (v): v is string =>
        typeof v === "string" && v.trim() !== "" && !/[\r\n]/.test(v),
    );
    return strings.length === values.length &&
      new Set(strings).size === strings.length
      ? strings.join("\n")
      : null;
  }
  if (type === "number" || type === "integer") {
    const numbers = values.filter(
      (v): v is number =>
        typeof v === "number" && (type === "number" || Number.isInteger(v)),
    );
    return numbers.length === values.length ? numbers.join("\n") : null;
  }
  return null;
}

/** Reads one field schema into a row. */
export function schemaToRow(
  name: string,
  schema: Json | undefined,
  required: boolean,
): DesignerRow {
  const row = newRow();
  row.name = name;
  row.required = required;
  if (!isJsonObject(schema)) {
    // A boolean schema (true: anything, false: nothing) is kept as written.
    return {
      ...row,
      type: "advanced",
      locked: "true/false schema",
      source: schema,
      original: { type: "advanced", nullable: false },
    };
  }
  const title = own(schema, "title");
  const description = own(schema, "description");
  row.title = typeof title === "string" ? title : "";
  row.description = typeof description === "string" ? description : "";
  const modeled = new Set(["title", "description"]);
  if (typeof title !== "string") modeled.delete("title");
  if (typeof description !== "string") modeled.delete("description");

  const lock = LOCKING.find((k) => own(schema, k) !== undefined);
  const raw = own(schema, "type");
  let type: RowType;
  let nullable = false;
  if (lock) type = "advanced";
  else if (typeof raw === "string")
    type =
      SCALAR.has(raw) || raw === "object" || raw === "array"
        ? (raw as RowType)
        : "advanced";
  else if (Array.isArray(raw)) {
    const others = raw.filter((t) => t !== "null");
    nullable = others.length < raw.length;
    type =
      others.length === 1 &&
      typeof others[0] === "string" &&
      others[0] !== "null"
        ? (others[0] as RowType)
        : "advanced";
    if (type === "advanced") nullable = false;
  } else if (raw === undefined)
    type = isJsonObject(own(schema, "properties"))
      ? "object"
      : own(schema, "items") !== undefined
        ? "array"
        : "any";
  else type = "advanced";
  const locked = lock ?? (type === "advanced" ? "type" : null);
  if (!locked || raw !== undefined) modeled.add("type");

  if (!locked) {
    const values = own(schema, "enum");
    if (Array.isArray(values)) {
      // A nullable field lists null among its values; the row shows the others.
      const listed = nullable ? values.filter((v) => v !== null) : values;
      const text = choicesText(listed, type);
      if (text !== null) {
        row.choices = text;
        modeled.add("enum");
      } else row.enumKept = true;
    }
    const bounds = boundsFor(type);
    if (bounds) {
      const [low, high] = bounds;
      const min = own(schema, low);
      const max = own(schema, high);
      if (typeof min === "number") ((row.min = String(min)), modeled.add(low));
      if (typeof max === "number") ((row.max = String(max)), modeled.add(high));
    }
    const format = own(schema, "format");
    if (type === "string" && typeof format === "string") {
      if (FORMATS.has(format)) {
        row.format = format as Format;
        modeled.add("format");
      } else row.formatKept = true;
    }
    if (type === "object") {
      const properties = own(schema, "properties");
      const requiredNames = own(schema, "required");
      const names = new Set(
        Array.isArray(requiredNames)
          ? requiredNames.filter((n) => typeof n === "string")
          : [],
      );
      if (isJsonObject(properties)) {
        row.children = Object.keys(properties).map((key) =>
          schemaToRow(key, properties[key], names.has(key)),
        );
        modeled.add("properties");
      }
      if (Array.isArray(requiredNames)) modeled.add("required");
      const additional = own(schema, "additionalProperties");
      if (additional === undefined || typeof additional === "boolean") {
        row.closed = additional === false;
        if (additional !== undefined) modeled.add("additionalProperties");
      } else row.closedKept = true;
    }
    if (type === "array") {
      const items = own(schema, "items");
      if (isJsonObject(items)) {
        row.item = schemaToRow("", items, false);
        modeled.add("items");
      } else row.item = null;
    }
  }
  return {
    ...row,
    type,
    nullable,
    locked,
    kept: Object.keys(schema).filter((k) => !modeled.has(k) && k !== locked),
    source: schema,
    original: { type, nullable },
  };
}

/** Reads a schema (or nothing yet) into an editable field list. */
export function schemaToModel(schema: unknown): DesignerModel {
  const model: DesignerModel = {
    editable: true,
    rows: [],
    closed: false,
    closedKept: false,
    kept: [],
    source: schema,
  };
  if (schema === undefined || schema === null) return model;
  const refuse = (what: string): DesignerModel => ({
    ...model,
    editable: false,
    reason: `This schema ${what}, so it is not a list of fields. It is kept unchanged; edit it in Source.`,
  });
  if (!isJsonObject(schema)) return refuse("is not an object");
  const lock = LOCKING.find((k) => own(schema, k) !== undefined);
  if (lock) return refuse(`uses ${lock}`);
  const type = own(schema, "type");
  if (type !== undefined && type !== "object")
    return refuse("describes a single value rather than fields");
  const properties = own(schema, "properties");
  if (properties !== undefined && !isJsonObject(properties))
    return refuse("has unreadable properties");
  const required = own(schema, "required");
  const names = new Set(Array.isArray(required) ? required : []);
  if (isJsonObject(properties))
    model.rows = Object.keys(properties).map((key) =>
      schemaToRow(key, properties[key], names.has(key)),
    );
  const additional = own(schema, "additionalProperties");
  if (additional === undefined || typeof additional === "boolean")
    model.closed = additional === false;
  else model.closedKept = true;
  const modeled = new Set(["type", "properties", "required"]);
  if (!model.closedKept) modeled.add("additionalProperties");
  model.kept = Object.keys(schema).filter(
    (k) => !modeled.has(k) && !ROOT_ANNOTATIONS.has(k),
  );
  return model;
}

/** Writes `value` at `key` only when it differs (key order matters); undefined removes it. */
function put(target: JsonObject, key: string, value: Json | undefined) {
  const current = own(target, key);
  if (value === undefined) {
    if (current !== undefined) delete target[key];
    return;
  }
  if (
    current !== undefined &&
    JSON.stringify(current) === JSON.stringify(value)
  )
    return;
  define(target, key, value);
}

function parseNumber(text: string, whole: boolean): number | undefined {
  const trimmed = text.trim();
  if (!trimmed) return undefined;
  const value = Number(trimmed);
  if (!Number.isFinite(value) || (whole && !Number.isInteger(value)))
    return undefined;
  return value;
}
const choiceLines = (text: string) =>
  text
    .split("\n")
    .map((line) => line.replace(/\r$/, ""))
    .filter((line) => line.trim() !== "");

/** Fields and their required list, keeping the source's required order. */
function writeFields(
  target: JsonObject,
  rows: DesignerRow[],
  source: JsonObject,
) {
  const properties: JsonObject = {};
  for (const row of rows) define(properties, row.name, rowToSchema(row));
  const sourceProperties = own(source, "properties");
  if (rows.length || isJsonObject(sourceProperties))
    put(target, "properties", properties);
  const previous = own(source, "required");
  const declared = new Set(
    isJsonObject(sourceProperties) ? Object.keys(sourceProperties) : [],
  );
  const wanted = rows.filter((r) => r.required).map((r) => r.name);
  const keep = (Array.isArray(previous) ? previous : []).filter(
    (name): name is string =>
      typeof name === "string" &&
      (wanted.includes(name) || !declared.has(name)),
  );
  const required = [...keep, ...wanted.filter((name) => !keep.includes(name))];
  // An explicit empty list stays as written; otherwise an empty list is left out.
  const unchangedEmpty = Array.isArray(previous) && !previous.length;
  put(
    target,
    "required",
    required.length ? required : unchangedEmpty ? previous : undefined,
  );
}

/** Writes one row back as a schema, touching only what changed. */
export function rowToSchema(row: DesignerRow): Json {
  if (row.locked && !isJsonObject(row.source)) return row.source as Json;
  const source = isJsonObject(row.source) ? row.source : {};
  const target: JsonObject = { ...source };
  const changed = row.type !== row.original.type;
  if (
    !row.locked &&
    (changed ||
      row.nullable !== row.original.nullable ||
      row.source === undefined)
  ) {
    if (row.type === "any") put(target, "type", undefined);
    else
      put(
        target,
        "type",
        row.nullable && row.type !== "null" ? [row.type, "null"] : row.type,
      );
  }
  if (changed && !row.locked) {
    // Rules that only made sense for the old type no longer apply.
    const family = familyOf(row.type);
    for (const [owner, keys] of Object.entries(ONLY_FOR))
      if (owner !== family) for (const key of keys) put(target, key, undefined);
    put(target, "enum", undefined);
  }
  put(target, "title", row.title.trim() ? row.title : undefined);
  put(
    target,
    "description",
    row.description.trim() ? row.description : undefined,
  );
  if (row.locked) return unchanged(target, row.source);
  if (!row.enumKept) {
    const lines = choiceLines(row.choices);
    const values =
      row.type === "string"
        ? lines
        : row.type === "number" || row.type === "integer"
          ? lines.map((line) => Number(line.trim()))
          : [];
    const next: Json[] =
      values.length && row.nullable ? [...values, null] : values;
    const before = own(source, "enum");
    // The same values in another order are left as written.
    const same =
      Array.isArray(before) &&
      before.length === next.length &&
      JSON.stringify(before.map((v) => JSON.stringify(v)).sort()) ===
        JSON.stringify(next.map((v) => JSON.stringify(v)).sort());
    if (!same || changed) put(target, "enum", next.length ? next : undefined);
  }
  const bounds = boundsFor(row.type);
  if (bounds) {
    const whole = row.type !== "number";
    put(target, bounds[0], parseNumber(row.min, whole));
    put(target, bounds[1], parseNumber(row.max, whole));
  }
  if (row.type === "string" && !row.formatKept)
    put(target, "format", row.format || undefined);
  if (row.type === "object") {
    writeFields(target, row.children, source);
    if (!row.closedKept) {
      const before = own(source, "additionalProperties");
      put(
        target,
        "additionalProperties",
        row.closed ? false : before === true ? true : undefined,
      );
    }
  }
  if (row.type === "array" && row.item)
    put(target, "items", rowToSchema(row.item));
  return unchanged(target, row.source);
}

/** The source object itself when nothing changed, so untouched subtrees are shared. */
function unchanged(target: JsonObject, source: unknown): JsonObject {
  return isJsonObject(source) &&
    JSON.stringify(target) === JSON.stringify(source)
    ? source
    : target;
}

/** The schema the model describes; an unedited model returns the source as is. */
export function modelToSchema(model: DesignerModel): unknown {
  if (!model.editable) return model.source;
  const source = isJsonObject(model.source) ? model.source : {};
  const target: JsonObject = { ...source };
  if (model.rows.length && own(source, "type") === undefined) {
    const typed: JsonObject = { type: "object" };
    for (const key of Object.keys(target)) define(typed, key, target[key]);
    writeFields(typed, model.rows, source);
    return finishRoot(typed, model, source);
  }
  writeFields(target, model.rows, source);
  return finishRoot(target, model, source);
}
function finishRoot(
  target: JsonObject,
  model: DesignerModel,
  source: JsonObject,
) {
  if (!model.closedKept) {
    const before = own(source, "additionalProperties");
    put(
      target,
      "additionalProperties",
      model.closed ? false : before === true ? true : undefined,
    );
  }
  if (model.source === undefined && !Object.keys(target).length) return {};
  return unchanged(target, model.source);
}

/** Problems that block writing the schema, in row order, in plain words. */
export function designerIssues(model: DesignerModel): DesignerIssue[] {
  const issues: DesignerIssue[] = [];
  const visit = (rows: DesignerRow[]) => {
    const counts = new Map<string, number>();
    for (const row of rows)
      counts.set(row.name, (counts.get(row.name) ?? 0) + 1);
    for (const row of rows) {
      const issue = (field: DesignerIssue["field"], message: string) =>
        issues.push({ row: row.uid, field, message });
      if (!row.name.trim()) issue("name", "Enter a field name.");
      else if (row.name === "__proto__")
        issue("name", "Choose another name; “__proto__” is reserved.");
      else if ((counts.get(row.name) ?? 0) > 1)
        issue("name", `Another field here is already named “${row.name}”.`);
      rowRules(row, issue);
      if (row.type === "object" && !row.locked) visit(row.children);
      if (row.type === "array" && row.item && !row.item.locked) {
        rowRules(row.item, (field, message) =>
          issues.push({ row: row.item!.uid, field, message }),
        );
        if (row.item.type === "object") visit(row.item.children);
      }
    }
  };
  if (model.editable) visit(model.rows);
  return issues;
}

function rowRules(
  row: DesignerRow,
  issue: (field: DesignerIssue["field"], message: string) => void,
) {
  if (row.locked) return;
  const bounds = boundsFor(row.type);
  if (bounds) {
    const whole = row.type !== "number";
    const counting = row.type !== "number" && row.type !== "integer";
    const read = (field: "min" | "max", text: string) => {
      if (!text.trim()) return undefined;
      const value = parseNumber(text, whole);
      if (value === undefined || (counting && value < 0)) {
        issue(
          field,
          counting
            ? "Enter a whole number of 0 or more."
            : whole
              ? "Enter a whole number."
              : "Enter a number.",
        );
        return null;
      }
      return value;
    };
    const low = read("min", row.min);
    const high = read("max", row.max);
    if (typeof low === "number" && typeof high === "number" && high < low)
      issue("max", "The largest value is smaller than the smallest.");
  }
  if (!row.enumKept && row.choices.trim()) {
    const lines = choiceLines(row.choices);
    if (row.type === "number" || row.type === "integer") {
      const bad = lines.find(
        (line) => parseNumber(line, row.type === "integer") === undefined,
      );
      if (bad !== undefined)
        issue(
          "choices",
          `“${bad.trim()}” is not ${row.type === "integer" ? "a whole number" : "a number"}.`,
        );
    } else if (row.type !== "string")
      issue(
        "choices",
        "Only text and numbers can have a list of allowed values.",
      );
    const seen = new Set<string>();
    for (const line of lines) {
      const key = row.type === "string" ? line : String(Number(line.trim()));
      if (seen.has(key)) {
        issue("choices", `“${line.trim()}” is listed twice.`);
        break;
      }
      seen.add(key);
    }
  }
}

/** Where a row lives: the list that holds it. */
function containerOf(
  model: DesignerModel,
  target: DesignerRow,
): DesignerRow[] | null {
  const search = (rows: DesignerRow[]): DesignerRow[] | null => {
    for (const row of rows) {
      if (row === target) return rows;
      const inner =
        search(row.children) ?? (row.item ? search(row.item.children) : null);
      if (inner) return inner;
    }
    return null;
  };
  return search(model.rows);
}

/** Adds a blank field to the root (parent null) or to a group row. */
export function addRow(
  model: DesignerModel,
  parent: DesignerRow | null,
): DesignerRow {
  const row = newRow();
  (parent ? parent.children : model.rows).push(row);
  return row;
}

export function removeRow(model: DesignerModel, row: DesignerRow) {
  const rows = containerOf(model, row);
  if (rows) rows.splice(rows.indexOf(row), 1);
}

/** Moves a field up (-1) or down (+1) among its siblings; false at either end. */
export function moveRow(
  model: DesignerModel,
  row: DesignerRow,
  delta: -1 | 1,
): boolean {
  const rows = containerOf(model, row);
  if (!rows) return false;
  const index = rows.indexOf(row);
  const next = index + delta;
  if (next < 0 || next >= rows.length) return false;
  rows.splice(index, 1);
  rows.splice(next, 0, row);
  return true;
}

/** Changes a row's type, clearing rules that only applied to the old one. */
export function changeType(row: DesignerRow, type: EditableType) {
  if (row.locked || row.type === type) return;
  const numeric = (t: RowType) => t === "number" || t === "integer";
  if (!(numeric(row.type) && numeric(type))) {
    row.min = "";
    row.max = "";
    row.choices = "";
  }
  row.format = "";
  row.enumKept = false;
  row.formatKept = false;
  const family = familyOf(type);
  const dropped = new Set(
    Object.entries(ONLY_FOR)
      .filter(([owner]) => owner !== family)
      .flatMap(([, keys]) => keys),
  );
  row.kept = row.kept.filter((key) => !dropped.has(key) && key !== "enum");
  if (type === "array" && !row.item) row.item = newRow();
  if (type === "null") row.nullable = false;
  row.type = type;
}

export type InferredMode = "replace" | "add";

/** Puts the fields of an inferred schema in the model; returns how many were added. */
export function applyInferred(
  model: DesignerModel,
  schema: unknown,
  mode: InferredMode,
): number {
  const rows = schemaToModel(schema).rows;
  if (mode === "replace") {
    model.rows = rows;
    return rows.length;
  }
  const names = new Set(model.rows.map((r) => r.name));
  const added = rows.filter((r) => !names.has(r.name));
  model.rows.push(...added);
  return added.length;
}

/** Plain notes about inferred fields a person should review. */
export function inferenceNotes(model: DesignerModel): string[] {
  const notes: string[] = [];
  const visit = (rows: DesignerRow[], prefix: string) => {
    for (const row of rows) {
      const name = prefix + row.name;
      if (row.type === "null")
        notes.push(
          `“${name}” was empty in every example, so its type is Empty. Choose its real type.`,
        );
      visit(row.children, name + " › ");
      if (row.item) visit(row.item.children, name + " › ");
    }
  };
  visit(model.rows, "");
  return notes;
}

/** A shape path: property names, with 0 for "each list item". */
export type ShapePath = (string | 0)[];
/** Deeper numbers are left alone; inference stops long before this depth. */
const MAX_SHAPE_DEPTH = 32;

/**
 * Where an example writes a number with a fraction or exponent ("10.0",
 * "1e3"). `JSON.parse` turns "10.0" into 10, so inference would type a price
 * as a whole number and reject 10.5 at run time; the paths let the inferred
 * schema be widened back to Number. Only names are returned, never values.
 * `text` must be valid JSON (it has already been parsed).
 */
export function decimalPaths(text: string): ShapePath[] {
  const found = new Map<string, ShapePath>();
  const scalar =
    /-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?|true|false|null/y;
  let at = 0;
  const space = () => {
    while (at < text.length && " \t\n\r".includes(text[at])) at++;
  };
  const string = (): string => {
    const start = at++;
    while (text[at] !== '"') at += text[at] === "\\" ? 2 : 1;
    at++;
    return JSON.parse(text.slice(start, at)) as string;
  };
  /** Skips one value without recursion (for values nested too deeply to matter). */
  const skip = () => {
    let depth = 0;
    do {
      space();
      const char = text[at];
      if (char === '"') string();
      else if (char === "{" || char === "[") (depth++, at++);
      else if (char === "}" || char === "]") (depth--, at++);
      else if (char === "," || char === ":") at++;
      else {
        scalar.lastIndex = at;
        at = scalar.exec(text) ? scalar.lastIndex : at + 1;
      }
    } while (depth > 0 && at < text.length);
  };
  const value = (path: ShapePath): void => {
    space();
    if (path.length > MAX_SHAPE_DEPTH) return skip();
    const char = text[at];
    if (char === "{" || char === "[") {
      const object = char === "{";
      at++;
      space();
      if (text[at] === (object ? "}" : "]")) return void at++;
      for (;;) {
        space();
        if (object) {
          const key = string();
          space();
          at++; // ":"
          value([...path, key]);
        } else value([...path, 0]);
        space();
        if (text[at++] !== ",") return;
      }
    }
    if (char === '"') return void string();
    scalar.lastIndex = at;
    const match = scalar.exec(text);
    if (!match) return void at++;
    at = scalar.lastIndex;
    if (match[1] !== undefined || match[2] !== undefined)
      found.set(JSON.stringify(path), path);
  };
  value([]);
  return [...found.values()];
}

/**
 * The inferred schema with "integer" widened to "number" wherever an example
 * wrote a decimal (see `decimalPaths`). Follows properties, maps, list items
 * and nullable alternatives; untouched subtrees are shared.
 */
export function widenDecimals(schema: Json, paths: ShapePath[]): Json {
  if (!paths.length || !isJsonObject(schema)) return schema;
  let next = schema;
  const edit = (key: string, value: Json) => {
    if (next === schema) next = { ...schema };
    define(next, key, value);
  };
  if (paths.some((path) => !path.length)) {
    const type = own(schema, "type");
    if (type === "integer") edit("type", "number");
    else if (Array.isArray(type) && type.includes("integer")) {
      const types = [
        ...new Set(type.map((t) => (t === "integer" ? "number" : t))),
      ];
      edit("type", types.length === 1 ? types[0] : types);
    }
  }
  const deeper = paths.filter((path) => path.length);
  const below = (match: (head: string | 0) => boolean) =>
    deeper.filter((path) => match(path[0])).map((path) => path.slice(1));
  const properties = own(schema, "properties");
  if (isJsonObject(properties)) {
    let widened = properties;
    for (const key of Object.keys(properties)) {
      const child = widenDecimals(
        properties[key],
        below((head) => head === key),
      );
      if (child === properties[key]) continue;
      if (widened === properties) widened = { ...properties };
      define(widened, key, child);
    }
    if (widened !== properties) edit("properties", widened);
  }
  const additional = own(schema, "additionalProperties");
  if (isJsonObject(additional)) {
    const listed = isJsonObject(properties) ? properties : {};
    const child = widenDecimals(
      additional,
      below((head) => head !== 0 && own(listed, head) === undefined),
    );
    if (child !== additional) edit("additionalProperties", child);
  }
  const items = own(schema, "items");
  if (isJsonObject(items)) {
    const child = widenDecimals(
      items,
      below((head) => head === 0),
    );
    if (child !== items) edit("items", child);
  }
  const options = own(schema, "anyOf");
  if (Array.isArray(options)) {
    const widened = options.map((option) => widenDecimals(option, paths));
    if (widened.some((option, index) => option !== options[index]))
      edit("anyOf", widened);
  }
  return next;
}

/** Turns example JSON into a schema; values are never kept. */
export type InferSchema = (
  samples: Json[],
) => Promise<{ schema: JsonObject; warnings?: string[] }>;

/**
 * Inference through the local Studio host's HTTP action builder
 * (`POST /studio/local/http-action`), which runs the same `infer_schema` as
 * the CLI. The examples are sent as a request body sample (a list, so several
 * examples merge); the body schema's `items` is the inferred schema. `post`
 * is the paired host call, for example
 * `(path, body) => api.request(path, "POST", body, {}, 35000)`.
 */
export function inferViaHttpAction(
  post: (path: string, body: unknown) => Promise<unknown>,
): InferSchema {
  return async (samples) => {
    const response = await post("/studio/local/http-action", {
      request: {
        name: "sample-inference",
        method: "POST",
        pathTemplate: "/sample",
        bodySample: samples,
      },
    });
    const result = isJsonObject(response) ? response : {};
    const diagnostics = Array.isArray(result["diagnostics"])
      ? result["diagnostics"].filter(isJsonObject)
      : [];
    const body = getAt(result["action"], [
      "spec",
      "inputSchema",
      "properties",
      "body",
    ]);
    if (result["ok"] !== true || !isJsonObject(body)) {
      const error = diagnostics.find((d) => d["severity"] === "error");
      throw Error(
        typeof error?.["message"] === "string"
          ? error["message"]
          : "Studio could not read the example. Check that it is a JSON object.",
      );
    }
    const items = own(body, "items");
    const warnings = diagnostics
      .filter(
        (d) =>
          d["severity"] === "warning" &&
          typeof d["path"] === "string" &&
          d["path"].startsWith("/bodySample") &&
          typeof d["message"] === "string",
      )
      .map((d) => d["message"] as string);
    return { schema: isJsonObject(items) ? items : {}, warnings };
  };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export type DesignerPurpose = "input" | "output" | "payload" | "form" | "other";
const previewHeadings: Record<DesignerPurpose, string> = {
  input: "What people starting a run will see",
  output: "Preview of the output fields",
  payload: "Preview of the signal fields",
  form: "What reviewers will see",
  other: "Preview",
};
/** Nesting shown in the editor; deeper fields are kept and edited in Source. */
const MAX_EDIT_DEPTH = 6;
/** Total example text sent for inference (the host's limits are larger). */
const SAMPLE_LIMIT_BYTES = 256 * 1024;
const MAX_SAMPLES = 10;
let sequence = 0;

/**
 * Schema designer. Rows edit name, type, required, title, description,
 * allowed values, bounds, text format, nullability and nesting (groups and
 * lists of groups). Keywords it cannot edit are kept and listed. With
 * `inferSchema`, "Paste sample JSON" finds fields from examples.
 *
 * Mount lazily, for example:
 *   @defer (on immediate) {
 *     <weave-schema-designer
 *       heading="Form fields" purpose="form"
 *       [schema]="step.formSchema" [inferSchema]="infer"
 *       (schemaChange)="update(['formSchema'], $event)"
 *       (validityChange)="formValid = $event" />
 *   }
 */
@Component({
  selector: "weave-schema-designer",
  standalone: true,
  imports: [NgTemplateOutlet, Modal, TaskForm],
  template: `<section class="sd" [attr.aria-labelledby]="prefix + '-heading'">
    <header class="sd-head">
      <h3 [id]="prefix + '-heading'">{{ heading() }}</h3>
      @if (model.editable && !readonly()) {
        <div class="sd-tools">
          @if (inferSchema()) {
            <button
              type="button"
              [id]="prefix + '-paste'"
              (click)="openSample()"
            >
              Paste sample JSON
            </button>
          }
          <button type="button" [id]="addId(null)" (click)="add(null)">
            Add field
          </button>
        </div>
      }
    </header>
    @if (!model.editable) {
      <p class="notice">{{ model.reason }}</p>
    } @else {
      @if (notes.length) {
        <div class="notice sd-notes" role="status">
          <ul>
            @for (note of notes; track $index) {
              <li>{{ note }}</li>
            }
          </ul>
          <button type="button" class="sd-small" (click)="notes = []">
            Dismiss
          </button>
        </div>
      }
      @if (!model.rows.length) {
        <p class="hint sd-empty">
          No fields yet. Add a field{{
            inferSchema() ? " or paste a sample" : ""
          }}.
        </p>
      } @else {
        <ng-container
          *ngTemplateOutlet="
            rowsTpl;
            context: { $implicit: model.rows, parent: null, depth: 0 }
          "
        />
      }
      <div class="sd-root-options">
        <label class="checkbox-field"
          ><input
            type="checkbox"
            [checked]="model.closed"
            [disabled]="readonly() || model.closedKept"
            (change)="setRootClosed($event)"
          />Reject fields that are not listed</label
        >
        @if (model.kept.length) {
          <p class="sd-flag">
            Also kept as written: {{ model.kept.join(", ") }}.
          </p>
        }
      </div>
    }
    <p class="sr-only" aria-live="polite" aria-atomic="true">{{ status() }}</p>
    @if (issueCount()) {
      <p class="sd-summary" [id]="prefix + '-summary'">
        {{
          issueCount() === 1
            ? "Fix 1 problem to update the schema."
            : "Fix " + issueCount() + " problems to update the schema."
        }}
      </p>
    }
    @if (preview() && model.editable && model.rows.length) {
      <section class="sd-preview" [attr.aria-labelledby]="prefix + '-preview'">
        <h4 [id]="prefix + '-preview'">{{ previewHeading() }}</h4>
        <weave-task-form [schema]="previewSchema" />
      </section>
    }
    <details class="sd-json">
      <summary>Show schema JSON</summary>
      <pre>{{ previewText() }}</pre>
    </details>

    <ng-template #rowsTpl let-rows let-parent="parent" let-depth="depth">
      <ol class="sd-rows" [class.nested]="depth > 0">
        @for (
          row of rows;
          track row.uid;
          let first = $first;
          let last = $last
        ) {
          <li class="sd-row" [attr.data-row]="row.uid">
            <div class="sd-main">
              <div class="sd-field">
                <label [for]="id(row, 'name')">Field name</label>
                <input
                  [id]="id(row, 'name')"
                  autocomplete="off"
                  spellcheck="false"
                  [value]="row.name"
                  [disabled]="readonly()"
                  [attr.aria-invalid]="shownIssue(row, 'name') ? 'true' : null"
                  [attr.aria-describedby]="
                    shownIssue(row, 'name') ? id(row, 'name-error') : null
                  "
                  (input)="setName(row, $event)"
                  (blur)="touch(row)"
                />
              </div>
              <div class="sd-field">
                <label [for]="id(row, 'type')">Type</label>
                @if (row.locked) {
                  <input
                    [id]="id(row, 'type')"
                    readonly
                    [value]="'Advanced (' + row.locked + ')'"
                  />
                } @else {
                  <select
                    [id]="id(row, 'type')"
                    [attr.aria-label]="'Type of ' + nameOf(row)"
                    [disabled]="readonly()"
                    (change)="setType(row, $event)"
                  >
                    @for (option of typeOptions; track option.value) {
                      <option
                        [value]="option.value"
                        [selected]="option.value === row.type"
                      >
                        {{ option.label }}
                      </option>
                    }
                  </select>
                }
              </div>
              <label class="checkbox-field sd-required"
                ><input
                  type="checkbox"
                  [checked]="row.required"
                  [disabled]="readonly()"
                  [attr.aria-label]="'Required: ' + nameOf(row)"
                  (change)="setRequired(row, $event)"
                />Required</label
              >
              <div class="sd-actions">
                <button
                  type="button"
                  class="sd-small"
                  [id]="id(row, 'details')"
                  [attr.aria-expanded]="row.expanded"
                  [attr.aria-controls]="row.expanded ? id(row, 'panel') : null"
                  [attr.aria-label]="'Details for ' + nameOf(row)"
                  (click)="row.expanded = !row.expanded"
                >
                  Details
                </button>
                @if (!readonly()) {
                  <button
                    type="button"
                    class="sd-icon"
                    [id]="id(row, 'up')"
                    [disabled]="first"
                    [attr.aria-label]="'Move ' + nameOf(row) + ' up'"
                    (click)="move(row, -1)"
                  >
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M6 15l6-6 6 6" />
                    </svg>
                  </button>
                  <button
                    type="button"
                    class="sd-icon"
                    [id]="id(row, 'down')"
                    [disabled]="last"
                    [attr.aria-label]="'Move ' + nameOf(row) + ' down'"
                    (click)="move(row, 1)"
                  >
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M6 9l6 6 6-6" />
                    </svg>
                  </button>
                  <button
                    type="button"
                    class="sd-icon danger"
                    [id]="id(row, 'remove')"
                    [attr.aria-label]="'Remove ' + nameOf(row)"
                    (click)="remove(row, rows, parent)"
                  >
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M4 6h16M9 6V3h6v3M7 6l1 15h8l1-15" />
                    </svg>
                  </button>
                }
              </div>
            </div>
            @if (shownIssue(row, "name"); as message) {
              <p class="sd-error" [id]="id(row, 'name-error')">
                {{ message }}
              </p>
            }
            @if (row.locked) {
              <p class="sd-flag warn">
                Uses {{ row.locked }}, which this designer cannot edit. It is
                kept unchanged; edit it in Source.
              </p>
            }
            @if (row.kept.length) {
              <p class="sd-flag">
                Also kept as written: {{ row.kept.join(", ") }}.
              </p>
            }
            @if (row.expanded) {
              <div
                class="sd-details"
                [id]="id(row, 'panel')"
                role="group"
                [attr.aria-label]="'Details for ' + nameOf(row)"
              >
                <ng-container
                  *ngTemplateOutlet="
                    detailsTpl;
                    context: { $implicit: row, owner: row }
                  "
                />
                @if (row.type === "array" && row.item && !row.item.locked) {
                  <fieldset class="sd-item">
                    <legend>Each item in {{ nameOf(row) }}</legend>
                    <ng-container
                      *ngTemplateOutlet="
                        detailsTpl;
                        context: { $implicit: row.item, owner: row }
                      "
                    />
                  </fieldset>
                }
              </div>
            }
            @if (row.type === "array" && !row.locked) {
              <div class="sd-field sd-each">
                <label [for]="id(row, 'item-type')">Each item is</label>
                @if (row.item?.locked) {
                  <input
                    [id]="id(row, 'item-type')"
                    readonly
                    [value]="'Advanced (' + row.item!.locked + ')'"
                  />
                } @else {
                  <select
                    [id]="id(row, 'item-type')"
                    [disabled]="readonly()"
                    (change)="setItemType(row, $event)"
                  >
                    @if (!row.item) {
                      <option value="" selected>
                        Not described (any value)
                      </option>
                    }
                    @for (option of typeOptions; track option.value) {
                      <!-- A list of lists only comes from Source; show it as it is. -->
                      @if (
                        option.value !== "array" || row.item?.type === "array"
                      ) {
                        <option
                          [value]="option.value"
                          [selected]="option.value === row.item?.type"
                        >
                          {{ option.label }}
                        </option>
                      }
                    }
                  </select>
                }
              </div>
              @if (row.item?.type === "array") {
                <p class="sd-flag">
                  Each item is itself a list; what it holds is kept as written.
                  Edit it in Source.
                </p>
              }
            }
            @if (groupOf(row); as group) {
              @if (depth + 1 >= maxDepth) {
                <p class="sd-flag">
                  Fields nested this deeply are kept as written; edit them in
                  Source.
                </p>
              } @else {
                <div class="sd-group">
                  @if (group.children.length) {
                    <ng-container
                      *ngTemplateOutlet="
                        rowsTpl;
                        context: {
                          $implicit: group.children,
                          parent: group,
                          depth: depth + 1,
                        }
                      "
                    />
                  }
                  @if (!readonly()) {
                    <button
                      type="button"
                      class="sd-small sd-add-nested"
                      [id]="addId(group)"
                      (click)="add(group)"
                    >
                      {{
                        group === row
                          ? "Add field to " + nameOf(row)
                          : "Add field to each " + nameOf(row) + " item"
                      }}
                    </button>
                  }
                </div>
              }
            }
          </li>
        }
      </ol>
    </ng-template>

    <ng-template #detailsTpl let-row let-owner="owner">
      <div class="sd-details-grid">
        <!-- A true/false schema has nowhere to keep a label; it is edited in Source. -->
        @if (row === owner && labelled(row)) {
          <div class="sd-field">
            <label [for]="id(row, 'title')">Label shown to people</label>
            <input
              [id]="id(row, 'title')"
              [value]="row.title"
              [disabled]="readonly()"
              (input)="setText(row, 'title', $event)"
            />
          </div>
          <div class="sd-field sd-wide">
            <label [for]="id(row, 'description')">Help text</label>
            <textarea
              rows="2"
              [id]="id(row, 'description')"
              [value]="row.description"
              [disabled]="readonly()"
              (input)="setText(row, 'description', $event)"
            ></textarea>
          </div>
        }
        @if (!row.locked) {
          @if (bounds(row); as b) {
            <div class="sd-field">
              <label [for]="id(row, 'min')">{{ b[0] }}</label>
              <input
                [id]="id(row, 'min')"
                inputmode="decimal"
                [value]="row.min"
                [disabled]="readonly()"
                [attr.aria-invalid]="issue(row, 'min') ? 'true' : null"
                [attr.aria-describedby]="
                  issue(row, 'min') ? id(row, 'min-error') : null
                "
                (input)="setText(row, 'min', $event)"
              />
              @if (issue(row, "min"); as message) {
                <p class="sd-error" [id]="id(row, 'min-error')">
                  {{ message }}
                </p>
              }
            </div>
            <div class="sd-field">
              <label [for]="id(row, 'max')">{{ b[1] }}</label>
              <input
                [id]="id(row, 'max')"
                inputmode="decimal"
                [value]="row.max"
                [disabled]="readonly()"
                [attr.aria-invalid]="issue(row, 'max') ? 'true' : null"
                [attr.aria-describedby]="
                  issue(row, 'max') ? id(row, 'max-error') : null
                "
                (input)="setText(row, 'max', $event)"
              />
              @if (issue(row, "max"); as message) {
                <p class="sd-error" [id]="id(row, 'max-error')">
                  {{ message }}
                </p>
              }
            </div>
          }
          @if (row.type === "string") {
            <div class="sd-field">
              <label [for]="id(row, 'format')">Text format</label>
              @if (row.formatKept) {
                <input
                  [id]="id(row, 'format')"
                  readonly
                  [value]="'Kept as written'"
                />
              } @else {
                <select
                  [id]="id(row, 'format')"
                  [disabled]="readonly()"
                  (change)="setFormat(row, $event)"
                >
                  @for (option of formatOptions; track option.value) {
                    <option
                      [value]="option.value"
                      [selected]="option.value === row.format"
                    >
                      {{ option.label }}
                    </option>
                  }
                </select>
              }
            </div>
          }
          @if (
            (row.type === "string" ||
              row.type === "number" ||
              row.type === "integer") &&
            !row.enumKept
          ) {
            <div class="sd-field sd-wide">
              <label [for]="id(row, 'choices')">Allowed values</label>
              <textarea
                rows="3"
                spellcheck="false"
                [id]="id(row, 'choices')"
                [value]="row.choices"
                [disabled]="readonly()"
                [attr.aria-invalid]="issue(row, 'choices') ? 'true' : null"
                [attr.aria-describedby]="
                  id(row, 'choices-hint') +
                  (issue(row, 'choices') ? ' ' + id(row, 'choices-error') : '')
                "
                (input)="setText(row, 'choices', $event)"
              ></textarea>
              <p class="hint" [id]="id(row, 'choices-hint')">
                One per line. Leave empty to allow any value.
              </p>
              @if (issue(row, "choices"); as message) {
                <p class="sd-error" [id]="id(row, 'choices-error')">
                  {{ message }}
                </p>
              }
            </div>
          }
          @if (row.type !== "null" && row.type !== "any") {
            <label class="checkbox-field"
              ><input
                type="checkbox"
                [checked]="row.nullable"
                [disabled]="readonly()"
                (change)="setNullable(row, $event)"
              />Can be empty (null)</label
            >
          }
          @if (row.type === "object") {
            <label class="checkbox-field"
              ><input
                type="checkbox"
                [checked]="row.closed"
                [disabled]="readonly() || row.closedKept"
                (change)="setClosed(row, $event)"
              />Reject fields that are not listed</label
            >
          }
        }
      </div>
    </ng-template>

    @if (sampleOpen) {
      <weave-modal
        heading="Find fields from an example"
        [describedBy]="prefix + '-sample-help'"
        [wide]="true"
        (dismiss)="closeSample()"
      >
        <p class="dialog-message" [id]="prefix + '-sample-help'">
          Paste an example as JSON, such as a request or a record. Studio reads
          only the field names and types; the values are not saved.
        </p>
        <div class="dialog-form">
          @for (sample of samples; track $index) {
            <label [for]="prefix + '-sample-' + $index"
              >Example {{ $index + 1 }}</label
            >
            <textarea
              class="monospace"
              rows="8"
              spellcheck="false"
              [id]="prefix + '-sample-' + $index"
              [attr.data-initial-focus]="$index === 0 ? '' : null"
              [value]="sample"
              [attr.aria-describedby]="
                sampleError ? prefix + '-sample-error' : null
              "
              (input)="setSample($index, $event)"
            ></textarea>
            @if ($index > 0) {
              <button
                type="button"
                class="sd-small"
                [attr.aria-label]="'Remove example ' + ($index + 1)"
                (click)="removeSample($index)"
              >
                Remove example
              </button>
            }
          }
          @if (samples.length < maxSamples) {
            <button type="button" class="sd-small" (click)="addSample()">
              Add another example
            </button>
          }
          @if (model.rows.length) {
            <fieldset class="sd-mode">
              <legend>The fields you already have</legend>
              <label class="checkbox-field"
                ><input
                  type="radio"
                  [name]="prefix + '-mode'"
                  [checked]="sampleMode === 'add'"
                  (change)="sampleMode = 'add'"
                />Keep them and add new fields only</label
              >
              <label class="checkbox-field"
                ><input
                  type="radio"
                  [name]="prefix + '-mode'"
                  [checked]="sampleMode === 'replace'"
                  (change)="sampleMode = 'replace'"
                />Replace them with the example's fields</label
              >
            </fieldset>
          }
        </div>
        @if (sampleError) {
          <p
            class="notice error-notice"
            role="alert"
            [id]="prefix + '-sample-error'"
          >
            {{ sampleError }}
          </p>
        }
        <div class="dialog-actions">
          <button type="button" (click)="closeSample()">Cancel</button>
          <button
            type="button"
            class="primary"
            [disabled]="sampleBusy"
            (click)="runInference()"
          >
            {{ sampleBusy ? "Finding fields…" : "Find fields" }}
          </button>
        </div>
      </weave-modal>
    }
  </section>`,
  styles: [
    `
      :host {
        display: block;
        min-width: 0;
        container-type: inline-size;
        container-name: designer;
      }
      .sd {
        display: grid;
        gap: 10px;
      }
      .sd-head {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
      }
      .sd-head h3 {
        margin: 0;
      }
      .sd-tools {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
      }
      .sd-rows {
        list-style: none;
        margin: 0;
        padding: 0;
        display: grid;
        gap: 8px;
      }
      .sd-rows.nested {
        border-left: 2px solid var(--line);
        padding-left: 10px;
      }
      .sd-row {
        border: 1px solid var(--line);
        border-radius: 6px;
        padding: 8px 10px;
        background: var(--surface);
        display: grid;
        gap: 6px;
        min-width: 0;
      }
      .sd-main {
        display: grid;
        grid-template-columns: minmax(0, 1.6fr) minmax(0, 1.2fr) auto auto;
        align-items: end;
        gap: 8px;
      }
      .sd-field {
        display: grid;
        gap: 4px;
        min-width: 0;
      }
      .sd-field input,
      .sd-field select,
      .sd-field textarea {
        width: 100%;
        padding: 7px 8px;
      }
      .sd-required {
        padding-bottom: 9px;
        font-weight: 600;
      }
      .sd-actions {
        display: flex;
        gap: 4px;
        flex-wrap: wrap;
      }
      button.sd-small {
        min-height: 32px;
        padding: 4px 10px;
        font-size: 12px;
      }
      button.sd-icon {
        min-height: 32px;
        width: 32px;
        padding: 0;
      }
      button.sd-icon svg {
        width: 16px;
        height: 16px;
        fill: none;
        stroke: currentColor;
        stroke-width: 2;
        stroke-linecap: round;
        stroke-linejoin: round;
      }
      button.sd-icon.danger {
        color: var(--danger);
      }
      .sd-details {
        border-top: 1px dashed var(--line);
        padding-top: 8px;
      }
      .sd-details-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
        align-items: start;
      }
      .sd-wide {
        grid-column: 1 / -1;
      }
      .sd-item {
        margin: 8px 0 0;
        border: 1px solid var(--line);
        border-radius: 6px;
        padding: 6px 10px 10px;
        min-width: 0;
      }
      .sd-item legend {
        font-size: 12px;
        font-weight: 600;
        padding: 0 4px;
      }
      .sd-each {
        max-width: 320px;
      }
      .sd-group {
        display: grid;
        gap: 6px;
        min-width: 0;
      }
      .sd-add-nested {
        justify-self: start;
      }
      .sd-flag,
      .sd-error,
      .sd-summary,
      .sd-empty {
        margin: 0;
        font-size: 12px;
        line-height: 1.5;
        overflow-wrap: anywhere;
      }
      .sd-flag {
        color: var(--muted);
      }
      .sd-flag.warn {
        color: #5f4612;
      }
      .sd-error,
      .sd-summary {
        color: var(--danger);
      }
      .sd-notes {
        display: flex;
        gap: 8px;
        align-items: flex-start;
        justify-content: space-between;
      }
      .sd-notes ul {
        margin: 0;
        padding-left: 18px;
      }
      .sd-root-options {
        display: grid;
        gap: 4px;
      }
      .sd-preview {
        border: 1px solid var(--line);
        border-radius: 6px;
        padding: 10px 12px;
        background: var(--mist);
      }
      .sd-preview h4 {
        margin: 0 0 8px;
        font-size: 13px;
      }
      .sd-json summary {
        cursor: pointer;
        font-size: 12px;
        color: var(--muted);
      }
      .sd-json pre {
        max-height: 240px;
        overflow: auto;
        font-size: 11px;
        background: var(--mist);
        padding: 8px;
        border-radius: 6px;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
      }
      .sd-mode {
        border: 0;
        padding: 0;
        margin: 16px 0 0;
        display: grid;
        gap: 6px;
      }
      .sd-mode legend {
        font-size: 12px;
        font-weight: 600;
        margin-bottom: 6px;
      }
      .sd-mode input {
        width: 16px;
      }
      @container designer (max-width: 560px) {
        .sd-main {
          grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
        }
        .sd-required {
          padding-bottom: 0;
        }
      }
      @container designer (max-width: 340px) {
        .sd-main,
        .sd-details-grid {
          grid-template-columns: minmax(0, 1fr);
        }
      }
    `,
  ],
})
export class SchemaDesigner implements OnChanges {
  /** The schema being edited (any JSON; undefined for none yet). */
  schema = input<unknown>(undefined);
  /** When set, the designer reloads `schema` only when this number changes. */
  revision = input<number | null>(null);
  heading = input("Fields");
  purpose = input<DesignerPurpose>("other");
  readonly = input(false);
  /** Example-to-schema inference; hides "Paste sample JSON" when null. */
  inferSchema = input<InferSchema | null>(null);
  /** Shows a live form preview of the schema. */
  preview = input(true);
  /** Each valid edit: the whole schema. Nothing is emitted while rows are invalid. */
  schemaChange = output<JsonObject>();
  /**
   * Whether the rows can be written: on every edit, and after a reload (new
   * `schema` or `revision`) whose answer differs from the last one sent
   * (never sent counts as valid).
   */
  validityChange = output<boolean>();

  readonly prefix = `schema-designer-${++sequence}`;
  readonly typeOptions = typeOptions;
  readonly formatOptions = formatOptions;
  readonly maxDepth = MAX_EDIT_DEPTH;
  readonly maxSamples = MAX_SAMPLES;
  model: DesignerModel = schemaToModel(undefined);
  issues: DesignerIssue[] = [];
  notes: string[] = [];
  previewSchema: TaskSchema = {};
  readonly status = signal("");
  readonly issueCount = signal(0);
  readonly previewText = signal("{}");
  readonly previewHeading = computed(() => previewHeadings[this.purpose()]);
  sampleOpen = false;
  samples: string[] = [""];
  sampleMode: InferredMode = "replace";
  sampleError = "";
  sampleBusy = false;

  private touched = new Set<number>();
  private lastEmitted: string | null = null;
  private loadedRevision: number | null = null;
  /** Bumped to discard an inference answer that is still on its way. */
  private inference = 0;
  /** The validity last told to the host (null: never told, taken as valid). */
  private lastValidity: boolean | null = null;
  private loads = 0;
  private destroyed = false;
  private readonly cdr = inject(ChangeDetectorRef);
  private readonly injector = inject(Injector);
  private readonly host = inject(ElementRef<HTMLElement>);

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.destroyed = true;
      this.inference++;
    });
  }

  ngOnChanges(changes: SimpleChanges) {
    if (!changes["schema"] && !changes["revision"]) return;
    const revision = this.revision();
    if (revision !== null) {
      if (revision === this.loadedRevision && !changes["revision"]?.firstChange)
        return;
      this.loadedRevision = revision;
    } else if (
      this.lastEmitted !== null &&
      canonicalJson(this.schema()) === this.lastEmitted
    )
      return; // Our own edit coming back through the host.
    this.load();
  }

  private load() {
    // The example dialog belongs to the fields being replaced.
    if (this.sampleOpen || this.sampleBusy) this.closeSample();
    this.model = schemaToModel(this.schema());
    this.touched.clear();
    this.notes = [];
    this.lastEmitted = null;
    this.refresh(false);
    // A host that tracks validity must not keep the previous schema's answer
    // (for example after switching steps with an unfinished row). Emitted
    // after this change detection pass, never during the host's own.
    const valid = this.issues.length === 0;
    if (valid !== (this.lastValidity ?? true)) {
      const ticket = ++this.loads;
      queueMicrotask(() => {
        if (ticket !== this.loads || this.destroyed) return;
        this.lastValidity = valid;
        this.validityChange.emit(valid);
      });
    }
  }

  /** Recomputes issues and the preview; emits the schema when valid. */
  private refresh(emit: boolean) {
    this.issues = designerIssues(this.model);
    this.issueCount.set(this.issues.filter((i) => !this.hidden(i)).length);
    const valid = this.issues.length === 0;
    if (valid) {
      const next = modelToSchema(this.model);
      this.previewSchema = (isJsonObject(next) ? next : {}) as TaskSchema;
      this.previewText.set(JSON.stringify(next ?? {}, null, 2));
      if (emit && this.model.editable && isJsonObject(next)) {
        this.lastEmitted = canonicalJson(next);
        this.schemaChange.emit(next);
      }
    }
    if (emit) {
      this.loads++; // a newer answer than any validity still queued by load()
      this.lastValidity = valid;
      this.validityChange.emit(valid);
    }
  }
  private changed() {
    this.refresh(true);
  }

  id(row: DesignerRow, part: string) {
    return `${this.prefix}-${row.uid}-${part}`;
  }
  addId(parent: DesignerRow | null) {
    return `${this.prefix}-${parent ? parent.uid : "root"}-add`;
  }
  nameOf(row: DesignerRow) {
    return row.name.trim() ? `“${row.name}”` : "the new field";
  }
  /** False for a true/false schema, which cannot carry a label or help text. */
  labelled(row: DesignerRow) {
    return !row.locked || isJsonObject(row.source);
  }
  /** The row that holds nested fields: the group itself, or a list's group item. */
  groupOf(row: DesignerRow): DesignerRow | null {
    if (row.locked) return null;
    if (row.type === "object") return row;
    if (row.type === "array" && row.item?.type === "object" && !row.item.locked)
      return row.item;
    return null;
  }
  bounds(row: DesignerRow): [string, string] | null {
    if (row.type === "string") return ["Shortest length", "Longest length"];
    if (row.type === "number" || row.type === "integer")
      return ["Smallest value", "Largest value"];
    if (row.type === "array") return ["Fewest items", "Most items"];
    return null;
  }
  issue(row: DesignerRow, field: DesignerIssue["field"]): string {
    return (
      this.issues.find((i) => i.row === row.uid && i.field === field)
        ?.message ?? ""
    );
  }
  /** A missing name is shown once the person has left the field. */
  private hidden(issue: DesignerIssue) {
    return (
      issue.message === "Enter a field name." && !this.touched.has(issue.row)
    );
  }
  shownIssue(row: DesignerRow, field: DesignerIssue["field"]): string {
    const found = this.issues.find(
      (i) => i.row === row.uid && i.field === field,
    );
    return found && !this.hidden(found) ? found.message : "";
  }
  touch(row: DesignerRow) {
    if (this.touched.has(row.uid)) return;
    this.touched.add(row.uid);
    this.refresh(false);
  }

  private value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  private checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }
  setName(row: DesignerRow, event: Event) {
    row.name = this.value(event);
    this.changed();
  }
  setType(row: DesignerRow, event: Event) {
    changeType(row, this.value(event) as EditableType);
    if (row.type === "object" || row.type === "array") row.expanded = false;
    this.changed();
  }
  setItemType(row: DesignerRow, event: Event) {
    const type = this.value(event) as EditableType;
    if (!row.item) row.item = newRow(type);
    else changeType(row.item, type);
    this.changed();
  }
  setRequired(row: DesignerRow, event: Event) {
    row.required = this.checked(event);
    this.changed();
  }
  setText(
    row: DesignerRow,
    key: "title" | "description" | "choices" | "min" | "max",
    event: Event,
  ) {
    row[key] = this.value(event);
    this.changed();
  }
  setFormat(row: DesignerRow, event: Event) {
    row.format = this.value(event) as Format;
    this.changed();
  }
  setNullable(row: DesignerRow, event: Event) {
    row.nullable = this.checked(event);
    this.changed();
  }
  setClosed(row: DesignerRow, event: Event) {
    row.closed = this.checked(event);
    this.changed();
  }
  setRootClosed(event: Event) {
    this.model.closed = this.checked(event);
    this.changed();
  }

  add(parent: DesignerRow | null) {
    const row = addRow(this.model, parent);
    this.changed();
    this.status.set("Field added. Enter its name.");
    this.focusLater(this.id(row, "name"));
  }
  remove(row: DesignerRow, rows: DesignerRow[], parent: DesignerRow | null) {
    const index = rows.indexOf(row);
    const neighbor = rows[index - 1] ?? rows[index + 1];
    const label = row.name.trim() || "the new field";
    removeRow(this.model, row);
    this.touched.delete(row.uid);
    this.changed();
    this.status.set(`Removed ${label}.`);
    this.focusLater(neighbor ? this.id(neighbor, "name") : this.addId(parent));
  }
  move(row: DesignerRow, delta: -1 | 1) {
    if (!moveRow(this.model, row, delta)) return;
    this.changed();
    this.status.set(
      `Moved ${row.name.trim() || "the field"} ${delta < 0 ? "up" : "down"}.`,
    );
    // Moving the element drops its focus; put it back, or on the other arrow at an end.
    const same = this.id(row, delta < 0 ? "up" : "down");
    const other = this.id(row, delta < 0 ? "down" : "up");
    this.focusLater(same, other);
  }

  private focusLater(id: string, fallback?: string) {
    afterNextRender(
      () => {
        const root = this.host.nativeElement as HTMLElement;
        const find = (key: string) =>
          root.querySelector<HTMLElement>(`#${CSS.escape(key)}`);
        let element = find(id);
        if ((!element || (element as HTMLButtonElement).disabled) && fallback)
          element = find(fallback);
        element?.focus();
      },
      { injector: this.injector },
    );
  }

  openSample() {
    this.samples = [""];
    this.sampleError = "";
    this.sampleMode = this.model.rows.length ? "add" : "replace";
    this.sampleOpen = true;
  }
  closeSample() {
    // Example values are never kept once the dialog closes, and an answer
    // still on its way is discarded.
    this.inference++;
    this.sampleBusy = false;
    this.sampleOpen = false;
    this.samples = [""];
    this.sampleError = "";
  }
  setSample(index: number, event: Event) {
    this.samples[index] = this.value(event);
  }
  addSample() {
    if (this.samples.length >= MAX_SAMPLES) return;
    this.samples = [...this.samples, ""];
    this.focusLater(`${this.prefix}-sample-${this.samples.length - 1}`);
  }
  removeSample(index: number) {
    this.samples = this.samples.filter((_, i) => i !== index);
    this.focusLater(`${this.prefix}-sample-${Math.max(0, index - 1)}`);
  }

  async runInference() {
    const infer = this.inferSchema();
    if (!infer || this.sampleBusy) return;
    const texts = this.samples.filter((text) => text.trim());
    if (!texts.length) {
      this.sampleError = "Paste at least one example.";
      return;
    }
    if (new TextEncoder().encode(texts.join("")).length > SAMPLE_LIMIT_BYTES) {
      this.sampleError =
        "The examples are too large. Paste at most 256 KB of JSON in total.";
      return;
    }
    const values: Json[] = [];
    for (const [index, text] of this.samples.entries()) {
      if (!text.trim()) continue;
      const parsed = parseJsonText(text);
      const label = this.samples.length > 1 ? `Example ${index + 1}: ` : "";
      if (!parsed.ok) {
        this.sampleError = label + parsed.message;
        return;
      }
      if (!isJsonObject(parsed.value)) {
        this.sampleError = `${label}Paste a JSON object such as {"id": 1}, so its fields can be listed.`;
        return;
      }
      values.push(parsed.value);
    }
    const decimals = texts.flatMap(decimalPaths);
    this.sampleBusy = true;
    this.sampleError = "";
    // Closing the dialog, loading another schema or leaving discards the answer.
    const ticket = ++this.inference;
    try {
      const result = await infer(values);
      if (ticket !== this.inference) return;
      const schema = widenDecimals(result.schema, decimals) as JsonObject;
      const warnings = result.warnings;
      const found = schemaToModel(schema).rows.length;
      if (!found) {
        this.sampleError = isJsonObject(own(schema, "additionalProperties"))
          ? "Studio found no field names: the example's keys look like data, such as IDs, numbers or text with spaces. Add the fields yourself."
          : "The example has no fields to add.";
        return;
      }
      const added = applyInferred(this.model, schema, this.sampleMode);
      this.changed();
      this.notes = [...(warnings ?? []), ...inferenceNotes(this.model)];
      this.status.set(
        added === 1
          ? "Found 1 field in the example."
          : `Found ${added} fields in the example.`,
      );
      this.closeSample();
    } catch (error) {
      if (ticket === this.inference)
        this.sampleError = describeError(error).message;
    } finally {
      if (ticket === this.inference) this.sampleBusy = false;
      this.cdr.markForCheck();
    }
  }
}
