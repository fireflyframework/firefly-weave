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
// The undo steps a transaction's steps have: what runs each, and which steps a
// transaction holds. Pure; loaded with Execute and step details.
import { sequencesOf, type Step, type Workflow } from "../../model";
import { allSteps } from "../ndv/step-walk";
import { enclosingTransaction } from "./group";
import type { Phase } from "./phase-keys";
import { OPERATION_LABELS, type UndoOperation } from "./undo";

/** The step's field each undo step is declared in, in the order a person meets them. */
const FIELDS: readonly (readonly [Phase, UndoOperation])[] = [
  ["undo", "compensate"],
  ["cancel", "cancel"],
  ["confirm", "confirm"],
];
const FIELD = Object.fromEntries(FIELDS) as Record<Phase, UndoOperation>;
/** Every undo step a step may have, in that order. */
export const PHASES: readonly Phase[] = FIELDS.map(([phase]) => phase);

/** What runs an undo step of a step, and what its field is called; null when the step doesn't have it. */
export function phaseOperation(
  step: Step,
  phase: Phase,
): { kind: "action" | "callWorkflow"; uses: string; label: string } | null {
  const operation = step[FIELD[phase]] as
    | { kind?: unknown; uses?: unknown }
    | null
    | undefined;
  if (!operation || typeof operation !== "object") return null;
  return {
    kind: operation.kind === "callWorkflow" ? "callWorkflow" : "action",
    uses: typeof operation.uses === "string" ? operation.uses : "",
    label: OPERATION_LABELS[FIELD[phase]],
  };
}
/** The undo steps a step has. */
export const phasesOf = (step: Step): Phase[] =>
  PHASES.filter((phase) => phaseOperation(step, phase));

/** The transaction whose own steps hold a step, at any depth; null outside one. */
export function transactionHolding(
  workflow: Workflow,
  stepId: string,
): Step | null {
  for (const [owner, steps] of sequencesOf(workflow))
    if (steps.some((step) => step.id === stepId))
      return enclosingTransaction(workflow, owner);
  return null;
}
/** A transaction's own steps, at any depth. */
export const stepsInside = (transaction: Step): Step[] =>
  allSteps(
    Array.isArray(transaction["steps"]) ? (transaction["steps"] as Step[]) : [],
  );
