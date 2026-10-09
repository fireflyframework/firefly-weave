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
// The one way step details walks a workflow's steps: every step at any depth,
// in the order they read (a step, then what its decision cases, Otherwise and
// parallel branches hold). It looks for lists of steps rather than for kinds,
// so a new kind that holds steps is walked without changes here.
import type { Step } from "../../model";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** The steps a step holds: the `steps` of a branch among its values (a list of branches, one branch, or branches by name). */
function held(step: Step): Step[][] {
  const lists: Step[][] = [];
  for (const value of Object.values(step)) {
    const branches: unknown[] = Array.isArray(value)
      ? value
      : isRecord(value)
        ? Array.isArray(value["steps"])
          ? [value]
          : Object.values(value)
        : [];
    for (const branch of branches) {
      const inner = isRecord(branch) ? branch["steps"] : undefined;
      if (Array.isArray(inner)) lists.push(inner.filter(isRecord) as Step[]);
    }
  }
  return lists;
}

function* walk(steps: readonly Step[], seen: Set<Step>): Generator<Step> {
  for (const step of steps) {
    // A step reached twice (a shared YAML anchor) is listed once, and a loop can't run forever.
    if (seen.has(step)) continue;
    seen.add(step);
    yield step;
    for (const inner of held(step)) yield* walk(inner, seen);
  }
}

/** Every step, nested ones included, depth-first. */
export const allSteps = (steps: Step[]): Step[] => [...walk(steps, new Set())];

/** The first step (depth-first) the test accepts, or null; the step itself, not a copy. */
export function findStep(
  steps: Step[],
  test: (step: Step) => boolean,
): Step | null {
  for (const step of walk(steps, new Set())) if (test(step)) return step;
  return null;
}
