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
// Where an expression sits, so the forms that edit it can suggest exactly
// the data the compiler lets it read (forms/core/scope.ts). The designer
// shell passes a context; the lazily loaded forms compute the suggestions,
// which keeps the scope walker out of the initial bundle.
import { visibleRefs, type ScopeEntry } from "./scope";

export interface ReferenceContext {
  /** The applied workflow definition. */
  definition: unknown;
  /** The step whose fields are edited, or `$workflow` for workflow settings. */
  stepId: string;
  /** Output schema of a published action by its `uses`, when known. */
  actionOutput?: (uses: string) => unknown;
}

const cache = new WeakMap<ReferenceContext, Map<string, ScopeEntry[]>>();

/**
 * Suggestions at `fieldPath` (relative to the step, such as `/with` or
 * `/cases/0/when`; `/spec/output` for the workflow). Computed once per
 * context and field.
 */
export function referencesAt(
  context: ReferenceContext | null | undefined,
  fieldPath: string,
): ScopeEntry[] {
  if (!context) return [];
  let fields = cache.get(context);
  if (!fields) cache.set(context, (fields = new Map()));
  let entries = fields.get(fieldPath);
  if (!entries) {
    entries = visibleRefs(context.definition, context.stepId, fieldPath, {
      actionOutput: context.actionOutput,
    });
    fields.set(fieldPath, entries);
  }
  return entries;
}
