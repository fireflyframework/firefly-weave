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
