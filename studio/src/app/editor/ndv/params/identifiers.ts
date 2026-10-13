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
// Step names, branch names, answers, error codes and signal names are IDs:
// typing is free, and leaving the field turns the text into an ID.

/**
 * Lower case; spaces and characters other than letters, digits, ".", "_"
 * and "-" become "-"; repeated hyphens collapse; leading symbols drop; at
 * most `max` characters, without a trailing hyphen. "" when nothing is left.
 */
export function normalizeIdentifier(text: string, max = 128): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^[^a-z0-9]+/, "")
    .slice(0, max)
    .replace(/-+$/, "");
}

/** `base`, or `base-2`, `base-3`… when taken, shortened to fit `max`. */
export function uniqueIdentifier(
  base: string,
  taken: ReadonlySet<string>,
  max = 128,
): string {
  if (!taken.has(base)) return base;
  for (let n = 2; ; n++) {
    const suffix = `-${n}`;
    const name = `${base.slice(0, max - suffix.length)}${suffix}`;
    if (!taken.has(name)) return name;
  }
}
