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
// Reads a user or connector JSON Schema the way a form needs it: references
// followed, `allOf` object branches merged, the nullable spellings collapsed
// into one flag, and a widget kind and a plain label for every field. No
// Angular imports; vitest runs it under Node.
//
// Only local references are followed: "#/..." inside the document and bundle
// keys ("common.json", "common.json#/$defs/Id"). Nothing is ever fetched.
// The server stays authoritative for validation; this module only decides
// how a field is shown.

import {
  canonicalJson,
  getAt,
  hasOwn,
  isJsonObject,
  parsePointer,
  type Json,
  type JsonObject,
} from "./json";

export interface ResolveOptions {
  /** The document that local "#/..." references resolve against (default: the schema itself). */
  root?: unknown;
  /** Bundle documents by key; "key" and "key#/pointer" references resolve against them. */
  bundle?: Readonly<Record<string, unknown>>;
}

export interface ResolvedSchema {
  /** The normalized schema: references followed, `allOf` merged, `null` removed from `type`. */
  schema: JsonObject;
  /** True when null is also accepted (`type: [T, "null"]`, a null alternative, or null in `enum`). */
  nullable: boolean;
  /** A mixed type array lost null during normalization (unlike null in an enum). */
  removedNullType: boolean;
  /** Resolution encountered composition; normalization is not a validity proof. */
  composed: boolean;
  /** True for the `false` schema, which accepts no value. */
  never: boolean;
  /** True when a reference cycle was cut here; the field is edited as JSON. */
  cyclic: boolean;
  /** References that point nowhere (or outside the document); the field is edited as JSON. */
  unresolved: string[];
  /** Keywords the form cannot render; the server still validates them. */
  unsupported: string[];
  /** Where nested local references resolve (a bundle document once a reference entered it). */
  context: ResolveOptions;
}

export type FieldKind =
  | "text"
  | "multiline"
  | "number"
  | "integer"
  | "boolean"
  | "choice"
  | "const"
  | "datetime"
  | "email"
  | "uri"
  | "uuid"
  | "secret"
  | "object"
  | "map"
  | "list"
  | "table"
  | "union"
  | "any"
  | "never"
  | "json";

export interface ChoiceOption {
  value: Json;
  label: string;
}

export interface FieldInfo {
  key: string;
  /** Plain label: the schema title unless it is Pydantic's default, else the humanized key. */
  label: string;
  description?: string;
  kind: FieldKind;
  /** The resolved schema; children and items resolve with `context`. */
  schema: JsonObject;
  context: ResolveOptions;
  required: boolean;
  nullable: boolean;
  readOnly: boolean;
  deprecated: boolean;
  /** `x-secret` or `writeOnly`: a form never writes a value here. */
  secret: boolean;
  format?: string;
  /** For `choice`: `enum` values, or `oneOf`/`anyOf` constants with their titles. */
  options?: ChoiceOption[];
  /** For `const` (a `{type: "null"}` field is the constant null). */
  constValue?: Json;
  default?: Json;
  examples?: Json[];
  unsupported: string[];
  unresolved: string[];
}

/** The string formats the platform's schema profile checks (compiler/schema_profile.py). */
export const SUPPORTED_FORMATS = ["date-time", "email", "uri", "uuid"] as const;
const FORMAT_KINDS: Record<string, FieldKind> = {
  "date-time": "datetime",
  email: "email",
  uri: "uri",
  uuid: "uuid",
};
/** Keywords a form cannot render; when they are all a schema has, the field is JSON. */
const UNSUPPORTED = [
  "not",
  "if",
  "then",
  "else",
  "dependentSchemas",
  "prefixItems",
  "contains",
];
/** Keywords that only describe a value; they never change which values are valid. */
const ANNOTATIONS = new Set([
  "title",
  "description",
  "default",
  "examples",
  "deprecated",
  "readOnly",
  "writeOnly",
  "x-secret",
  "$comment",
]);
const LOWER_BOUNDS = new Set([
  "minimum",
  "exclusiveMinimum",
  "minLength",
  "minItems",
  "minProperties",
  "minContains",
]);
const UPPER_BOUNDS = new Set([
  "maximum",
  "exclusiveMaximum",
  "maxLength",
  "maxItems",
  "maxProperties",
  "maxContains",
]);
/** Strings that may be longer than this get a multi-line editor (names and IDs stay single-line). */
const MULTILINE_LENGTH = 1000;
/** Nested references followed before giving up (the profile allows 64). */
const MAX_REF_DEPTH = 64;

