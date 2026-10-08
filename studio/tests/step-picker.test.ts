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
import { kinds } from "../src/app/model";
import {
  ACTION_LIMIT,
  pickerOptions,
  placePopover,
  stepKindDescriptions,
  stepKindLabels,
} from "../src/app/designer/step-picker";

const actions = [
  {
    name: "crm.lookup-customer",
    version: "1.2.0",
    description: "Find a customer by email",
    group: "weave-http@2.0.0",
  },
  { name: "billing.create-invoice", version: "2.0.0", group: "billing-worker" },
];

describe("step picker options", () => {
  it("lists every step kind with plain names and no internal codes", () => {
    const { steps } = pickerOptions("", kinds, []);
    expect(steps.map((o) => o.kind)).toEqual([...kinds]);
    for (const kind of kinds) {
      expect(stepKindLabels[kind]).not.toBe(kind === "transform" ? "" : kind);
      expect(stepKindDescriptions[kind].length).toBeGreaterThan(8);
    }
  });

  it("finds steps by everyday words", () => {
    expect(pickerOptions("appro", kinds, []).steps.map((o) => o.label)).toEqual(
      ["Human task"],
    );
    expect(pickerOptions("if", kinds, []).steps.map((o) => o.kind)).toContain(
      "switch",
    );
    expect(pickerOptions("delay", kinds, []).steps.map((o) => o.kind)).toEqual([
      "wait",
    ]);
    expect(pickerOptions("api", kinds, []).steps.map((o) => o.kind)).toEqual([
      "action",
    ]);
    expect(stepKindLabels.action).toBe("Call an action");
    expect(stepKindDescriptions.action).toBe("Run a published action");
  });

  it("matches every query word across label, description, kind and action reference", () => {
    expect(pickerOptions("wait", kinds, []).steps.map((o) => o.kind)).toEqual([
      "wait",
      "signal",
    ]);
    expect(
      pickerOptions("condition", kinds, []).steps.map((o) => o.kind),
    ).toEqual(["switch"]);
    expect(
      pickerOptions("humanTask", kinds, []).steps.map((o) => o.kind),
    ).toEqual(["humanTask"]);
    const byEmail = pickerOptions("customer email", kinds, actions);
    expect(byEmail.steps).toEqual([]);
    expect(byEmail.actions.map((o) => o.uses)).toEqual([
      "crm.lookup-customer@1.2.0",
    ]);
    expect(
      pickerOptions("invoice@2.0.0", kinds, actions).actions.map((o) => o.uses),
    ).toEqual(["billing.create-invoice@2.0.0"]);
    expect(
      pickerOptions("billing-worker", kinds, actions).actions[0],
    ).toMatchObject({
      kind: "action",
      section: "actions",
      label: "billing.create-invoice",
      description: "Version 2.0.0 · billing-worker",
    });
  });

  it("gives actions stable, unique option identities", () => {
    const { steps, actions: found } = pickerOptions("", kinds, actions);
    const ids = [...steps, ...found].map((o) => o.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(found.map((o) => o.id)).toEqual([
      "action:crm.lookup-customer@1.2.0",
      "action:billing.create-invoice@2.0.0",
    ]);
  });

  it("caps long action lists and reports how many are hidden", () => {
    const many = Array.from({ length: ACTION_LIMIT + 7 }, (_, i) => ({
      name: `action-${i}`,
      version: "1.0.0",
    }));
    const result = pickerOptions("", kinds, many);
    expect(result.actions).toHaveLength(ACTION_LIMIT);
    expect(result.hiddenActions).toBe(7);
    expect(pickerOptions("action-56", kinds, many).actions).toHaveLength(1);
  });
});

describe("popover placement", () => {
  const viewport = { width: 1440, height: 900 };
  const size = { width: 340, height: 420 };

  it("opens below the anchor, aligned with its left edge", () => {
    expect(
      placePopover(
        { left: 300, top: 100, width: 36, height: 36 },
        size,
        viewport,
      ),
    ).toEqual({ left: 300, top: 142, maxHeight: 750, side: "below" });
  });

  it("opens above when the room below is short, pinned by its bottom edge", () => {
    const placed = placePopover(
      { left: 300, top: 800, width: 36, height: 36 },
      size,
      viewport,
    );
    expect(placed.side).toBe("above");
    expect(placed.top).toBeUndefined();
    expect(placed.bottom).toBe(900 - 800 + 6);
    expect(placed.maxHeight).toBe(800 - 6 - 8);
  });

  it("stays inside a 600x500 window and clamps the right edge", () => {
    const placed = placePopover(
      { left: 560, top: 120, width: 36, height: 36 },
      size,
      { width: 600, height: 500 },
    );
    expect(placed.left).toBe(600 - 340 - 8);
    expect(placed).toMatchObject({ side: "below", top: 162 });
    expect(placed.maxHeight).toBe(500 - 156 - 6 - 8);
  });

  it("picks the side with more room when neither fits the list", () => {
    const placed = placePopover(
      { left: 100, top: 240, width: 36, height: 36 },
      size,
      { width: 600, height: 500 },
    );
    expect(placed).toMatchObject({ side: "above", bottom: 500 - 240 + 6 });
    expect(placed.maxHeight).toBe(240 - 6 - 8);
  });

  it("fits a 360 px phone with an 8 px margin", () => {
    const placed = placePopover(
      { left: 200, top: 100, width: 36, height: 36 },
      size,
      { width: 360, height: 640 },
    );
    expect(placed.left).toBe(12);
    expect(placed.left + 340).toBeLessThanOrEqual(360 - 8);
  });

  it("never reports a negative height", () => {
    const placed = placePopover(
      { left: 10, top: 10, width: 10, height: 480 },
      size,
      { width: 600, height: 500 },
    );
    expect(placed.maxHeight).toBeGreaterThanOrEqual(0);
  });
});
