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
// Before an execution: errors in what it runs ask to be fixed first, and a
// pinned target asks whether to unpin it. Warnings never block. DOM-free.

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
