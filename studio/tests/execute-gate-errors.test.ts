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
// Execute step on a step inside an error path its step won't take: with
// this test data the step succeeds, so Studio asks to simulate its failure
// first, naming the outermost such step; none once every path around the
// target is taken, or when Studio can't simulate that step's failure.
import { describe, expect, it } from "vitest";
import { untakenPath } from "../src/app/editor/state/execute-gate";
import type { Step } from "../src/app/model";

const step = (id: string, extra: object = {}) =>
  ({ id, kind: "transform", ...extra }) as Step;
const STEPS = [
  step("submit", {
    onError: "errorOutput",
    errorPath: {
      steps: [
        step("notify-team", {
          onError: "errorOutput",
          errorPath: { steps: [step("page-ops")], output: null },
        }),
      ],
      output: null,
    },
  }),
  step("wait-reply", {
    kind: "signal",
    onError: "errorOutput",
    errorPath: { steps: [step("remind")], output: null },
  }),
  step("summarize"),
];
const failing =
  (...ids: string[]) =>
  (id: string) =>
    ids.includes(id);
const anyStep = () => true;

describe("Execute step inside an error path", () => {
  it("asks to simulate a failure of the step whose path holds the target", () => {
    expect(untakenPath(STEPS, "notify-team", failing(), anyStep)).toEqual({
      owner: "submit",
      stepId: "notify-team",
    });
    expect(
      untakenPath(STEPS, "notify-team", failing("submit"), anyStep),
    ).toBeNull();
  });

  it("names the outermost path that won't run, then the next one in", () => {
    expect(untakenPath(STEPS, "page-ops", failing(), anyStep)?.owner).toBe(
      "submit",
    );
    expect(
      untakenPath(STEPS, "page-ops", failing("submit"), anyStep)?.owner,
    ).toBe("notify-team");
    expect(
      untakenPath(STEPS, "page-ops", failing("submit", "notify-team"), anyStep),
    ).toBeNull();
  });

  it("asks nothing outside error paths, or for a step Studio can't fail", () => {
    expect(untakenPath(STEPS, "summarize", failing(), anyStep)).toBeNull();
    expect(untakenPath(STEPS, "submit", failing(), anyStep)).toBeNull();
    // A signal fails through its Time out answer: the host says why it isn't reached.
    expect(
      untakenPath(
        STEPS,
        "remind",
        failing(),
        (owner) => owner.kind !== "signal",
      ),
    ).toBeNull();
  });
});
