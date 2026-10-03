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
// Schema helpers shared by the task form and the shell: which schemas render
// as field groups or lists, writing nested values, and required fields still
// missing. Kept apart from the form component so they load without it.
export interface Schema {
  type?: string;
  title?: string;
  description?: string;
  enum?: unknown[];
  properties?: Record<string, Schema>;
  required?: string[];
  items?: Schema;
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
}
export type Path = string[];
export type Data = Record<string, unknown>;
export const scalarTypes = ["string", "number", "integer", "boolean"];
export const isRecord = (v: unknown): v is Data =>
  !!v && typeof v === "object" && !Array.isArray(v);

/** True when a schema renders as grouped fields instead of raw JSON. */
export function groupedObject(schema: Schema) {
  return (
    schema.type === "object" &&
    !schema.enum &&
    Object.keys(schema.properties ?? {}).length > 0
  );
}
/** True when a schema renders as an editable list of scalar values. */
export function scalarList(schema: Schema) {
  return (
    schema.type === "array" &&
    !schema.enum &&
    !!schema.items &&
    (!!schema.items.enum || scalarTypes.includes(String(schema.items.type)))
  );
}
/**
 * Returns a copy of data with value written at path. Removing a value also
 * removes the objects it leaves empty, so an optional group the person cleared
 * is absent again instead of an empty object whose required fields still apply.
 */
export function withValue(data: Data, path: Path, value: unknown): Data {
  const next = structuredClone(data);
  const parents: Data[] = [];
  let target: Data = next;
  for (const part of path.slice(0, -1)) {
    if (!isRecord(target[part])) target[part] = {};
    parents.push(target);
    target = target[part] as Data;
  }
  const last = path.at(-1)!;
  if (value !== undefined) {
    target[last] = value;
    return next;
  }
  delete target[last];
  for (let depth = parents.length - 1; depth >= 0; depth--) {
    const child = path[depth];
    if (Object.keys(parents[depth][child] as Data).length) break;
    delete parents[depth][child];
  }
  return next;
}
/** Required leaf paths that hold no value, as human-readable labels. */
export function missingRequired(schema: Schema, data: unknown, prefix = "") {
  const missing: string[] = [];
  const values = isRecord(data) ? data : {};
  for (const [name, field] of Object.entries(schema.properties ?? {})) {
    const label = prefix + (field.title || name);
    const value = values[name];
    if (
      schema.required?.includes(name) &&
      (value === undefined || value === "")
    )
      missing.push(label);
    else if (groupedObject(field) && value !== undefined)
      missing.push(...missingRequired(field, value, label + " › "));
  }
  return missing;
}
