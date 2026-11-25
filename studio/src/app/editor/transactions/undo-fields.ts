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
// The Settings fields of How it's undone: the choice, then the fields of the
// way chosen. How it's undone has no field of its own in the document; its
// choice is read from the fields each way has.
import type { Step } from "../../model";
import { durationParam } from "../ndv/kinds/forms/shared";
import type { KindContext, ParamSpec } from "../ndv/registry";
import {
  OPERATION_LABELS,
  UNDO_OPERATIONS,
  undoRow,
  type UndoContext,
  type UndoOperation,
  type UndoRow,
} from "./undo";
import type { UndoMode } from "./transaction-words";

export interface UndoOption {
  value: UndoMode;
  label: string;
  description: string;
}
/** The three ways, in the words of the specification. */
export const UNDO_OPTIONS: readonly UndoOption[] = [
  {
    value: "reserve",
    label: "Reserve, then confirm",
    description:
      "This step holds something; Weave confirms it when the transaction succeeds, or releases it if anything fails.",
  },
  {
    value: "compensate",
    label: "An action that reverses it",
    description:
      "If anything later fails, Weave runs this to reverse the step.",
  },
  {
    value: "irreversible",
    label: "Can't be undone",
    description:
      "Once this step runs, the transaction can't go back. It must be the last step that changes data.",
  },
];
/** A pair needs both (TXN_RESERVE_PAIR, in the words of its finding). */
const NEEDS_PAIR =
  "A step that reserves needs both Confirm with and Release with.";
const NEEDS_REVERSE = "Choose what reverses this step.";

export interface UndoFieldsContext extends KindContext, UndoContext {}

/** The field that holds a way's operation: an action or a workflow version. */
const operationParam = (name: UndoOperation): ParamSpec => ({
  id: name,
  path: [name, "uses"],
  type: "resource",
  label: OPERATION_LABELS[name],
  required: true,
  requiredText: name === "compensate" ? NEEDS_REVERSE : NEEDS_PAIR,
  placeholder: "name@1.0.0",
  whenHidden: "keep",
});

/** Hold for: the transaction's own unless the step sets another. */
const holdParam = (step: Step, holdDefault: number): ParamSpec =>
  durationParam("undoHold", ["holdSeconds"], "Hold for", {
    min: 30,
    max: 86_400,
    default: holdDefault,
    whenHidden: "keep",
    ...(step["holdSeconds"] === undefined
      ? { hint: "Same as the transaction's Hold reservations for." }
      : {}),
  });

const choiceParam = (row: Exclude<UndoRow, null>): ParamSpec => ({
  id: "undo",
  // Only the third way is a field of its own; the others are their operations.
  path: ["irreversible"],
  type: "select",
  label: "How it's undone",
  mapping: "fixed",
  choices: UNDO_OPTIONS.map(({ value, label, description }) => ({
    value,
    label,
    description,
  })),
  placeholder: "Choose how it's undone",
  whenHidden: "keep",
  ...(row.kind === "choices" && row.problems.length
    ? { required: true, requiredText: row.problems[0] }
    : {}),
  ...(row.kind === "read-only" ? { readOnly: () => row.reason } : {}),
});

/** How it's undone and the fields of the way chosen; none for a step that has no say. */
export function undoFields(step: Step, ctx: UndoFieldsContext): ParamSpec[] {
  const row = undoRow(step, ctx);
  if (!row) return [];
  const choice = choiceParam(row);
  if (row.kind !== "choices" || !row.value) return [choice];
  return [
    choice,
    ...UNDO_OPERATIONS[row.value].map(operationParam),
    ...(row.value === "reserve" ? [holdParam(step, row.holdDefault)] : []),
  ];
}
