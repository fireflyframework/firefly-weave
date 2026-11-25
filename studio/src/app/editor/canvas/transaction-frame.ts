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
// A transaction on the canvas is a frame around its own steps. This module
// holds what the frame says; the layout places it as it places a loop's
// frame. Pure, and only loaded with the editor's canvas.
import type { Step } from "../../model";
import { undoChoiceOf } from "../transactions/points";
import type { UndoMode } from "../transactions/transaction-words";
import type { StepFacts } from "./tile-facts";

/** A transaction's promise, after its name and "Transaction" in its header. */
export const TRANSACTION_PROMISE =
  "All of these succeed, or what's done is undone";
export const TRANSACTION_HEADER = `Transaction · ${TRANSACTION_PROMISE}`;

/** What a transaction without steps needs: Diagnostics' words, as its setup badge says them. */
export const EMPTY_TRANSACTION = "Add a step to the transaction.";

export const isTransaction = (step: Step): boolean =>
  (step.kind as string) === "transaction";

/**
 * A transaction's facts for its header's badge. One without steps shows what
 * it needs as setup, not as a failure: Diagnostics counts it as an error.
 */
export function transactionFacts(step: Step, facts: StepFacts): StepFacts {
  const steps = step["steps"];
  if (Array.isArray(steps) && steps.length) return facts;
  return {
    ...facts,
    errors: facts.errors.filter((text) => text !== EMPTY_TRANSACTION),
    setup: [...facts.setup, EMPTY_TRANSACTION],
  };
}

export interface UndoBadge {
  icon: "undo" | "lock" | "critical";
  label: "Reversible" | "Reserved" | "Can't be undone";
}
const UNDO: Readonly<Record<UndoMode, UndoBadge & { text: string }>> = {
  reserve: {
    icon: "lock",
    label: "Reserved",
    text: "is reserved, then confirmed",
  },
  compensate: {
    icon: "undo",
    label: "Reversible",
    text: "can be undone by reversing it",
  },
  irreversible: {
    icon: "critical",
    label: "Can't be undone",
    text: "can't be undone",
  },
};
/** How a step of a transaction is undone, as its tile shows it; null for one that says nothing. */
export function undoBadge(step: Step): UndoBadge | null {
  const choice = undoChoiceOf(step);
  if (!choice) return null;
  const { icon, label } = UNDO[choice];
  return { icon, label };
}
/** What the end of a tile's name says of it: "can be undone by reversing it"; "" for nothing. */
export function undoText(step: Step): string {
  const choice = undoChoiceOf(step);
  return choice ? UNDO[choice].text : "";
}

/** The steps inside a transaction's own steps, at any depth; not its error path's. */
export function stepsInTransactions(
  tiles: readonly { id: string; owner: string }[],
  frames: readonly { id: string; transaction?: true }[],
): ReadonlySet<string> {
  const bodies = new Set(
    frames.flatMap(({ id, transaction }) =>
      transaction ? [`${id}/steps`] : [],
    ),
  );
  const found = new Set<string>();
  if (!bodies.size) return found;
  // A block comes before the steps inside it.
  for (const { id, owner } of tiles)
    if (bodies.has(owner) || found.has(owner.slice(0, owner.indexOf("/"))))
      found.add(id);
  return found;
}

/** A point of no return's accessible name, and what it means. */
export function pointOfNoReturnText(stepId: string): {
  name: string;
  description: string;
} {
  return {
    name: `Point of no return, after ${stepId}`,
    description: `Once ${stepId} succeeds, the transaction finishes. Steps after this line retry until they succeed and are never undone.`,
  };
}
