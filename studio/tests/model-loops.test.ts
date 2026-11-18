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
// A loop's steps live in its body, the sequence "<loop>/body": every edit
// the model makes to a decision's or a parallel's steps works there too.
import { describe, expect, it } from "vitest";
import {
  StructuredCanvasAdapter,
  createStep,
  freshWorkflow,
  ownerLabel,
  renamedSteps,
  stepIdPrefix,
  type Step,
  type Workflow,
} from "../src/app/model";

/** A workflow whose loop over input.orders sends a message for each order. */
function withLoop(body: Step[] = [], extra: Partial<Step> = {}): Workflow {
  const workflow = freshWorkflow();
  workflow.spec.steps = [
    { id: "fetch", kind: "transform", value: { literal: [] } },
    {
      ...createStep("forEach", "orders"),
      items: { ref: "/steps/fetch/output" },
      ...extra,
      body: { steps: body, output: { ref: "/item" } },
    },
  ];
  return workflow;
}
function opened(workflow: Workflow) {
  const model = new StructuredCanvasAdapter();
  model.setSource(JSON.stringify(workflow, null, 2), "json");
  expect(model.error).toBe("");
  return model;
}
const send = (id = "send"): Step => ({
  id,
  kind: "transform",
  value: { ref: "/item" },
});
const loopOf = (model: StructuredCanvasAdapter, id = "orders") =>
  model.nodes().find((node) => node.step.id === id)!.step;
const bodyIds = (model: StructuredCanvasAdapter, owner = "orders/body") =>
  model
    .owners()
    .get(owner)!
    .map((step) => step.id);

