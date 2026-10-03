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
// Weave Expression <-> bound field tree, so a form can show each field of an
// expression-valued input as a value, a data reference or a formula.
//
// Decoding: `literal`, `object` and `array` descend into their children; a
// `ref` becomes a reference leaf; an `op`, an unknown shape, or a container
// whose shape contradicts the field schema becomes a formula leaf on that
// node only.
//
// Encoding is shape-preserving: every decoded node remembers its original
// expression (`source`) until it is edited, and an untouched node encodes to
// that same object, so an unedited input yields the identical object and the
// editor never rewrites the YAML. Edited containers are re-encoded
// canonically: all-literal children collapse to one `{literal}`, otherwise
// `{object}`/`{array}` with per-child encoding (untouched children kept).

type JsonObject = Record<string, unknown>;
export type Path = (string | number)[];

export type Bound =
  | { kind: "value"; value: unknown; source?: unknown }
  | { kind: "ref"; pointer: string; source?: unknown }
  | { kind: "formula"; expression: unknown; source?: unknown }
  | { kind: "object"; entries: Record<string, Bound>; source?: unknown }
  | { kind: "array"; items: Bound[]; source?: unknown };

/** The per-field editor a bound node calls for. */
export type BindingMode = "value" | "data" | "formula" | "fields";

const isObject = (value: unknown): value is JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const onlyKey = (value: JsonObject, key: string) =>
  key in value && Object.keys(value).length === 1;

function resolve(schema: unknown, root: unknown): unknown {
  let current = schema;
  for (let hops = 0; hops < 16 && isObject(current); hops++) {
    const target = current["$ref"];
    if (typeof target !== "string" || !target.startsWith("#/")) break;
    let node: unknown = root;
    for (const raw of target.slice(2).split("/")) {
      const segment = raw.replace(/~1/g, "/").replace(/~0/g, "~");
      node = isObject(node) ? node[segment] : undefined;
    }
    if (node === undefined) break;
    current = node;
  }
  return current;
}

/** True when the schema declares types and none of them is `type`. */
function excludes(schema: unknown, type: "object" | "array"): boolean {
  if (!isObject(schema)) return schema === false;
  const declared = schema["type"];
  if (typeof declared === "string") return declared !== type;
  if (Array.isArray(declared)) return !declared.includes(type);
  return false;
}

function childSchema(schema: unknown, key: string, root: unknown): unknown {
  if (!isObject(schema)) return undefined;
  const properties = schema["properties"];
  if (isObject(properties) && key in properties)
    return resolve(properties[key], root);
  const patterns = schema["patternProperties"];
  if (isObject(patterns))
    for (const [pattern, value] of Object.entries(patterns)) {
      try {
        if (new RegExp(pattern, "u").test(key)) return resolve(value, root);
      } catch {
        // An unsupported pattern gives no guidance; the compiler reports it.
      }
    }
  const additional = schema["additionalProperties"];
  return isObject(additional) ? resolve(additional, root) : undefined;
}

function itemSchema(schema: unknown, index: number, root: unknown): unknown {
  if (!isObject(schema)) return undefined;
  const prefix = schema["prefixItems"];
  if (Array.isArray(prefix) && index < prefix.length)
    return resolve(prefix[index], root);
  return isObject(schema["items"]) ? resolve(schema["items"], root) : undefined;
}

function decodeLiteral(
  literal: unknown,
  schema: unknown,
  root: unknown,
  source?: unknown,
): Bound {
  const base = source === undefined ? {} : { source };
  if (isObject(literal) && !excludes(schema, "object"))
    return {
      kind: "object",
      entries: Object.fromEntries(
        Object.entries(literal).map(([key, child]) => [
          key,
          decodeLiteral(child, childSchema(schema, key, root), root),
        ]),
      ),
      ...base,
    };
  if (Array.isArray(literal) && !excludes(schema, "array"))
    return {
      kind: "array",
      items: literal.map((child, i) =>
        decodeLiteral(child, itemSchema(schema, i, root), root),
      ),
      ...base,
    };
  return { kind: "value", value: literal, ...base };
}

