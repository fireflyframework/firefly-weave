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
// A transaction's own settings: the platform's defaults and how often it
// tries an undo step. Pure.
import type { Step } from "../../model";

/** The platform's defaults (contracts/transactions.py), in the order it writes them. */
export const POLICY = {
  holdSeconds: 900,
  undoRetry: { maxAttempts: 10, initialDelaySeconds: 2, maxDelaySeconds: 300 },
  undoTimeoutSeconds: 86_400,
};

/** A transaction's Max tries: how often an undo step that keeps failing runs. */
export function triesOf(transaction: Step): number {
  const policy = transaction["policy"] as
    | { undoRetry?: { maxAttempts?: unknown } }
    | null
    | undefined;
  const tries = policy?.undoRetry?.maxAttempts;
  return typeof tries === "number" ? tries : POLICY.undoRetry.maxAttempts;
}

/** The most tries a simulated failure takes (the host's ScriptedFailure). */
const MOST_TRIES = 1000;
/** How often Execute fails an undo step that keeps failing: the Max tries, up to what the host takes. */
export function undoTries(transaction: Step): number {
  return Math.min(triesOf(transaction), MOST_TRIES);
}
