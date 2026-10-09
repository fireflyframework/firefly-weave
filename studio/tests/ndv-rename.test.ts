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
import { describe, expect, it } from "vitest";
import {
  newConnectorRecipe,
  newHttpRecipe,
} from "../src/app/editor/ndv/owned/owned-actions";
import {
  ownedActionsOf,
  withOwnedAction,
} from "../src/app/editor/ndv/owned/owned-store";
import {
  renameHint,
  renameTarget,
  renameWithExtras,
} from "../src/app/editor/ndv/rename";
import {
  stepNotesOf,
  withStepNote,
} from "../src/app/editor/state/canvas-sidecar";
import { StructuredCanvasAdapter } from "../src/app/model";

function model() {
  const m = new StructuredCanvasAdapter();
  m.insert("action", "root", undefined, {
    id: "get-orders",
    uses: "untitled-workflow.get-orders@1.0.0",
    with: { literal: {} },
  });
  m.insert("transform", "root", undefined, {
    id: "shape",
    value: { ref: "/steps/get-orders/output" },
  });
  m.canvas = withOwnedAction(
    m.canvas,
    "untitled-workflow.get-orders@1.0.0",
    newHttpRecipe(),
  );
  m.canvas = withStepNote(m.canvas, "get-orders", { text: "Sandbox only" });
  return m;
}

describe("renaming a step", () => {
  it("turns free text into an unused ID", () => {
    const taken = new Set(["get-orders", "check-customer"]);
    expect(renameTarget("Get customer orders!", "get-orders", taken)).toBe(
      "get-customer-orders",
    );
    expect(renameTarget("Check customer", "get-orders", taken)).toBe(
      "check-customer-2",
    );
    expect(renameTarget("get-orders", "get-orders", taken)).toBeNull();
    expect(renameTarget("!!", "get-orders", taken)).toBeNull();
    expect(renameHint("Check customer", "get-orders", taken)).toBe(
      "Saved as check-customer-2",
    );
    expect(renameHint("get-orders", "get-orders", taken)).toBe("");
    expect(renameHint("check-customer-2", "get-orders", taken)).toBe("");
  });
  it("moves notes and owned actions with the step, in one undo step", () => {
    const m = model();
    const result = renameWithExtras(m, "get-orders", "Fetch orders");
    expect(result).toEqual({
      id: "fetch-orders",
      references: 1,
      uses: {
        from: "untitled-workflow.get-orders@1.0.0",
        to: "untitled-workflow.fetch-orders@1.0.0",
      },
    });
    const steps = m.definition.spec.steps;
    expect(steps[0]).toMatchObject({
      id: "fetch-orders",
      uses: "untitled-workflow.fetch-orders@1.0.0",
    });
    expect(steps[1]["value"]).toEqual({ ref: "/steps/fetch-orders/output" });
    expect(Object.keys(stepNotesOf(m.canvas))).toEqual(["fetch-orders"]);
    expect(Object.keys(ownedActionsOf(m.canvas))).toEqual([
      "untitled-workflow.fetch-orders@1.0.0",
    ]);
    m.undo();
    expect(m.definition.spec.steps[0].id).toBe("get-orders");
    expect(m.definition.spec.steps[0]["uses"]).toBe(
      "untitled-workflow.get-orders@1.0.0",
    );
    expect(Object.keys(ownedActionsOf(m.canvas))).toEqual([
      "untitled-workflow.get-orders@1.0.0",
    ]);
    expect(Object.keys(stepNotesOf(m.canvas))).toEqual(["get-orders"]);
  });
  it("suffixes a taken name", () => {
    const m = model();
    expect(renameWithExtras(m, "get-orders", "Shape").id).toBe("shape-2");
  });
  it("changes nothing when the text is the current name or has no usable characters", () => {
    const m = model();
    const before = m.source;
    const revision = m.revision;
    for (const text of ["get-orders", "Get-Orders", "!!", "  "])
      expect(renameWithExtras(m, "get-orders", text)).toEqual({
        id: "get-orders",
        references: 0,
        uses: null,
      });
    expect(m.source).toBe(before);
    expect(m.revision).toBe(revision);
  });
  it("leaves an action that other steps share, and any connector action, under its name", () => {
    const m = model();
    m.insert("action", "root", undefined, {
      id: "again",
      uses: "untitled-workflow.get-orders@1.0.0",
      with: { literal: {} },
    });
    expect(renameWithExtras(m, "get-orders", "Fetch orders")).toEqual({
      id: "fetch-orders",
      references: 1,
      uses: null,
    });
    expect(Object.keys(ownedActionsOf(m.canvas))).toEqual([
      "untitled-workflow.get-orders@1.0.0",
    ]);
    expect(Object.keys(stepNotesOf(m.canvas))).toEqual(["fetch-orders"]);

    const email = new StructuredCanvasAdapter();
    email.insert("action", "root", undefined, {
      id: "send",
      uses: "untitled-workflow.send-email@1.0.0",
      with: { literal: {} },
    });
    email.canvas = withOwnedAction(
      email.canvas,
      "untitled-workflow.send-email@1.0.0",
      newConnectorRecipe("weave-email@1.0.0", "send"),
    );
    expect(renameWithExtras(email, "send", "Notify").uses).toBeNull();
    expect(email.definition.spec.steps[0]["uses"]).toBe(
      "untitled-workflow.send-email@1.0.0",
    );
    expect(Object.keys(ownedActionsOf(email.canvas))).toEqual([
      "untitled-workflow.send-email@1.0.0",
    ]);
  });
  it("follows a step nested in a decision path", () => {
    const m = new StructuredCanvasAdapter();
    const route = m.insert("switch", "root");
    m.insert("action", `${route.id}/case 1`, undefined, {
      id: "deep",
      uses: "untitled-workflow.deep@1.0.0",
      with: { literal: {} },
    });
    m.canvas = withOwnedAction(
      m.canvas,
      "untitled-workflow.deep@1.0.0",
      newHttpRecipe(),
    );
    const result = renameWithExtras(m, "deep", "Deeper");
    expect(result.uses?.to).toBe("untitled-workflow.deeper@1.0.0");
    expect(Object.keys(ownedActionsOf(m.canvas))).toEqual([
      "untitled-workflow.deeper@1.0.0",
    ]);
  });
  it("refuses a rename the canvas can't hold and leaves everything as it was", () => {
    const m = model();
    // A workflow name with a space can't begin an action name a canvas file reads.
    m.renameWorkflow("Order intake");
    const source = m.source;
    const steps = structuredClone(m.definition.spec.steps);
    const canvas = structuredClone(m.canvas);
    expect(() => renameWithExtras(m, "get-orders", "Fetch orders")).toThrow(
      "Use letters, numbers, dots, underscores or hyphens for the action name, starting with a letter or number.",
    );
    expect(m.source).toBe(source);
    expect(m.definition.spec.steps).toEqual(steps);
    expect(m.canvas).toEqual(canvas);
    expect(m.selected).toBe("shape");
    // No undo step was left behind: Undo goes back past the workflow rename.
    m.undo();
    expect(m.definition.metadata.name).toBe("untitled-workflow");
  });
});
