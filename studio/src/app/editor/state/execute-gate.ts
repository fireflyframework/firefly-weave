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
// Before an execution: errors in what it runs ask to be fixed first, a step
// inside an error path its step won't take asks for a simulated failure of
// that step, and a pinned target asks whether to unpin it. Warnings never
// block. DOM-free.
import type { Step } from "../../model";
import { blocksOf } from "../step-blocks";

export interface BlockingIssue {
  stepId: string;
  severity: "error" | "warning";
}
export type ExecuteGate =
  | { kind: "go" }
  | { kind: "fix"; title: string; issues: BlockingIssue[] }
  | { kind: "unpin"; title: string; stepId: string };

/**
 * What Execute does first. `runs` are the steps the execution runs (the
 * slice, or every step for the whole workflow); `pinned` says whether the
 * target step's output is pinned (Execute step only).
 */
export function executeGate(
  issues: readonly BlockingIssue[],
  runs: readonly string[],
  pinned: boolean,
): ExecuteGate {
  const inScope = new Set(runs);
  const errors = issues.filter(
    (issue) => issue.severity === "error" && inScope.has(issue.stepId),
  );
  if (errors.length)
    return {
      kind: "fix",
      title: `Fix ${errors.length} issue${errors.length === 1 ? "" : "s"} first`,
      issues: errors,
    };
  if (pinned)
    return {
      kind: "unpin",
      title: "Unpin and execute?",
      stepId: runs[runs.length - 1],
    };
  return { kind: "go" };
}

/**
 * The steps whose error paths hold each step, outermost first: a decision
 * or a loop inside a path keeps its steps in that path.
 */
export function errorPathsAround(
  steps: readonly Step[],
): (stepId: string) => readonly string[] {
  const around = new Map<string, readonly string[]>();
  const walk = (list: readonly Step[], owners: readonly string[]) => {
    for (const step of list) {
      if (owners.length) around.set(step.id, owners);
      for (const block of blocksOf(step)) {
        const inner =
          block.kind === "errorPath" ? [...owners, step.id] : owners;
        const blockSteps = block.branch?.steps;
        if (Array.isArray(blockSteps)) walk(blockSteps, inner);
      }
    }
  };
  walk(steps, []);
  return (stepId) => around.get(stepId) ?? [];
}

/**
 * Execute step on a step inside error paths: the outermost step whose path
 * holds it and that won't fail with this test data (`failing` says whether
 * a simulated failure applies to a step), so the target isn't reached; null
 * when every path around it runs. A step Studio can't fail itself
 * (`canFail`, such as a signal, which fails through its Time out answer) is
 * left to the host, which says why the target isn't reached.
 */
export function untakenPath(
  steps: readonly Step[],
  stepId: string,
  failing: (owner: string) => boolean,
  canFail: (owner: Step) => boolean,
): { owner: string; stepId: string } | null {
  const owners = errorPathsAround(steps)(stepId);
  const owner = owners.find((id) => !failing(id));
  if (owner === undefined) return null;
  const found = findStep(steps, owner);
  return found && canFail(found) ? { owner, stepId } : null;
}

/** A step of the workflow, however deep. */
function findStep(steps: readonly Step[], id: string): Step | null {
  for (const step of steps) {
    if (step.id === id) return step;
    for (const block of blocksOf(step)) {
      const inner = block.branch?.steps;
      const found = Array.isArray(inner) ? findStep(inner, id) : null;
      if (found) return found;
    }
  }
  return null;
}
