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
// On error in Settings: what a step's final failure does. Stop the run (the
// language's default, written by leaving `onError` out), Continue (the
// output is null and later steps can read the error), or Continue with
// error output (the failure runs the step's error path, then the run goes
// on). Which steps may choose comes from the target's language manifest;
// a decision, a parallel step and a loop always stop, and a platform that
// can't handle failures keeps the read-only "Stop the run".
import type { Step } from "../../../model";
import { ERROR_PATH } from "../../step-blocks";
import { actionSideEffect } from "../kinds/forms/action-forms";
import { STOP_ONLY_KINDS } from "../kinds/forms/shared";
import type { Edit, Json, KindContext, ParamSpec } from "../registry";
import { allSteps } from "../step-walk";
import {
  STOP_ONLY_REASON,
  STOP_RUN_REASON,
  onErrorText,
} from "./settings-rules";

export type OnErrorValue = "stop" | "continue" | "errorOutput";
/** The language feature a target lists when its runs handle step failures. */
export const ON_ERROR_FEATURE = "flow.onError";

export interface OnErrorChoice {
  value: OnErrorValue;
  label: string;
  description: string;
}
export const ON_ERROR_CHOICES: readonly OnErrorChoice[] = [
  {
    value: "stop",
    label: "Stop the run",
    description:
      "The run stops with an incident you can retry or resolve in Operate.",
  },
  {
    value: "continue",
    label: "Continue",
    description:
      "The run goes on. This step's output is empty (null), and later steps can read its error.",
  },
  {
    value: "errorOutput",
    label: "Continue with error output",
    description:
      "Failures take this step's Error output on the canvas; its steps run, then the run goes on after this step.",
  },
];

/** What each choice does once a transaction is undone. */
const UNDONE: Readonly<Record<OnErrorValue, string>> = {
  stop: "If the transaction is undone, the run fails. Nothing stays applied.",
  continue:
    "If the transaction is undone, the run goes on; later steps read its error.",
  errorOutput: "If the transaction is undone, the run takes the error path.",
};
const TRANSACTION_CHOICES: readonly OnErrorChoice[] = ON_ERROR_CHOICES.map(
  (choice) => ({ ...choice, description: UNDONE[choice.value] }),
);
const isTransaction = (step: Step) => (step.kind as string) === "transaction";
/** The choices as a step's kind says what they do: always the same list. */
export const onErrorChoices = (step: Step): readonly OnErrorChoice[] =>
  isTransaction(step) ? TRANSACTION_CHOICES : ON_ERROR_CHOICES;

/** Retry on fail already says the step changes data and is never retried. */
export const UNKNOWN_OUTCOME_HINT =
  "If its outcome is unknown, the run still stops with an incident until someone checks it.";
export const RESULT_STATUS_HINT =
  "A called run that fails continues with its result status. On error handles a call that can't start.";
export const SIGNAL_HINT =
  "Applies when no signal arrives within Wait at most.";
export const HUMAN_HINT = "Applies when the task expires (Expires after).";
export const IN_DOUBT_HINT =
  "A transaction in doubt always waits for a person; On error never handles it.";
export const RESULT_ON_ERROR_HINT =
  "What later steps read as this step's output after the error path.";

export type OnErrorRow =
  | { kind: "choices"; value: OnErrorValue; hints: string[] }
  | { kind: "read-only"; reason: string }
  | null;

