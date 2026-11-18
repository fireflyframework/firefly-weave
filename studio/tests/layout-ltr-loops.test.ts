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
// A loop on the canvas is a frame around its body: the header on top, its
// steps on the row the loop sits on, 64 units in from the frame's sides,
// with the room under them that their labels need. Sizes are the mockups'
// (design criteria 2.1), in canvas units.
import { describe, expect, it } from "vitest";
import {
  LTR,
  completes,
  layoutLtr,
  type LtrLayout,
  type LtrOptions,
} from "../src/app/editor/canvas/layout-ltr";
import { tidyNotes, tileBoxes, freeSpot } from "../src/app/editor/canvas/tidy";
import type { NodeRole } from "../src/app/editor/ndv/registry";
import { createStep, freshWorkflow, type Step } from "../src/app/model";

const roles: Record<string, NodeRole> = {
  forEach: "loop",
  transform: "transform",
  switch: "decision",
  fail: "fail",
  wait: "wait",
};
const options: LtrOptions = {
  role: (kind) => roles[kind],
  triggers: [{ id: "manual", kind: "manual" }],
};
const step = (id: string, kind = "transform"): Step => ({ id, kind }) as Step;
const loop = (id: string, body: Step[], extra = {}): Step => ({
  ...createStep("forEach", id),
  items: { ref: "/input/orders" },
  ...extra,
  body: { steps: body, output: { ref: "/item" } },
});
const layoutOf = (steps: Step[]): LtrLayout => {
  const workflow = freshWorkflow();
  workflow.spec.steps = steps;
  return layoutLtr(workflow, options);
};
const frame = (layout: LtrLayout, id: string) =>
  layout.frames.find((item) => item.id === id)!;
const tile = (layout: LtrLayout, id: string) =>
  layout.tiles.find((item) => item.id === id)!;
const edge = (layout: LtrLayout, key: string) =>
  layout.edges.find((item) => item.key === key);
const handle = (layout: LtrLayout, key: string) =>
  layout.handles.find((item) => item.key === key)!;
/** Where the main sequence starts and its row, after the trigger's column. */
const START = LTR.originX + LTR.tile + LTR.edge;
const ROW = LTR.originY;
const MID = ROW + LTR.tile / 2;

