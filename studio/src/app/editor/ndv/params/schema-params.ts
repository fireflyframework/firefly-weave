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
// Fields for an input that has a schema (an action's input, a decision
// table's input, a connector action's input): one field per property, with
// required properties shown and optional ones behind Add option. Every value
// field takes Fixed or Mapped values at its data path inside the expression.
import { isFileSchema } from "../../../forms/core/file-reference";
import { memberField } from "../../../forms/core/form-model";
import { canonicalJson } from "../../../forms/core/json";
import { fieldsOf, type FieldInfo } from "../../../forms/core/resolve";
import type { FormSpec, ParamSpec, ParamType, Path } from "../registry";

export interface SchemaParamOptions {
  /**
   * Prefixes the top-level field IDs ("input." gives "input.limit"). A nested
   * field's ID extends its parent's ("input.parameters.customerId").
   */
  idPrefix: string;
  /** Deepest nested group drawn as fields before a group becomes JSON (default 3). */
  maxDepth?: number;
}

const typeOf = (
  info: FieldInfo,
  depth: number,
  maxDepth: number,
): ParamType => {
  if (isFileSchema(info.schema)) return "fileRef";
  switch (info.kind) {
    case "multiline":
      return "multiline";
    case "number":
    case "integer":
      return "number";
    case "boolean":
      return "boolean";
    case "choice":
      return "select";
    case "datetime":
      return "dateTime";
    case "object":
      return depth < maxDepth ? "fields" : "json";
    case "map":
      return "keyValue";
    case "list":
    case "table":
      return "list";
    case "text":
    case "email":
    case "uri":
    case "uuid":
    case "secret":
    case "const":
      return "text";
    default:
      return "json";
  }
};

/** An example as placeholder text: strings as they are, anything else as JSON. */
const exampleText = (value: unknown): string =>
  typeof value === "string" ? value : JSON.stringify(value);

/**
 * A secret anywhere in a value: the value itself, or the items or values of
 * a list, table or map (recursively). A form never writes one into workflow
 * source. `ancestors` holds the schemas of the containers above this one: a
 * schema that refers back to one of them (a map whose values are the map) has
 * already been scanned there, so the walk stops instead of looping.
 */
const holdsSecret = (
  info: FieldInfo,
  ancestors: ReadonlySet<string> = new Set(),
): boolean => {
  if (info.secret) return true;
  // memberField gives any non-container a fresh placeholder member, so only
  // containers are followed.
  if (info.kind !== "list" && info.kind !== "table" && info.kind !== "map")
    return false;
  const key = canonicalJson(info.schema);
  if (ancestors.has(key)) return false;
  const member = memberField(info);
  return member !== null && holdsSecret(member, new Set([...ancestors, key]));
};

/**
 * The properties a form shows. A secret is never written from a form, and a
 * `false` schema holds no value, so neither gets a field, in a group or not.
 */
export const isShownField = (info: FieldInfo): boolean =>
  !holdsSecret(info) && info.kind !== "never";

const shownInfos = (infos: FieldInfo[]): FieldInfo[] =>
  infos.filter(isShownField);

/**
 * The value a field starts with, as the classic form fills it: a constant is
 * its own value, and a required boolean starts at its default or `false`.
 * A starting value counts as set, so no "required" message shows for it.
 */
const defaultOf = (info: FieldInfo): ParamSpec["default"] => {
  if (info.kind === "const") return info.constValue;
  if (info.kind === "boolean" && info.required)
    return typeof info.default === "boolean" ? info.default : false;
  return info.default;
};

/** One field for one property of a schema, at `path`, with its ID after `idPrefix`. */
export function paramFromField(
  info: FieldInfo,
  path: Path,
  idPrefix: string,
  depth: number,
  maxDepth = 3,
): ParamSpec {
  const type = typeOf(info, depth, maxDepth);
  const id = `${idPrefix}${info.key}`;
  const starting = defaultOf(info);
  const spec: ParamSpec = {
    id,
    path,
    type,
    label: info.label,
    required: info.required,
    mapping: type === "fileRef" ? "mapped" : "both",
    ...(info.description ? { description: info.description } : {}),
    ...(starting !== undefined ? { default: starting } : {}),
    ...(starting === undefined && info.examples?.length
      ? { placeholder: exampleText(info.examples[0]) }
      : {}),
  };
  const schema = info.schema;
  if (typeof schema["minimum"] === "number") spec.min = schema["minimum"];
  if (typeof schema["maximum"] === "number") spec.max = schema["maximum"];
  if (typeof schema["maxLength"] === "number")
    spec.maxLength = schema["maxLength"];
  if (typeof schema["minItems"] === "number")
    spec.minItems = schema["minItems"];
  if (typeof schema["maxItems"] === "number")
    spec.maxItems = schema["maxItems"];
  if (type === "select")
    spec.choices = (info.options ?? []).map((option) => ({
      value: option.value,
      label: option.label,
    }));
  if (type === "fields") {
    const children = shownInfos(fieldsOf(info.schema, info.context));
    spec.children = () =>
      children.map((child) =>
        paramFromField(
          child,
          [...path, child.key],
          `${id}.`,
          depth + 1,
          maxDepth,
        ),
      );
  }
  if (type === "list") {
    // The item's own ID is `${id}.item`, the key memberField gives it; its
    // paths are relative to the item, so they start empty.
    const member = memberField(info);
    spec.item = member
      ? paramFromField(member, [], `${id}.`, depth + 1, maxDepth)
      : {
          id: `${id}.item`,
          path: [],
          type: "json",
          label: "Item",
          mapping: "both",
        };
    spec.addLabel = `Add ${info.label.toLowerCase()}`;
  }
  return spec;
}

/**
 * Required properties as fields, optional ones as options, in schema order;
 * secrets and never-valued properties are left out (see shownInfos).
 */
export function paramsFromSchema(
  schema: unknown,
  base: Path,
  options: SchemaParamOptions,
): FormSpec {
  const maxDepth = options.maxDepth ?? 3;
  const params = shownInfos(fieldsOf(schema)).map((info) =>
    paramFromField(info, [...base, info.key], options.idPrefix, 0, maxDepth),
  );
  return {
    fields: params.filter((param) => param.required),
    options: params.filter((param) => !param.required),
  };
}
