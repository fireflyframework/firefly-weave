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
// A transaction on the canvas is a frame around its own steps, as a loop is:
// its header on top, its steps on the row the transaction sits on, and its
// outputs on the frame's right border when it continues with error output.
// Sizes are in canvas units.
import { describe, expect, it } from "vitest";
import {
  LTR,
  completes,
  layoutLtr,
  type LtrLayout,
  type LtrOptions,
} from "../src/app/editor/canvas/layout-ltr";
import { frameBadge } from "../src/app/editor/canvas/loop-frame";
import { tileBoxes } from "../src/app/editor/canvas/tidy";
import type { TileBadge } from "../src/app/editor/canvas/tile-facts";
import {
  EMPTY_TRANSACTION,
  TRANSACTION_HEADER,
  transactionFacts,
} from "../src/app/editor/canvas/transaction-frame";
import type { NodeRole } from "../src/app/editor/ndv/registry";
import { newTransactionStep } from "../src/app/editor/transactions/group";
import { createStep, freshWorkflow, type Step } from "../src/app/model";

const roles: Record<string, NodeRole> = {
  action: "app",
  forEach: "loop",
  transaction: "group",
  transform: "transform",
  switch: "decision",
  parallel: "parallel",
  fail: "fail",
};
const options: LtrOptions = {
  role: (kind) => roles[kind],
  triggers: [{ id: "manual", kind: "manual" }],
};
const step = (id: string, kind = "transform"): Step => ({ id, kind }) as Step;
const transaction = (
  id: string,
  steps: Step[],
  extra: Record<string, unknown> = {},
): Step => ({ ...newTransactionStep(id), steps, ...extra });
/** A transaction that continues with error output, its error path's steps. */
const guarded = (id: string, steps: Step[], path: Step[]): Step =>
  transaction(id, steps, {
    onError: "errorOutput",
    errorPath: { steps: path, output: { literal: null } },
  });
const loop = (id: string, body: Step[]): Step => ({
  ...createStep("forEach", id),
  items: { ref: "/input/moves" },
  body: { steps: body, output: { ref: "/item" } },
});
const route = (id: string, first: Step[], otherwise: Step[]): Step =>
  ({
    ...createStep("switch", id),
    cases: [{ when: { literal: true }, steps: first, output: { literal: {} } }],
    default: { steps: otherwise, output: { literal: {} } },
  }) as Step;
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
  layout.handles.find((item) => item.key === key);
/** Where the main sequence starts and its row, after the trigger's column. */
const START = LTR.originX + LTR.tile + LTR.edge;
const ROW = LTR.originY;
const MID = ROW + LTR.tile / 2;
/** A frame around steps on the main row: 56 above it, its labels and 24 below it. */
const BOTTOM = ROW + LTR.tile + 88 + 24;

