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
// A transaction as a builder makes one: what a new one holds, how many steps
// it has, and the transaction a place is in. Pure, and only loaded with the
// editor.
import type { Step, Workflow } from "../../model";
import { blocksOf } from "../step-blocks";

/** The platform's rule, in its words (TXN_NESTED). */
export const NESTED_TRANSACTION =
  "A transaction can't contain another transaction.";

/** A new transaction: no steps yet, and no result until a step gives one. */
export const newTransactionStep = (id: string): Step => ({
  id,
  // The canvas model's Kind lists the kinds it creates by itself; a
  // transaction comes from its own descriptor.
  kind: "transaction" as Step["kind"],
  steps: [],
  output: { literal: null },
});

/** "3 steps": the steps a transaction holds itself; nothing while it is empty. */
export function stepCount(step: Step): string {
  const own = step["steps"];
  const count = Array.isArray(own) ? own.length : 0;
  return count ? `${count} ${count === 1 ? "step" : "steps"}` : "";
}

/**
 * The transaction whose own steps hold a place (a sequence's owner), at any
 * depth; null outside one. A transaction's error path runs after it is over,
 * so it is not inside it.
 */
export function enclosingTransaction(
  workflow: Workflow,
  owner: string,
): Step | null {
  const find = (
    steps: readonly Step[],
    at: string,
    inside: Step | null,
  ): Step | null | undefined => {
    if (at === owner) return inside;
    for (const step of steps)
      for (const block of blocksOf(step)) {
        const own =
          (step.kind as string) === "transaction" && block.kind === "body";
        const found = find(
          block.branch.steps,
          `${step.id}/${block.name}`,
          own ? step : inside,
        );
        if (found !== undefined) return found;
      }
    return undefined;
  };
  return find(workflow.spec.steps, "root", null) ?? null;
}
