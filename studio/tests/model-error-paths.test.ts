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
// A step's error path is the sequence "<step>/errorPath": the model edits
// its steps as it edits a decision's or a loop's, and the path goes with
// its step when the step is renamed, copied or deleted.
import { describe, expect, it } from "vitest";
import { planPaste, readClipboard } from "../src/app/editor/state/clipboard";
import {
  StructuredCanvasAdapter,
  branchTitle,
  freshWorkflow,
  ownerLabel,
  type Step,
  type Workflow,
} from "../src/app/model";
import { referenceScope } from "../src/app/forms/core/scope";
import { stringify } from "yaml";

const transform = (id: string, ref = "/input"): Step => ({
  id,
  kind: "transform",
  value: { ref },
});
/** lookup, then submit, whose error path notifies the team and flags the request, then after. */
function withErrorPath(): Workflow {
  const workflow = freshWorkflow();
  workflow.spec.steps = [
    transform("lookup"),
    {
      id: "submit",
      kind: "action",
      uses: "submit@1.0.0",
      with: { ref: "/steps/lookup/output" },
      onError: "errorOutput",
      errorPath: {
        steps: [
          transform("notify-team", "/steps/submit/error/code"),
          transform("flag-request", "/steps/notify-team/output"),
        ],
        output: { ref: "/steps/submit/error/message" },
      },
    },
    transform("after", "/steps/submit/output"),
  ];
  return workflow;
}
function opened(workflow: Workflow) {
  const model = new StructuredCanvasAdapter();
  model.setSource(JSON.stringify(workflow, null, 2), "json");
  expect(model.error).toBe("");
  return model;
}
const pathIds = (model: StructuredCanvasAdapter, owner = "submit/errorPath") =>
  model
    .owners()
    .get(owner)
    ?.map((step) => step.id);
const submitOf = (model: StructuredCanvasAdapter) =>
  model.definition.spec.steps.find((step) => step.id === "submit")!;

