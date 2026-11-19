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
// Step details targets for triggers. "$trigger" is the workflow's input form;
// "$trigger:<id>" is one trigger choice of the canvas sidecar, as its tile on
// the canvas is named. Both start the workflow, so what follows a trigger
// (the first step, Execute workflow) is the same for every one of them.
import {
  triggerIntents,
  type CanvasSidecar,
  type TriggerIntent,
} from "../state/canvas-sidecar";

const CHOICE = "$trigger:";

/** A trigger choice's step details target, as its tile is named. */
export const triggerChoiceTarget = (id: string): string => `${CHOICE}${id}`;

/** True for the target of one trigger choice. */
export const isTriggerChoice = (target: string): boolean =>
  target.startsWith(CHOICE);

/** True for the input form and for every trigger choice. */
export const isTriggerTarget = (target: string): boolean =>
  target === "$trigger" || isTriggerChoice(target);

/** The trigger choice a target names, or null when it names none of the canvas's. */
export function triggerChoiceOf(
  canvas: CanvasSidecar,
  target: string,
): TriggerIntent | null {
  if (!isTriggerChoice(target)) return null;
  const id = target.slice(CHOICE.length);
  return triggerIntents(canvas).find((intent) => intent.id === id) ?? null;
}