describe("a transaction's frame", () => {
  it("is 720 by 264 around three steps, 64 in from its sides and 56 below its top", () => {
    const layout = layoutOf([
      transaction("move", [step("hold"), step("add"), step("submit")]),
    ]);
    expect(frame(layout, "move")).toEqual({
      id: "move",
      transaction: true,
      x: START,
      y: ROW - 56,
      width: 720,
      height: 264,
      header: "Transaction · All of these succeed, or what's done is undone",
      depth: 0,
    });
    expect(TRANSACTION_HEADER).toBe(frame(layout, "move").header);
    expect([tile(layout, "hold").x, tile(layout, "hold").y]).toEqual([
      START + 64,
      ROW,
    ]);
    // The last step's right side is 64 in from the frame's right side.
    expect(START + 720 - (tile(layout, "submit").x + LTR.tile)).toBe(64);
  });

  it("is selected by its header: a 32-unit band across the frame's top", () => {
    const layout = layoutOf([transaction("move", [step("hold")])]);
    const header = tile(layout, "move");
    const box = frame(layout, "move");
    expect(header).toMatchObject({
      kind: "transaction",
      role: "group",
      x: box.x,
      y: box.y,
      width: box.width,
      height: 32,
      owner: "root",
      place: "Main sequence",
    });
    expect(header.labelY).toBe(box.y + box.height + LTR.labelGap);
  });

  it("is at least 520 wide, so a new transaction's whole header shows", () => {
    expect(frame(layoutOf([transaction("move", [])]), "move").width).toBe(520);
    expect(
      frame(
        layoutOf([transaction("move", [step("hold"), step("add")])]),
        "move",
      ).width,
    ).toBe(520);
    // Loops keep their own minimum.
    expect(
      frame(layoutOf([createStep("forEach", "loop-1")]), "loop-1").width,
    ).toBe(360);
  });

  it("keeps an empty body as one slot that adds the transaction's first step", () => {
    const layout = layoutOf([newTransactionStep("move")]);
    const box = frame(layout, "move");
    expect(layout.slots).toEqual([
      {
        owner: "move/steps",
        tile: "move",
        x: box.x + 64,
        y: MID - LTR.slot / 2,
        label: "Transaction",
        name: "Add a step to transaction move",
        insert: { owner: "move/steps", index: 0 },
      },
    ]);
    expect(edge(layout, "$slot:move/steps>$exit:move")).toMatchObject({
      b: { x: box.x + box.width, y: MID },
    });
  });

  it("places its steps in it, named after it", () => {
    const layout = layoutOf([transaction("move", [step("hold"), step("add")])]);
    expect(tile(layout, "add")).toMatchObject({
      owner: "move/steps",
      parent: "move",
      depth: 1,
      index: 1,
      count: 2,
      place: "Transaction move",
      after: { owner: "move/steps", index: 2 },
    });
    expect(layout.tiles.map((item) => item.id)).toEqual([
      "$trigger:manual",
      "move",
      "hold",
      "add",
      "$end",
    ]);
  });

  it("brings edges in at its left side and out at its right, with a + after it", () => {
    const layout = layoutOf([
      step("lookup"),
      transaction("move", [step("hold"), step("add")]),
      step("summarize"),
    ]);
    const box = frame(layout, "move");
    expect(edge(layout, "lookup>move")).toMatchObject({
      b: { x: box.x, y: MID },
      insert: { owner: "root", index: 1 },
    });
    expect(edge(layout, "move>hold")).toMatchObject({
      a: { x: box.x, y: MID },
      b: { x: box.x + 64, y: MID },
      insert: { owner: "move/steps", index: 0 },
      name: "Insert a step at the start of transaction move",
      enters: "hold",
    });
    expect(edge(layout, "add>$exit:move")).toMatchObject({
      b: { x: box.x + box.width, y: MID },
      leaves: "add",
    });
    expect(handle(layout, "add>out")).toMatchObject({
      plus: { x: tile(layout, "add").x + LTR.tile + LTR.plus, y: MID },
      insert: { owner: "move/steps", index: 2 },
      name: "Add a step at the end of transaction move",
    });
    expect(edge(layout, "move>summarize")).toMatchObject({
      a: { x: box.x + box.width + 6, y: MID },
      insert: { owner: "root", index: 2 },
      name: "Insert a step between move and summarize",
    });
    expect(tile(layout, "summarize").x).toBe(box.x + box.width + LTR.edge);
    expect(
      handle(layoutOf([transaction("move", [step("hold")])]), "move>out"),
    ).toMatchObject({
      plus: { x: START + 520 + LTR.plus, y: MID },
      insert: { owner: "root", index: 1 },
      name: "Add a step after move",
    });
  });

  it("is one box for notes to keep clear of", () => {
    const layout = layoutOf([transaction("move", [step("hold")])]);
    const box = frame(layout, "move");
    expect(tileBoxes(layout)[1]).toEqual({
      x: box.x,
      y: box.y,
      width: box.width,
      height: box.height + LTR.labelGap + LTR.labelHeight,
    });
  });
});

