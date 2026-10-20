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
// The fixed formula workload the performance spec times in the page under
// test: Studio's own parser and evaluator, with no DOM. Browser-only code,
// so formula-calibration.ts can bundle it into the page.
import { evaluate } from "../../src/app/editor/formula/evaluate";
import { parseTemplate } from "../../src/app/editor/formula/grammar";

/** The fixed workload: the formula as typed so far, parsed and evaluated. */
export interface Workload {
  text: string;
  context: unknown;
  features: readonly string[];
  /** Parse-and-evaluate rounds in one timed run. */
  rounds: number;
  /** Timed runs; the median is the result. */
  runs: number;
}

/** Parses and evaluates `text` plus a growing suffix, `rounds` times. */
export function formulaWork(work: Workload): number {
  let ok = 0;
  let suffix = "";
  for (let round = 0; round < work.rounds; round++) {
    suffix = suffix.length < 40 ? suffix + "x" : "";
    const parsed = parseTemplate(work.text + suffix, {
      features: work.features,
      loops: [],
    });
    if (parsed.kind !== "ok") continue;
    if (evaluate(parsed.expression, work.context).ok) ok++;
  }
  return ok;
}

/** The median of `runs` timed runs of the workload, in milliseconds. */
export function timeFormulaWork(work: Workload): {
  medianMs: number;
  runsMs: number[];
  evaluated: number;
} {
  // One untimed run warms the JIT, as the keystrokes before the measured
  // ones warm the preview path.
  const evaluated = formulaWork(work);
  const runsMs: number[] = [];
  for (let run = 0; run < work.runs; run++) {
    const start = performance.now();
    formulaWork(work);
    runsMs.push(performance.now() - start);
  }
  const sorted = [...runsMs].sort((a, b) => a - b);
  return {
    medianMs: sorted[Math.floor(sorted.length / 2)],
    runsMs,
    evaluated,
  };
}