describe("a loop's frame", () => {
  it("is 472 by 264 around two steps, 64 in from its sides and 56 below its top", () => {
    const layout = layoutOf([loop("orders", [step("check"), step("send")])]);
    expect(frame(layout, "orders")).toEqual({
      id: "orders",
      x: START,
      y: ROW - 56,
      width: 472,
      height: 264,
      header: "For each item in input.orders",
      depth: 0,
    });
    expect([tile(layout, "check").x, tile(layout, "check").y]).toEqual([
      START + 64,
      ROW,
    ]);
    expect(tile(layout, "send").x).toBe(START + 64 + LTR.tile + LTR.edge);
    // The last step's right side is 64 in from the frame's right side.
    expect(START + 472 - (tile(layout, "send").x + LTR.tile)).toBe(64);
  });

  it("selects the loop by its header: a 32-unit band across the frame's top", () => {
    const layout = layoutOf([loop("orders", [step("send")])]);
    const header = tile(layout, "orders");
    const box = frame(layout, "orders");
    expect(header).toMatchObject({
      kind: "forEach",
      role: "loop",
      x: box.x,
      y: box.y,
      width: box.width,
      height: 32,
      owner: "root",
      place: "Main sequence",
    });
    // What the loop says under it (its output once it has run) sits under the frame.
    expect(header.labelY).toBe(box.y + box.height + LTR.labelGap);
  });

  it("keeps an empty body as one slot 64 in from the left, in a frame at least 360 wide", () => {
    const layout = layoutOf([createStep("forEach", "loop-1")]);
    const box = frame(layout, "loop-1");
    expect([box.width, box.height]).toEqual([360, 264]);
    expect(layout.slots).toEqual([
      {
        owner: "loop-1/body",
        tile: "loop-1",
        x: box.x + 64,
        y: MID - LTR.slot / 2,
        label: "For each item",
        name: "Add a step to For each item of loop-1",
        insert: { owner: "loop-1/body", index: 0 },
      },
    ]);
    expect(box.header).toBe("For each item in …");
  });

  it("places its steps in its body, named after it", () => {
    const layout = layoutOf([loop("orders", [step("check"), step("send")])]);
    expect(tile(layout, "send")).toMatchObject({
      owner: "orders/body",
      parent: "orders",
      depth: 1,
      index: 1,
      count: 2,
      place: "For each item of orders",
      after: { owner: "orders/body", index: 2 },
    });
    expect(layout.tiles.map((item) => item.id)).toEqual([
      "$trigger:manual",
      "orders",
      "check",
      "send",
      "$end",
    ]);
  });

  it("starts the step after it 152 units after its right side", () => {
    const layout = layoutOf([
      loop("orders", [step("check"), step("send")]),
      step("summarize"),
    ]);
    expect(tile(layout, "summarize").x).toBe(START + 472 + LTR.edge);
  });

  it("brings edges in at its left side and out at its right, on its steps' row", () => {
    const layout = layoutOf([
      step("fetch"),
      loop("orders", [step("check"), step("send")]),
      step("summarize"),
    ]);
    const box = frame(layout, "orders");
    expect(edge(layout, "fetch>orders")).toMatchObject({
      b: { x: box.x, y: MID },
      insert: { owner: "root", index: 1 },
      name: "Insert a step between fetch and orders",
      enters: "orders",
    });
    expect(edge(layout, "orders>check")).toMatchObject({
      a: { x: box.x, y: MID },
      b: { x: box.x + 64, y: MID },
      insert: { owner: "orders/body", index: 0 },
      name: "Insert a step at the start of For each item of orders",
      enters: "check",
    });
    expect(edge(layout, "check>send")?.insert).toEqual({
      owner: "orders/body",
      index: 1,
    });
    expect(edge(layout, "send>$exit:orders")).toMatchObject({
      b: { x: box.x + box.width, y: MID },
      insert: null,
      leaves: "send",
    });
    expect(edge(layout, "orders>summarize")).toMatchObject({
      a: { x: box.x + box.width + 6, y: MID },
      insert: { owner: "root", index: 2 },
      name: "Insert a step between orders and summarize",
    });
  });

  it("puts a + 40 units after the body's last step, and one after the frame", () => {
    const layout = layoutOf([loop("orders", [step("check"), step("send")])]);
    const send = tile(layout, "send");
    const box = frame(layout, "orders");
    expect(handle(layout, "send>out")).toMatchObject({
      plus: { x: send.x + LTR.tile + LTR.plus, y: MID },
      insert: { owner: "orders/body", index: 2 },
      name: "Add a step at the end of For each item of orders",
    });
    expect(handle(layout, "orders>out")).toMatchObject({
      x: box.x + box.width,
      plus: { x: box.x + box.width + LTR.plus, y: MID },
      insert: { owner: "root", index: 1 },
      name: "Add a step after orders",
    });
    // The body's + fits inside the frame.
    expect(send.x + LTR.tile + LTR.plus + 12).toBeLessThan(box.x + box.width);
  });

  it("nests: an inner frame sits inside its parent's body, 56 lower and 24 above its bottom", () => {
    const inner = loop("lines", [step("send")], {
      items: { ref: "/item/lines" },
    });
    const layout = layoutOf([loop("orders", [inner])]);
    const outer = frame(layout, "orders");
    const box = frame(layout, "lines");
    expect(box).toMatchObject({
      depth: 1,
      header: "For each item in item.lines",
    });
    expect(box.x).toBe(outer.x + 64);
    expect(box.y).toBe(outer.y + 56);
    expect(outer.y).toBe(ROW - 112);
    expect(outer.y + outer.height - (box.y + box.height)).toBe(24);
    expect(outer.x + outer.width - (box.x + box.width)).toBe(64);
    expect(tile(layout, "send")).toMatchObject({
      x: box.x + 64,
      y: ROW,
      owner: "lines/body",
      depth: 2,
    });
  });

  it("leaves no way past it when a Fail stops its body", () => {
    for (const body of [
      [step("check"), step("stop", "fail")],
      [step("stop", "fail"), step("check")],
    ]) {
      expect(completes([loop("orders", body)])).toBe(false);
      const layout = layoutOf([loop("orders", body), step("summarize")]);
      expect(layout.handles.some((item) => item.key === "orders>out")).toBe(
        false,
      );
      expect(edge(layout, "orders>summarize")).toBeUndefined();
      expect(edge(layout, "check>$exit:orders")).toBeUndefined();
    }
    expect(completes([loop("orders", [step("check")])])).toBe(true);
  });

  it("sits under the path above it, clear of that path's steps and labels", () => {
    const decision = {
      ...createStep("switch", "route"),
      cases: [
        {
          when: { literal: true },
          steps: [step("a")],
          output: { literal: {} },
        },
      ],
      default: {
        steps: [loop("orders", [step("b")])],
        output: { literal: {} },
      },
    } as Step;
    const layout = layoutOf([decision]);
    const above = tile(layout, "a");
    const box = frame(layout, "orders");
    // The path above keeps its whole pitch (its tile, its labels, a gap).
    expect(box.y).toBe(above.y + LTR.lane);
    expect(tile(layout, "b").y).toBe(box.y + 56);
    expect(handle(layout, "route:path:default").y).toBe(
      box.y + 56 + LTR.tile / 2,
    );
  });

  it("leaves room under it for its label before the path below", () => {
    const route = (id: string, first: Step[], second: Step[]): Step =>
      ({
        ...createStep("switch", id),
        cases: [
          { when: { literal: true }, steps: first, output: { literal: {} } },
        ],
        default: { steps: second, output: { literal: {} } },
      }) as Step;
    const layout = layoutOf([
      route(
        "outer",
        [route("inner", [step("a")], [loop("orders", [step("b")])])],
        [step("c")],
      ),
    ]);
    const box = frame(layout, "orders");
    // The path under the inner decision starts below the loop's frame and its label line.
    expect(tile(layout, "c").y - (box.y + box.height)).toBe(48);
    const direct = layoutOf([
      route("route", [loop("orders", [step("b")])], [step("c")]),
    ]);
    const own = frame(direct, "orders");
    expect(tile(direct, "c").y - (own.y + own.height)).toBe(48);
  });

  it("keeps the whole frame in the workflow's bounds", () => {
    const inner = loop("lines", [step("send")]);
    const layout = layoutOf([loop("orders", [inner])]);
    const outer = frame(layout, "orders");
    expect(layout.bounds.minY).toBeLessThanOrEqual(outer.y);
    expect(layout.bounds.maxY).toBeGreaterThanOrEqual(outer.y + outer.height);
  });

  it("is never covered by a note: Tidy moves one off it, and a new one starts beside it", () => {
    const layout = layoutOf([createStep("forEach", "loop-1"), step("after")]);
    const box = frame(layout, "loop-1");
    const inside = { x: box.x + 200, y: box.y + 120, width: 120, height: 80 };
    const moved = tidyNotes(
      [{ id: "n", anchored: false, ...inside }],
      tileBoxes(layout),
    ).get("n")!;
    const overlaps = (x: number, y: number) =>
      x < box.x + box.width &&
      box.x < x + inside.width &&
      y < box.y + box.height &&
      box.y < y + inside.height;
    expect(moved).toBeDefined();
    expect(overlaps(moved.x, moved.y)).toBe(false);
    const spot = freeSpot(inside, tileBoxes(layout));
    expect(overlaps(spot.x, spot.y)).toBe(false);
  });
});
