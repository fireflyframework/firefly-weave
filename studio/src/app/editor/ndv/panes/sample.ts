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
import {
  resolveSchema,
  type ResolveOptions,
} from "../../../forms/core/resolve";
import { formatProblem } from "../../../forms/core/form-model";
import type { Json } from "../registry";

const record = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const secret = (schema: Record<string, unknown>) =>
  schema["x-secret"] === true || schema["writeOnly"] === true;
const failure = () =>
  new Error(
    "Studio couldn't generate a sample for these constraints. Edit the input fields or simplify the schema and try again.",
  );
const keywords = new Set([
  "type",
  "properties",
  "required",
  "items",
  "additionalProperties",
  "minItems",
  "maxItems",
  "minLength",
  "maxLength",
  "minimum",
  "maximum",
  "exclusiveMinimum",
  "exclusiveMaximum",
  "enum",
  "const",
  "default",
  "format",
  "$defs",
  "definitions",
  "$schema",
  "$id",
  "title",
  "description",
  "examples",
  "readOnly",
  "writeOnly",
  "x-secret",
  "deprecated",
  "$comment",
]);

/** Copy data while dropping secret values, including nested lists and resolved references. */
export function safeSample(
  schema: unknown,
  value: Json | undefined,
  options: ResolveOptions = { root: schema },
): Json | undefined {
  let left = 1000;
  const visit = (
    raw: unknown,
    data: Json,
    options: ResolveOptions,
    depth: number,
  ): Json | undefined => {
    if (--left < 0 || depth > 12) return undefined;
    const resolved = resolveSchema(raw, options);
    let node: Record<string, unknown> = resolved.schema;
    let context = resolved.context;
    if (resolved.composed) {
      // Only direct unions can be checked branch by branch without trusting
      // normalization, which may discard secret annotations in intersections.
      if (
        !record(raw) ||
        Object.hasOwn(raw, "$ref") ||
        Object.hasOwn(raw, "allOf") ||
        !["oneOf", "anyOf"].some((key) => Array.isArray(raw[key]))
      )
        return undefined;
      node = raw;
      context = { root: options.root ?? raw, bundle: options.bundle };
    }
    if (secret(node) || resolved.unresolved.length || resolved.cyclic)
      return undefined;
    for (const key of ["oneOf", "anyOf"]) {
      if (!Array.isArray(node[key])) continue;
      for (const branch of node[key]) {
        const clean = visit(branch, data, context, depth + 1);
        if (clean === undefined) return undefined;
        data = clean;
      }
    }
    // Dynamic/conditional property schemas cannot safely expose a sample here.
    if (
      [
        "patternProperties",
        "if",
        "then",
        "else",
        "dependentSchemas",
        "not",
      ].some((key) => key in node)
    )
      return undefined;
    if (Array.isArray(data)) {
      if (
        Array.isArray(node["items"]) ||
        ["contains", "unevaluatedItems", "additionalItems"].some((key) =>
          Object.hasOwn(node, key),
        )
      )
        return undefined;
      const prefix = Array.isArray(node["prefixItems"])
        ? node["prefixItems"]
        : [];
      const clean: Json[] = [];
      for (const [index, item] of data.entries()) {
        const safe = visit(
          index < prefix.length ? prefix[index] : node["items"],
          item,
          context,
          depth + 1,
        );
        // Removing an item would change positional meaning; withhold the array instead.
        if (safe === undefined) return undefined;
        clean.push(safe);
      }
      return clean;
    }
    if (record(data)) {
      const properties = record(node["properties"]) ? node["properties"] : {};
      return Object.fromEntries(
        Object.entries(data).flatMap(([key, item]) => {
          const safe = visit(
            Object.hasOwn(properties, key)
              ? properties[key]
              : node["additionalProperties"],
            item as Json,
            context,
            depth + 1,
          );
          return safe === undefined ? [] : [[key, safe]];
        }),
      );
    }
    return data;
  };
  return value === undefined ? undefined : visit(schema, value, options, 0);
}

