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
// The URL of Call an API: the connection supplies the address; the step
// keeps the path with {name} placeholders, each filled from the step's input
// (with.path.<name>). A placeholder is a whole path segment, named with a
// letter or "_" and then letters, digits, "_" or "-", at most 128 characters,
// exactly as the action builder reads the path.
import { PATH_PLACEHOLDER, pathPlaceholders } from "../owned/owned-actions";
import { rowKey } from "./drop";
import { uniqueIdentifier } from "./identifiers";

export type UrlPart = { text: string } | { name: string };

const MAX_NAME = 128;

export function urlParts(path: string): UrlPart[] {
  const parts: UrlPart[] = [];
  let last = 0;
  for (const match of path.matchAll(PATH_PLACEHOLDER)) {
    if (match.index > last) parts.push({ text: path.slice(last, match.index) });
    parts.push({ name: match[1] });
    last = match.index + match[0].length;
  }
  if (last < path.length) parts.push({ text: path.slice(last) });
  return parts;
}

/**
 * A placeholder named after the dropped field (a step's whole output after
 * the step), unique among `taken` with "-2", "-3"…, within 128 characters.
 */
export function placeholderKey(
  ref: string,
  taken: ReadonlySet<string>,
): string {
  const raw = rowKey(ref, new Set()).replace(/[^A-Za-z0-9_-]/g, "_");
  const base = (/^[A-Za-z_]/.test(raw) ? raw : `p${raw}`).slice(0, MAX_NAME);
  return uniqueIdentifier(base, taken, MAX_NAME);
}

/**
 * The path with `{key}` at the caret (the end when there is none). The
 * placeholder becomes a segment of its own: a caret inside a segment moves to
 * the segment's end, and slashes are added around it as needed.
 */
export function insertPlaceholder(
  path: string,
  caret: number | null,
  key: string,
): string {
  let at =
    caret === null ? path.length : Math.max(0, Math.min(caret, path.length));
  if (at > 0 && path[at - 1] !== "/" && at < path.length && path[at] !== "/") {
    const end = path.indexOf("/", at);
    at = end < 0 ? path.length : end;
  }
  const before = path.slice(0, at);
  const after = path.slice(at);
  const head = before.endsWith("/") ? before : `${before}/`;
  const tail = after && !after.startsWith("/") ? `/${after}` : after;
  const next = `${head}{${key}}${tail}`;
  return next.startsWith("/") ? next : `/${next}`;
}

/** Path values (with.path keys) that no placeholder uses. */
export const stalePathKeys = (path: string, keys: string[]): string[] => {
  const used = new Set(pathPlaceholders(path));
  return keys.filter((key) => !used.has(key));
};

/**
 * What an edit of the path does to its values. `snapshot` holds the values
 * of the placeholders the path had when editing began: a value whose
 * placeholder the edit removed leaves the input, and comes back when the
 * placeholder returns. Values the path never used then are left alone.
 */
export function reconcilePathParts<T>(
  snapshot: ReadonlyMap<string, T>,
  path: string,
  present: ReadonlySet<string>,
): { remove: string[]; restore: [string, T][] } {
  const used = new Set(pathPlaceholders(path));
  const remove: string[] = [];
  const restore: [string, T][] = [];
  for (const [key, value] of snapshot) {
    if (!used.has(key) && present.has(key)) remove.push(key);
    else if (used.has(key) && !present.has(key)) restore.push([key, value]);
  }
  return { remove, restore };
}
