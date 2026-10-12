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
// What the Settings tab says: why On error can't change yet, whether retry
// is on and its waits make sense, where an owned action keeps its retry and
// timeout, where the step sits and what runs it, and whether anything
// differs from the defaults.
import type { Step, Workflow } from "../../../model";
import type { StepNote } from "../../state/canvas-sidecar";
import { implementationText, sideEffectText } from "../header";
import { actionSideEffect } from "../kinds/forms/action-forms";
import { ON_ERROR_KINDS, STOP_ONLY_KINDS } from "../kinds/forms/shared";
import { breadcrumb } from "../navigation";
import {
  DEFAULT_RETRY,
  HTTP_CONNECTOR,
  recipeOf,
  type RetrySettings,
} from "../owned/owned-actions";
import type { KindContext } from "../registry";

export const STOP_RUN_REASON = "Update the platform to continue after errors.";
export const STOP_ONLY_REASON =
  "The run fails with this step's error. You can retry it from Operate.";

/** The reason On error stays "Stop the run", or null where it doesn't apply. */
export function onErrorText(kind: string): string | null {
  if (ON_ERROR_KINDS.has(kind)) return STOP_RUN_REASON;
  if (STOP_ONLY_KINDS.has(kind)) return STOP_ONLY_REASON;
  return null;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** Retry is on when the action may run more than once. */
export const retryOn = (value: unknown): boolean =>
  isRecord(value) && Number(value["maxAttempts"] ?? 1) > 1;

/** Retry is on for a step whose action may run again: never one that changes data. */
export const retryShown = (
  step: Step,
  ctx: KindContext,
  value: unknown,
): boolean =>
  actionSideEffect(step, ctx) !== "non_idempotent" && retryOn(value);

/** The retry the switch writes: on asks for three tries, off for one; the waits stay. */
export function retrySwitched(value: unknown, on: boolean): RetrySettings {
  const current = isRecord(value) ? value : {};
  return {
    ...DEFAULT_RETRY,
    ...(current as Partial<RetrySettings>),
    maxAttempts: on ? DEFAULT_RETRY.maxAttempts : 1,
  };
}

/** Why the retry waits can't work, or "" (also while retry is off). */
export function retryProblem(value: unknown): string {
  if (!retryOn(value)) return "";
  const retry = value as Record<string, unknown>;
  const first = Number(
    retry["initialDelaySeconds"] ?? DEFAULT_RETRY.initialDelaySeconds,
  );
  const longest = Number(
    retry["maxDelaySeconds"] ?? DEFAULT_RETRY.maxDelaySeconds,
  );
  return longest < first
    ? "The longest wait must be at least the wait between tries."
    : "";
}

/** Where a workflow-owned action keeps its retry and timeout, and who shares it. */
export function savedInText(
  uses: string,
  kind: "http" | "connector",
  users: number,
): string {
  const action = kind === "http" ? "the API action" : "the action";
  return users > 1
    ? `Saved in ${action} ${uses}, used by ${users} steps.`
    : `Saved in ${action} ${uses}, which only this step uses.`;
}

/** The Settings dot: a setting off its default, or a note. */
export const settingsChanged = (
  atDefault: boolean[],
  note: StepNote | undefined,
): boolean => atDefault.some((value) => !value) || !!note?.text.trim();

/** Where the step sits, in the words of the breadcrumb; the trigger starts the workflow. */
export function placementText(workflow: Workflow, target: string): string {
  if (target === "$trigger") return "Starts the workflow";
  if (target.startsWith("$")) return "";
  return breadcrumb(workflow, target);
}

/**
 * An action's About rows: what runs it and what it does to data. Before
 * Studio builds an API action's document, its recipe already tells the
 * request.
 */
export function aboutRows(step: Step, ctx: KindContext): [string, string][] {
  if (step.kind !== "action") return [];
  const document = ctx.actionContract(String(step["uses"] ?? ""));
  const recipe = recipeOf(step, ctx);
  const runs =
    !document && recipe?.kind === "http"
      ? [HTTP_CONNECTOR, `${recipe.method} ${recipe.pathTemplate}`.trim()].join(
          " · ",
        )
      : implementationText(document);
  return [
    ["Runs", runs],
    ["Side effect", sideEffectText(actionSideEffect(step, ctx))],
  ];
}
