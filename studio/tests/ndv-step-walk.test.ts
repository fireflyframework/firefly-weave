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
import { describe, expect, it } from "vitest";
import type { Kind, Step } from "../src/app/model";
import { allSteps, findStep } from "../src/app/editor/ndv/step-walk";

const branch = (...steps: Step[]) => ({ steps, output: { literal: {} } });
const action = (id: string, uses = `${id}@1.0.0`): Step => ({
  id,
  kind: "action",
  uses,
  with: { literal: {} },
});

/** first, a decision (two cases and Otherwise), a parallel step whose first branch holds another decision, last. */
function workflowSteps(): Step[] {
  return [
    action("first"),
    {
      id: "decide",
      kind: "switch",
      cases: [
        { when: { literal: true }, ...branch(action("in-case-1")) },
        { when: { literal: false }, ...branch(action("in-case-2")) },
      ],
      default: branch(action("in-otherwise")),
    },
    {
      id: "fan-out",
      kind: "parallel",
      branches: {
        left: branch(action("in-left"), {
          id: "inner",
          kind: "switch",
          cases: [{ when: { literal: true }, ...branch(action("deep")) }],
          default: branch(),
        }),
        right: branch(action("in-right")),
      },
      concurrency: 2,
    },
    action("last"),
  ];
}

describe("walking the steps of a workflow", () => {
  it("lists every step depth-first, containers before what they hold", () => {
    expect(allSteps(workflowSteps()).map((step) => step.id)).toEqual([
      "first",
      "decide",
      "in-case-1",
      "in-case-2",
      "in-otherwise",
      "fan-out",
      "in-left",
      "inner",
      "deep",
      "in-right",
      "last",
    ]);
    expect(allSteps([])).toEqual([]);
  });
  it("finds a step in a decision case, in Otherwise and in a parallel branch, at any depth", () => {
    const steps = workflowSteps();
    for (const id of ["in-case-2", "in-otherwise", "in-right", "deep"])
      expect(
        findStep(steps, (step) => step["uses"] === `${id}@1.0.0`)?.id,
      ).toBe(id);
    expect(findStep(steps, (step) => step.id === "decide")?.kind).toBe(
      "switch",
    );
  });
  it("returns the step itself, so an edit through it changes the workflow", () => {
    const steps = workflowSteps();
    const deep = findStep(steps, (step) => step.id === "deep");
    expect(deep).toBe(allSteps(steps).find((step) => step.id === "deep"));
    deep!["uses"] = "changed@1.0.0";
    expect(
      findStep(steps, (step) => step["uses"] === "changed@1.0.0")?.id,
    ).toBe("deep");
  });
  it("returns null when no step matches, and the first match in order when several do", () => {
    const steps = [
      action("a", "shared@1.0.0"),
      ...workflowSteps(),
      action("z", "shared@1.0.0"),
    ];
    expect(
      findStep(steps, (step) => step["uses"] === "nothing@1.0.0"),
    ).toBeNull();
    expect(findStep(steps, (step) => step["uses"] === "shared@1.0.0")?.id).toBe(
      "a",
    );
    expect(findStep([], () => true)).toBeNull();
  });
  it("stops at the first match", () => {
    const seen: string[] = [];
    findStep(workflowSteps(), (step) => {
      seen.push(step.id);
      return step.id === "in-case-1";
    });
    expect(seen).toEqual(["first", "decide", "in-case-1"]);
  });
  it("walks the body of a loop", () => {
    const steps: Step[] = [
      {
        id: "each",
        kind: "forEach" as Kind,
        items: { ref: "/input/orders" },
        body: branch(action("per-item"), {
          id: "per-item-decision",
          kind: "switch",
          cases: [],
          default: branch(action("per-item-otherwise")),
        }),
      },
      action("after"),
    ];
    expect(allSteps(steps).map((step) => step.id)).toEqual([
      "each",
      "per-item",
      "per-item-decision",
      "per-item-otherwise",
      "after",
    ]);
  });
  it("never walks data that happens to hold a list called steps", () => {
    const lookalike = { id: "x", kind: "action", uses: "x@1.0.0" };
    const steps: Step[] = [
      {
        id: "carry",
        kind: "transform",
        value: { literal: { steps: [lookalike, { n: 2 }] } },
      },
      {
        id: "call",
        kind: "action",
        uses: "real@1.0.0",
        with: { object: { payload: { literal: { steps: [lookalike] } } } },
      },
      {
        id: "decide",
        kind: "switch",
        cases: [
          {
            when: { literal: true },
            steps: [],
            output: { literal: { steps: [lookalike] } },
          },
        ],
        default: {
          steps: [],
          output: { object: { rows: { literal: { steps: [lookalike] } } } },
        },
      },
      {
        id: "fan-out",
        kind: "parallel",
        branches: {
          one: { steps: [], output: { literal: { steps: [lookalike] } } },
        },
        concurrency: 1,
      },
      { id: "plain", kind: "wait", durationSeconds: 5, steps: [lookalike] },
      {
        id: "stray",
        kind: "transform",
        cases: [{ steps: [lookalike] }],
        default: { steps: [lookalike] },
        branches: { one: { steps: [lookalike] } },
        body: { steps: [lookalike] },
        value: { literal: {} },
      },
    ];
    expect(allSteps(steps).map((step) => step.id)).toEqual([
      "carry",
      "call",
      "decide",
      "fan-out",
      "plain",
      "stray",
    ]);
    expect(findStep(steps, (step) => step["uses"] === "x@1.0.0")).toBeNull();
  });
  it("skips malformed containers instead of failing", () => {
    const steps = [
      { id: "a", kind: "switch", cases: "none", default: null },
      { id: "b", kind: "parallel", branches: [1, 2] },
      {
        id: "c",
        kind: "switch",
        cases: [null, 3, { steps: "no" }],
        default: { steps: [null, 4] },
      },
      { id: "d", kind: "forEach", body: "text" },
    ] as unknown as Step[];
    expect(allSteps(steps).map((step) => step.id)).toEqual([
      "a",
      "b",
      "c",
      "d",
    ]);
  });
});
