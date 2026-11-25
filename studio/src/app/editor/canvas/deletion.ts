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
// What deleting steps from the canvas asks and says. One rule for one step
// and several: a step's error path goes with it unasked, and the toast names
// its steps; a group with steps inside asks first. Loaded when a step is
// deleted, not with the first page.
import type { DialogOptions } from "../../dialog";
import type { Workflow } from "../../model";
import { removedPathToast } from "../ndv/settings/on-error";
import { allSteps, findStep } from "../ndv/step-walk";
import { errorLane } from "./error-lane";

export interface Deletion {
  /** The question to ask first; null when deleting asks nothing. */
  question: DialogOptions | null;
  /** The toast's words once the steps went, `pinned` when pinned output went with them. */
  done(pinned: boolean): { text: string; restored: string; detail?: string };
}

const stepCount = (count: number) =>
  count === 1 ? "1 step" : `${count} steps`;

/**
 * Deleting `ids` from `workflow`, read before the deletion. `contained`
 * counts the steps inside each of them, as the model does: its error
 * path's steps too, which never count toward the question.
 */
export function deletionOf(
  workflow: Workflow,
  ids: readonly string[],
  contained: readonly number[],
): Deletion {
  const one = ids.length === 1 ? ids[0] : "";
  /** The steps of each error path that goes, in order. */
  const paths = ids.flatMap((id) => {
    const step = findStep(workflow.spec.steps, (item) => item.id === id);
    const lane = step && errorLane(step);
    const held = lane ? allSteps(lane.steps).map((inner) => inner.id) : [];
    return held.length ? [held] : [];
  });
  const held = paths.flat();
  const inside = contained.reduce((sum, count) => sum + count, 0) - held.length;
  const question: DialogOptions | null = !inside
    ? null
    : one
      ? {
          title: `Delete ${one} and the ${stepCount(inside)} inside it?`,
          message:
            "The group and every step in its branches are removed from the workflow. You can undo this.",
          confirmLabel: "Delete group",
          danger: true,
        }
      : {
          title: `Delete ${ids.length} steps and the ${stepCount(inside)} inside them?`,
          message:
            "The groups and every step in their branches are removed from the workflow. You can undo this.",
          confirmLabel: "Delete steps",
          danger: true,
        };
  const what = one || `${ids.length} steps`;
  const whose = one ? "its" : "their";
  return {
    question,
    done(pinned) {
      const data = pinned ? ` and ${whose} pinned output` : "";
      const removed = removedPathToast(held.length, held);
      if (!removed)
        return {
          text: `Deleted ${what}${data}.`,
          restored: `Restored ${what}${data}.`,
          detail: undefined,
        };
      const path = one
        ? "its error path"
        : paths.length === 1
          ? "an error path"
          : `${paths.length} error paths`;
      const detail =
        paths.length === 1
          ? removed.detail
          : removed.detail.replace(/ in it\.$/, " in them.");
      return pinned
        ? {
            text: `Deleted ${what}, ${path} (${stepCount(held.length)})${data}`,
            restored: `Restored ${what}, ${path}${data}.`,
            detail,
          }
        : {
            text: `Deleted ${what} and ${path} (${stepCount(held.length)})`,
            restored: `Restored ${what} and ${path}.`,
            detail,
          };
    },
  };
}
