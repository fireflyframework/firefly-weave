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
// Which transaction a place is in.
import { describe, expect, it } from "vitest";
import { enclosingTransaction } from "../src/app/editor/transactions/group";
import { freshWorkflow, sequencesOf, type Step } from "../src/app/model";

const step = (value: Record<string, unknown>) => value as unknown as Step;
const transform = (id: string) =>
  step({ id, kind: "transform", value: { literal: {} } });
/**
 * move holds hold, add (with an error path) and each (a loop) in a
 * transaction with an error path of its own; notify comes after it.
 */
function nested() {
  const workflow = freshWorkflow();
  workflow.spec.steps = [
    step({
      id: "move",
      kind: "transaction",
      steps: [
        transform("hold"),
        step({
          ...transform("add"),
          onError: "errorOutput",
          errorPath: { steps: [transform("flag")], output: { literal: null } },
        }),
        step({
          id: "each",
          kind: "forEach",
          items: { literal: [] },
          body: { steps: [transform("send")], output: { ref: "/item" } },
        }),
      ],
      output: { ref: "/steps/add/output" },
      onError: "errorOutput",
      errorPath: { steps: [transform("tell")], output: { literal: null } },
    }),
    transform("notify"),
  ];
  return workflow;
}

describe("the transaction a place is in", () => {
  it("is the one whose own steps hold it, at any depth, and never its error path", () => {
    const workflow = nested();
    const inside = (owner: string) =>
      enclosingTransaction(workflow, owner)?.id ?? null;
    expect(
      [...sequencesOf(workflow).keys()].map((owner) => [owner, inside(owner)]),
    ).toEqual([
      ["root", null],
      ["move/steps", "move"],
      ["add/errorPath", "move"],
      ["each/body", "move"],
      ["move/errorPath", null],
    ]);
    expect(inside("gone/steps")).toBeNull();
  });
});
