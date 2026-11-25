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
// A transaction as a builder makes one: what a new one holds, how many steps
// it has, the transaction a place is in, and grouping steps that exist in
// one, with the later reads that would break. Only loaded with the editor.
import {
  sequencesOf,
  type ReferenceSite,
  type Step,
  type StructuredCanvasAdapter,
  type Workflow,
} from "../../model";
import { blocksOf } from "../step-blocks";

/** The platform's rule, in its words (TXN_NESTED). */
export const NESTED_TRANSACTION =
  "A transaction can't contain another transaction.";

const isTransaction = (step: Step) => (step.kind as string) === "transaction";

/** A new transaction: no steps yet, and no result until a step gives one. */
export const newTransactionStep = (id: string): Step => ({
  id,
  // The canvas model's Kind lists the kinds it creates by itself; a
  // transaction comes from its own descriptor.
  kind: "transaction" as Step["kind"],
  steps: [],
  output: { literal: null },
});

/** "3 steps": the steps a transaction holds itself; nothing while it is empty. */
export function stepCount(step: Step): string {
  const own = step["steps"];
  const count = Array.isArray(own) ? own.length : 0;
  return count ? `${count} ${count === 1 ? "step" : "steps"}` : "";
}

/**
 * The transaction whose own steps hold a place (a sequence's owner), at any
 * depth; null outside one. A transaction's error path runs after it is over,
 * so it is not inside it.
 */
export function enclosingTransaction(
  workflow: Workflow,
  owner: string,
): Step | null {
  const find = (
    steps: readonly Step[],
    at: string,
    inside: Step | null,
  ): Step | null | undefined => {
    if (at === owner) return inside;
    for (const step of steps)
      for (const block of blocksOf(step)) {
        const own = isTransaction(step) && block.kind === "body";
        const found = find(
          block.branch.steps,
          `${step.id}/${block.name}`,
          own ? step : inside,
        );
        if (found !== undefined) return found;
      }
    return undefined;
  };
  return find(workflow.spec.steps, "root", null) ?? null;
}

/** A run of steps next to each other in one sequence, both ends included. */
export interface GroupRun {
  owner: string;
  from: number;
  to: number;
}
export interface GroupedSteps {
  definition: Workflow;
  /** Reads of the grouped steps' data, or errors, from outside the run. */
  outsideReads: ReferenceSite[];
}

const stepsIn = (workflow: Workflow, run: GroupRun): Step[] =>
  (sequencesOf(workflow).get(run.owner) ?? []).slice(run.from, run.to + 1);

/** A transaction among these steps, or anywhere inside them. */
const holdsTransaction = (steps: readonly Step[]): boolean =>
  steps.some(
    (step) =>
      isTransaction(step) ||
      blocksOf(step).some((block) => holdsTransaction(block.branch.steps)),
  );

/** Why a run can't be grouped where transactions may not nest, or null. */
export function nestedReason(workflow: Workflow, run: GroupRun): string | null {
  return enclosingTransaction(workflow, run.owner) ||
    holdsTransaction(stepsIn(workflow, run))
    ? NESTED_TRANSACTION
    : null;
}

/** A Stop with error and a call that doesn't wait have no output to give. */
const hasOutput = (step: Step) =>
  step.kind !== "fail" &&
  !((step.kind as string) === "callWorkflow" && step["mode"] === "detach");

/** Every read of these steps in a workflow, with the step (or "", the result) that makes it. */
function readsOf(
  workflow: Workflow,
  ids: ReadonlySet<string>,
): ReferenceSite[] {
  const found: ReferenceSite[] = [];
  const read = (
    value: unknown,
    path: (string | number)[],
    holder: string,
    elsewhere: ReadonlySet<unknown>,
    named = false,
  ) => {
    if (!value || typeof value !== "object" || elsewhere.has(value)) return;
    const entries = Object.entries(value);
    // The keys of an object's fields are names a person gave, not operators.
    const alone = !named && !Array.isArray(value) && entries.length === 1;
    for (const [key, item] of entries) {
      // Literal content is data, never a reference.
      if (alone && key === "literal") continue;
      const at = [...path, Array.isArray(value) ? Number(key) : key];
      const step =
        key === "ref" && typeof item === "string"
          ? /^\/steps\/([^/]+)(?:\/|$)/.exec(item)?.[1]
          : undefined;
      if (step !== undefined && ids.has(step))
        found.push({ path: at, ref: item as string, holder });
      else read(item, at, holder, elsewhere, alone && key === "object");
    }
  };
  const visit = (steps: readonly Step[], path: (string | number)[]) =>
    steps.forEach((step, index) => {
      const at = [...path, index];
      const blocks = blocksOf(step);
      read(
        step,
        at,
        step.id,
        new Set(blocks.map((block) => block.branch.steps)),
      );
      for (const block of blocks)
        visit(block.branch.steps, [...at, ...block.path, "steps"]);
    });
  visit(workflow.spec.steps, ["spec", "steps"]);
  read(workflow.spec["output"], ["spec", "output"], "", new Set());
  return found;
}

/**
 * A run of steps as one transaction in their place, whose result is the last
 * step's output. Nothing else changes: no step says how it is undone, and no
 * read is rewritten, so reads from outside the run are listed instead.
 */
export function groupInTransaction(
  definition: Workflow,
  run: GroupRun,
  id: string,
): GroupedSteps | { refused: string } {
  const refused = nestedReason(definition, run);
  if (refused) return { refused };
  const steps = stepsIn(definition, run);
  const last = steps[steps.length - 1];
  const next = structuredClone(definition);
  sequencesOf(next)
    .get(run.owner)!
    .splice(run.from, steps.length, {
      ...newTransactionStep(id),
      steps: structuredClone(steps),
      output: hasOutput(last)
        ? { ref: `/steps/${last.id}/output` }
        : { literal: null },
    });
  const grouped = new Set<string>();
  const gather = (step: Step) => {
    grouped.add(step.id);
    for (const block of blocksOf(step)) block.branch.steps.forEach(gather);
  };
  steps.forEach(gather);
  return {
    definition: next,
    outsideReads: readsOf(
      definition,
      new Set(steps.map((step) => step.id)),
    ).filter((site) => !grouped.has(site.holder)),
  };
}

/** Groups the run in the open workflow as one undo step, and selects the block. */
export function applyGroup(
  model: StructuredCanvasAdapter,
  run: GroupRun,
  grouped: GroupedSteps,
): void {
  const block = sequencesOf(grouped.definition).get(run.owner)![run.from];
  const steps = block["steps"] as Step[];
  model.batch(() => {
    model.insertSteps(run.owner, run.from, [{ ...block, steps: [] }]);
    steps.forEach((step, index) =>
      model.move(step.id, `${block.id}/steps`, index),
    );
    model.selected = block.id;
  });
}

/** What reads the grouped steps from outside, as the question before grouping says it. */
export function readsText(sites: readonly ReferenceSite[]): string {
  const steps = new Set(
    sites.flatMap((site) => (site.holder ? [site.holder] : [])),
  ).size;
  const result = sites.some((site) => !site.holder);
  const several = steps + Number(result) > 1;
  const who = [
    ...(steps ? [`${steps} later ${steps === 1 ? "step" : "steps"}`] : []),
    ...(result
      ? [steps ? "the workflow's result" : "The workflow's result"]
      : []),
  ].join(" and ");
  return `${who} ${several ? "read" : "reads"} data from these steps. After grouping ${several ? "they" : "it"} can read only the transaction's result.`;
}
