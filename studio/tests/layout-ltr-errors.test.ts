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
// A step that continues with error output has two outputs on the canvas:
// Success, on its row, straight to the join after it, and Error, one lane
// below, leading to its error path, which merges into the same join. The
// lane is laid out like a decision's path. Sizes are in canvas units.
import { describe, expect, it } from "vitest";
import {
  LTR,
  layoutLtr,
  type LtrLayout,
  type LtrOptions,
} from "../src/app/editor/canvas/layout-ltr";
import { errorLane, failureText } from "../src/app/editor/canvas/error-lane";
import type { NodeRole } from "../src/app/editor/ndv/registry";
import { createStep, freshWorkflow, type Step } from "../src/app/model";

const roles: Record<string, NodeRole> = {
  action: "app",
  forEach: "loop",
  transform: "transform",
  switch: "decision",
};
const options: LtrOptions = {
  role: (kind) => roles[kind],
  triggers: [{ id: "manual", kind: "manual" }],
};
const step = (id: string, kind = "transform"): Step => ({ id, kind }) as Step;
const guarded = (id: string, path: Step[]): Step => ({
  id,
  kind: "action",
  uses: "requests.submit@1.0.0",
  onError: "errorOutput",
  errorPath: { steps: path, output: { literal: null } },
});
const layoutOf = (steps: Step[]): LtrLayout => {
  const workflow = freshWorkflow();
  workflow.spec.steps = steps;
  return layoutLtr(workflow, options);
};
const tile = (layout: LtrLayout, id: string) =>
  layout.tiles.find((item) => item.id === id)!;
const edge = (layout: LtrLayout, key: string) =>
  layout.edges.find((item) => item.key === key);
const handle = (layout: LtrLayout, key: string) =>
  layout.handles.find((item) => item.key === key);
const START = LTR.originX + LTR.tile + LTR.edge;
const ROW = LTR.originY;
const MID = ROW + LTR.tile / 2;
/** The error lane's row, one lane under the step's. */
const ERROR = MID + LTR.lane;

describe("the error lane of a step", () => {
  it("is named On error and lists its steps; a step without a path has none", () => {
    const submit = guarded("submit", [step("notify-team")]);
    expect(errorLane(submit)).toEqual({
      owner: "submit/errorPath",
      label: "On error",
      handleName: "Add a step to the error path of submit",
      steps: [step("notify-team")],
    });
    expect(
      errorLane({ ...step("lookup", "action"), onError: "continue" }),
    ).toBeNull();
    expect(errorLane(step("plain"))).toBeNull();
  });

  it("names a tile by what its failure does", () => {
    expect(
      failureText({ ...step("lookup", "action"), onError: "continue" }),
    ).toBe("continues on error");
    expect(failureText(guarded("submit", []))).toBe(
      "continues with error output",
    );
    expect(failureText(step("plain"))).toBe("");
  });
});