export interface OnErrorContext extends KindContext {
  /**
   * The step kinds the target's language manifest lets choose On error (its
   * `onError` step field); null when the manifest lists none, as on a
   * platform that predates step error handling.
   */
  onErrorKinds: readonly string[] | null;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** The kinds a language manifest's `onError` step field lists; null when it has none. */
export function onErrorKinds(manifest: unknown): string[] | null {
  const fields = isRecord(manifest) ? manifest["step_fields"] : undefined;
  if (!Array.isArray(fields)) return null;
  const entry = fields.find(
    (field) => isRecord(field) && field["name"] === "onError",
  );
  const kinds = isRecord(entry) ? entry["kinds"] : undefined;
  return Array.isArray(kinds)
    ? kinds.filter((kind): kind is string => typeof kind === "string")
    : null;
}

/** The step's setting; a step without one stops the run. */
export function onErrorValue(step: Step): OnErrorValue {
  const value = step["onError"];
  return value === "continue" || value === "errorOutput" ? value : "stop";
}

/** A call step (its kind comes from its own descriptor, not the palette's kinds). */
const isCall = (step: Step) => (step.kind as string) === "callWorkflow";

/** What On error says under the choice for this step: when it applies, and what it can't change. */
function hints(step: Step, ctx: KindContext): string[] {
  if (
    step.kind === "action" &&
    actionSideEffect(step, ctx) === "non_idempotent"
  )
    return [UNKNOWN_OUTCOME_HINT];
  if (isCall(step) && step["onFailure"] === "continue")
    return [RESULT_STATUS_HINT];
  if (step.kind === "signal") return [SIGNAL_HINT];
  if (step.kind === "humanTask") return [HUMAN_HINT];
  if (isTransaction(step)) return [IN_DOUBT_HINT];
  return [];
}

/**
 * The On error row of a step: the three choices, the read-only "Stop the
 * run" with its reason, or nothing (a Wait, a Stop with error, a call that
 * doesn't wait for its result).
 */
export function onErrorRow(step: Step, ctx: OnErrorContext): OnErrorRow {
  if (STOP_ONLY_KINDS.has(step.kind))
    return { kind: "read-only", reason: STOP_ONLY_REASON };
  if (isCall(step) && step["mode"] === "detach") return null;
  if (
    ctx.features.includes(ON_ERROR_FEATURE) &&
    !!ctx.onErrorKinds?.includes(step.kind)
  )
    return {
      kind: "choices",
      value: onErrorValue(step),
      hints: hints(step, ctx),
    };
  // A platform that can't continue after errors: Stop the run, and why.
  const reason =
    onErrorText(step.kind) ??
    (ctx.onErrorKinds?.includes(step.kind) ? STOP_RUN_REASON : null);
  return reason ? { kind: "read-only", reason } : null;
}

export interface OnErrorWrite {
  /** The edits, relative to the step, made as one undo step. */
  patch: Edit[];
  /** How many steps the error path held that go with it, nested ones counted. */
  removed: number;
  /** Their IDs, in the order they run. */
  names: string[];
}

/** Every step inside a step's error path, in the order they run. */
function errorPathSteps(step: Step): Step[] {
  const path = step[ERROR_PATH];
  const steps = isRecord(path) ? path["steps"] : undefined;
  return Array.isArray(steps) ? allSteps(steps.filter(isRecord) as Step[]) : [];
}

/**
 * What choosing `value` writes. Continue with error output adds an empty
 * error path whose result is null; Continue removes the path; Stop the run
 * removes the setting and the path (the language's default is written by
 * leaving `onError` out, never as "stop").
 */
export function writeOnError(step: Step, value: OnErrorValue): OnErrorWrite {
  if (value === "errorOutput")
    return {
      patch: [
        { path: ["onError"], value: "errorOutput" },
        ...(isRecord(step[ERROR_PATH])
          ? []
          : [
              {
                path: [ERROR_PATH],
                value: { steps: [], output: { literal: null } } as Json,
              },
            ]),
      ],
      removed: 0,
      names: [],
    };
  const held = errorPathSteps(step);
  return {
    patch: [
      { path: ["onError"], value: value === "continue" ? value : undefined },
      { path: [ERROR_PATH], value: undefined },
    ],
    removed: held.length,
    names: held.map((inner) => inner.id),
  };
}

/**
 * What a change that ends On error removes with it: a call that no longer
 * waits for its result can't handle its failures, so its setting and its
 * error path go. Null when the step keeps them.
 */
export function endedOnError(step: Step): OnErrorWrite | null {
  if (!isCall(step) || step["mode"] !== "detach") return null;
  if (step["onError"] === undefined && step[ERROR_PATH] === undefined)
    return null;
  return writeOnError(step, "stop");
}

/** "a", "a and b", "a, b and c". */
const listed = (names: readonly string[]) =>
  names.length < 2
    ? (names[0] ?? "")
    : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;

/**
 * The toast after an error path went with its steps: "Removed the error
 * path (2 steps)" and, under it, which steps were in it (up to three names).
 * Null when the path held none.
 */
export function removedPathToast(
  removed: number,
  names: readonly string[],
): { text: string; detail: string } | null {
  if (!removed) return null;
  const count = removed === 1 ? "1 step" : `${removed} steps`;
  const shown =
    names.length > 3
      ? `${names.slice(0, 2).join(", ")} and ${names.length - 2} more`
      : listed(names);
  return {
    text: `Removed the error path (${count})`,
    detail: `${shown} ${names.length === 1 ? "was" : "were"} in it.`,
  };
}

/**
 * The Settings fields On error adds after a kind's own settings: On error
 * itself, and Result on error while the step continues with error output.
 */
export function onErrorFields(step: Step, ctx: OnErrorContext): ParamSpec[] {
  const row = onErrorRow(step, ctx);
  if (!row) return [];
  const onError: ParamSpec = {
    id: "onError",
    path: ["onError"],
    type: "select",
    label: "On error",
    mapping: "fixed",
    choices: onErrorChoices(step).map(({ value, label, description }) => ({
      value,
      label,
      description,
    })),
    // Its writes keep the error path in step: nothing clears it behind them.
    whenHidden: "keep",
    ...(row.kind === "read-only" ? { readOnly: () => row.reason } : {}),
  };
  if (row.kind !== "choices") return [onError];
  return [
    onError,
    {
      id: "resultOnError",
      path: [ERROR_PATH, "output"],
      type: "json",
      label: "Result on error",
      mapping: "both",
      // The language needs the path's output: cleared, it is null again.
      default: null,
      whenRemoved: "default",
      emptyMapped: true,
      placeholder: "Empty (null)",
      hint: RESULT_ON_ERROR_HINT,
      showWhen: (current) =>
        current["onError"] === "errorOutput" && isRecord(current[ERROR_PATH]),
      whenHidden: "keep",
    },
  ];
}
