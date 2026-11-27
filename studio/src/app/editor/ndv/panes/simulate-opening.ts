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
// What Simulate a failure opens with for a step, and the failure it writes:
// kept as they were when the dialog opened, so an answer that comes after the
// picked item changed is dropped.
import { failureOf } from "../../state/test-data-ops";
import type { ErrorEntry } from "../../state/test-data";
import { phaseKey, type Phase } from "../../transactions/phase-keys";
import type {
  Effect,
  TransactionFailure,
} from "../../transactions/simulate-undo";
import { actionSideEffect } from "../kinds/forms/action-forms";
import type { StepDetailsController } from "../step-details-controller";
import { loopRows } from "./iteration-picker";
import {
  codeSuggestions,
  failureKey,
  simulatedOutcome,
} from "./simulate-failure";

/** What the dialog answers: the step's failure, or one of its undo steps'. */
export interface FailureChoice {
  phase: Phase | null;
  code: string;
  outcome: "failed" | "unknown" | "not_started";
  /** Whether it may have changed anything; only for a step its transaction undoes, or can't. */
  effect?: Effect;
  every: boolean;
}
/** A failure simulated already, as the dialog starts on it. */
interface Current extends FailureChoice {
  /** Where it is written: an edit that changes Which items replaces it. */
  key: string;
}
export interface SimulateOpening {
  step: string;
  /** The item picked inside a loop's body, else the step's own key. */
  key: string;
  /** That item, as people count it ("3"); null outside loops. */
  item: string | null;
  /** The innermost loop around the step, for the hint. */
  loop: string;
  /** A step that changes data and is never retried: outside a transaction, Unknown stops the run. */
  changesData: boolean;
  /** The outcome its failure takes without asking; null when the dialog asks. */
  outcome: "not_started" | null;
  suggestions: ReturnType<typeof codeSuggestions>;
  /** Inside a transaction: what may fail and what the failure leads to. */
  inside: TransactionFailure | null;
  /** What the dialog starts on: the step, or the undo step whose failure is edited. */
  phase: Phase | null;
  /** The failures simulated now, of the step and of each undo step. */
  current: readonly Current[];
}

/**
 * What Simulate a failure opens with for a step, on `phase` of it; null when
 * the step is gone. Execute step's "Simulate a failure of submit first" opens
 * it for another step than the one shown.
 */
export function simulateOpening(
  controller: StepDetailsController,
  stepId: string,
  phase: Phase | null = null,
): SimulateOpening | null {
  const step = controller.step(stepId);
  if (!step) return null;
  const uses = step["uses"];
  const contract =
    typeof uses === "string"
      ? (controller.kindContext().actionContract(uses) ?? null)
      : null;
  const key = controller.pinKey(stepId);
  const inside = controller.transactionFailure(stepId);
  const data = controller.testData.data();
  const current = [null, ...(inside?.phases.map(({ phase }) => phase) ?? [])]
    .map((at): Current | null => {
      const own = at ? phaseKey(key, at) : key;
      const found = failureOf(data, own);
      return (
        found && {
          phase: at,
          key: found.key,
          code: found.entry.code,
          outcome: found.entry.outcome ?? "failed",
          ...(found.entry.effect ? { effect: found.entry.effect } : {}),
          every: key !== stepId && found.key !== own,
        }
      );
    })
    .filter((found) => found !== null);
  return {
    step: stepId,
    key,
    item:
      key === stepId
        ? null
        : [...key.matchAll(/\[(\d+)\]/g)]
            .map((match) => (Number(match[1]) + 1).toLocaleString("en-US"))
            .join(" › "),
    loop:
      loopRows(controller.iterations, stepId, (id) => controller.step(id)).find(
        (row) => row.role === "Loop",
      )?.loopId ?? "",
    changesData:
      actionSideEffect(step, controller.kindContext()) === "non_idempotent",
    outcome: simulatedOutcome(step),
    suggestions: codeSuggestions(step, contract),
    inside,
    phase,
    current,
  };
}

export const PICK_CHANGED =
  "The picked item changed while the dialog was open, so nothing was simulated.";
/**
 * The failure an answer writes, and where; `replacing` is the one the dialog
 * edited, which goes when Which items changed. Null when the item picked now
 * (`key`) isn't the one the dialog opened on: the answer isn't for it.
 */
export function failureWritten(
  open: SimulateOpening,
  choice: FailureChoice,
  key: string,
): {
  key: string;
  entry: Omit<ErrorEntry, "source">;
  replacing: string | null;
} | null {
  if (key !== open.key) return null;
  const { phase, code, outcome, effect } = choice;
  return {
    key: failureKey(open.key, choice.every, phase),
    // "failed" is the platform's default: it isn't written. An undo step
    // fails every one of the transaction's tries, which Execute reads when
    // it runs: they aren't written either.
    entry: phase
      ? { code }
      : {
          code,
          ...(outcome !== "failed" ? { outcome } : {}),
          ...(effect ? { effect } : {}),
        },
    replacing:
      open.current.find((failure) => failure.phase === phase)?.key ?? null,
  };
}
