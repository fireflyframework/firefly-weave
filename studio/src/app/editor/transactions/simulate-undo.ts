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
// Simulate a failure inside a transaction: what may fail (the step, its undo,
// its release, its confirm), what its failure may have changed, and the words
// the dialog and the Output banner say. Pure; loaded with step details.
import type { Step, Workflow } from "../../model";
import {
  codeProblem,
  codeSuggestions,
  effectDefault,
} from "../ndv/panes/simulate-failure";
import { POLICY } from "../ndv/kinds/forms/transaction-forms";
import type { Json } from "../ndv/registry";
import { instanceStep } from "../state/instance-keys";
import type { ErrorEntry } from "../state/test-data";
import { splitPhase, type Phase } from "./phase-keys";
import {
  phaseOperation,
  phasesOf,
  stepsInside,
  transactionHolding,
} from "./phases";
import { afterPointOfNoReturn, undoChoiceOf } from "./points";
import { phaseRowText, type UndoMode } from "./transaction-words";

export type Effect = "none" | "unknown";
type Outcome = "not_started" | "failed" | "unknown";

/** "undo", "release", "confirm": an undo step as a sentence names it. */
export const PHASE_NOUNS: Readonly<Record<Phase, string>> = {
  undo: "undo",
  cancel: "release",
  confirm: "confirm",
};
/** A step the transaction undoes, or can't: its failure says what it may have changed. */
export interface Participant {
  mode: UndoMode;
  kind: "action" | "callWorkflow";
  /** What it runs ("target-system.add-quantity"). */
  name: string;
  /** The action's document, or null when Studio can't read it. */
  contract: Json | null;
}
/** What Simulate a failure needs of a step inside a transaction. */
export interface TransactionFailure {
  /** The transaction's ID. */
  transaction: string;
  /** Its Max tries: how often a failing undo step runs. */
  tries: number;
  /** Null for a step the transaction has nothing to undo for. */
  participant: Participant | null;
  /** The step's undo steps, which may fail instead of the step. */
  phases: {
    phase: Phase;
    /** What runs it. */
    kind: "action" | "callWorkflow";
    label: string;
    /** The code it starts on: one a person waits out, as an undo step is run again until it works. */
    code: string;
    suggestions: ReturnType<typeof codeSuggestions>;
  }[];
  /** What the step's own failure leads to: its On error, the transaction's undo, or it depends. */
  leads: "setting" | "undo" | "depends";
  /** A failure of a later step of the transaction is simulated already. */
  laterFails: boolean;
}

const nameOf = (uses: unknown) =>
  typeof uses === "string" && uses.lastIndexOf("@") > 0
    ? uses.slice(0, uses.lastIndexOf("@"))
    : String(uses ?? "");

function triesOf(transaction: Step): number {
  const policy = transaction["policy"] as
    | { undoRetry?: { maxAttempts?: unknown } }
    | null
    | undefined;
  const tries = policy?.undoRetry?.maxAttempts;
  return typeof tries === "number" ? tries : POLICY.undoRetry.maxAttempts;
}

/** What Simulate a failure offers for a step inside a transaction; null for one outside. */
export function transactionFailure(
  workflow: Workflow,
  stepId: string,
  reads: {
    /** The document of what a step or an undo step runs; null when unknown. */
    contract(uses: string, kind: "action" | "callWorkflow"): Json | null;
    /** The failures simulated now, by key. */
    errors: Readonly<Record<string, ErrorEntry>>;
  },
): TransactionFailure | null {
  const transaction = transactionHolding(workflow, stepId);
  if (!transaction) return null;
  const inside = stepsInside(transaction);
  const at = inside.findIndex(({ id }) => id === stepId);
  if (at < 0) return null;
  const step = inside[at];
  const later = new Set(inside.slice(at + 1).map(({ id }) => id));
  const mode = undoChoiceOf(step);
  const kind =
    (step.kind as string) === "callWorkflow" ? "callWorkflow" : "action";
  const handled =
    step["onError"] === "continue" || step["onError"] === "errorOutput";
  return {
    transaction: transaction.id,
    tries: triesOf(transaction),
    participant: mode && {
      mode,
      kind,
      name: nameOf(step["uses"]),
      contract:
        kind === "action" ? reads.contract(String(step["uses"]), kind) : null,
    },
    phases: phasesOf(step).map((phase) => {
      const { kind, uses } = phaseOperation(step, phase)!;
      return {
        phase,
        kind,
        label: `Its ${PHASE_NOUNS[phase]}`,
        code: kind === "action" ? "UNAVAILABLE" : "WV-RUN-TIMED_OUT",
        suggestions: codeSuggestions(
          { kind: kind as Step["kind"] },
          kind === "action" ? reads.contract(uses, kind) : null,
        ),
      };
    }),
    leads: handled
      ? "setting"
      : mode === "irreversible" || afterPointOfNoReturn(transaction, stepId)
        ? "depends"
        : "undo",
    laterFails: Object.keys(reads.errors).some(
      (key) => !splitPhase(key) && later.has(instanceStep(key)),
    ),
  };
}

