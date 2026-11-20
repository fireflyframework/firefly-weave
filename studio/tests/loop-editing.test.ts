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
// Editing the steps inside a loop: copy and paste a loop with its body,
// paste into a body, select and move steps in and out of it.
import { describe, expect, it } from "vitest";
import {
  copySteps,
  planPaste,
  readClipboard,
  type ClipboardContent,
  type PastePlan,
} from "../src/app/editor/state/clipboard";
import {
  contiguity,
  extended,
  moveAllowed,
  only,
  selectionOf,
  type StepPlace,
} from "../src/app/editor/state/selection";
import { layoutLtr } from "../src/app/editor/canvas/layout-ltr";
import { placesOf } from "../src/app/editor/canvas/navigation";
import { StructuredCanvasAdapter, type Step } from "../src/app/model";

const source = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: order-notify, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {type: object}
  steps:
    - {id: fetch, kind: transform, value: {object: {orders: {ref: /input/orders}}}}
    - id: orders
      kind: forEach
      items: {ref: /steps/fetch/output/orders}
      concurrency: 4
      maxItems: 1000
      collect: all
      body:
        steps:
          - {id: check, kind: transform, value: {object: {order: {ref: /steps/fetch/output}}}}
          - {id: send, kind: transform, value: {object: {ok: {ref: /steps/check/output/ok}, to: {ref: /item/email}}}}
        output: {ref: /steps/send/output}
    - {id: summarize, kind: transform, value: {ref: /steps/orders/output}}
  output: {literal: {}}
`;
const model = () => {
  const adapter = new StructuredCanvasAdapter();
  adapter.setSource(source, "yaml");
  return adapter;
};
const read = (text: string): ClipboardContent => {
  const reading = readClipboard(text);
  if (!reading.ok) throw new Error(reading.reason);
  return reading.content;
};
const plan = (...args: Parameters<typeof planPaste>): PastePlan => {
  const planned = planPaste(...args);
  if ("reason" in planned) throw new Error(planned.reason);
  return planned;
};
const places = (adapter = model()): Map<string, StepPlace> =>
  placesOf(
    layoutLtr(adapter.definition, { role: () => undefined, triggers: [] }),
  );
const body = (step: Step) =>
  (step["body"] as { steps: Step[]; output: unknown }).steps;

describe("copying and pasting a loop", () => {
  it("reads a copied loop back with the steps inside it", () => {
    const copied = read(copySteps(model().definition, ["orders"]).yaml);
    expect(copied.steps.map((step) => step.id)).toEqual(["orders"]);
    expect(body(copied.steps[0]).map((step) => step.id)).toEqual([
      "check",
      "send",
    ]);
  });

  it("reports each reference the new place can't read once, on the step that holds it", () => {
    const adapter = model();
    const copied = read(copySteps(adapter.definition, ["orders"]).yaml);
    // Before fetch: the loop's list and check both read fetch, which runs later.
    const planned = plan(
      adapter.definition,
      copied,
      { owner: "root", index: 0 },
      adapter.stepIdSet(),
    );
    expect(
      planned.unreadable.map(({ stepId, fieldPath, target }) => ({
        stepId,
        fieldPath,
        target,
      })),
    ).toEqual([
      { stepId: "orders-1", fieldPath: "/items", target: "fetch" },
      { stepId: "check-1", fieldPath: "/value/object/order", target: "fetch" },
    ]);
  });

  it("pastes a loop with new IDs for it and its steps, its body and the references between them kept", () => {
    const adapter = model();
    const copied = read(copySteps(adapter.definition, ["orders"]).yaml);
    const planned = plan(
      adapter.definition,
      copied,
      { owner: "root", index: 3 },
      adapter.stepIdSet(),
    );
    const [loop] = planned.steps;
    expect(loop.id).toBe("orders-1");
    expect(body(loop).map((step) => step.id)).toEqual(["check-1", "send-1"]);
    expect(body(loop)[1]["value"]).toEqual({
      object: {
        ok: { ref: "/steps/check-1/output/ok" },
        to: { ref: "/item/email" },
      },
    });
    expect((loop["body"] as { output: unknown }).output).toEqual({
      ref: "/steps/send-1/output",
    });
    // The steps inside read each other where they sit: nothing to fix.
    expect(planned.unreadable).toEqual([]);
  });

  it("pastes a step into a loop's body, where it reads the steps before it there", () => {
    const adapter = model();
    const copied = read(copySteps(adapter.definition, ["send"]).yaml);
    const after = plan(
      adapter.definition,
      copied,
      { owner: "orders/body", index: 2 },
      adapter.stepIdSet(),
    );
    expect(after.ids).toEqual(["send-1"]);
    expect(after.unreadable).toEqual([]);
    // Before check, the copy can't read it yet.
    const before = plan(
      adapter.definition,
      copied,
      { owner: "orders/body", index: 0 },
      adapter.stepIdSet(),
    );
    expect(
      before.unreadable.map(({ stepId, target, reason }) => ({
        stepId,
        target,
        reason,
      })),
    ).toEqual([{ stepId: "send-1", target: "check", reason: "later" }]);
  });
});

describe("selecting and moving steps inside a loop", () => {
  it("copies consecutive steps of a body, never a body step with a step outside the loop", () => {
    const where = places();
    expect(contiguity(selectionOf(["check", "send"]), where)).toMatchObject({
      ok: true,
      owner: "orders/body",
      from: 0,
      to: 1,
    });
    expect(contiguity(selectionOf(["send", "summarize"]), where)).toEqual({
      ok: false,
      reason: "spread",
    });
  });

  it("extends a selection along the body with Shift and the arrows, never past its end", () => {
    const where = places();
    expect(extended(only("check"), where, "downstream").ids).toEqual([
      "check",
      "send",
    ]);
    expect(
      extended(selectionOf(["check", "send"], "send"), where, "downstream").ids,
    ).toEqual(["check", "send"]);
  });

  it("moves a step into a loop's body and out of it, never the loop into its own body", () => {
    const where = places();
    expect(
      moveAllowed(where, "summarize", { owner: "orders/body", index: 1 }),
    ).toBe(true);
    expect(moveAllowed(where, "check", { owner: "root", index: 3 })).toBe(true);
    expect(
      moveAllowed(where, "orders", { owner: "orders/body", index: 1 }),
    ).toBe(false);
  });
});