function own(object: JsonObject, key: string): Json | undefined {
  return hasOwn(object, key) ? object[key] : undefined;
}
function without(object: JsonObject, ...keys: string[]): JsonObject {
  const next: JsonObject = {};
  for (const key of Object.keys(object))
    if (!keys.includes(key))
      Object.defineProperty(next, key, {
        value: object[key],
        enumerable: true,
        writable: true,
        configurable: true,
      });
  return next;
}
function define(object: JsonObject, key: string, value: Json) {
  Object.defineProperty(object, key, {
    value,
    enumerable: true,
    writable: true,
    configurable: true,
  });
}

// Reference cycles are detected per document, so equal pointers in two
// bundle documents never look like a cycle.
const documents = new WeakMap<object, number>();
let documentSequence = 0;
function documentKey(root: unknown): string {
  if (typeof root !== "object" || root === null) return "-";
  let id = documents.get(root);
  if (id === undefined) documents.set(root, (id = ++documentSequence));
  return String(id);
}

interface Lookup {
  schema: unknown;
  context: ResolveOptions;
}

/** Finds a local or bundle reference target; null when there is none. */
function lookup(ref: string, context: ResolveOptions): Lookup | null {
  const hash = ref.indexOf("#");
  const documentKey = hash < 0 ? ref : ref.slice(0, hash);
  const fragment = hash < 0 ? "" : ref.slice(hash + 1);
  let document: unknown;
  let next = context;
  if (documentKey === "") document = context.root;
  else {
    const bundle = context.bundle ?? {};
    if (!hasOwn(bundle, documentKey)) return null;
    document = bundle[documentKey];
    next = { root: document, bundle: context.bundle };
  }
  let pointer: string;
  try {
    pointer = decodeURIComponent(fragment);
  } catch {
    return null;
  }
  const segments = parsePointer(pointer);
  if (segments === null) return null;
  const target = getAt(document, segments);
  return target === undefined ? null : { schema: target, context: next };
}

const nullSchema = (schema: unknown): boolean =>
  isJsonObject(schema) &&
  (own(schema, "type") === "null" ||
    (hasOwn(schema, "const") && schema["const"] === null) ||
    (Array.isArray(own(schema, "enum")) &&
      (schema["enum"] as Json[]).length === 1 &&
      (schema["enum"] as Json[])[0] === null));

interface Work {
  schema: JsonObject;
  nullable: boolean;
  removedNullType: boolean;
  composed: boolean;
  never: boolean;
  cyclic: boolean;
  unresolved: string[];
  unsupported: string[];
  context: ResolveOptions;
}

/** Keywords of `b` that `a` already has with another meaning, after tightening bounds. */
function merge(a: JsonObject, b: JsonObject): JsonObject | null {
  const result: JsonObject = { ...a };
  for (const key of Object.keys(b)) {
    const right = b[key];
    if (!hasOwn(result, key)) {
      define(result, key, right);
      continue;
    }
    const left = result[key];
    if (canonicalJson(left) === canonicalJson(right)) continue;
    if (ANNOTATIONS.has(key)) continue; // The first branch's wording wins.
    if (
      (LOWER_BOUNDS.has(key) || UPPER_BOUNDS.has(key)) &&
      typeof left === "number" &&
      typeof right === "number"
    ) {
      define(
        result,
        key,
        LOWER_BOUNDS.has(key) ? Math.max(left, right) : Math.min(left, right),
      );
      continue;
    }
    if (key === "type") {
      const l = Array.isArray(left) ? left : [left];
      const r = new Set(Array.isArray(right) ? right : [right]);
      const both = l.filter(
        (t) => r.has(t) || (t === "integer" && r.has("number")),
      );
      const also = [...r].filter(
        (t) => t === "integer" && l.includes("number"),
      );
      const types = [...new Set([...both, ...also])] as Json[];
      if (!types.length) return null;
      define(result, key, types.length === 1 ? types[0] : types);
      continue;
    }
    if (key === "required" && Array.isArray(left) && Array.isArray(right)) {
      define(result, key, [...new Set([...left, ...right])] as Json[]);
      continue;
    }
    if (key === "properties" && isJsonObject(left) && isJsonObject(right)) {
      const properties: JsonObject = { ...left };
      for (const name of Object.keys(right))
        define(
          properties,
          name,
          hasOwn(properties, name)
            ? { allOf: [properties[name], right[name]] }
            : right[name],
        );
      define(result, key, properties);
      continue;
    }
    if (key === "enum" && Array.isArray(left) && Array.isArray(right)) {
      const allowed = new Set(right.map(canonicalJson));
      const values = left.filter((v) => allowed.has(canonicalJson(v)));
      if (!values.length) return null;
      define(result, key, values);
      continue;
    }
    if (key === "additionalProperties" && (left === false || right === false)) {
      define(result, key, false);
      continue;
    }
    return null;
  }
  return result;
}