/** The dialog's first sentence: what fails, and what that does. */
export function failureLead(
  step: string,
  inside: TransactionFailure | null,
  phase: Phase | null,
): string {
  if (!inside || (!phase && inside.leads === "setting"))
    return "Execute step and Execute workflow fail this step with this code; its On error setting decides what happens next. Real runs ignore it.";
  if (phase)
    return `When transaction ${inside.transaction} ${phase === "confirm" ? "succeeds" : "is undone"}, Execute workflow fails ${step}'s ${PHASE_NOUNS[phase]} every time Weave tries it. Real runs ignore it.`;
  return inside.leads === "undo"
    ? `Execute workflow fails ${step} with this code, and transaction ${inside.transaction} undoes what's done. Real runs ignore it.`
    : `Execute workflow fails ${step} with this code. Real runs ignore it.`;
}

/** What a failing undo step leads to, and what must fail first for it to run. */
export function phaseNotes(
  step: string,
  inside: TransactionFailure,
  phase: Phase,
): string[] {
  const { tries, laterFails } = inside;
  const its = `Its ${PHASE_NOUNS[phase]} fails`;
  return [
    tries === 1
      ? `${its}, and Max tries is 1, so the transaction ends in doubt.`
      : `${its} all ${tries} tries, the transaction's Max tries, so the transaction ends in doubt.`,
    // A confirm runs when the transaction succeeds.
    ...(phase === "confirm" || laterFails
      ? []
      : [
          `Something must fail after ${step} for the transaction to be undone: simulate a failure of a later step too.`,
        ]),
  ];
}

/** Did it change anything?: what each answer makes of the step. */
export function effectChoices(
  step: string,
  mode: UndoMode,
  outcome: Outcome,
): { may: string; nothing: string } {
  const unknown = "Not with an unknown outcome: nobody knows what changed.";
  const [may, nothing] =
    mode === "irreversible"
      ? [
          `${step} can't be undone, so the transaction is in doubt.`,
          "Weave undoes what's done before it.",
        ]
      : mode === "reserve"
        ? [
            `Weave releases ${step} with the rest.`,
            `Weave skips releasing ${step}.`,
          ]
        : [
            `Weave undoes ${step} with the rest.`,
            `Weave skips undoing ${step}.`,
          ];
  return { may, nothing: outcome === "unknown" ? unknown : nothing };
}

/** The answer that shows: the builder's once picked, else the platform's rule; nobody knows with an unknown outcome. */
export const shownEffect = (
  { contract }: Participant,
  code: string,
  outcome: Outcome,
  picked: Effect | null,
): Effect =>
  outcome === "unknown"
    ? "unknown"
    : (picked ?? effectDefault(code, outcome, contract));

/** Why that answer was chosen, until the builder picks one; null when there is nothing to say. */
export function effectReason(
  { kind, name, contract }: Participant,
  code: string,
  outcome: Outcome,
  picked: Effect | null,
): string | null {
  if (picked || outcome === "unknown" || codeProblem(code)) return null;
  if (kind === "callWorkflow")
    return "Chosen because a called workflow can't say a failure changed nothing.";
  if (!contract)
    return `Chosen because Studio can't read which codes of ${name} change nothing.`;
  return effectDefault(code, outcome, contract) === "none"
    ? `Chosen from the code: ${name} says ${code} changes nothing.`
    : `Chosen from the code: ${name} doesn't say ${code} changes nothing.`;
}

/** The Output banner over a step whose undo step fails: whose it is, and that real runs ignore it. */
export function bannerSentence(
  phase: Phase,
  scope: "step" | "item" | "every",
): string {
  const whose =
    scope === "item"
      ? "This item's"
      : scope === "every"
        ? "Every item's"
        : "This step's";
  return `${whose} ${PHASE_NOUNS[phase]} fails when you execute it in Studio. Real runs ignore it.`;
}
/** What the banner says after the code: "Undo of add · fails all 10 tries". */
export function bannerLine(
  step: string,
  phase: Phase,
  { attempts = 1 }: Pick<ErrorEntry, "attempts">,
): string {
  const times = attempts > 1 ? `fails all ${attempts} tries` : "fails once";
  return `${phaseRowText(phase, step)} · ${times}`;
}
/** The Output pane's pill: "Undo fails when executed". */
export const failsPill = (phase: Phase): string => {
  const noun = PHASE_NOUNS[phase];
  return `${noun.charAt(0).toUpperCase()}${noun.slice(1)} fails when executed`;
};
