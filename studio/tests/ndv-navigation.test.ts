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
import "@angular/compiler";
import { readFileSync } from "node:fs";
import { beforeAll, describe, expect, it } from "vitest";
import { parse } from "yaml";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import {
  adjacent,
  breadcrumb,
  neighbors,
  outlineOrder,
  placeOf,
  stepsAfter,
} from "../src/app/editor/ndv/navigation";
import { ndvRegistry, registerKind } from "../src/app/editor/ndv/registry";
import type { Kind, Step, Workflow } from "../src/app/model";

const workflow = parse(
  readFileSync(
    new URL("./fixtures/step-details.yaml", import.meta.url),
    "utf8",
  ),
) as Workflow;
beforeAll(() => loadKindRegistrations());

/** The fixture with a wait step added right after the parallel group, inside Otherwise. */
function afterTheParallel(): Workflow {
  const copy = structuredClone(workflow);
  const route = copy.spec.steps[2] as Step;
  (route["default"] as { steps: Step[] }).steps.push({
    id: "settle",
    kind: "wait",
    durationSeconds: 5,
  });
  return copy;
}

describe("step details navigation", () => {
  it("orders steps as they run, entering paths and branches", () => {
    expect(outlineOrder(workflow)).toEqual([
      "check-customer",
      "lookup",
      "route-by-value",
      "notify-sales",
      "fan-out",
      "wait-a-minute",
      "summarize",
      "score",
      "wait-for-payment",
      "reject-order",
    ]);
    expect(adjacent(workflow, "route-by-value", 1)).toBe("notify-sales");
    expect(adjacent(workflow, "check-customer", -1)).toBeNull();
    expect(adjacent(workflow, "reject-order", 1)).toBeNull();
    expect(adjacent(workflow, "not-a-step", 1)).toBeNull();
  });
  it("says where a step is", () => {
    expect(placeOf(workflow, "lookup")).toEqual({
      id: "lookup",
      ownerStep: null,
      lane: "Main sequence",
      index: 1,
      count: 7,
    });
    expect(placeOf(workflow, "not-a-step")).toBeNull();
    expect(breadcrumb(workflow, "not-a-step")).toBe("");
    expect(breadcrumb(workflow, "lookup")).toBe("Main sequence · step 2 of 7");
    expect(breadcrumb(workflow, "notify-sales")).toBe(
      "Path amount > 1000 of route-by-value · step 1 of 1",
    );
    expect(breadcrumb(workflow, "fan-out")).toBe(
      "Otherwise of route-by-value · step 1 of 1",
    );
    expect(breadcrumb(workflow, "wait-a-minute")).toBe(
      "Branch ledger of fan-out · step 1 of 1",
    );
  });
  it("finds the steps that run right before and right after", () => {
    expect(neighbors(workflow, "check-customer")).toEqual({
      before: ["$trigger"],
      after: ["lookup"],
    });
    expect(neighbors(workflow, "route-by-value")).toEqual({
      before: ["lookup"],
      after: ["notify-sales", "summarize", "fan-out"],
    });
    expect(neighbors(workflow, "notify-sales")).toEqual({
      before: ["route-by-value"],
      after: ["summarize"],
    });
    expect(neighbors(workflow, "reject-order")).toEqual({
      before: ["wait-for-payment"],
      after: [],
    });
    expect(neighbors(workflow, "not-a-step")).toEqual({
      before: [],
      after: [],
    });
  });
  it("lists every path that leads into the step after a decision", () => {
    // Path 1 ends at notify-sales; path 2 is empty, so the run also comes straight
    // from the decision; Otherwise ends at the parallel group, whose branch
    // ledger ends at wait-a-minute while branch audit is empty.
    expect(neighbors(workflow, "summarize").before).toEqual([
      "notify-sales",
      "route-by-value",
      "wait-a-minute",
      "fan-out",
    ]);
    expect(neighbors(workflow, "wait-a-minute").after).toEqual(["summarize"]);
  });
  it("lists every branch that leads into the step after a parallel group", () => {
    const after = afterTheParallel();
    expect(neighbors(after, "settle").before).toEqual([
      "wait-a-minute",
      "fan-out",
    ]);
    expect(stepsAfter(after, "wait-a-minute")).toEqual(["settle"]);
    expect(neighbors(after, "fan-out").after).toEqual([
      "wait-a-minute",
      "settle",
    ]);
    // Otherwise now ends at the new step, not at the parallel group.
    expect(neighbors(after, "summarize").before).toEqual([
      "notify-sales",
      "route-by-value",
      "settle",
    ]);
  });
  it("counts a decision with no Otherwise as a way into the next step", () => {
    const copy = structuredClone(workflow);
    const route = copy.spec.steps[2] as Step;
    delete route["default"];
    route["cases"] = [
      {
        when: { literal: true },
        steps: [{ id: "only", kind: "wait", durationSeconds: 5 }],
        output: { literal: {} },
      },
    ];
    expect(neighbors(copy, "summarize").before).toEqual([
      "only",
      "route-by-value",
    ]);
    expect(neighbors(copy, "route-by-value").after).toEqual([
      "only",
      "summarize",
    ]);
  });
  it("walks the body of a loop like any other group of steps", () => {
    const wait = ndvRegistry.kind("wait")!;
    if (!ndvRegistry.kind("forEach"))
      registerKind({
        ...wait,
        kind: "forEach",
        idPrefix: "loop",
        containers: () => [
          {
            path: ["body", "steps"],
            layout: "frame",
            label: () => "Loop body",
          },
        ],
      });
    const copy = structuredClone(workflow);
    const loop = (id: string, steps: Step[]): Step => ({
      id,
      kind: "forEach" as Kind,
      body: { steps },
    });
    const pause = (id: string): Step => ({
      id,
      kind: "wait",
      durationSeconds: 5,
    });
    copy.spec.steps.splice(
      1,
      0,
      loop("each-order", [pause("first"), pause("last")]),
      loop("empty-loop", []),
    );
    expect(outlineOrder(copy).slice(0, 6)).toEqual([
      "check-customer",
      "each-order",
      "first",
      "last",
      "empty-loop",
      "lookup",
    ]);
    expect(breadcrumb(copy, "last")).toBe(
      "Loop body of each-order · step 2 of 2",
    );
    expect(neighbors(copy, "first").before).toEqual(["each-order"]);
    expect(neighbors(copy, "each-order").after).toEqual(["first"]);
    expect(neighbors(copy, "last").after).toEqual(["empty-loop"]);
    expect(neighbors(copy, "empty-loop").before).toEqual(["last"]);
    expect(neighbors(copy, "lookup").before).toEqual(["empty-loop"]);
    expect(neighbors(copy, "empty-loop").after).toEqual(["lookup"]);
  });
});
