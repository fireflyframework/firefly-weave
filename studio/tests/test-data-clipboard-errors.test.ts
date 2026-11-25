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
// Copied steps carry the failures simulated on them by hand, under their
// new IDs once pasted; failures recorded from a run stay behind, as pins
// recorded from a run do (the OS clipboard is outside the run.read check).
import { describe, expect, it } from "vitest";
import {
  copySteps,
  planPaste,
  readClipboard,
} from "../src/app/editor/state/clipboard";
import type { ErrorEntry } from "../src/app/editor/state/test-data";
import { freshWorkflow, type Step, type Workflow } from "../src/app/model";

const transform = (id: string): Step => ({
  id,
  kind: "transform",
  value: { literal: 1 },
  onError: "continue",
});
function workflow(): Workflow {
  const document = freshWorkflow();
  document.spec.steps = [transform("submit"), transform("after")];
  return document;
}
const MANUAL: ErrorEntry = { code: "REQUEST_REFUSED", source: "manual" };
const FROM_RUN: ErrorEntry = {
  code: "TIMEOUT",
  source: "run",
  runId: "8f0f6b0e-6a43-4f0e-9a43-6b1f7a2f9c11",
  runEnvironmentId: "1b0f6b0e-6a43-4f0e-9a43-6b1f7a2f9c12",
};

describe("simulated failures on copied steps", () => {
  it("carries only the failures set by hand, and pastes them under the new step ID", () => {
    const copied = copySteps(
      workflow(),
      ["submit"],
      {},
      {},
      {
        submit: MANUAL,
        "submit[2]": FROM_RUN,
        "submit[3]": { code: "UNAVAILABLE" },
        after: MANUAL,
      },
    );
    expect(copied.document.errors).toEqual({
      submit: MANUAL,
      "submit[3]": { code: "UNAVAILABLE" },
    });
    const reading = readClipboard(copied.yaml);
    if (!reading.ok) throw Error(reading.reason);
    const plan = planPaste(
      workflow(),
      reading.content,
      { owner: "root", index: 2 },
      new Set(["submit", "after"]),
    );
    if ("reason" in plan) throw Error(plan.reason);
    expect(plan.ids).toEqual(["submit-1"]);
    expect(plan.errors).toEqual({
      "submit-1": MANUAL,
      "submit-1[3]": { code: "UNAVAILABLE" },
    });
  });

  it("leaves a run's failure behind even when the clipboard brings one", () => {
    const reading = readClipboard(
      JSON.stringify({
        weaveClipboard: 1,
        steps: [transform("submit")],
        connections: {},
        llmProfiles: {},
        pins: {},
        errors: { submit: FROM_RUN },
      }),
    );
    if (!reading.ok) throw Error(reading.reason);
    const plan = planPaste(
      workflow(),
      reading.content,
      { owner: "root", index: 2 },
      new Set(["submit", "after"]),
    );
    if ("reason" in plan) throw Error(plan.reason);
    expect(plan.errors).toEqual({});
  });
});