function resolveNode(
  input: unknown,
  context: ResolveOptions,
  seen: ReadonlySet<string>,
  depth: number,
): Work {
  const base = (schema: JsonObject): Work => ({
    schema,
    nullable: false,
    removedNullType: false,
    composed: ["allOf", "oneOf", "anyOf"].some((key) => hasOwn(schema, key)),
    never: false,
    cyclic: false,
    unresolved: [],
    unsupported: [],
    context,
  });
  if (input === false) return { ...base({}), never: true };
  if (!isJsonObject(input)) return base({});
  let schema = input;
  let work = base(schema);

  const ref = own(schema, "$ref");
  if (typeof ref === "string") {
    const siblings = without(schema, "$ref");
    const key = documentKey(context.root) + "\u0000" + ref;
    const target = depth < MAX_REF_DEPTH ? lookup(ref, context) : null;
    if (!target) return { ...base(siblings), unresolved: [ref] };
    if (seen.has(key)) return { ...base(siblings), cyclic: true };
    const inner = resolveNode(
      target.schema,
      target.context,
      new Set([...seen, key]),
      depth + 1,
    );
    if (inner.cyclic || inner.unresolved.length || inner.never)
      return {
        ...inner,
        schema: { ...inner.schema, ...siblings },
      };
    const onlyAnnotations = Object.keys(siblings).every((k) =>
      ANNOTATIONS.has(k),
    );
    if (onlyAnnotations) {
      // Siblings win: a field's own title describes it better than its type's.
      const combined: JsonObject = { ...inner.schema };
      for (const k of Object.keys(siblings)) define(combined, k, siblings[k]);
      return { ...inner, schema: combined };
    }
    const merged = merge(inner.schema, siblings);
    if (!merged)
      return {
        ...base(schema),
        unsupported: ["allOf"],
      };
    schema = merged;
    work = {
      ...inner,
      schema,
      // Structural reference siblings are also an intersection. Definitions and
      // document identifiers alone do not constrain the referenced value.
      composed:
        inner.composed ||
        Object.keys(siblings).some(
          (key) =>
            !ANNOTATIONS.has(key) &&
            !["$defs", "definitions", "$schema", "$id"].includes(key),
        ),
    };
  }

  const allOf = own(schema, "allOf");
  if (Array.isArray(allOf)) {
    let merged: JsonObject | null = without(schema, "allOf");
    let nullable = true;
    const flags = {
      cyclic: false,
      unresolved: [] as string[],
      unsupported: [] as string[],
    };
    for (const branch of allOf) {
      const part = resolveNode(branch, context, seen, depth + 1);
      if (part.never) return { ...base(schema), never: true };
      flags.cyclic ||= part.cyclic;
      flags.unresolved.push(...part.unresolved);
      flags.unsupported.push(...part.unsupported);
      nullable &&= part.nullable;
      merged = merged && merge(merged, part.schema);
    }
    if (!merged)
      return { ...work, schema, unsupported: [...work.unsupported, "allOf"] };
    schema = merged;
    work = {
      ...work,
      schema,
      nullable: work.nullable || (nullable && allOf.length > 0),
      cyclic: work.cyclic || flags.cyclic,
      unresolved: [...work.unresolved, ...flags.unresolved],
      unsupported: [...work.unsupported, ...flags.unsupported],
    };
  }

  // Nullable spellings: `type: [T, "null"]`, a null alternative, null in `enum`.
  const type = own(schema, "type");
  if (Array.isArray(type) && type.includes("null") && type.length > 1) {
    const rest = type.filter((t) => t !== "null");
    schema = { ...schema };
    define(schema, "type", rest.length === 1 ? rest[0] : rest);
    work = { ...work, schema, nullable: true, removedNullType: true };
  }
  for (const key of ["anyOf", "oneOf"]) {
    const alternatives = own(schema, key);
    if (!Array.isArray(alternatives) || alternatives.length < 2) continue;
    const others = alternatives.filter((a) => !nullSchema(a));
    if (others.length === alternatives.length || !others.length) continue;
    if (others.length > 1) {
      schema = { ...schema };
      define(schema, key, others);
      work = { ...work, schema, nullable: true };
      continue;
    }
    const inner = resolveNode(others[0], context, seen, depth + 1);
    const combined = merge(inner.schema, without(schema, key));
    if (!combined) continue;
    schema = combined;
    work = {
      ...work,
      ...inner,
      schema,
      composed: true,
      nullable: true,
      unresolved: [...work.unresolved, ...inner.unresolved],
      unsupported: [...work.unsupported, ...inner.unsupported],
    };
  }
  const values = own(schema, "enum");
  if (Array.isArray(values) && values.includes(null))
    work = { ...work, nullable: true };

  const unsupported = UNSUPPORTED.filter((k) => hasOwn(schema, k));
  return {
    ...work,
    schema,
    unsupported: [...new Set([...work.unsupported, ...unsupported])],
  };
}