describe("a transaction's way on", () => {
  it("goes past it when its steps end, and stops at a Fail among them", () => {
    expect(completes([transaction("move", [step("hold")])])).toBe(true);
    const stops = transaction("move", [step("hold"), step("stop", "fail")]);
    expect(completes([stops])).toBe(false);
    const layout = layoutOf([stops, step("summarize")]);
    expect(handle(layout, "move>out")).toBeUndefined();
    expect(edge(layout, "move>summarize")).toBeUndefined();
    expect(edge(layout, "hold>$exit:move")).toBeUndefined();
  });

  it("goes on after a Fail when the transaction handles being undone", () => {
    const fails = [step("hold"), step("stop", "fail")];
    expect(
      completes([transaction("move", fails, { onError: "continue" })]),
    ).toBe(true);
    const layout = layoutOf([
      transaction("move", fails, { onError: "continue" }),
      step("summarize"),
    ]);
    expect(edge(layout, "move>summarize")).toBeDefined();
    // Nothing passes the Fail itself.
    expect(edge(layout, "stop>$exit:move")).toBeUndefined();
    expect(completes([guarded("move", fails, [step("report")])])).toBe(true);
    const caught = layoutOf([
      guarded("move", fails, [step("report")]),
      step("summarize"),
    ]);
    expect(edge(caught, "move>summarize")).toBeDefined();
    expect(edge(caught, "report>$join:move")).toBeDefined();
    // Its steps never end: no run leaves through Success.
    expect(edge(caught, "move:path:success>$join:move")).toBeUndefined();
    const stuck = guarded("move", fails, [step("stop-too", "fail")]);
    expect(completes([stuck])).toBe(false);
    expect(handle(layoutOf([stuck]), "move>out")).toBeUndefined();
  });
});

describe("a transaction inside other blocks", () => {
  it("sits inside a loop's body, 56 below its frame's top and 24 above its bottom", () => {
    const layout = layoutOf([
      loop("moves", [step("check"), transaction("move", [step("hold")])]),
    ]);
    const outer = frame(layout, "moves");
    const box = frame(layout, "move");
    expect(outer.transaction).toBeUndefined();
    expect(box.depth).toBe(1);
    expect(box.x).toBe(tile(layout, "check").x + LTR.tile + LTR.edge);
    expect(box.y).toBe(outer.y + 56);
    expect(outer.y + outer.height - (box.y + box.height)).toBe(24);
    expect(outer.x + outer.width - (box.x + box.width)).toBe(64);
    expect(tile(layout, "hold")).toMatchObject({
      y: ROW,
      owner: "move/steps",
      depth: 2,
    });
  });

  it("holds a loop, which draws inside its frame", () => {
    const layout = layoutOf([
      transaction("move", [loop("moves", [step("add")]), step("submit")]),
    ]);
    const outer = frame(layout, "move");
    const box = frame(layout, "moves");
    expect(box.x).toBe(outer.x + 64);
    expect(box.y).toBe(outer.y + 56);
    expect(outer.y + outer.height - (box.y + box.height)).toBe(24);
    expect(tile(layout, "submit").x).toBe(box.x + box.width + LTR.edge);
  });

  it("sits under the path above it, and above the path below it, without touching either", () => {
    const layout = layoutOf([
      route(
        "outer",
        [route("inner", [step("a")], [transaction("move", [step("hold")])])],
        [step("c")],
      ),
    ]);
    const box = frame(layout, "move");
    expect(box.y).toBe(tile(layout, "a").y + LTR.lane);
    expect(tile(layout, "hold").y).toBe(box.y + 56);
    // Room under the frame for its own label before the next path.
    expect(tile(layout, "c").y - (box.y + box.height)).toBe(48);
  });

  it("keeps the whole frame in the workflow's bounds", () => {
    const layout = layoutOf([transaction("move", [step("hold")])]);
    const box = frame(layout, "move");
    expect(layout.bounds.minY).toBeLessThanOrEqual(box.y);
    expect(layout.bounds.maxY).toBeGreaterThanOrEqual(box.y + box.height);
    expect(layout.bounds.maxX).toBeGreaterThanOrEqual(box.x + box.width);
  });
});

