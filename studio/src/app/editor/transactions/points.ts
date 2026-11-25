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
// How far a transaction can still be undone, step by step: where it can't any
// more (its points of no return), where a step that can be undone may not go,
// and what is reserved when a step runs. The platform's compiler decides; this
// reads the same paths for what Studio shows and offers. Pure, and loaded with
// the editor.
import type { Step, Workflow } from "../../model";
import { blocksOf } from "../step-blocks";
import {
  NESTED_TRANSACTION,
  enclosingTransaction,
  holdsTransaction,
} from "./group";
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

/** A place the walk reaches: a list's place before a step, or at its end. */
interface Place {
  owner: string;
  index: number;
  /** The step there; null at the end of the list. */
  step: Step | null;
  reach: Reach;
  /** What the other branches of the parallel steps around it can reserve meanwhile. */
  beside: readonly Step[];
  /** Outside every loop and parallel step: the platform lets only a step here decide. */
  decides: boolean;
}
interface Walk {
  visit(place: Place): void;
  /** A step read as if it weren't there. */
  without?: string;
}
type Around = Pick<Place, "beside" | "decides">;

const stepsIn = (branch: { steps: Step[] }): Step[] =>
  Array.isArray(branch.steps) ? branch.steps : [];

/** Walks a list of steps from a state, as a run may take it, to the state after it. */
function walk(
  steps: readonly Step[],
  owner: string,
  from: Reach,
  plan: Walk,
  around: Around = { beside: [], decides: true },
): Reach {
  let reach = from;
  steps.forEach((step, index) => {
    plan.visit({ owner, index, step, reach, ...around });
    if (step.id !== plan.without) reach = past(step, reach, plan, around);
  });
  plan.visit({ owner, index: steps.length, step: null, reach, ...around });
  return reach;
}

/** The state after a step, and after everything inside it. */
function past(step: Step, from: Reach, plan: Walk, around: Around): Reach {
  let reach = from;
  const choice = undoChoiceOf(step);
  if (choice === "irreversible")
    reach = { open: false, passed: reach.open || reach.passed, held: [] };
  else if (choice === "reserve" && reach.open)
    reach = { ...reach, held: [...new Set([...reach.held, step])] };
  const blocks = blocksOf(step);
  const own = blocks.filter(({ kind }) => kind !== "errorPath");
  const into = (
    { name, branch }: (typeof blocks)[number],
    at: Reach,
    inside = around,
  ) => walk(stepsIn(branch), `${step.id}/${name}`, at, plan, inside);
  const at = reach;
  switch (step.kind as string) {
    case "fail":
      reach = NEVER;
      break;
    case "switch":
      reach = either(own.map((block) => into(block, at)));
      break;
    case "parallel": {
      const reserves = own.map(({ branch }) => reservedIn(stepsIn(branch)));
      const ends = own.map((block, index) =>
        into(block, at, {
          decides: false,
          beside: [
            ...around.beside,
            ...reserves.filter((_, other) => other !== index).flat(),
          ],
        }),
      );
      // Every branch runs: it ends when each of them does.
      reach = ends.every(({ open, passed }) => open || passed)
        ? {
            open: ends.every(({ open }) => open),
            passed: ends.some(({ passed }) => passed),
            held: either(ends).held,
          }
        : NEVER;
      break;
    }
    case "forEach": {
      const items = { ...around, decides: false };
      const item = (start: Reach) =>
        own.reduce((state, block) => into(block, state, items), start);
      const once = item(at);
      // A second item differs only when the first changed something.
      const again =
        step["maxItems"] === 1 || same(once, at) ? once : item(once);
      // A loop may run no item.
      reach = either([at, once, again]);
      break;
    }
  }
  // After a step's failure its error path runs instead of nothing.
  for (const block of blocks)
    if (block.kind === "errorPath") reach = either([reach, into(block, reach)]);
  return reach;
}

/** What a branch can reserve while it can still be undone. */
function reservedIn(steps: readonly Step[]): Step[] {
  const found = new Set<Step>();
  walk(steps, "", START, {
    visit: ({ step, reach }) => {
      if (step && reach.open && undoChoiceOf(step) === "reserve")
        found.add(step);
    },
  });
  return [...found];
}

const inside = (transaction: Step, plan: Walk) =>
  walk(
    stepsIn(transaction as unknown as { steps: Step[] }),
    `${transaction.id}/steps`,
    START,
    plan,
  );

/** The reservations a step of a transaction may find held when it runs. */
export function heldAt(transaction: Step, id: string): Step[] {
  const found = new Set<Step>();
  inside(transaction, {
    visit: ({ step, reach, beside }) => {
      if (step?.id === id && reach.open)
        for (const reserved of [...reach.held, ...beside]) found.add(reserved);
    },
  });
  return [...found];
}

/**
 * The steps a transaction can't be undone after, one per path that has one:
 * the first step on it that can't be undone.
 */
export function pointsOfNoReturn(transaction: Step): Set<string> {
  const found = new Set<string>();
  inside(transaction, {
    visit: ({ step, reach, decides }) => {
      if (
        step &&
        decides &&
        reach.open &&
        undoChoiceOf(step) === "irreversible"
      )
        found.add(step.id);
    },
  });
  return found;
}

/** Whether a step of a transaction runs only past a step that can't be undone. */
export function afterPointOfNoReturn(transaction: Step, id: string): boolean {
  let before = false;
  let after = false;
  inside(transaction, {
    visit: ({ step, reach }) => {
      if (step?.id !== id) return;
      before ||= reach.open;
      after ||= reach.passed;
    },
  });
  return after && !before;
}

/** The platform's rule (TXN_ORDER), as a place a step may not be moved to says it. */
export const AFTER_POINT =
  "Steps that can be undone must come before the step that can't be undone.";

/** A step that can be undone, or one anywhere inside it. */
const undoable = (step: Step): boolean => {
  const choice = undoChoiceOf(step);
  return (
    choice === "reserve" ||
    choice === "compensate" ||
    blocksOf(step).some(({ branch }) => stepsIn(branch).some(undoable))
  );
};

/**
 * Why a step may not be moved to a place in a transaction: it can be undone,
 * and a path gets there past a step that can't. Null where it may.
 */
export function dropAfterPointOfNoReturn(
  transaction: Step,
  moving: Step,
  owner: string,
  index: number,
): string | null {
  if (!undoable(moving)) return null;
  let passed = false;
  inside(transaction, {
    without: moving.id,
    visit: (place) => {
      if (place.owner === owner && place.index === index)
        passed ||= place.reach.passed;
    },
  });
  return passed ? AFTER_POINT : null;
}

/** Why a step may not be moved to a place of a workflow because of its transactions, or null. */
export function moveRefusal(
  workflow: Workflow,
  moving: Step,
  at: { owner: string; index: number },
): string | null {
  const transaction = enclosingTransaction(workflow, at.owner);
  if (!transaction) return null;
  return holdsTransaction([moving])
    ? NESTED_TRANSACTION
    : dropAfterPointOfNoReturn(transaction, moving, at.owner, at.index);
}