/**
 * Normalizes a schema for display: follows local references (cycles are cut
 * and reported), merges `allOf` object branches, and turns the nullable
 * spellings into the `nullable` flag. The input is never modified.
 */
export function resolveSchema(
  schema: unknown,
  options: ResolveOptions = {},
): ResolvedSchema {
  const context: ResolveOptions = {
    root: options.root ?? schema,
    bundle: options.bundle,
  };
  const work = resolveNode(schema, context, new Set(), 0);
  return {
    schema: work.schema,
    nullable: work.nullable,
    removedNullType: work.removedNullType,
    composed: work.composed,
    never: work.never,
    cyclic: work.cyclic,
    unresolved: [...new Set(work.unresolved)],
    unsupported: [...new Set(work.unsupported)],
    context: work.context,
  };
}

const ACRONYMS: Record<string, string> = {
  api: "API",
  cpu: "CPU",
  csv: "CSV",
  dns: "DNS",
  html: "HTML",
  http: "HTTP",
  https: "HTTPS",
  id: "ID",
  ids: "IDs",
  imap: "IMAP",
  ip: "IP",
  json: "JSON",
  jwt: "JWT",
  oauth: "OAuth",
  pdf: "PDF",
  sasl: "SASL",
  sms: "SMS",
  smtp: "SMTP",
  sql: "SQL",
  ssl: "SSL",
  tls: "TLS",
  ttl: "TTL",
  uri: "URI",
  url: "URL",
  urls: "URLs",
  utc: "UTC",
  uuid: "UUID",
  xml: "XML",
};

/** "statement_timeout_ms" → "Statement timeout ms"; "baseURL" → "Base URL". */
export function humanize(key: string): string {
  const words = key
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
    .replace(/([A-Za-z])([0-9])/g, "$1 $2")
    .replace(/([0-9])([A-Za-z])/g, "$1 $2")
    .split(/[\s_.-]+/)
    .filter(Boolean)
    .map((word) => {
      const known = ACRONYMS[word.toLowerCase()];
      if (known) return known;
      return word.length > 1 && word === word.toUpperCase()
        ? word
        : word.toLowerCase();
    });
  if (!words.length) return "";
  words[0] = words[0].charAt(0).toUpperCase() + words[0].slice(1);
  return words.join(" ");
}