describe("a transaction that continues with error output", () => {
  const RIGHT = START + 520;
  const LANES = RIGHT + LTR.edge;
  /** The error path's row: 48 under the frame's bottom. */
  const ERROR_ROW = BOTTOM + 48;

  it("has its Success and Error outputs on the frame's right border", () => {
    const layout = layoutOf([
      guarded("move", [step("hold"), step("add")], []),
      step("summarize"),
    ]);
    const box = frame(layout, "move");
    expect(box.y + box.height).toBe(BOTTOM);
    expect(box.x + box.width).toBe(RIGHT);
    expect(handle(layout, "move:path:success")).toEqual({
      key: "move:path:success",
      tile: "move",
      x: RIGHT,
      y: MID,
      insert: { owner: "root", index: 1 },
      label: "Success",
      plus: null,
      name: "Add a step after move",
    });
    // 24 above the frame's bottom.
    expect(handle(layout, "move:path:errorPath")).toEqual({
      key: "move:path:errorPath",
      tile: "move",
      x: RIGHT,
      y: BOTTOM - 24,
      insert: { owner: "move/errorPath", index: 0 },
      label: "Error",
      plus: null,
      name: "Add a step to the error path of move",
      tone: "danger",
    });
    // Its steps lead to Success, and nothing fans out of the frame.
    expect(edge(layout, "add>$exit:move")).toMatchObject({
      b: { x: RIGHT - 6, y: MID },
      leaves: "add",
    });
    expect(layout.edges.filter((item) => item.shape === "fan")).toEqual([]);
    expect(layout.labels).toEqual([
      {
        owner: "move/success",
        text: "Success",
        x: RIGHT + 6,
        y: MID - LTR.pathLabelRise - LTR.line,
        width: LANES - 8 - (RIGHT + 6),
        tone: "muted",
      },
      {
        owner: "move/errorPath",
        text: "Error",
        x: RIGHT + 6,
        y: BOTTOM - 24 - LTR.pathLabelRise - LTR.line,
        width: LANES - 8 - (RIGHT + 6),
        tone: "danger",
      },
    ]);
  });

  it("offers an empty error path's slot below the frame, and joins before the next step", () => {
    const layout = layoutOf([
      guarded("move", [step("hold"), step("add")], []),
      step("summarize"),
    ]);
    const joinX = LANES + LTR.slot + LTR.edge - 6;
    const y = ERROR_ROW + LTR.tile / 2;
    expect(layout.slots).toEqual([
      {
        owner: "move/errorPath",
        tile: "move",
        x: LANES,
        y: y - LTR.slot / 2,
        label: "Error",
        name: "Add a step to the error path of move",
        insert: { owner: "move/errorPath", index: 0 },
      },
    ]);
    expect(edge(layout, "move:path:success>$join:move")).toMatchObject({
      shape: "curve",
      a: { x: RIGHT + 6, y: MID },
      b: { x: joinX, y: MID },
      leaves: "move",
      success: "move",
    });
    expect(
      edge(layout, "move:path:errorPath>$slot:move/errorPath"),
    ).toMatchObject({
      shape: "curve",
      a: { x: RIGHT + 6, y: BOTTOM - 24 },
      b: { x: LANES, y },
    });
    expect(edge(layout, "$slot:move/errorPath>$join:move")).toMatchObject({
      shape: "merge",
      b: { x: joinX, y: MID },
    });
    expect(tile(layout, "summarize").x).toBe(joinX + 6 + LTR.edge);
    expect(edge(layout, "move>summarize")).toMatchObject({
      a: { x: joinX + 12, y: MID },
      insert: { owner: "root", index: 1 },
    });
  });

  it("lays out its error path's steps 48 below the frame, merging into the join", () => {
    const layout = layoutOf([
      guarded("move", [step("hold"), step("add")], [step("report")]),
      step("summarize"),
    ]);
    const y = ERROR_ROW + LTR.tile / 2;
    const joinX = LANES + LTR.tile + LTR.edge - 6;
    expect(tile(layout, "report")).toMatchObject({
      x: LANES,
      y: ERROR_ROW,
      owner: "move/errorPath",
      parent: "move",
      depth: 1,
      place: "Error path of move",
    });
    expect(edge(layout, "move:path:errorPath>report")).toMatchObject({
      shape: "curve",
      a: { x: RIGHT + 6, y: BOTTOM - 24 },
      b: { x: LANES, y },
      insert: { owner: "move/errorPath", index: 0 },
      name: "Insert a step between move and report",
      enters: "report",
    });
    expect(edge(layout, "report>$join:move")).toMatchObject({
      shape: "merge",
      a: { x: LANES + LTR.tile + 6, y },
      b: { x: joinX, y: MID },
      leaves: "report",
    });
    expect(handle(layout, "report>out")).toMatchObject({
      plus: { x: LANES + LTR.tile + LTR.plus, y },
    });
    // No + of its own between the frame and the join: one after it.
    expect(
      layout.handles.filter((item) => item.plus).map((item) => item.key),
    ).toEqual(["add>out", "report>out", "summarize>out"]);
  });

  it("makes room for a frame in its error path, and keeps the next path and a loop around it clear", () => {
    const layout = layoutOf([
      route(
        "first",
        [
          loop("moves", [
            guarded(
              "move",
              [step("hold")],
              [transaction("report", [step("send")])],
            ),
          ]),
        ],
        [step("after")],
      ),
    ]);
    const box = frame(layout, "move");
    const inner = frame(layout, "report");
    const around = frame(layout, "moves");
    expect(inner.y).toBe(box.y + box.height + 48);
    expect(inner.x).toBe(box.x + box.width + LTR.edge);
    expect(around.y + around.height - (inner.y + inner.height)).toBe(24);
    expect(around.x + around.width).toBeGreaterThanOrEqual(
      inner.x + inner.width + 64,
    );
    expect(tile(layout, "after").y - (around.y + around.height)).toBe(48);
  });
});