describe("a step that continues with error output", () => {
  it("keeps one output when it only continues on error", () => {
    const layout = layoutOf([
      { ...step("lookup", "action"), onError: "continue" },
      step("after"),
    ]);
    expect(
      layout.handles.filter((item) => item.tile === "lookup").map((h) => h.key),
    ).toEqual(["lookup>out"]);
    expect(tile(layout, "after").x).toBe(START + LTR.tile + LTR.edge);
  });

  it("has a Success and an Error output, an empty lane's slot, and a join before the next step", () => {
    const layout = layoutOf([guarded("submit", []), step("summarize")]);
    const right = START + LTR.tile;
    const lanesLeft = right + LTR.edge;
    const joinX = lanesLeft + LTR.slot + LTR.edge - 6;
    expect(handle(layout, "submit:path:success")).toMatchObject({
      tile: "submit",
      x: right + LTR.rail,
      y: MID,
      label: "Success",
      plus: null,
      insert: { owner: "root", index: 1 },
    });
    expect(handle(layout, "submit:path:errorPath")).toEqual({
      key: "submit:path:errorPath",
      tile: "submit",
      x: right + LTR.rail,
      y: ERROR,
      insert: { owner: "submit/errorPath", index: 0 },
      label: "Error",
      plus: null,
      name: "Add a step to the error path of submit",
      tone: "danger",
    });
    expect(layout.slots).toEqual([
      {
        owner: "submit/errorPath",
        tile: "submit",
        x: lanesLeft,
        y: ERROR - LTR.slot / 2,
        label: "Error",
        name: "Add a step to the error path of submit",
        insert: { owner: "submit/errorPath", index: 0 },
      },
    ]);
    // Both outputs fan from the tile; Success runs straight to the join.
    expect(edge(layout, "submit>submit:path:success")).toMatchObject({
      shape: "fan",
      a: { x: right, y: MID },
      b: { x: right + LTR.rail - 6, y: MID },
    });
    expect(edge(layout, "submit>submit:path:errorPath")).toMatchObject({
      shape: "fan",
      a: { x: right, y: MID },
      b: { x: right + LTR.rail - 6, y: ERROR },
    });
    expect(edge(layout, "submit:path:success>$join:submit")).toMatchObject({
      shape: "curve",
      a: { x: right + LTR.rail + 6, y: MID },
      b: { x: joinX, y: MID },
      leaves: "submit",
    });
    expect(edge(layout, "$slot:submit/errorPath>$join:submit")).toMatchObject({
      shape: "merge",
      b: { x: joinX, y: MID },
    });
    // The labels: Success muted, Error in the danger tone.
    expect(
      layout.labels.map(({ owner, text, tone }) => ({ owner, text, tone })),
    ).toEqual([
      { owner: "submit/success", text: "Success", tone: "muted" },
      { owner: "submit/errorPath", text: "Error", tone: "danger" },
    ]);
    // The next step sits after the join, with one "+" at the end of the sequence.
    expect(tile(layout, "summarize").x).toBe(joinX + 6 + LTR.edge);
    expect(edge(layout, "submit>summarize")).toMatchObject({
      a: { x: joinX + 12, y: MID },
      insert: { owner: "root", index: 1 },
    });
    expect(
      layout.handles.filter((item) => item.plus).map((item) => item.key),
    ).toEqual(["summarize>out"]);
  });

  it("lays out the steps of its error path on the Error row, merging into the join", () => {
    const layout = layoutOf([
      guarded("submit", [step("notify-team"), step("flag-request")]),
      step("summarize"),
    ]);
    const lanesLeft = START + LTR.tile + LTR.edge;
    expect(tile(layout, "notify-team")).toMatchObject({
      x: lanesLeft,
      y: ERROR - LTR.tile / 2,
      owner: "submit/errorPath",
      parent: "submit",
      place: "Error path of submit",
      depth: 1,
    });
    expect(tile(layout, "flag-request").x).toBe(
      lanesLeft + LTR.tile + LTR.edge,
    );
    const joinX = lanesLeft + 2 * (LTR.tile + LTR.edge) - 6;
    expect(edge(layout, "submit:path:errorPath>notify-team")).toMatchObject({
      insert: { owner: "submit/errorPath", index: 0 },
      name: "Insert a step between submit and notify-team",
      enters: "notify-team",
    });
    expect(edge(layout, "flag-request>$join:submit")).toMatchObject({
      shape: "merge",
      b: { x: joinX, y: MID },
      leaves: "flag-request",
    });
    // The lane's own "+" after its last step.
    expect(handle(layout, "flag-request>out")).toMatchObject({
      plus: { x: lanesLeft + 2 * LTR.tile + LTR.edge + LTR.plus, y: ERROR },
      insert: { owner: "submit/errorPath", index: 2 },
    });
    expect(tile(layout, "summarize").x).toBe(joinX + 6 + LTR.edge);
  });

  it("nests its lane inside a decision path, which pushes the next path down", () => {
    const route: Step = {
      ...createStep("switch", "route"),
      cases: [
        {
          when: { literal: true },
          steps: [guarded("submit", [step("notify-team")])],
          output: { literal: {} },
        },
      ],
      default: { steps: [step("record")], output: { literal: {} } },
    };
    const layout = layoutOf([route]);
    const submit = tile(layout, "submit");
    const notify = tile(layout, "notify-team");
    const record = tile(layout, "record");
    expect(notify.y).toBe(submit.y + LTR.lane);
    // Otherwise starts below the error lane: max(224, its drop + 40) under the case.
    expect(record.y).toBeGreaterThanOrEqual(notify.y + LTR.lane);
  });

  it("grows a loop's frame down to hold its lane", () => {
    const orders: Step = {
      ...createStep("forEach", "orders"),
      items: { ref: "/input/orders" },
      body: {
        steps: [guarded("check", [step("skip-item")])],
        output: { ref: "/item" },
      },
    };
    const layout = layoutOf([orders, step("after")]);
    const frame = layout.frames.find((item) => item.id === "orders")!;
    const skip = tile(layout, "skip-item");
    // Its labels and the frame's padding stay inside the frame.
    expect(frame.y + frame.height).toBeGreaterThanOrEqual(
      skip.labelY + LTR.labelHeight + 24,
    );
    expect(frame.x + frame.width).toBeGreaterThan(skip.x + LTR.tile);
    expect(tile(layout, "after").x).toBeGreaterThan(frame.x + frame.width);
  });
});
