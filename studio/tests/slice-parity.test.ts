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
// Studio lists the steps an execution runs with its own mirror of the host's
// slice; the host's answers for the shared workflow are in slice-cases.json,
// written by scripts/studio_slice_cases.py.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { sliceStepIds } from "../src/app/editor/state/execute-request";
import type { Workflow } from "../src/app/model";

const workflow = JSON.parse(
  readFileSync(resolve("../tests/fixtures/studio/slice-workflow.json"), "utf8"),
) as Workflow;
const { cases } = JSON.parse(
  readFileSync(resolve("tests/fixtures/slice-cases.json"), "utf8"),
) as { cases: { stepId: string; kept: string[] }[] };

describe("Studio's slice", () => {
  it("covers every step of the shared workflow", () => {
    expect(cases.length).toBe(14);
  });
  for (const { stepId, kept } of cases)
    it(`keeps what the host keeps for ${stepId}`, () => {
      expect(sliceStepIds(workflow, stepId)).toEqual(kept);
    });
});
