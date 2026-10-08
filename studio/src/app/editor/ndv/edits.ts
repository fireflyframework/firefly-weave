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
// What `NdvContext.edit()` does to the definition: paths relative to the step
// (or, with scope "workflow", to the document), applied in order to a copy.
// Identity and structure stay with their own commands: a step's ID changes
// through Rename (which rewrites references) and steps move on the canvas.
import { getAt, setAt } from "../../forms/core/json";
import type { Step, Workflow } from "../../model";
import {
  ndvRegistry,
  type ContainerSpec,
  type Edit,
  type Path,
} from "./registry";

export class EditError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "EditError";
  }
}

type Containers = (step: Step) => readonly ContainerSpec[];
const registeredContainers: Containers = (step) =>
  ndvRegistry.kind(step.kind)?.containers?.(step) ?? [];
const isStep = (value: unknown): value is Step =>
  !!value &&
  typeof value === "object" &&
  !Array.isArray(value) &&
  typeof (value as { id?: unknown }).id === "string";

/** Where a step sits in the document, through registered containers; null when absent. */
export function stepPath(
  workflow: Workflow,
  stepId: string,
  containers: Containers = registeredContainers,
): Path | null {
  const search = (steps: unknown, at: Path): Path | null => {
    if (!Array.isArray(steps)) return null;
    for (const [index, step] of steps.entries()) {
      if (!isStep(step)) continue;
      const here = [...at, index];
      if (step.id === stepId) return here;
      for (const container of containers(step)) {
        const found = search(getAt(step, container.path), [
          ...here,
          ...container.path,
        ]);
        if (found) return found;
      }
    }
    return null;
  };
  return search(workflow.spec.steps, ["spec", "steps"]);
}

/** setAt, with a list position past the end reported as a refusal. */
function writeAt(value: unknown, path: Path, next: unknown): unknown {
  try {
    return setAt(value, path, next);
  } catch (error) {
    if (error instanceof RangeError)
      throw new EditError("That list has no item at that position.");
    throw error;
  }
}

/** A copy without one key or list item; unlike removeAt, emptied parents stay. */
function deleteAt(value: unknown, path: Path): unknown {
  if (getAt(value, path) === undefined) return value;
  const parentPath = path.slice(0, -1);
  const last = path[path.length - 1];
  const parent = getAt(value, parentPath);
  const next = Array.isArray(parent)
    ? parent.filter((_, index) => index !== Number(last))
    : Object.fromEntries(
        Object.entries(parent as Record<string, unknown>).filter(
          ([key]) => key !== String(last),
        ),
      );
  return parentPath.length ? setAt(value, parentPath, next) : next;
}

function check(change: Edit): void {
  const [head, next] = change.path;
  if (!change.path.length) throw new EditError("An edit needs a path.");
  if ((change.scope ?? "step") === "step") {
    if (head === "id" || head === "kind")
      throw new EditError(
        "Rename a step with Rename; a step's kind can't change.",
      );
    return;
  }
  if (head !== "spec" && head !== "metadata")
    throw new EditError("Workflow edits change spec or metadata fields.");
  if (change.path.length < 2)
    throw new EditError("Name the spec or metadata field to change.");
  if (head === "spec" && next === "steps")
    throw new EditError("Steps change on the canvas, not through parameters.");
}

/** The workflow after step details edits; the input workflow is never modified. */
export function applyEdits(
  workflow: Workflow,
  stepId: string,
  changes: readonly Edit[],
  containers: Containers = registeredContainers,
): Workflow {
  changes.forEach(check);
  const base = stepPath(workflow, stepId, containers);
  let next: unknown = workflow;
  for (const change of changes) {
    const scope = change.scope ?? "step";
    if (scope === "step" && !base)
      throw new EditError(`Step ${stepId} isn't in this workflow.`);
    const target =
      scope === "workflow" ? change.path : [...(base ?? []), ...change.path];
    next =
      change.value === undefined
        ? deleteAt(next, target)
        : writeAt(next, target, change.value);
  }
  return next as Workflow;
}
