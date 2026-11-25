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
// Simulate a failure, in the Output pane's More output actions: which steps
// offer it and why it is disabled, the key its failure is written under in
// the test data, and the codes the platform accepts (ErrorEntry in
// contracts/studio_documents.py).
import type { Step } from "../../../model";
import { instanceStep } from "../../state/instance-keys";
import type { StepErrorView } from "../../state/execution";
import { phaseKey, type Phase } from "../../transactions/phase-keys";
import { specOf } from "../kinds/shared";
import type { Json } from "../registry";
import {
  PLATFORM_FAILURES,
  genericFailureMessage,
  platformFailures,
} from "../../state/failure-codes";

/** Kinds that can fail on their own, whatever their On error says. */
const ALWAYS = new Set(["action", "llm", "agent"]);
/** Kinds that fail only on a value they read: a failure that stops the run shows nothing new, unless a transaction undoes it. */
const HANDLED_ONLY = new Set(["transform", "decisionTable"]);
const ANSWERED: Record<string, string> = {
  signal:
    "In its Test answer, check Let it time out instead; On error decides what happens next.",
  humanTask:
    "In its Test answer, check Let it expire instead; On error decides what happens next.",
};

/**
 * Whether Simulate a failure applies to `step`: undefined leaves the menu
 * item out (the step can't fail on its own), null offers it, and a text
 * disables it with that reason. `inCall` is a step inside a called
 * workflow's frame; `inTransaction` one inside a transaction, which handles
 * its failure whatever its On error.
 */
export function simulateBlock(
  step: Step,
  inCall: boolean,
  inTransaction = false,
): string | null | undefined {
  // A call's kind isn't one the designer lists yet: compare it as text.
  const kind: string = step.kind;
  const waits = kind === "callWorkflow" && step["mode"] !== "detach";
  const offered =
    ALWAYS.has(kind) ||
    waits ||
    HANDLED_ONLY.has(kind) ||
    Object.hasOwn(ANSWERED, kind);
  if (!offered) return undefined;
  if (inCall)
    return "Studio can't simulate a failure inside a called workflow yet. Simulate it on the call step.";
  if (Object.hasOwn(ANSWERED, kind)) return ANSWERED[kind];
  const handled =
    step["onError"] === "continue" || step["onError"] === "errorOutput";
  if (HANDLED_ONLY.has(kind) && !handled && !inTransaction)
    return "This step's failure would stop the run. Choose Continue or Continue with error output first.";
  return null;
}

/**
 * The outcome a simulated failure of `step` is saved with, when Simulate a
 * failure doesn't ask: a transform or a decision table changes nothing
 * outside the run, so the platform reports its failure as not started.
 * Null when the dialog asks.
 */
export function simulatedOutcome(step: Step): "not_started" | null {
  return HANDLED_ONLY.has(step.kind) ? "not_started" : null;
}

/** The test data key of a failure, of the step or one of its undo steps: the item's own, or its step's for every item. */
export function failureKey(
  instance: string,
  every: boolean,
  phase: Phase | null = null,
): string {
  const key = every ? instanceStep(instance) : instance;
  return phase ? phaseKey(key, phase) : key;
}

/** The codes an action's document says change nothing. */
const definiteFailures = (contract: Json | null): string[] => {
  const listed = specOf(contract)["definiteFailures"];
  return Array.isArray(listed)
    ? listed.filter((code): code is string => typeof code === "string")
    : [];
};

/**
 * Whether a simulated failure may have changed anything, when nobody says:
 * the platform's rule (scripted_effect in operations/debug/transactions.py),
 * except that nobody knows with an unknown outcome, whatever the code. A
 * called workflow declares no codes.
 */
export const effectDefault = (
  code: string,
  outcome: StepErrorView["outcome"],
  contract: Json | null,
): "none" | "unknown" =>
  outcome === "not_started" ||
  (outcome === "failed" && definiteFailures(contract).includes(code))
    ? "none"
    : "unknown";

const CODE = /^[A-Z][A-Z0-9_.-]*$/;
/** What to change in a typed code, or null when the platform accepts it. */
export function codeProblem(code: string): string | null {
  if (!CODE.test(code))
    return "Use capital letters, digits, dots, dashes and underscores, starting with a letter.";
  return code.length > 128 ? "Use at most 128 characters." : null;
}

export interface CodeSuggestion {
  code: string;
  /** The platform's words for the code. */
  message: string;
  /** The option's label: its group, then its words ("Platform failure · …"). */
  label: string;
}
/** The platform's message for a code; any other reads as a generic failure that names it (step_error_message). */
const messageOf = (code: string) =>
  PLATFORM_FAILURES.find((item) => item.code === code)?.message ??
  genericFailureMessage(code);

/**
 * The codes the Code field suggests: the action's own failures (its
 * contract's definiteFailures) under "This action's failures", then the
 * platform's codes its kind may return under "Platform failures". Empty
 * groups are left out; the first code is the field's starting value.
 */
export function codeSuggestions(
  step: Pick<Step, "kind">,
  contract: Json | null,
): { label: string; items: CodeSuggestion[] }[] {
  const own = definiteFailures(contract);
  const platform = platformFailures(step.kind).filter(
    (item) => !own.includes(item.code),
  );
  // The browser's list of suggestions shows no groups: each label names its own.
  const item = (code: string, message: string, group: string) => ({
    code,
    message,
    label: `${group} · ${message}`,
  });
  return [
    {
      label: "This action's failures",
      items: own.map((code) =>
        item(code, messageOf(code), "This action's failure"),
      ),
    },
    {
      label: "Platform failures",
      items: platform.map(({ code, message }) =>
        item(code, message, "Platform failure"),
      ),
    },
  ].filter((group) => group.items.length);
}
