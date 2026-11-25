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
// Every walk of a workflow's steps sees the steps inside every block kind:
// a decision's paths and Otherwise, a parallel branch, a loop's body and a
// step's error path, nested in each other.
import "@angular/compiler";
import { beforeAll, describe, expect, it } from "vitest";
import { stringify } from "yaml";
import { calledReferenceSites } from "../src/app/editor/activation/callee-pins";
import { missingConditions } from "../src/app/designer/conditions";
import { locateDiagnostic } from "../src/app/designer/diagnostic-location";
import { computeLaneLayout } from "../src/app/designer/lane-layout";
import { enclosingLoops, stepPointerOf } from "../src/app/editor/formula/loops";
import { stepPath } from "../src/app/editor/ndv/edits";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import {
  breadcrumb,
  neighbors,
  outlineOrder,
} from "../src/app/editor/ndv/navigation";
import {
  allSteps,
  childLists,
  withChildIds,
} from "../src/app/editor/ndv/step-walk";
import { readClipboard } from "../src/app/editor/state/clipboard";
import { sliceStepIds } from "../src/app/editor/state/execute-request";
import { ERROR_PATH, blocksOf } from "../src/app/editor/step-blocks";
import {
  StructuredCanvasAdapter,
  freshWorkflow,
  kinds,
  sequencesOf,
  type Step,
  type Workflow,
} from "../src/app/model";

beforeAll(() => loadKindRegistrations());

const transform = (id: string, ref = "/input"): Step => ({
  id,
  kind: "transform",
  value: { ref },
});
const action = (id: string, extra: Partial<Step> = {}): Step => ({
  id,
  kind: "action",
  uses: "lookup@1.0.0",
  with: { literal: {} },
  ...extra,
});
const withErrorPath = (id: string, steps: Step[]): Step =>
  action(id, {
    onError: "errorOutput",
    errorPath: { steps, output: { literal: null } },
  });

/**
 * A step nested through each block kind: route's first path holds a
 * parallel step, whose branch holds a loop, whose body holds a step with an
 * error path; submit's error path holds two steps and a call.
 */
function nested(): Workflow {
  const workflow = freshWorkflow();
  workflow.spec.steps = [
    transform("intake"),
    {
      id: "route",
      kind: "switch",
      cases: [
        {
          when: { literal: true },
          output: { literal: {} },
          steps: [
            {
              id: "fan",
              kind: "parallel",
              concurrency: 2,
              branches: {
                left: {
                  output: { literal: {} },
                  steps: [
                    {
                      id: "orders",
                      kind: "forEach",
                      items: { ref: "/input/orders" },
                      concurrency: 1,
                      maxItems: 1000,
                      collect: "all",
                      body: {
                        output: { ref: "/item" },
                        steps: [withErrorPath("check", [transform("flag")])],
                      },
                    },
                  ],
                },
              },
            },
          ],
        },
      ],
      default: { output: { literal: {} }, steps: [transform("other")] },
    },
    withErrorPath("submit", [
      transform("notify-team", "/steps/submit/error/code"),
      transform("flag-request", "/steps/notify-team/output"),
      {
        id: "tell",
        kind: "callWorkflow",
        uses: "notify-customer@1.0.0",
        with: { literal: {} },
        mode: "wait",
      },
    ]),
    transform("after", "/steps/submit/output"),
  ];
  return workflow;
}
/** Every step inside a block of another step, each in one block kind. */
const NESTED = [
  "fan",
  "other",
  "orders",
  "check",
  "flag",
  "notify-team",
  "flag-request",
  "tell",
];
/** Where each nested step sits in the document. */
const POINTERS: Record<string, string> = {
  fan: "/spec/steps/1/cases/0/steps/0",
  other: "/spec/steps/1/default/steps/0",
  orders: "/spec/steps/1/cases/0/steps/0/branches/left/steps/0",
  check: "/spec/steps/1/cases/0/steps/0/branches/left/steps/0/body/steps/0",
  flag: "/spec/steps/1/cases/0/steps/0/branches/left/steps/0/body/steps/0/errorPath/steps/0",
  "notify-team": "/spec/steps/2/errorPath/steps/0",
  "flag-request": "/spec/steps/2/errorPath/steps/1",
  tell: "/spec/steps/2/errorPath/steps/2",
};
function opened(workflow: Workflow) {
  const model = new StructuredCanvasAdapter();
  model.setSource(JSON.stringify(workflow, null, 2), "json");
  expect(model.error).toBe("");
  return model;
}