describe("a loop in the workflow model", () => {
  it("is written out with its defaults and no items", () => {
    expect(createStep("forEach", "loop-1")).toEqual({
      id: "loop-1",
      kind: "forEach",
      concurrency: 1,
      maxItems: 1000,
      collect: "all",
      body: { steps: [], output: { ref: "/item" } },
    });
  });

  it("gets an ID that starts with loop", () => {
    const model = new StructuredCanvasAdapter();
    expect(stepIdPrefix.forEach).toBe("loop");
    expect(model.insert("forEach").id).toBe("loop-1");
    expect(model.insert("forEach").id).toBe("loop-2");
  });

  it("opens for editing", () => {
    const model = opened(withLoop([send()]));
    expect(model.readonly).toBe(false);
    expect(model.nodes().map((node) => [node.step.id, node.owner])).toEqual(
      expect.arrayContaining([
        ["orders", "root"],
        ["send", "orders/body"],
      ]),
    );
  });

  it("holds its steps in the sequence <loop>/body, labeled For each item of <loop>", () => {
    const model = opened(withLoop([send()]));
    expect([...model.owners().keys()]).toEqual(["root", "orders/body"]);
    const steps = new Map(
      model.nodes().map((node) => [node.step.id, node.step]),
    );
    expect(ownerLabel("orders/body", steps)).toBe("For each item of orders");
  });

  it("puts a step inserted into its body there", () => {
    const model = opened(withLoop([send()]));
    const wait = model.insert("wait", "orders/body", 0);
    expect(bodyIds(model)).toEqual([wait.id, "send"]);
    expect(
      (loopOf(model)["body"] as { steps: Step[] }).steps.map((s) => s.id),
    ).toEqual([wait.id, "send"]);
  });

  it("moves a step out of its body and back in", () => {
    const model = opened(withLoop([send()]));
    model.move("send", "root", 0);
    expect(bodyIds(model)).toEqual([]);
    expect(
      model
        .owners()
        .get("root")!
        .map((s) => s.id),
    ).toEqual(["send", "fetch", "orders"]);
    model.move("send", "orders/body", 0);
    expect(bodyIds(model)).toEqual(["send"]);
    expect(() => model.move("orders", "orders/body", 0)).toThrow(
      "A step cannot contain itself.",
    );
  });

  it("keeps its steps when it is renamed, and its body follows the new name", () => {
    const model = opened(withLoop([send()]));
    model.renameStep("orders", "each-order");
    expect(bodyIds(model, "each-order/body")).toEqual(["send"]);
    expect(model.owners().has("orders/body")).toBe(false);
  });

  it("renames a step inside its body, and the references to it", () => {
    const model = opened(
      withLoop([
        send(),
        { id: "log", kind: "transform", value: { ref: "/steps/send/output" } },
      ]),
    );
    expect(model.renameStep("send", "notify")).toBe(1);
    expect(bodyIds(model)).toEqual(["notify", "log"]);
    const log = model.owners().get("orders/body")![1];
    expect(log["value"]).toEqual({ ref: "/steps/notify/output" });
    expect(JSON.parse(model.source).spec.steps[1].body.steps[0].id).toBe(
      "notify",
    );
  });

  it("rewrites its items when the step they read is renamed", () => {
    const model = opened(withLoop([send()]));
    expect(model.renameStep("fetch", "load")).toBe(1);
    expect(loopOf(model)["items"]).toEqual({ ref: "/steps/load/output" });
  });

  it("tells a rename inside its body as a rename, and a different body as a change", () => {
    const before = withLoop([send()]);
    const after = structuredClone(before);
    (after.spec.steps[1]["body"] as { steps: Step[] }).steps[0].id = "notify";
    expect(renamedSteps(before, after)).toEqual([
      { from: "send", to: "notify" },
    ]);
    const added = structuredClone(before);
    (added.spec.steps[1]["body"] as { steps: Step[] }).steps.push(send("more"));
    expect(renamedSteps(before, added)).toEqual([]);
  });

  it("is refused without a body to hold its steps", () => {
    const model = new StructuredCanvasAdapter();
    const workflow = withLoop();
    delete workflow.spec.steps[1]["body"];
    model.setSource(JSON.stringify(workflow), "json");
    expect(model.error).toBe("Invalid branches in orders.");
    expect(model.readonly).toBe(true);
  });

  it("is deleted with its steps when asked, and Undo puts them back", () => {
    const model = opened(withLoop([send()]));
    const before = model.source;
    expect(model.containedSteps("orders")).toBe(1);
    expect(() => model.remove("orders")).toThrow(
      "Move or delete the contained branch steps before deleting this group.",
    );
    model.remove("orders", { contents: true });
    expect(model.stepIdSet()).toEqual(new Set(["fetch"]));
    model.undo();
    expect(model.source).toBe(before);
    expect(bodyIds(model)).toEqual(["send"]);
  });

  it("copies its steps with new IDs when it is duplicated", () => {
    const model = opened(withLoop([send()]));
    const [copy] = model.duplicateRun(["orders"]);
    expect(copy).toBe("orders-1");
    expect(bodyIds(model, "orders-1/body")).toEqual(["send-1"]);
    expect(bodyIds(model)).toEqual(["send"]);
  });

  it("links the step before it to its first step and its last step to the step after", () => {
    const workflow = withLoop([send(), send("log")]);
    workflow.spec.steps.push({ id: "done", kind: "wait", durationSeconds: 1 });
    const links = opened(workflow).visualConnections();
    expect(links).toEqual(
      expect.arrayContaining([
        { from: "orders", to: "send" },
        { from: "send", to: "log" },
        { from: "log", to: "done" },
      ]),
    );
  });

  it("holds nested loops: the outer body holds the inner loop, whose body holds the step", () => {
    const inner = {
      ...createStep("forEach", "lines"),
      items: { ref: "/item/lines" },
      body: { steps: [send()], output: { ref: "/item" } },
    };
    const model = opened(withLoop([inner as Step]));
    expect(bodyIds(model)).toEqual(["lines"]);
    expect(bodyIds(model, "lines/body")).toEqual(["send"]);
    expect(model.nodes().find((node) => node.step.id === "send")?.owner).toBe(
      "lines/body",
    );
    model.move("send", "orders/body", 0);
    expect(bodyIds(model)).toEqual(["send", "lines"]);
  });
});