function decodeAt(expression: unknown, schema: unknown, root: unknown): Bound {
  const opaque: Bound = {
    kind: "formula",
    expression,
    source: expression,
  };
  if (!isObject(expression)) return opaque;
  if (onlyKey(expression, "literal"))
    return decodeLiteral(expression["literal"], schema, root, expression);
  if (onlyKey(expression, "ref") && typeof expression["ref"] === "string")
    return { kind: "ref", pointer: expression["ref"], source: expression };
  if (onlyKey(expression, "object") && isObject(expression["object"])) {
    if (excludes(schema, "object")) return opaque;
    return {
      kind: "object",
      entries: Object.fromEntries(
        Object.entries(expression["object"]).map(([key, child]) => [
          key,
          decodeAt(child, childSchema(schema, key, root), root),
        ]),
      ),
      source: expression,
    };
  }
  if (onlyKey(expression, "array") && Array.isArray(expression["array"])) {
    if (excludes(schema, "array")) return opaque;
    return {
      kind: "array",
      items: expression["array"].map((child, i) =>
        decodeAt(child, itemSchema(schema, i, root), root),
      ),
      source: expression,
    };
  }
  return opaque;
}

/** Expression → bound tree, guided by the target field schema when known. */
export function decode(expression: unknown, schema?: unknown): Bound {
  return decodeAt(expression, resolve(schema, schema), schema);
}

const literalExpression = (source: unknown): source is { literal: unknown } =>
  isObject(source) && onlyKey(source, "literal");

function literalAble(bound: Bound): boolean {
  if (bound.source !== undefined) return literalExpression(bound.source);
  if (bound.kind === "value") return true;
  if (bound.kind === "object")
    return Object.values(bound.entries).every(literalAble);
  if (bound.kind === "array") return bound.items.every(literalAble);
  return false;
}

function plain(bound: Bound): unknown {
  if (bound.source !== undefined && literalExpression(bound.source))
    return bound.source.literal;
  if (bound.kind === "object")
    return Object.fromEntries(
      Object.entries(bound.entries).map(([key, child]) => [key, plain(child)]),
    );
  if (bound.kind === "array") return bound.items.map(plain);
  return bound.kind === "value" ? bound.value : undefined;
}

/** Bound tree → Expression; untouched nodes return their original object. */
export function encode(bound: Bound): unknown {
  if (bound.source !== undefined) return bound.source;
  switch (bound.kind) {
    case "value":
      return { literal: bound.value };
    case "ref":
      return { ref: bound.pointer };
    case "formula":
      return bound.expression;
    case "object":
      return literalAble(bound)
        ? { literal: plain(bound) }
        : {
            object: Object.fromEntries(
              Object.entries(bound.entries).map(([key, child]) => [
                key,
                encode(child),
              ]),
            ),
          };
    case "array":
      return literalAble(bound)
        ? { literal: plain(bound) }
        : { array: bound.items.map(encode) };
  }
}

function constant(bound: Bound): boolean {
  if (bound.kind === "object")
    return Object.values(bound.entries).every(constant);
  if (bound.kind === "array") return bound.items.every(constant);
  return bound.kind === "value";
}

/** The literal JSON a node stands for, or `undefined` when any part is bound. */
export function literalValue(bound: Bound): unknown {
  if (!constant(bound)) return undefined;
  const json = (node: Bound): unknown =>
    node.kind === "object"
      ? Object.fromEntries(
          Object.entries(node.entries).map(([key, child]) => [
            key,
            json(child),
          ]),
        )
      : node.kind === "array"
        ? node.items.map(json)
        : (node as { value: unknown }).value;
  return json(bound);
}

export function bindingMode(bound: Bound): BindingMode {
  if (bound.kind === "ref") return "data";
  if (bound.kind === "formula") return "formula";
  return bound.kind === "value" ? "value" : "fields";
}

/** A literal value; objects and arrays become editable field groups. */
export function value(json: unknown): Bound {
  if (json === undefined)
    throw TypeError(
      "A value must be JSON; remove the field to leave it unset.",
    );
  return decodeLiteral(json, undefined, undefined);
}
export const reference = (pointer: string): Bound => ({ kind: "ref", pointer });
export const formula = (expression: unknown): Bound => ({
  kind: "formula",
  expression,
});

