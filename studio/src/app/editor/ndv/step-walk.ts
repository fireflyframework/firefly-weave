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
// in the order they read (a step, then what its decision cases, Otherwise,
// parallel branches and loop body hold). It follows only the step lists the
// language declares, never a field that holds data or a formula, so a value
// that happens to contain a list called `steps` is not read as steps.
import type { Step } from "../../model";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** The `steps` of a branch, when it is one; only objects can be steps. */
const stepsOf = (branch: unknown): Step[] => {
  const inner = isRecord(branch) ? branch["steps"] : undefined;
  return Array.isArray(inner) ? (inner.filter(isRecord) as Step[]) : [];
};

/**
 * The step lists each kind declares (the language's `Branch`, in
 * src/firefly_weave/contracts/definitions.py): a decision's `cases[].steps`
 * and `default.steps` (Otherwise), a parallel step's `branches.<name>.steps`
 * and a loop's `body.steps`. A kind that holds steps in a new place is added
 * here, and nowhere else.
 */
const CONTAINERS: Record<string, (step: Step) => Step[][]> = {
  switch: (step) => {
    const cases = Array.isArray(step["cases"]) ? step["cases"] : [];
    return [...cases.map(stepsOf), stepsOf(step["default"])];
  },
  parallel: (step) =>
    isRecord(step["branches"])
      ? Object.values(step["branches"]).map(stepsOf)
      : [],
  forEach: (step) => [stepsOf(step["body"])],
};

function* walk(steps: readonly Step[], seen: Set<Step>): Generator<Step> {
  for (const step of steps) {
    // A step reached twice (a shared YAML anchor) is listed once, and a loop can't run forever.
    if (seen.has(step)) continue;
    seen.add(step);
    yield step;
    const containers = Object.hasOwn(CONTAINERS, step.kind)
      ? CONTAINERS[step.kind](step)
      : [];
    for (const inner of containers) yield* walk(inner, seen);
  }
}

/** The step lists a step holds directly (none for most kinds). */
export const childLists = (step: Step): Step[][] =>
  Object.hasOwn(CONTAINERS, step.kind)
    ? CONTAINERS[step.kind](step).filter((list) => list.length > 0)
    : [];

/** A branch with its steps replaced by their IDs. */
const idsOf = (branch: unknown): unknown =>
  isRecord(branch) && Array.isArray(branch["steps"])
    ? { ...branch, steps: stepsOf(branch).map((step) => step.id) }
    : branch;
/**
 * The step without the steps it holds: each list it declares becomes the
 * list of their IDs (what the step itself is, apart from its children).
 */
export function withChildIds(step: Step): Step {
  switch (step.kind as string) {
    case "switch":
      return {
        ...step,
        ...(Array.isArray(step["cases"])
          ? { cases: step["cases"].map(idsOf) }
          : {}),
        ...("default" in step ? { default: idsOf(step["default"]) } : {}),
      };
    case "parallel":
      return isRecord(step["branches"])
        ? {
            ...step,
            branches: Object.fromEntries(
              Object.entries(step["branches"]).map(([name, branch]) => [
                name,
                idsOf(branch),
              ]),
            ),
          }
        : step;
    case "forEach":
      return "body" in step ? { ...step, body: idsOf(step["body"]) } : step;
    default:
      return step;
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
