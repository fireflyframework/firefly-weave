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
// parallel branches, loop body and error path hold). It follows only the
// step lists the language declares (the blocks of each step), never a field
// that holds data or a formula, so a value that happens to contain a list
// called `steps` is not read as steps.
import type { Step } from "../../model";
import { blockSteps, blocksOf } from "../step-blocks";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** The `steps` of a branch, when it is one; only objects can be steps. */
const stepsOf = (branch: unknown): Step[] => {
  const inner = isRecord(branch) ? branch["steps"] : undefined;
  return Array.isArray(inner) ? (inner.filter(isRecord) as Step[]) : [];
};

function* walk(steps: readonly Step[], seen: Set<Step>): Generator<Step> {
  for (const step of steps) {
    // A step reached twice (a shared YAML anchor) is listed once, and a loop can't run forever.
    if (seen.has(step)) continue;
    seen.add(step);
    yield step;
    for (const block of blocksOf(step)) yield* walk(blockSteps(block), seen);
  }
}

/** The step lists a step holds directly (none for most kinds). */
export const childLists = (step: Step): Step[][] =>
  blocksOf(step)
    .map(blockSteps)
    .filter((list) => list.length > 0);

/** A branch with its steps replaced by their IDs. */
const idsOf = (branch: unknown): unknown =>
  isRecord(branch) && Array.isArray(branch["steps"])
    ? { ...branch, steps: stepsOf(branch).map((step) => step.id) }
    : branch;
/** A copy of `value` with `next` at `path`, sharing everything else; [] replaces the value. */
function replaceAt(
  value: unknown,
  path: readonly (string | number)[],
  next: unknown,
): unknown {
  if (!path.length) return next;
  const [head, ...rest] = path;
  if (Array.isArray(value)) {
    const copy = [...value];
    copy[Number(head)] = replaceAt(value[Number(head)], rest, next);
    return copy;
  }
  const record = value as Record<string, unknown>;
  return { ...record, [head]: replaceAt(record[head], rest, next) };
}
/**
 * The step without the steps it holds: each list it declares becomes the
 * list of their IDs (what the step itself is, apart from its children).
 */
export function withChildIds(step: Step): Step {
  let result: unknown = step;
  for (const block of blocksOf(step))
    if (isRecord(block.branch) && Array.isArray(block.branch["steps"]))
      result = replaceAt(result, block.path, idsOf(block.branch));
  return result as Step;
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