/** Python's `str.title()` for the ASCII and Unicode letters a key can hold. */
function pythonTitle(text: string): string {
  return text.replace(
    /\p{L}+/gu,
    (word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase(),
  );
}

/** True when `title` is the one Pydantic generates for `key` ("Sasl Mechanism"). */
export function isPydanticDefaultTitle(key: string, title: string): boolean {
  return pythonTitle(key).replace(/_/g, " ").trim() === title.trim();
}

// "BrokerConnectionConfig": a model class name Pydantic uses as a root title.
const CLASS_NAME = /^[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+$/;

/** A schema's own title for headings, without generated class-name titles. */
export function schemaTitle(schema: unknown): string | undefined {
  if (!isJsonObject(schema)) return undefined;
  const title = own(schema, "title");
  if (typeof title !== "string" || !title.trim()) return undefined;
  return CLASS_NAME.test(title.trim()) ? undefined : title.trim();
}

/**
 * The label for a field: its title, unless that is Pydantic's default title
 * for the key or a generated class name; otherwise the humanized key.
 */
export function fieldLabel(
  key: string,
  schema: unknown,
  resolved?: JsonObject,
): string {
  for (const candidate of [schema, resolved]) {
    if (!isJsonObject(candidate)) continue;
    const title = own(candidate, "title");
    if (typeof title !== "string" || !title.trim()) continue;
    if (isPydanticDefaultTitle(key, title)) break;
    if (candidate === resolved && CLASS_NAME.test(title.trim())) break;
    return title.trim();
  }
  return humanize(key) || key;
}

const choiceLabel = (value: Json): string =>
  value === null
    ? "None"
    : typeof value === "string"
      ? value
      : JSON.stringify(value);

/** Every alternative is a constant: a labelled choice ("oneOf": [{const, title}]). */
function constantAlternatives(
  alternatives: Json[],
  context: ResolveOptions,
): ChoiceOption[] | null {
  const options: ChoiceOption[] = [];
  for (const alternative of alternatives) {
    const resolved = resolveSchema(alternative, context).schema;
    let value: Json | undefined;
    if (hasOwn(resolved, "const")) value = resolved["const"];
    else {
      const values = own(resolved, "enum");
      if (Array.isArray(values) && values.length === 1) value = values[0];
    }
    if (value === undefined) return null;
    const title = own(resolved, "title");
    options.push({
      value,
      label:
        typeof title === "string" && title.trim()
          ? title.trim()
          : choiceLabel(value),
    });
  }
  return options;
}

function typesOf(schema: JsonObject): string[] {
  const type = own(schema, "type");
  if (typeof type === "string") return [type];
  if (Array.isArray(type))
    return type.filter((t): t is string => typeof t === "string");
  return [];
}

/** Distinct types, or objects that share a constant discriminator property. */
function isUnion(alternatives: Json[], context: ResolveOptions): boolean {
  const resolved = alternatives.map((a) => resolveSchema(a, context).schema);
  const types = resolved.map(typesOf);
  if (types.every((t) => t.length === 1))
    if (new Set(types.map((t) => t[0])).size === types.length) return true;
  const objects = resolved.filter((s) => isJsonObject(own(s, "properties")));
  if (objects.length !== resolved.length) return false;
  const shared = Object.keys(objects[0]["properties"] as JsonObject).filter(
    (name) =>
      objects.every((s) => {
        const property = own(s["properties"] as JsonObject, name);
        return isJsonObject(property) && hasOwn(property, "const");
      }),
  );
  return shared.length > 0;
}

/** True while a list's item kind is being read (see the array case in `kindOf`). */
let classifyingItem = false;

function kindOf(
  resolved: ResolvedSchema,
  secret: boolean,
): { kind: FieldKind; options?: ChoiceOption[]; constValue?: Json } {
  const schema = resolved.schema;
  if (resolved.never) return { kind: "never" };
  if (resolved.cyclic || resolved.unresolved.length) return { kind: "json" };
  if (resolved.unsupported.includes("allOf")) return { kind: "json" };
  if (secret) return { kind: "secret" };
  if (hasOwn(schema, "const"))
    return { kind: "const", constValue: schema["const"] };
  const types = typesOf(schema);
  if (types.length === 1 && types[0] === "null")
    return { kind: "const", constValue: null };
  const values = own(schema, "enum");
  if (Array.isArray(values) && values.length)
    return {
      kind: "choice",
      options: values.map((value) => ({ value, label: choiceLabel(value) })),
    };
  for (const key of ["oneOf", "anyOf"]) {
    const alternatives = own(schema, key);
    if (!Array.isArray(alternatives) || !alternatives.length) continue;
    const options = constantAlternatives(alternatives, resolved.context);
    if (options) return { kind: "choice", options };
    return {
      kind:
        alternatives.length > 1 && isUnion(alternatives, resolved.context)
          ? "union"
          : "json",
    };
  }
  if (types.length > 1) return { kind: "union" };
  const type =
    types[0] ??
    (isJsonObject(own(schema, "properties"))
      ? "object"
      : hasOwn(schema, "items") || hasOwn(schema, "prefixItems")
        ? "array"
        : hasOwn(schema, "additionalProperties") ||
            hasOwn(schema, "patternProperties")
          ? "object"
          : undefined);
  if (type === undefined)
    return { kind: resolved.unsupported.length ? "json" : "any" };
  if (type === "string") {
    const format = own(schema, "format");
    if (typeof format === "string" && FORMAT_KINDS[format])
      return { kind: FORMAT_KINDS[format] };
    const maxLength = own(schema, "maxLength");
    return {
      kind:
        typeof maxLength === "number" && maxLength > MULTILINE_LENGTH
          ? "multiline"
          : "text",
    };
  }
  if (type === "integer" || type === "number" || type === "boolean")
    return { kind: type };
  if (type === "object") {
    const properties = own(schema, "properties");
    if (isJsonObject(properties) && Object.keys(properties).length)
      return { kind: "object" };
    return {
      kind: own(schema, "additionalProperties") === false ? "object" : "map",
    };
  }
  if (type === "array") {
    if (hasOwn(schema, "prefixItems")) return { kind: "json" };
    // Classifying an item that is itself a list: whatever its items are, the
    // outer list becomes JSON, so its items are not read. This also stops a
    // recursive list ("items": {"$ref": "#"}) from recursing forever.
    if (!hasOwn(schema, "items") || classifyingItem) return { kind: "list" };
    let items: FieldInfo;
    classifyingItem = true;
    try {
      items = describeField("item", schema["items"], resolved.context);
    } finally {
      classifyingItem = false;
    }
    if (items.kind === "object") return { kind: "table" };
    if (["list", "table", "map", "union", "json", "never"].includes(items.kind))
      return { kind: "json" };
    return { kind: "list" };
  }
  return { kind: "json" };
}

/** How one field is shown: its resolved schema, widget kind, label and flags. */
export function describeField(
  key: string,
  schema: unknown,
  options: ResolveOptions & { required?: boolean } = {},
): FieldInfo {
  const resolved = resolveSchema(schema, options);
  const s = resolved.schema;
  const flag = (name: string) => own(s, name) === true;
  const secret = flag("x-secret") || flag("writeOnly");
  const { kind, options: choices, constValue } = kindOf(resolved, secret);
  const raw = isJsonObject(schema) ? schema : {};
  const description = own(raw, "description") ?? own(s, "description");
  const format = own(s, "format");
  const examples = own(s, "examples");
  const info: FieldInfo = {
    key,
    label: fieldLabel(key, raw, s),
    kind,
    schema: s,
    context: resolved.context,
    required: options.required ?? false,
    nullable: resolved.nullable,
    readOnly: flag("readOnly"),
    deprecated: flag("deprecated"),
    secret,
    unsupported: resolved.unsupported,
    unresolved: resolved.unresolved,
  };
  if (typeof description === "string" && description.trim())
    info.description = description.trim();
  if (typeof format === "string") info.format = format;
  if (choices) info.options = choices;
  if (kind === "const") info.constValue = constValue ?? null;
  if (hasOwn(s, "default")) info.default = s["default"];
  if (Array.isArray(examples)) info.examples = examples;
  return info;
}

/** The fields of an object schema, in declaration order, with their required flags. */
export function fieldsOf(
  schema: unknown,
  options: ResolveOptions = {},
): FieldInfo[] {
  const resolved = resolveSchema(schema, options);
  const properties = own(resolved.schema, "properties");
  if (!isJsonObject(properties)) return [];
  const required = own(resolved.schema, "required");
  const names = new Set(
    Array.isArray(required)
      ? required.filter((n): n is string => typeof n === "string")
      : [],
  );
  return Object.keys(properties).map((key) =>
    describeField(key, properties[key], {
      ...resolved.context,
      required: names.has(key),
    }),
  );
}