/** A bounded generator for simple resolved schemas; unsupported constraints are refused. */
export function sampleFromSchema(schema: unknown): Json {
  let left = 200;
  const build = (
    raw: unknown,
    options: ResolveOptions,
    depth: number,
  ): Json => {
    if (--left < 0 || depth > 6) throw failure();
    // Normalization is designed for forms, not a proof that arbitrary intersections fit.
    if (record(raw) && ["allOf", "oneOf", "anyOf"].some((key) => key in raw))
      throw failure();
    const resolved = resolveSchema(raw, options);
    const node = resolved.schema;
    if (
      resolved.composed ||
      resolved.never ||
      resolved.cyclic ||
      resolved.unresolved.length ||
      resolved.unsupported.length ||
      secret(node)
    )
      throw failure();
    if (Object.keys(node).some((key) => !keywords.has(key))) throw failure();
    const types = Array.isArray(node["type"]) ? node["type"] : [node["type"]];
    const type =
      types.find((item) => item !== "null") ??
      (resolved.nullable ? "null" : undefined);
    let candidate: Json;
    if ("const" in node) candidate = structuredClone(node["const"]) as Json;
    else if (Array.isArray(node["enum"])) {
      if (!node["enum"].length) throw failure();
      candidate = structuredClone(node["enum"][0]) as Json;
    } else if ("default" in node)
      candidate = structuredClone(node["default"]) as Json;
    else if (type === "string") {
      const formats: Record<string, string> = {
        email: "name@example.com",
        "date-time": "2026-01-15T09:30:00Z",
        uri: "https://example.com",
        uuid: "00000000-0000-4000-8000-000000000000",
      };
      const format = node["format"];
      if (format && (typeof format !== "string" || !formats[format]))
        throw failure();
      candidate = format ? formats[String(format)] : "text";
      const min = Number(node["minLength"] ?? 0),
        max = Number(node["maxLength"] ?? 1024);
      if (min > 1024 || max < min || min < 0) throw failure();
      if (!format) candidate = candidate.slice(0, max).padEnd(min, "x");
    } else if (type === "integer" || type === "number") {
      const min =
        typeof node["minimum"] === "number" ? node["minimum"] : -Infinity;
      const max =
        typeof node["maximum"] === "number" ? node["maximum"] : Infinity;
      candidate = Math.max(min, Math.min(1, max));
      if (
        typeof node["exclusiveMinimum"] === "number" &&
        candidate <= node["exclusiveMinimum"]
      )
        candidate = node["exclusiveMinimum"] + 1;
      if (
        typeof node["exclusiveMaximum"] === "number" &&
        candidate >= node["exclusiveMaximum"]
      )
        candidate = node["exclusiveMaximum"] - 1;
      if (type === "integer") candidate = Math.ceil(candidate);
    } else if (type === "boolean") candidate = true;
    else if (type === "null") candidate = null;
    else if (type === "array") {
      const min = Number(node["minItems"] ?? 0),
        max = Number(node["maxItems"] ?? 20);
      if (min > 20 || min > max || min < 0) throw failure();
      const length = Math.max(min, Math.min(1, max));
      candidate = Array.from({ length }, () =>
        build(node["items"], resolved.context, depth + 1),
      );
    } else if (type === "object" || record(node["properties"])) {
      const props = record(node["properties"]) ? node["properties"] : {};
      if (Object.keys(props).length > 100) throw failure();
      candidate = Object.fromEntries(
        Object.entries(props).flatMap(([key, child]) => {
          if (secret(resolveSchema(child, resolved.context).schema)) return [];
          return [[key, build(child, resolved.context, depth + 1)]];
        }),
      );
    } else candidate = null;
    // Check before sanitation can omit an unsupported subtree from a default.
    validate(
      node,
      candidate,
      resolved.context,
      depth,
      resolved.removedNullType,
    );
    const clean = safeSample(raw, candidate, options);
    // Use this document's context when validating a local-reference candidate.
    validate(node, clean, resolved.context, depth, resolved.removedNullType);
    return clean as Json;
  };
  const validate = (
    node: Record<string, unknown>,
    value: Json | undefined,
    options: ResolveOptions,
    depth: number,
    removedNullType: boolean,
  ): void => {
    if (value === undefined || depth > 6) throw failure();
    const types = Array.isArray(node["type"])
      ? node["type"]
      : node["type"]
        ? [node["type"]]
        : [];
    const type =
      value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
    if (
      types.length &&
      !types.includes(type) &&
      !(type === "null" && removedNullType) &&
      !(
        type === "number" &&
        types.includes("integer") &&
        Number.isInteger(value)
      )
    )
      throw failure();
    if (
      "const" in node &&
      JSON.stringify(value) !== JSON.stringify(node["const"])
    )
      throw failure();
    if (
      Array.isArray(node["enum"]) &&
      !node["enum"].some(
        (item) => JSON.stringify(item) === JSON.stringify(value),
      )
    )
      throw failure();
    if (typeof value === "number") {
      if (
        !Number.isFinite(value) ||
        (typeof node["minimum"] === "number" && value < node["minimum"]) ||
        (typeof node["maximum"] === "number" && value > node["maximum"]) ||
        (typeof node["exclusiveMinimum"] === "number" &&
          value <= node["exclusiveMinimum"]) ||
        (typeof node["exclusiveMaximum"] === "number" &&
          value >= node["exclusiveMaximum"])
      )
        throw failure();
    }
    if (typeof value === "string") {
      if (
        value.length < Number(node["minLength"] ?? 0) ||
        value.length > Number(node["maxLength"] ?? Infinity)
      )
        throw failure();
      const format = node["format"];
      if (
        format &&
        !["email", "date-time", "uri", "uuid"].includes(String(format))
      )
        throw failure();
      if (
        format === "date-time" &&
        (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(
          value,
        ) ||
          !Number.isFinite(Date.parse(value)))
      )
        throw failure();
      if (
        format &&
        formatProblem(
          format === "date-time" ? "datetime" : (format as "email"),
          value,
          "Sample",
        )
      )
        throw failure();
    }
    if (Array.isArray(value)) {
      if (
        value.length < Number(node["minItems"] ?? 0) ||
        value.length > Number(node["maxItems"] ?? Infinity) ||
        value.length > 20
      )
        throw failure();
      for (const item of value)
        validateResolved(node["items"], item, options, depth + 1);
    } else if (record(value)) {
      if (
        Array.isArray(node["required"]) &&
        node["required"].some((key) => !Object.hasOwn(value, String(key)))
      )
        throw failure();
      const props = record(node["properties"]) ? node["properties"] : {};
      for (const [key, item] of Object.entries(value)) {
        if (
          !Object.hasOwn(props, key) &&
          node["additionalProperties"] === false
        )
          throw failure();
        validateResolved(
          Object.hasOwn(props, key) ? props[key] : node["additionalProperties"],
          item as Json,
          options,
          depth + 1,
        );
      }
    }
  };
  const validateResolved = (
    raw: unknown,
    value: Json,
    options: ResolveOptions,
    depth: number,
  ) => {
    if (
      --left < 0 ||
      (record(raw) && ["allOf", "oneOf", "anyOf"].some((key) => key in raw))
    )
      throw failure();
    const resolved = resolveSchema(raw, options);
    if (
      resolved.composed ||
      resolved.never ||
      resolved.cyclic ||
      resolved.unresolved.length ||
      resolved.unsupported.length ||
      Object.keys(resolved.schema).some((key) => !keywords.has(key))
    )
      throw failure();
    validate(
      resolved.schema,
      value,
      resolved.context,
      depth,
      resolved.removedNullType,
    );
  };
  return build(schema, { root: schema }, 0);
}
