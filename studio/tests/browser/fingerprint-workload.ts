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
// The fingerprint workload the performance spec times in the page under
// test: Studio's own fingerprints() over a 200-step workflow, each call after
// one step's literal changed, as an edit would. Browser-only code, so
// fingerprint-calibration.ts can bundle it into the page.
import type { Step, Workflow } from "../../src/app/model";
import { fingerprints } from "../../src/app/editor/state/fingerprints";

export interface FingerprintWork {
  steps: number;
  /** Calls not measured: they warm the JIT. */
  warmup: number;
  /** Measured calls. */
  calls: number;
}

/** The workflow of fingerprints.test.ts: each step reads the one before it and carries a 200-character literal. */
export function manySteps(count: number, edit = 0): Workflow {
  const steps: Step[] = Array.from({ length: count }, (_, index) => ({
    id: `step-${index}`,
    kind: "action",
    uses: "crm.lookup@1.0.0",
    with: {
      object: {
        id: index
          ? { ref: `/steps/step-${index - 1}/output/id` }
          : { ref: "/input/id" },
        // One step's literal differs per call, as typing in it would.
        note: {
          literal:
            index === edit % count
              ? `${"x".repeat(199)}${edit % 10}`
              : "x".repeat(200),
        },
      },
    },
  }));
  return {
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "orders", version: "1.0.0" },
    spec: {
      inputSchema: { type: "object" },
      outputSchema: {},
      steps,
      output: { literal: null },
    },
  } as unknown as Workflow;
}

/** Each measured call's time in milliseconds, and how many steps each call hashed. */
export function timeFingerprints(work: FingerprintWork): {
  callsMs: number[];
  hashed: number;
} {
  const run = (edit: number) => {
    const workflow = manySteps(work.steps, edit);
    const started = performance.now();
    const prints = fingerprints({
      workflow,
      testInput: { id: "c-1" },
      contractOf: (step) => String(step["uses"] ?? ""),
    });
    return { ms: performance.now() - started, hashed: prints.size };
  };
  for (let edit = 0; edit < work.warmup; edit++) run(edit);
  const callsMs: number[] = [];
  let hashed = 0;
  for (let edit = 0; edit < work.calls; edit++) {
    const result = run(work.warmup + edit);
    callsMs.push(result.ms);
    hashed = result.hashed;
  }
  return { callsMs, hashed };
}
