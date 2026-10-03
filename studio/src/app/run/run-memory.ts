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
// The last run input per workflow version, kept in this browser only. Values
// the input schema marks secret or write-only are never kept.
import type { Schema } from "../task-schema";

type Json = Record<string, unknown>;
const isRecord = (value: unknown): value is Json =>
  !!value && typeof value === "object" && !Array.isArray(value);
const storagePrefix = "weave-studio-run-input:";

const secretMark = (schema: Json) =>
  schema["x-secret"] === true || schema["writeOnly"] === true;
/** True when a schema marks a value secret or write-only anywhere inside it. */
function mentionsSecret(schema: unknown, depth = 0): boolean {
  // Deeper than any form Studio renders: assume the worst.
  if (depth > 64) return true;
  if (Array.isArray(schema))
    return schema.some((item) => mentionsSecret(item, depth + 1));
  if (!isRecord(schema)) return false;
  return (
    secretMark(schema) ||
    Object.values(schema).some((value) => mentionsSecret(value, depth + 1))
  );
}
/**
 * The part of a value that may stay in this browser. Values under a secret or
 * write-only schema, at any depth, are left out; where a secret could be
 * reached other than through named properties ($ref, allOf, items,
 * additionalProperties…), the whole value is left out instead of guessed at.
 */
function keepable(schema: unknown, value: unknown): unknown {
  if (!mentionsSecret(schema)) return value;
  if (!isRecord(schema) || secretMark(schema) || !isRecord(value))
    return undefined;
  const { properties, ...rest } = schema;
  if (mentionsSecret(rest) || !isRecord(properties)) return undefined;
  const kept: Json = {};
  for (const [key, item] of Object.entries(value)) {
    const safe = key in properties ? keepable(properties[key], item) : item;
    if (safe !== undefined) kept[key] = safe;
  }
  return kept;
}
/** Run input without values the schema marks as secret or write-only. */
export function rememberable(schema: Schema, data: Json): Json {
  const kept = keepable(schema, data);
  return isRecord(kept) ? kept : {};
}
export function remembered(key: string): Json | null {
  try {
    const value: unknown = JSON.parse(
      localStorage.getItem(storagePrefix + key) ?? "null",
    );
    return isRecord(value) ? value : null;
  } catch {
    return null;
  }
}
export function remember(key: string, data: Json) {
  try {
    localStorage.setItem(storagePrefix + key, JSON.stringify(data));
  } catch {
    // Storage can be unavailable (private windows); the next run starts empty.
  }
}
