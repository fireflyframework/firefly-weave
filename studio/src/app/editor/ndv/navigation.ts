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
// Where a step is and what runs around it, in Outline order (the order steps
// run in, entering decision paths, parallel branches and loop bodies): the
// breadcrumb at the top of step details, Previous and Next step, and the
// neighbor buttons. Lanes come from each kind's registered containers.
import { conditionSummary } from "../../designer/conditions";
import { getAt } from "../../forms/core/json";
import type { Step, Workflow } from "../../model";
import { ndvRegistry, type ContainerSpec, type KindContext } from "./registry";

export interface Place {
  id: string;
  /** The decision or parallel step whose path holds it; null in the main sequence. */
  ownerStep: string | null;
  /** "Main sequence", "Path amount > 1000", "Otherwise", "Branch ledger". */
  lane: string;
  index: number;
  count: number;
}
interface Lane {
  steps: Step[];
  ownerStep: string | null;
  label: string;
  /** A decision's Otherwise: it runs when no path applies. */
  otherwise: boolean;
}

const isStep = (value: unknown): value is Step =>
  !!value &&
  typeof value === "object" &&
  typeof (value as Step).id === "string";

function laneLabel(
  owner: Step,
  container: ContainerSpec,
  workflow: Workflow,
): string {
  const { path } = container;
  if (owner.kind === "switch") {
    if (path[0] === "default") return "Otherwise";
    const when = getAt(owner, ["cases", path[1], "when"]);
    return when === undefined
      ? `Path ${Number(path[1]) + 1}`
      : `Path ${conditionSummary(when, workflow)}`;
  }
  if (owner.kind === "parallel") return `Branch ${String(path[1])}`;
  const ctx: KindContext = {
    workflow,
    features: [],
    actionContract: () => null,
    tableContract: () => null,
    workflowContract: () => null,
  };
  return container.label(owner, ctx);
}

/** Every lane of a step, in container order (paths, then Otherwise; branches by name). */
function lanesOf(step: Step, workflow: Workflow): Lane[] {
  const containers = ndvRegistry.kind(step.kind)?.containers?.(step) ?? [];
  return containers.map((container) => {
    const inner = getAt(step, container.path);
    return {
      steps: Array.isArray(inner) ? inner.filter(isStep) : [],
      ownerStep: step.id,
      label: laneLabel(step, container, workflow),
      otherwise: step.kind === "switch" && container.path[0] === "default",
    };
  });
}

function walk(
  workflow: Workflow,
  visit: (step: Step, lane: Lane, index: number) => boolean | void,
) {
  const go = (lane: Lane): boolean => {
    for (const [index, step] of lane.steps.entries()) {
      if (visit(step, lane, index) === true) return true;
      for (const inner of lanesOf(step, workflow)) if (go(inner)) return true;
    }
    return false;
  };
  go({
    steps: workflow.spec.steps.filter(isStep),
    ownerStep: null,
    label: "Main sequence",
    otherwise: false,
  });
}

export function outlineOrder(workflow: Workflow): string[] {
  const order: string[] = [];
  walk(workflow, (step) => void order.push(step.id));
  return order;
}

/** The lane holding a step, with the step's index in it. */
function laneOf(
  workflow: Workflow,
  id: string,
): { lane: Lane; index: number } | null {
  let found: { lane: Lane; index: number } | null = null;
  walk(workflow, (step, lane, index) => {
    if (step.id !== id) return false;
    found = { lane, index };
    return true;
  });
  return found;
}

export function placeOf(workflow: Workflow, id: string): Place | null {
  const at = laneOf(workflow, id);
  return at
    ? {
        id,
        ownerStep: at.lane.ownerStep,
        lane: at.lane.label,
        index: at.index,
        count: at.lane.steps.length,
      }
    : null;
}

export function breadcrumb(workflow: Workflow, id: string): string {
  const place = placeOf(workflow, id);
  if (!place) return "";
  const where = place.ownerStep
    ? `${place.lane} of ${place.ownerStep}`
    : place.lane;
  return `${where} · step ${place.index + 1} of ${place.count}`;
}

export function adjacent(
  workflow: Workflow,
  id: string,
  delta: 1 | -1,
): string | null {
  const order = outlineOrder(workflow);
  const at = order.indexOf(id);
  return at < 0 ? null : (order[at + delta] ?? null);
}

/** What runs after a step finishes its own lane: the next sibling, else what runs after its owner. */
export function stepsAfter(workflow: Workflow, id: string): string[] {
  const at = laneOf(workflow, id);
  if (!at) return [];
  const next = at.lane.steps[at.index + 1];
  if (next) return [next.id];
  return at.lane.ownerStep ? stepsAfter(workflow, at.lane.ownerStep) : [];
}

/**
 * The steps the run can come from when it moves on after `step`: the step
 * itself when it holds nothing, else the last step of each path or branch
 * (the last step of that, again, when it holds paths of its own), plus the
 * step itself when a path is empty and the run can leave it directly.
 * Mirrors how the steps after a group fan out into its first steps.
 */
function endings(step: Step, workflow: Workflow): string[] {
  const lanes = lanesOf(step, workflow);
  if (!lanes.length) return [step.id];
  const paths: string[] = [];
  const otherwise: string[] = [];
  let direct = false;
  for (const lane of lanes) {
    const last = lane.steps.at(-1);
    if (!last) direct = true;
    else (lane.otherwise ? otherwise : paths).push(...endings(last, workflow));
  }
  return [...new Set([...paths, ...(direct ? [step.id] : []), ...otherwise])];
}

export function neighbors(
  workflow: Workflow,
  id: string,
): { before: string[]; after: string[] } {
  const at = laneOf(workflow, id);
  if (!at) return { before: [], after: [] };
  const previous = at.lane.steps[at.index - 1];
  const before = previous
    ? endings(previous, workflow)
    : [at.lane.ownerStep ?? "$trigger"];
  const lanes = lanesOf(at.lane.steps[at.index], workflow);
  if (!lanes.length) return { before, after: stepsAfter(workflow, id) };
  const firsts: string[] = [];
  const otherwise: string[] = [];
  let empty = false;
  for (const lane of lanes) {
    const first = lane.steps[0]?.id;
    if (!first) empty = true;
    else (lane.otherwise ? otherwise : firsts).push(first);
  }
  const following = empty ? stepsAfter(workflow, id) : [];
  return {
    before,
    after: [...new Set([...firsts, ...following, ...otherwise])],
  };
}