describe("the blocks of a step", () => {
  it("lists a step's own blocks in document order, then its error path", () => {
    const [, route, submit] = nested().spec.steps;
    expect(
      blocksOf(route).map(({ name, path, kind }) => ({ name, path, kind })),
    ).toEqual([
      { name: "case 1", path: ["cases", 0], kind: "case" },
      { name: "default", path: ["default"], kind: "default" },
    ]);
    expect(blocksOf(submit)).toEqual([
      {
        name: ERROR_PATH,
        path: ["errorPath"],
        kind: "errorPath",
        branch: submit["errorPath"],
      },
    ]);
    expect(blocksOf(transform("plain"))).toEqual([]);
  });

  it("names a parallel step's branches, a loop's body and a transaction's own steps", () => {
    const parallel: Step = {
      id: "fan",
      kind: "parallel",
      branches: {
        second: { steps: [], output: null },
        first: { steps: [], output: null },
      },
    };
    expect(blocksOf(parallel).map((block) => block.path)).toEqual([
      ["branches", "second"],
      ["branches", "first"],
    ]);
    const loop: Step = {
      id: "orders",
      kind: "forEach",
      body: { steps: [], output: null },
    };
    expect(blocksOf(loop).map((block) => [block.name, block.kind])).toEqual([
      ["body", "body"],
    ]);
    const transaction = {
      id: "book",
      kind: "transaction",
      steps: [],
      output: null,
      onError: "errorOutput",
      errorPath: { steps: [], output: null },
    } as unknown as Step;
    expect(blocksOf(transaction).map((block) => block.path)).toEqual([
      [],
      ["errorPath"],
    ]);
  });
});