describe("a step's error path in the workflow model", () => {
  it("is named On error of its step, as Outline lists it", () => {
    const model = opened(withErrorPath());
    const steps = new Map(
      model.nodes().map((node) => [node.step.id, node.step]),
    );
    expect(ownerLabel("submit/errorPath", steps)).toBe("On error of submit");
    expect(ownerLabel("submit/errorPath")).toBe("On error of submit");
    expect(branchTitle(submitOf(model), "errorPath")).toBe("On error");
  });

  it("refuses a step ID the error path repeats", () => {
    const workflow = withErrorPath();
    (workflow.spec.steps[1]["errorPath"] as { steps: Step[] }).steps.push(
      transform("lookup"),
    );
    const model = new StructuredCanvasAdapter();
    model.setSource(JSON.stringify(workflow), "json");
    expect(model.error).toBe("Steps must have unique IDs.");
    expect(model.readonly).toBe(true);
  });

  it("refuses an error path without a list of steps", () => {
    const workflow = withErrorPath();
    workflow.spec.steps[1]["errorPath"] = { output: { literal: null } };
    const model = new StructuredCanvasAdapter();
    model.setSource(JSON.stringify(workflow), "json");
    expect(model.error).toBe("Invalid branches in submit.");
  });

  it("refuses a decision without paths with the words it uses for any block", () => {
    const workflow = freshWorkflow();
    workflow.spec.steps = [
      { id: "route", kind: "switch", default: { steps: [], output: null } },
    ];
    const model = new StructuredCanvasAdapter();
    model.setSource(JSON.stringify(workflow), "json");
    expect(model.error).toBe("Invalid branches in route.");
  });

  it("inserts, updates and moves steps inside the error path", () => {
    const model = opened(withErrorPath());
    model.insert("transform", "submit/errorPath", 2);
    expect(pathIds(model)).toEqual([
      "notify-team",
      "flag-request",
      "transform-1",
    ]);
    model.update(
      "flag-request",
      JSON.stringify(transform("flag-request", "/steps/submit/error/outcome")),
    );
    const flag = model.owners().get("submit/errorPath")![1];
    expect(flag["value"]).toEqual({ ref: "/steps/submit/error/outcome" });
    model.move("transform-1", "root", 3);
    expect(pathIds(model)).toEqual(["notify-team", "flag-request"]);
    expect(() => model.move("submit", "submit/errorPath", 0)).toThrow(
      "A step cannot contain itself.",
    );
  });

  it("rewrites the step's error and output references when the step is renamed", () => {
    const model = opened(withErrorPath());
    expect(model.renameStep("submit", "send")).toBe(3);
    const send = model.definition.spec.steps[1];
    const path = send["errorPath"] as { steps: Step[]; output: unknown };
    expect(path.steps[0]["value"]).toEqual({ ref: "/steps/send/error/code" });
    expect(path.output).toEqual({ ref: "/steps/send/error/message" });
    expect(model.definition.spec.steps[2]["value"]).toEqual({
      ref: "/steps/send/output",
    });
    expect(pathIds(model, "send/errorPath")).toEqual([
      "notify-team",
      "flag-request",
    ]);
  });

  it("rewrites a renamed step's references inside an error path", () => {
    const model = opened(withErrorPath());
    expect(model.renameStep("notify-team", "tell-team")).toBe(1);
    expect(pathIds(model)).toEqual(["tell-team", "flag-request"]);
    expect(model.owners().get("submit/errorPath")![1]["value"]).toEqual({
      ref: "/steps/tell-team/output",
    });
  });

  it("deletes the error path with its step in one undo step", () => {
    const model = opened(withErrorPath());
    const before = model.source;
    // The step reads nothing after it, so only the reference from after blocks it.
    model.update("after", JSON.stringify(transform("after")));
    model.remove("submit");
    expect([...model.stepIdSet()].sort()).toEqual(["after", "lookup"]);
    expect(model.owners().has("submit/errorPath")).toBe(false);
    model.undo();
    expect(pathIds(model)).toEqual(["notify-team", "flag-request"]);
    model.undo();
    expect(model.source).toBe(before);
  });

  it("copies a step with its error path and new IDs inside it", () => {
    const model = opened(withErrorPath());
    const copy = model.duplicate("submit");
    expect(copy).toBe("submit-1");
    expect(pathIds(model, "submit-1/errorPath")).toEqual([
      "notify-team-1",
      "flag-request-1",
    ]);
    const copied = model.definition.spec.steps[2];
    const path = copied["errorPath"] as { steps: Step[]; output: unknown };
    expect(path.steps[0]["value"]).toEqual({
      ref: "/steps/submit-1/error/code",
    });
    expect(path.steps[1]["value"]).toEqual({
      ref: "/steps/notify-team-1/output",
    });
    expect(path.output).toEqual({ ref: "/steps/submit-1/error/message" });
    // The original keeps its own.
    expect(pathIds(model)).toEqual(["notify-team", "flag-request"]);
  });

  it("pastes a step with its error path, renumbering the IDs inside it", () => {
    const workflow = withErrorPath();
    const reading = readClipboard(stringify([workflow.spec.steps[1]]));
    if (!reading.ok) throw Error(reading.reason);
    const plan = planPaste(
      workflow,
      reading.content,
      { owner: "root", index: 3 },
      new Set(["lookup", "submit", "notify-team", "flag-request", "after"]),
    );
    if ("reason" in plan) throw Error(plan.reason);
    expect(plan.renamed).toEqual({
      submit: "submit-1",
      "notify-team": "notify-team-1",
      "flag-request": "flag-request-1",
    });
    const path = plan.steps[0]["errorPath"] as {
      steps: Step[];
      output: unknown;
    };
    expect(path.steps.map((step) => step.id)).toEqual([
      "notify-team-1",
      "flag-request-1",
    ]);
    expect(path.steps[0]["value"]).toEqual({
      ref: "/steps/submit-1/error/code",
    });
    expect(path.output).toEqual({ ref: "/steps/submit-1/error/message" });
  });

  it("offers the step's error to a step moved out of its path only as maybe missing, as the compiler warns", () => {
    const model = opened(withErrorPath());
    const errorOf = () =>
      referenceScope(model.definition, "notify-team", "/value").entries.find(
        (entry) => entry.ref === "/steps/submit/error",
      );
    // Inside the path submit has failed: its error is always there.
    expect(errorOf()?.optional).toBe(false);
    model.move("notify-team", "root", 3);
    expect(pathIds(model)).toEqual(["flag-request"]);
    // After submit it may have succeeded: the compiler warns that the
    // read's presence is unproved (WV-COMP-REFERENCE_PRESENCE).
    expect(errorOf()?.optional).toBe(true);
    model.move("notify-team", "root", 0);
    // Before submit there is no error to read at all.
    expect(errorOf()).toBeUndefined();
  });

  it("connects the step to its error path and, when it succeeds, to the next step", () => {
    const model = opened(withErrorPath());
    const links = model.visualConnections();
    expect(links).toContainEqual({ from: "submit", to: "notify-team" });
    expect(links).toContainEqual({ from: "submit", to: "after" });
    expect(links).toContainEqual({ from: "flag-request", to: "after" });
  });
});