function equivalent(a: Bound, b: Bound): boolean {
  if (a === b) return true;
  if (a.kind !== b.kind) return false;
  switch (a.kind) {
    case "value":
      return sameJson(a.value, (b as typeof a).value);
    case "ref":
      return a.pointer === (b as typeof a).pointer;
    case "formula":
      return sameJson(a.expression, (b as typeof a).expression);
    case "object": {
      const other = (b as typeof a).entries;
      const keys = Object.keys(a.entries);
      return (
        keys.length === Object.keys(other).length &&
        keys.every((k) => k in other && equivalent(a.entries[k], other[k]))
      );
    }
    case "array": {
      const other = (b as typeof a).items;
      return (
        a.items.length === other.length &&
        a.items.every((item, i) => equivalent(item, other[i]))
      );
    }
  }
}
function sameJson(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a))
    return (
      Array.isArray(b) &&
      a.length === b.length &&
      a.every((item, i) => sameJson(item, b[i]))
    );
  if (isObject(a) && isObject(b)) {
    const keys = Object.keys(a);
    return (
      keys.length === Object.keys(b).length &&
      keys.every((k) => k in b && sameJson(a[k], b[k]))
    );
  }
  return false;
}

export function get(bound: Bound, path: Path): Bound | undefined {
  let current: Bound | undefined = bound;
  for (const key of path) {
    if (current?.kind === "object" && typeof key === "string")
      current = Object.hasOwn(current.entries, key)
        ? current.entries[key]
        : undefined;
    else if (current?.kind === "array" && typeof key === "number")
      current = current.items[key];
    else return undefined;
  }
  return current;
}

const group = (path: Path): Bound =>
  typeof path[0] === "number"
    ? { kind: "array", items: [] }
    : { kind: "object", entries: {} };

/**
 * Returns a tree with `next` at `path`, creating missing groups. Equivalent
 * content keeps the original node, so the result is the same tree.
 */
export function set(bound: Bound, path: Path, next: Bound): Bound {
  if (!path.length) return equivalent(bound, next) ? bound : next;
  const [key, ...rest] = path;
  if (bound.kind === "object" && typeof key === "string") {
    const current = Object.hasOwn(bound.entries, key)
      ? bound.entries[key]
      : undefined;
    const child = set(current ?? group(rest), rest, next);
    if (child === current) return bound;
    return { kind: "object", entries: { ...bound.entries, [key]: child } };
  }
  if (bound.kind === "array" && typeof key === "number") {
    if (!Number.isInteger(key) || key < 0 || key > bound.items.length)
      throw RangeError(`List position ${key} does not exist.`);
    const current = bound.items[key];
    const child = set(current ?? group(rest), rest, next);
    if (child === current) return bound;
    const items = bound.items.slice();
    items[key] = child;
    return { kind: "array", items };
  }
  throw TypeError(
    "This field holds a single value; replace it before editing its parts.",
  );
}

/** Returns a tree without the field or list item at `path`. */
export function remove(bound: Bound, path: Path): Bound {
  if (!path.length) return bound;
  const [key, ...rest] = path;
  if (bound.kind === "object" && typeof key === "string") {
    if (!Object.hasOwn(bound.entries, key)) return bound;
    if (rest.length) {
      const child = remove(bound.entries[key], rest);
      return child === bound.entries[key]
        ? bound
        : { kind: "object", entries: { ...bound.entries, [key]: child } };
    }
    const entries = { ...bound.entries };
    delete entries[key];
    return { kind: "object", entries };
  }
  if (bound.kind === "array" && typeof key === "number") {
    if (key < 0 || key >= bound.items.length) return bound;
    const items = bound.items.slice();
    if (rest.length) {
      const child = remove(items[key], rest);
      if (child === items[key]) return bound;
      items[key] = child;
    } else items.splice(key, 1);
    return { kind: "array", items };
  }
  return bound;
}