describe("every walk finds the steps inside each block kind", () => {
  it("lists each block as a sequence, the error path as <step>/errorPath", () => {
    const sequences = sequencesOf(nested());
    expect(sequences.get("submit/errorPath")!.map((step) => step.id)).toEqual([
      "notify-team",
      "flag-request",
      "tell",
    ]);
    expect(sequences.get("check/errorPath")!.map((step) => step.id)).toEqual([
      "flag",
    ]);
    const all = [...sequences.values()].flat().map((step) => step.id);
    for (const id of NESTED) expect(all, id).toContain(id);
  });

  it("knows every step ID and draws every step in the designer", () => {
    const model = opened(nested());
    const ids = model.stepIdSet();
    const drawn = computeLaneLayout(nested()).nodes.map((node) => node.step.id);
    for (const id of NESTED) {
      expect(ids.has(id), id).toBe(true);
      expect(drawn, id).toContain(id);
    }
    expect(
      model
        .nodes()
        .filter((node) => node.owner === "submit/errorPath")
        .map((node) => [node.step.id, node.index]),
    ).toEqual([
      ["notify-team", 0],
      ["flag-request", 1],
      ["tell", 2],
    ]);
  });

  it("gives each step in Outline order a breadcrumb", () => {
    const workflow = nested();
    expect(outlineOrder(workflow)).toEqual([
      "intake",
      "route",
      "fan",
      "orders",
      "check",
      "flag",
      "other",
      "submit",
      "notify-team",
      "flag-request",
      "tell",
      "after",
    ]);
    for (const id of NESTED) expect(breadcrumb(workflow, id), id).not.toBe("");
    expect(breadcrumb(workflow, "notify-team")).toBe(
      "Error path of submit · step 1 of 3",
    );
    expect(breadcrumb(workflow, "flag")).toBe(
      "Error path of check · step 1 of 1",
    );
  });

  it("runs on after a step with an error path whether or not it failed", () => {
    const workflow = nested();
    expect(neighbors(workflow, "submit").after).toEqual([
      "notify-team",
      "after",
    ]);
    expect(neighbors(workflow, "after").before).toEqual(["tell", "submit"]);
    expect(neighbors(workflow, "tell").after).toEqual(["after"]);
  });

  it("walks every step for step details, each with its blocks' IDs only", () => {
    const steps = nested().spec.steps;
    const walked = allSteps(steps).map((step) => step.id);
    for (const id of NESTED) expect(walked, id).toContain(id);
    const submit = steps[2];
    expect(childLists(submit).map((list) => list.map((s) => s.id))).toEqual([
      ["notify-team", "flag-request", "tell"],
    ]);
    expect(withChildIds(submit)["errorPath"]).toEqual({
      steps: ["notify-team", "flag-request", "tell"],
      output: { literal: null },
    });
    // The step itself is untouched.
    expect((submit["errorPath"] as { steps: Step[] }).steps[0].id).toBe(
      "notify-team",
    );
  });

  it("finds where each step sits, for edits and for the loops around it", () => {
    const workflow = nested();
    for (const id of NESTED) {
      expect(stepPointerOf(workflow, id), id).toBe(POINTERS[id]);
      expect("/" + (stepPath(workflow, id) ?? []).join("/"), id).toBe(
        POINTERS[id],
      );
    }
    expect(enclosingLoops(workflow, "flag")).toEqual(["orders"]);
    expect(enclosingLoops(workflow, "notify-team")).toEqual([]);
  });

  it("checks the steps inside an error path when they are pasted", () => {
    const steps = nested().spec.steps;
    // A platform with workflow calls: the error path holds one.
    const known = new Set([...kinds, "callWorkflow"]);
    const reading = readClipboard(stringify(steps), known);
    expect(reading.ok ? "" : reading.reason).toBe("");
    const broken = structuredClone(steps);
    (broken[2]["errorPath"] as { steps: Step[] }).steps.push({
      id: "odd",
      kind: "teleport",
    } as unknown as Step);
    expect(readClipboard(stringify(broken), known)).toEqual({
      ok: false,
      reason: "This paste has a step kind Studio doesn't know: teleport.",
    });
  });

  it("keeps an error path's steps up to the target when Execute step slices", () => {
    const workflow = nested();
    expect(sliceStepIds(workflow, "flag-request")).toEqual([
      "intake",
      "route",
      "fan",
      "orders",
      "check",
      "flag",
      "other",
      "submit",
      "notify-team",
      "flag-request",
    ]);
    expect(sliceStepIds(workflow, "after")).toContain("tell");
    // The call inside the path is called in the platform's order.
    expect(calledReferenceSites(workflow)).toEqual([
      {
        reference: "notify-customer@1.0.0",
        path: "/spec/steps/2/errorPath/steps/2/uses",
      },
    ]);
  });

  it("points a diagnostic inside an error path at its step", () => {
    const workflow = nested();
    expect(
      locateDiagnostic("/spec/steps/2/errorPath/steps/1/value", workflow),
    ).toMatchObject({
      stepId: "flag-request",
      stepPointer: POINTERS["flag-request"],
      fieldPath: "/value",
      exact: true,
    });
    expect(
      locateDiagnostic(`${POINTERS["flag"]}/value/ref`, workflow),
    ).toMatchObject({ stepId: "flag", exact: true });
    // Result on error is a field of the step itself.
    expect(
      locateDiagnostic("/spec/steps/2/errorPath/output/literal", workflow),
    ).toMatchObject({
      stepId: "submit",
      field: ["errorPath", "output"],
      exact: true,
    });
  });

  it("finds a decision without a condition inside an error path", () => {
    const workflow = nested();
    (workflow.spec.steps[2]["errorPath"] as { steps: Step[] }).steps.push({
      id: "triage",
      kind: "switch",
      cases: [{ steps: [], output: { literal: {} } }],
      default: { steps: [], output: { literal: {} } },
    });
    expect(missingConditions(workflow)).toEqual([
      {
        stepId: "triage",
        caseIndex: 0,
        stepPointer: "/spec/steps/2/errorPath/steps/3",
        pointer: "/spec/steps/2/errorPath/steps/3/cases/0/when",
      },
    ]);
  });
});
