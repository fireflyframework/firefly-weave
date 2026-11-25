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
// How a step inside a transaction is undone: which steps choose, the choice a
// step shows, and what choosing writes. Who may choose, and the fields each
// way has, come from the target's language manifest; Studio never chooses for
// the builder. Pure, and only loaded with step details.
import type { Step } from "../../model";
import type { LanguageManifest, ManifestStepField } from "../language-manifest";
import type { Edit, Expression } from "../ndv/registry";
import type { UndoMode } from "./transaction-words";

/** How a step is undone; null while nothing is chosen. */
export type UndoChoice = UndoMode | null;
/** What a choice writes, relative to the step, as one undo step. */
export type StepPatch = Edit[];
/** One step of a step's undo: what confirms, releases or reverses it. */
export type UndoOperation = "confirm" | "cancel" | "compensate";

/** How long a transaction holds reservations when it doesn't say (contracts/transactions.py). */
const HOLD_SECONDS = 900;
export const NOTHING_TO_UNDO = "Doesn't change anything, nothing to undo";
/** The platform's finding on a step that changes data without a choice (TXN_UNDECLARED). */
export const UNDECLARED = "This step changes data. Choose how it's undone.";
/** The platform's finding on a step outside a transaction (TXN_OUTSIDE), and what to do. */
export const OUTSIDE =
  "How a step is undone applies only to steps inside a transaction. Move the step into a transaction, or clear How it's undone.";

/** The fields of each way of undoing a step, in the order they show. */
export const UNDO_OPERATIONS: Readonly<
  Record<UndoMode, readonly UndoOperation[]>
> = {
  reserve: ["confirm", "cancel"],
  compensate: ["compensate"],
  irreversible: [],
};
export const OPERATION_LABELS: Readonly<Record<UndoOperation, string>> = {
  confirm: "Confirm with",
  cancel: "Release with",
  compensate: "Reverse with",
};
const FIELDS = [
  "confirm",
  "cancel",
  "compensate",
  "irreversible",
  "holdSeconds",
] as const;

export type UndoRow =
  | {
      kind: "choices";
      value: UndoChoice;
      /** The transaction's hold, which a reservation keeps unless it sets its own. */
      holdDefault: number;
      problems: string[];
    }
  | { kind: "read-only"; reason: string }
  | { kind: "outside"; fields: string[] }
  | null;

export interface UndoContext {
  /** The transaction whose body holds the step, at any depth; null outside one. */
  inTransaction: Step | null;
  features: readonly string[];
  /** The target's language manifest, or nothing while it isn't known. */
  manifest: Pick<LanguageManifest, "step_fields"> | null;
  /** What an action does to data; "" while Studio can't tell. */
  sideEffect: string;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isCall = (step: Step) => (step.kind as string) === "callWorkflow";

/** The manifest's entry for how a step is undone, where the target runs transactions. */
function undoField(ctx: UndoContext): ManifestStepField | null {
  const field = ctx.manifest?.step_fields?.find(({ name }) => name === "undo");
  return field && (!field.feature || ctx.features.includes(field.feature))
    ? field
    : null;
}

/** The undo fields a step has, in the order the language lists them. */
const presentFields = (step: Step): string[] =>
  FIELDS.filter((name) => step[name] !== undefined);

/** The step's choice, as the platform reads it from the fields present. */
export function undoChoiceOf(step: Step): UndoChoice {
  if (step["confirm"] !== undefined || step["cancel"] !== undefined)
    return "reserve";
  if (step["compensate"] !== undefined) return "compensate";
  return step["irreversible"] !== undefined ? "irreversible" : null;
}

/** How long a transaction holds reservations. */
export function transactionHold(transaction: Step): number {
  const policy = transaction["policy"];
  const hold = isRecord(policy) ? policy["holdSeconds"] : undefined;
  return typeof hold === "number" ? hold : HOLD_SECONDS;
}

/**
 * How it's undone for a step: the choices, the reason there is nothing to
 * choose, the fields a step outside a transaction still has, or nothing.
 */
export function undoRow(step: Step, ctx: UndoContext): UndoRow {
  const field = undoField(ctx);
  if (!field?.kinds.includes(step.kind)) return null;
  const fields = presentFields(step);
  if (!ctx.inTransaction)
    return fields.length ? { kind: "outside", fields } : null;
  // A call that doesn't wait can't be undone; the compiler says so at its mode.
  if (isCall(step) && step["mode"] === "detach") return null;
  const value = undoChoiceOf(step);
  // A step that only reads takes no part unless the document says how it's undone.
  if (!isCall(step) && ctx.sideEffect === "read_only" && !fields.length)
    return { kind: "read-only", reason: NOTHING_TO_UNDO };
  const changesData = isCall(step) || !!ctx.sideEffect;
  return {
    kind: "choices",
    value,
    holdDefault: transactionHold(ctx.inTransaction),
    problems: value === null && changesData ? [UNDECLARED] : [],
  };
}

/** One undo step of a step as the language has it, its input still empty. */
export const undoOperation = (
  uses: string,
  kind: "action" | "callWorkflow",
): Expression => ({ kind, uses, with: { literal: {} } });

/** An operation whose action or workflow was chosen. */
const configured = (step: Step, name: UndoOperation): boolean => {
  const operation = step[name];
  return isRecord(operation) && !!operation["uses"];
};

/**
 * What choosing a way of undoing a step writes: its own operations, still to
 * choose where the step has none, and nothing of the other ways. `removed`
 * names the chosen operations that go.
 */
export function writeUndo(
  step: Step,
  choice: UndoMode,
): { patch: StepPatch; removed: string[] } {
  const own: readonly string[] = [
    ...UNDO_OPERATIONS[choice],
    ...(choice === "irreversible" ? ["irreversible"] : []),
    ...(choice === "reserve" ? ["holdSeconds"] : []),
  ];
  const dropped = presentFields(step).filter((name) => !own.includes(name));
  return {
    patch: [
      ...dropped.map((name) => ({ path: [name], value: undefined })),
      ...UNDO_OPERATIONS[choice]
        .filter((name) => step[name] === undefined)
        .map((name) => ({ path: [name], value: undoOperation("", "action") })),
      ...(choice === "irreversible"
        ? [{ path: ["irreversible"], value: true }]
        : []),
    ],
    removed: (["confirm", "cancel", "compensate"] as const)
      .filter((name) => dropped.includes(name) && configured(step, name))
      .map((name) => OPERATION_LABELS[name]),
  };
}

/** Removes every undo field of a step. */
export const clearUndo = (step: Step): StepPatch =>
  presentFields(step).map((name) => ({ path: [name], value: undefined }));

/** The toast after a choice dropped operations: "Removed Confirm with and Release with." */
export const removedUndoText = (removed: readonly string[]): string =>
  `Removed ${removed.join(" and ")}.`;

/**
 * What a change of Hold for makes of it: nothing while it is the
 * transaction's own. Null where the step keeps what it has.
 */
export function settledHold(step: Step, transaction: Step | null): Edit | null {
  return transaction && step["holdSeconds"] === transactionHold(transaction)
    ? { path: ["holdSeconds"], value: undefined }
    : null;
}
