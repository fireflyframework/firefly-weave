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
// Which connection revision the activation dialog preselects for a workflow
// connection slot. Pure, so the eager dialog stays small and testable.

/**
 * The connection revision to preselect for a slot among the compatible ones:
 * the latest revision of the connection named like the slot (as Studio names
 * the connection it creates for a slot), otherwise the only one.
 */
export function preferredConnection(
  slot: string,
  compatible: Record<string, unknown>[],
): string {
  const named = compatible
    .filter((connection) => connection["name"] === slot)
    .sort((a, b) => Number(b["revision"] ?? 0) - Number(a["revision"] ?? 0));
  if (named.length) return String(named[0]["id"]);
  return compatible.length === 1 ? String(compatible[0]["id"]) : "";
}
