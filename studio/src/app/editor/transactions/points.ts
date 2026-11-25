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
// How far a transaction can still be undone, step by step: what is reserved
// when a step runs. The platform's compiler decides; this reads the same paths
// for what Studio shows and offers. Pure, and loaded with the editor.
import type { Step } from "../../model";
import { blocksOf } from "../step-blocks";
import type { UndoMode } from "./transaction-words";

/** How a step is undone; null while nothing is chosen. */
export type UndoChoice = UndoMode | null;

/** The step's choice, as the platform reads it from the fields present. */
export function undoChoiceOf(step: Step): UndoChoice {
  if (step["confirm"] !== undefined || step["cancel"] !== undefined)
    return "reserve";
  if (step["compensate"] !== undefined) return "compensate";
  return step["irreversible"] !== undefined ? "irreversible" : null;
}

/** A transaction's state where a step is about to run. */
interface Reach {
  /** A path gets here that can still be undone whole. */
  open: boolean;
  /** A path gets here past a step that can't be undone. */
  passed: boolean;
  /** What a path that can still be undone has reserved. */
  held: readonly Step[];
}
const START: Reach = { open: true, passed: false, held: [] };
const NEVER: Reach = { open: false, passed: false, held: [] };
const either = (paths: readonly Reach[]): Reach => ({
  open: paths.some(({ open }) => open),
  passed: paths.some(({ passed }) => passed),
  held: [...new Set(paths.flatMap(({ held }) => held))],
});
const same = (a: Reach, b: Reach): boolean =>
  a.open === b.open && a.passed === b.passed && a.held.length === b.held.length;

/** What the walk tells of each step it reaches, before it runs. */
type Visit = (
  step: Step,
  reach: Reach,
  /** What the other branches of the parallel steps around it can reserve meanwhile. */
  beside: readonly Step[],
) => void;

const stepsIn = (branch: { steps: Step[] }): Step[] =>
  Array.isArray(branch.steps) ? branch.steps : [];

/** Walks a list of steps from a state, as a run may take it, to the state after it. */
function walk(
  steps: readonly Step[],
  from: Reach,
  visit: Visit,
  beside: readonly Step[] = [],
): Reach {
  let reach = from;
  for (const step of steps) {
    visit(step, reach, beside);
    const choice = undoChoiceOf(step);
    if (choice === "irreversible")
      reach = { open: false, passed: reach.open || reach.passed, held: [] };
    else if (choice === "reserve" && reach.open)
      reach = { ...reach, held: [...new Set([...reach.held, step])] };
    const blocks = blocksOf(step);
    const own = blocks.filter(({ kind }) => kind !== "errorPath");
    const into = (branch: { steps: Step[] }, at: Reach, more = beside) =>
      walk(stepsIn(branch), at, visit, more);
    switch (step.kind as string) {
      case "fail":
        reach = NEVER;
        break;
      case "switch":
        reach = either(own.map(({ branch }) => into(branch, reach)));
        break;
      case "parallel": {
        const at = reach;
        // What each branch can reserve while it can still be undone.
        const reserves = own.map(({ branch }) => {
          const found = new Set<Step>();
          walk(stepsIn(branch), START, (inside, { open }) => {
            if (open && undoChoiceOf(inside) === "reserve") found.add(inside);
          });
          return [...found];
        });
        const ends = own.map(({ branch }, index) =>
          into(branch, at, [
            ...beside,
            ...reserves.filter((_, other) => other !== index).flat(),
          ]),
        );
        const ended = ends.every(({ open, passed }) => open || passed);
        reach = !ended
          ? NEVER
          : {
              open: ends.every(({ open }) => open),
              passed: ends.some(({ passed }) => passed),
              held: either(ends).held,
            };
        break;
      }
      case "forEach": {
        const once = own.reduce((at, { branch }) => into(branch, at), reach);
        // A second item differs only when the first changed something.
        const again =
          step["maxItems"] === 1 || same(once, reach)
            ? once
            : own.reduce((at, { branch }) => into(branch, at), once);
        // A loop may run no item.
        reach = either([reach, once, again]);
        break;
      }
    }
    // After a step's failure its error path runs instead of nothing.
    if ((step.kind as string) !== "transaction")
      for (const { branch, kind } of blocks)
        if (kind === "errorPath") reach = either([reach, into(branch, reach)]);
  }
  return reach;
}
const bodyOf = (transaction: Step): Step[] =>
  stepsIn(transaction as unknown as { steps: Step[] });

/** The reservations a step of a transaction may find held when it runs. */
export function heldAt(transaction: Step, id: string): Step[] {
  const found = new Set<Step>();
  walk(bodyOf(transaction), START, (step, { open, held }, beside) => {
    if (step.id === id && open)
      for (const reserved of [...held, ...beside]) found.add(reserved);
  });
  return [...found];
}