describe("a transaction's points of no return", () => {
  const last = (id: string): Step =>
    ({ ...step(id, "action"), irreversible: true }) as Step;
  const held = (id: string): Step =>
    ({ ...step(id, "action"), compensate: {} }) as unknown as Step;
  /** From 8 above a row to 184 under it. */
  const onRow = (y: number) => ({ y1: y - 8, y2: y + 184 });

  it("is a line after the step that can't be undone, on the middle of its edge", () => {
    const layout = layoutOf([
      transaction("move", [held("add"), last("submit"), step("notify")]),
    ]);
    expect(layout.dividers).toEqual([
      {
        stepId: "submit",
        x: tile(layout, "submit").x + LTR.tile + 76,
        ...onRow(ROW),
        label: "Point of no return",
        room: null,
      },
    ]);
  });

  it("is 20 after the step when it is the last of its path", () => {
    const layout = layoutOf([
      transaction("move", [held("add"), last("submit")]),
    ]);
    expect(layout.dividers.map(({ x }) => x)).toEqual([
      tile(layout, "submit").x + LTR.tile + 20,
    ]);
  });

  it("is after both outputs meet, for a step that continues with error output", () => {
    const layout = layoutOf([
      transaction("move", [
        {
          ...last("submit"),
          onError: "errorOutput",
          errorPath: { steps: [step("report")], output: { literal: null } },
        },
        step("notify"),
      ]),
    ]);
    expect(layout.dividers.map(({ x }) => x)).toEqual([
      tile(layout, "notify").x - LTR.edge + 76,
    ]);
  });

  it("keeps its label off a frame right after it", () => {
    const room = (...after: Step[]) =>
      layoutOf([transaction("move", [last("submit"), ...after])]).dividers[0]
        .room;
    expect(room(loop("each", [step("note")]))).toBe(68);
    // Over a tile's column there is room for all of it at any zoom.
    expect(room(step("notify"))).toBeNull();
    expect(room()).toBeNull();
  });

  it("makes room for its label: a frame that holds one has 56 under its header", () => {
    const plain = layoutOf([transaction("move", [held("add")])]);
    const layout = layoutOf([
      step("lookup"),
      transaction("move", [held("add"), last("submit")]),
    ]);
    expect(frame(plain, "move").y).toBe(ROW - 56);
    const box = frame(layout, "move");
    expect(box.y).toBe(ROW - 88);
    expect(box.y + box.height).toBe(BOTTOM);
    // The steps stay on the row of the ones before.
    expect(tile(layout, "submit").y).toBe(tile(layout, "lookup").y);
    // The loop around it makes room too.
    const around = layoutOf([
      loop("moves", [transaction("move", [last("a")])]),
    ]);
    expect(around.dividers.map(({ stepId }) => stepId)).toEqual(["a"]);
    expect(frame(around, "move").y).toBe(ROW - 88);
    expect(frame(around, "moves").y).toBe(ROW - 144);
  });

  it("is drawn once per decision path that has one, whose row starts 40 lower", () => {
    const paths = (first: Step[], second: Step[]) =>
      layoutOf([
        transaction("move", [held("add"), route("route", first, second)]),
      ]);
    const layout = paths([last("submit"), step("notify")], [last("batch")]);
    const submit = tile(layout, "submit");
    const batch = tile(layout, "batch");
    expect(layout.dividers).toEqual([
      expect.objectContaining({
        stepId: "submit",
        x: submit.x + LTR.tile + 76,
        ...onRow(submit.y),
      }),
      expect.objectContaining({
        stepId: "batch",
        x: batch.x + LTR.tile + 20,
        ...onRow(batch.y),
      }),
    ]);
    expect(batch.y - submit.y).toBe(LTR.lane + 40);
    // A path without one keeps its place.
    const one = paths([last("submit")], [held("again")]);
    expect(tile(one, "again").y - tile(one, "submit").y).toBe(LTR.lane);
    // The frame holds the lower path, its labels and the line.
    const box = frame(layout, "move");
    expect(box.y + box.height).toBe(batch.y + 184 + 24);
    // A decision at the start of a path is on that path's row.
    const nested = paths(
      [held("a")],
      [route("inner", [last("b")], [held("c")])],
    );
    expect(tile(nested, "b").y - tile(nested, "a").y).toBe(LTR.lane + 40);
  });

  it("keeps the next path and what follows clear of a lower row", () => {
    const layout = layoutOf([
      route(
        "outer",
        [
          transaction("move", [
            route("route", [last("submit")], [last("batch")]),
          ]),
        ],
        [step("after")],
      ),
    ]);
    const box = frame(layout, "move");
    expect(tile(layout, "after").y - (box.y + box.height)).toBe(48);
  });

  it("pushes the path under a decision that holds lower rows", () => {
    const layout = layoutOf([
      transaction("move", [
        route("outer", [route("inner", [last("a")], [last("b")])], [held("c")]),
      ]),
    ]);
    expect(tile(layout, "b").y - tile(layout, "a").y).toBe(LTR.lane + 40);
    expect(tile(layout, "c").y - tile(layout, "b").y).toBe(LTR.lane);
  });

  it("starts a step's error path 40 lower when that path has one", () => {
    const layout = layoutOf([
      transaction("move", [
        {
          ...held("add"),
          onError: "errorOutput",
          errorPath: { steps: [last("submit")], output: { literal: null } },
        },
        held("again"),
      ]),
    ]);
    const submit = tile(layout, "submit");
    expect(submit.y - tile(layout, "add").y).toBe(LTR.lane + 40);
    const box = frame(layout, "move");
    expect(box.y + box.height).toBe(submit.y + 184 + 24);
  });

  it("is never drawn outside a transaction, nor inside a loop or a parallel step of one", () => {
    expect(layoutOf([last("submit"), step("notify")]).dividers).toEqual([]);
    expect(
      layoutOf([transaction("move", [loop("each", [last("submit")])])])
        .dividers,
    ).toEqual([]);
    // Nor in the transaction's own error path.
    expect(
      layoutOf([guarded("move", [held("add")], [last("report")])]).dividers,
    ).toEqual([]);
  });
});

