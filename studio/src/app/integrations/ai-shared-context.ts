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
import type { ScopeEntry } from "../forms/core/scope";

const record = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** Only compiler-visible complete AI results; no future or sibling branch reads. */
export function availableAiResults(references: ScopeEntry[]): ScopeEntry[] {
  return references.filter(
    (ref) =>
      ref.stepKind === "llm" &&
      !!ref.stepId &&
      !ref.optional &&
      ref.path.length === 1 &&
      ref.path[0] === "result",
  );
}

function envelope(value: unknown): Record<string, unknown> | null {
  if (
    !record(value) ||
    Object.keys(value).length !== 1 ||
    !record(value["object"])
  )
    return null;
  const fields = value["object"];
  const results = fields["sharedAiResults"];
  if (
    Object.keys(fields).length !== 2 ||
    !("data" in fields) ||
    !record(results) ||
    Object.keys(results).length !== 1 ||
    !record(results["object"])
  )
    return null;
  for (const [id, ref] of Object.entries(results["object"])) {
    if (
      !record(ref) ||
      Object.keys(ref).length !== 1 ||
      ref["ref"] !==
        `/steps/${id.replace(/~/g, "~0").replace(/\//g, "~1")}/output/result`
    )
      return null;
  }
  return fields;
}
export function sharedAiResults(value: unknown): string[] {
  const fields = envelope(value);
  return fields
    ? Object.keys(
        (fields["sharedAiResults"] as { object: Record<string, unknown> })
          .object,
      )
    : [];
}

/** Build ordinary expressions so persistence, scope and replay keep one authority. */
export function withSharedAiResults(
  value: unknown,
  selected: string[],
  references: ScopeEntry[],
): unknown {
  if (selected.length > 16) throw Error("Choose up to 16 prior AI results.");
  if (new Set(selected).size !== selected.length)
    throw Error("Choose each AI result only once.");
  const allowed = new Map(
    availableAiResults(references).map((ref) => [ref.stepId!, ref.ref]),
  );
  if (selected.some((id) => !allowed.has(id)))
    throw Error(
      "Choose only AI results available before this step. Remove unavailable results first.",
    );
  const data = envelope(value)?.["data"] ?? value;
  if (!selected.length) return data;
  return {
    object: {
      data,
      sharedAiResults: {
        object: Object.fromEntries(
          selected.map((id) => [id, { ref: allowed.get(id)! }]),
        ),
      },
    },
  };
}
