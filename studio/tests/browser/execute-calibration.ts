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
// The 200-step Execute workflow gate: the workflow it executes, and how its
// time is scaled by the runner's speed, exactly as the formula preview gate
// scales its own (the same calibration and allowance).
import { yamlFile, type WorkflowFile } from "./step-details-po";

/** Click to the run log's "Completed · 200 steps", on a runner as fast as the reference. */
export const EXECUTE_BUDGET_MS = 2000;
/** Timed executions after one warm-up; the median is the result. */
export const EXECUTE_RUNS = 5;

/** One pinned action, then 199 transforms each reading the one before. */
export function longWorkflow(): WorkflowFile {
  const steps = [
    `    - id: check
      kind: action
      uses: crm.lookup@1.0.0
      with: {object: {customerId: {literal: c-1}}}`,
  ];
  for (let index = 1; index < 200; index++) {
    const previous =
      index === 1
        ? "/steps/check/output/found"
        : `/steps/t${index - 1}/output/ok`;
    steps.push(`    - id: t${index}
      kind: transform
      value: {object: {ok: {ref: ${previous}}, pad: {literal: ${"x".repeat(200)}}}}`);
  }
  return yamlFile(
    "long.yaml",
    `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: long, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
${steps.join("\n")}
  output: {literal: null}
`,
  );
}

/**
 * The median scaled by the runner's speed: a runner slower than the
 * reference gets its own slowdown as headroom, up to `maxAllowance`; a
 * faster one gets none.
 */
export function scaledMs(
  medianMs: number,
  calibrationMs: number,
  referenceMs: number,
  maxAllowance: number,
) {
  const ratio = calibrationMs / referenceMs;
  const allowance = Math.min(Math.max(ratio, 1), maxAllowance);
  return { medianMs, scaledMs: medianMs / allowance, ratio, allowance };
}