describe("a transaction's badge", () => {
  const none = { errors: [], warnings: [], setup: [] };
  it("asks for a step as setup, in place of the error Diagnostics lists", () => {
    expect(EMPTY_TRANSACTION).toBe("Add a step to the transaction.");
    expect(
      transactionFacts(newTransactionStep("move"), {
        ...none,
        errors: [EMPTY_TRANSACTION, "Another problem."],
      }),
    ).toEqual({
      ...none,
      errors: ["Another problem."],
      setup: [EMPTY_TRANSACTION],
    });
    // Before the check has answered, too.
    expect(transactionFacts(newTransactionStep("move"), none).setup).toEqual([
      EMPTY_TRANSACTION,
    ]);
  });

  it("shows its own failure, unlike a loop's, and never how its steps ran", () => {
    const badge = (kind: TileBadge["kind"]): TileBadge => ({
      kind,
      tone: "info",
      icons: [],
      text: kind,
    });
    for (const kept of [
      "failed",
      "handled",
      "setup",
      "error",
      "warning",
    ] as const)
      expect(frameBadge(badge(kept), true)?.kind).toBe(kept);
    for (const dropped of ["done", "changed", "added", "pinned"] as const)
      expect(frameBadge(badge(dropped), true)).toBeNull();
  });

  it("leaves a transaction with steps its own facts", () => {
    const facts = { ...none, errors: ["A problem."] };
    expect(transactionFacts(transaction("move", [step("hold")]), facts)).toBe(
      facts,
    );
  });
});
