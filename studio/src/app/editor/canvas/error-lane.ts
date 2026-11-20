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
// A step's error path on the canvas: the lane its Error output leads to,
// named "On error" in Outline and for screen readers, and the words a
// tile's name adds for what the step's failure does.
import type { Step, Workflow } from "../../model";
import { removedPathToast } from "../ndv/settings/on-error";
import { allSteps, findStep } from "../ndv/step-walk";
import { blocksOf } from "../step-blocks";

export interface ErrorLane {
  /** The lane's sequence, "<step>/errorPath". */
  owner: string;
  label: string;
  /** The Error output's and the empty lane's accessible name. */
  handleName: string;
  steps: Step[];
}

/** The error path a step's Error output leads to; null for a step without one. */
export function errorLane(step: Step): ErrorLane | null {
  const block = blocksOf(step).find((item) => item.kind === "errorPath");
  if (!block) return null;
  const steps = block.branch?.steps;
  return {
    owner: `${step.id}/${block.name}`,
    label: "On error",
    handleName: `Add a step to the error path of ${step.id}`,
    steps: Array.isArray(steps) ? steps : [],
  };
}

/** What a tile's name adds for a step that handles its failures: "continues on error". */
export function failureText(step: Step): string {
  if (step["onError"] === "continue") return "continues on error";
  if (step["onError"] === "errorOutput") return "continues with error output";
  return "";
}

/**
 * The toast after step `id` was deleted with its error path: "Deleted
 * submit and its error path (2 steps)" and which steps were in it. Null
 * when the step has no error path, or one without steps: deleting it asks
 * nothing more. Read before the deletion.
 */
export function deletedPathToast(
  workflow: Workflow,
  id: string,
): { text: string; detail: string } | null {
  const step = findStep(workflow.spec.steps, (item) => item.id === id);
  const lane = step && errorLane(step);
  const held = lane ? allSteps(lane.steps).map((inner) => inner.id) : [];
  const removed = removedPathToast(held.length, held);
  if (!removed) return null;
  const count = held.length === 1 ? "1 step" : `${held.length} steps`;
  return {
    text: `Deleted ${id} and its error path (${count})`,
    detail: removed.detail,
  };
}
