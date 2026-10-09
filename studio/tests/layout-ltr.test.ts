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
import { readFileSync } from "node:fs";
import { parse } from "yaml";
import { describe, expect, it } from "vitest";
import {
  LTR,
  edgePath,
  fitView,
  labelScale,
  labelWidth,
  layoutLtr,
  levelOfDetail,
  midpoint,
  openView,
  pathLabelScale,
  type LtrEdge,
  type LtrLayout,
  type LtrOptions,
  type LtrTile,
} from "../src/app/editor/canvas/layout-ltr";
import { builtInKinds } from "../src/app/editor/ndv/kinds/builtin";
import { llmKind } from "../src/app/editor/ndv/kinds/llm";
import type { NodeRole } from "../src/app/editor/ndv/registry";
import {
  StructuredCanvasAdapter,
  createStep,
  freshWorkflow,
  type Step,
  type Workflow,
} from "../src/app/model";
import { workflowTemplates } from "../src/app/templates/catalog";

const roles = new Map<string, NodeRole>(
  [...builtInKinds, llmKind].map((kind) => [kind.kind, kind.role]),
);
const moreRoles: Record<string, NodeRole> = {
  agent: "ai-agent",
  callWorkflow: "sub-workflow",
};
const options = (extra: Partial<LtrOptions> = {}): LtrOptions => ({
  role: (kind) => roles.get(kind) ?? moreRoles[kind],
  triggers: [{ id: "manual", kind: "manual" }],
  ...extra,
});
const fixture = parse(
  readFileSync(
    new URL("./fixtures/vendor-payment-approval.yaml", import.meta.url),
    "utf8",
  ),
) as Workflow;
const workflowOf = (steps: Step[]): Workflow => {
  const workflow = freshWorkflow();
  workflow.spec.steps = steps;
  return workflow;
};

it("keeps a parallel output distinct from a branch named out", () => {
  const step = createStep("parallel", "p");
  step["branches"] = { out: { steps: [], output: { literal: {} } } };
  const layout = layoutLtr(workflowOf([step]), options());
  expect(new Set(layout.handles.map((item) => item.key)).size).toBe(
    layout.handles.length,
  );
  expect(layout.handles.find((item) => item.key === "p>out")?.insert).toEqual({
    owner: "root",
    index: 1,
  });
  expect(
    layout.handles.find((item) => item.key === "p:path:out")?.insert,
  ).toEqual({
    owner: "p/out",
    index: 0,
  });
});
const tile = (layout: LtrLayout, id: string): LtrTile =>
  layout.tiles.find((item) => item.id === id)!;
const card = (item: LtrTile) => ({
  x: item.x,
  y: item.y,
  width: item.width,
  height: item.height,
});
const handle = (layout: LtrLayout, key: string) =>
  layout.handles.find((item) => item.key === key)!;
const edge = (layout: LtrLayout, key: string) =>
  layout.edges.find((item) => item.key === key)!;

interface Box {
  what: string;
  left: number;
  top: number;
  right: number;
  bottom: number;
}
const box = (
  what: string,
  x: number,
  y: number,
  width: number,
  height: number,
): Box => ({ what, left: x, top: y, right: x + width, bottom: y + height });
const overlaps = (a: Box, b: Box) =>
  a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
/** Whether a path of straight segments runs through a box (touching isn't). */
const crosses = (points: readonly { x: number; y: number }[], other: Box) =>
  points.slice(1).some((to, i) => {
    const from = points[i];
    return (
      Math.min(from.x, to.x) < other.right &&
      Math.max(from.x, to.x) > other.left &&
      Math.min(from.y, to.y) < other.bottom &&
      Math.max(from.y, to.y) > other.top
    );
  });

/**
 * Every label as the canvas draws it at this zoom, in canvas units, at its
 * largest: a step's block holds a name on two lines and its subtitle, a
 * path label one line, "All branches done" up to three lines, and an empty
 * path's "Add a step" one line in its 80 px box.
 */
function drawnLabels(layout: LtrLayout, zoom: number): Box[] {
  const scale = labelScale(zoom);
  const path = pathLabelScale(zoom);
  return [
    ...layout.tiles.flatMap((item) => {
      if (item.kind === "end") return [];
      const width = labelWidth(zoom, item.shape);
      return [
        box(
          `${item.id}'s label`,
          item.x + item.width / 2 - width / 2,
          item.labelY,
          width,
          LTR.labelHeight * scale,
        ),
      ];
    }),
    ...layout.labels.map((label) =>
      box(
        `${label.owner} label`,
        label.x,
        label.y + LTR.line - LTR.line * path,
        label.width,
        LTR.line * path,
      ),
    ),
    ...layout.joins.map((join) =>
      box(
        `${join.id}'s join label`,
        join.x + join.width / 2 - LTR.joinLabel / 2,
        join.y + join.height + LTR.labelGap,
        LTR.joinLabel,
        3 * LTR.line * path,
      ),
    ),
    ...layout.slots.map((slot) =>
      box(
        `${slot.owner}'s Add a step`,
        slot.x + LTR.slot / 2 - (LTR.slotLabel * scale) / 2,
        slot.y + LTR.slot + 4,
        LTR.slotLabel * scale,
        LTR.line * scale,
      ),
    ),
  ];
}

/** Everything a label must keep clear of: steps, handles, "+", slots, joins and path ends. */
function obstaclesOf(layout: LtrLayout): Box[] {
  return [
    ...layout.tiles.flatMap((item) => [
      box(item.id, item.x, item.y, item.width, item.height),
      ...(item.kind === "trigger"
        ? []
        : [
            box(
              `${item.id}'s input`,
              item.x - 6,
              item.y + item.height / 2 - 6,
              12,
              12,
            ),
          ]),
    ]),
    ...layout.handles.flatMap((output) => [
      box(`${output.key} handle`, output.x - 6, output.y - 6, 12, 12),
      ...(output.plus
        ? [
            box(
              `${output.key}'s +`,
              output.plus.x - 12,
              output.plus.y - 12,
              24,
              24,
            ),
          ]
        : []),
    ]),
    ...layout.edges.flatMap((item) => {
      if (!item.insert) return [];
      const mid = midpoint(item);
      return [box(`${item.key}'s +`, mid.x - 12, mid.y - 12, 24, 24)];
    }),
    ...layout.slots.map((slot) =>
      box(`${slot.owner} slot`, slot.x, slot.y, LTR.slot, LTR.slot),
    ),
    ...layout.joins.map((join) =>
      box(`${join.id}'s join`, join.x, join.y, join.width, join.height),
    ),
    ...layout.terminals.map((end) =>
      box(`${end.step}'s path end`, end.x - 6, end.y - 6, 12, 12),
    ),
  ];
}

/** The vendor payment workflow, every template, and a few shapes they don't have. */
function workflowsToCheck(): [string, Workflow][] {
  const decision = createStep("switch", "decide-on-the-request");
  const parallel = createStep("parallel", "run-both-checks");
  const nested = createStep("switch", "route-by-amount");
  (nested["cases"] as { steps: Step[] }[])[0].steps.push(
    createStep("transform", "reconcile-vendor-ledgers"),
    createStep("parallel", "notify-in-parallel"),
  );
  (nested["default"] as { steps: Step[] }).steps.push(
    createStep("switch", "second-decision"),
    createStep("fail", "stop-the-payment"),
  );
  return [
    ["vendor-payment-approval", fixture],
    ...workflowTemplates.map((template): [string, Workflow] => [
      template.id,
      parse(template.yaml) as Workflow,
    ]),
    ["new decision and parallel step", workflowOf([decision, parallel])],
    [
      "nested groups",
      workflowOf([createStep("transform", "prepare-the-payment"), nested]),
    ],
  ];
}

describe("the left-to-right layout", () => {
  it("places the trigger in the first column and the main sequence on row 0 after it", () => {
    const layout = layoutLtr(fixture, options());
    expect(card(tile(layout, "$trigger:manual"))).toEqual({
      x: 96,
      y: 96,
      width: 96,
      height: 96,
    });
    const main = ["prepare-request", "approval", "route", "record-result"];
    expect(main.map((id) => tile(layout, id).x)).toEqual([344, 592, 840, 2080]);
    expect(main.map((id) => tile(layout, id).y)).toEqual([96, 96, 96, 96]);
    expect(tile(layout, "approval")).toMatchObject({
      shape: "square",
      role: "human",
      owner: "root",
      index: 1,
      count: 4,
      place: "Main sequence",
      after: { owner: "root", index: 2 },
    });
  });

  it("lays a decision's cases out as lanes below it, with a labeled output each, joined after the longest lane", () => {
    const layout = layoutLtr(fixture, options());
    expect(
      ["route:path:case 1", "route:path:case 2", "route:path:default"].map(
        (key) => {
          const output = handle(layout, key);
          return [output.x, output.y, output.label, output.insert];
        },
      ),
    ).toEqual([
      [956, 144, "Approve", { owner: "route/case 1", index: 0 }],
      [956, 592, "Reject", { owner: "route/case 2", index: 0 }],
      [956, 816, "Otherwise", { owner: "route/default", index: 0 }],
    ]);
    expect(
      layout.labels
        .filter((label) => label.owner.startsWith("route/"))
        .map((label) => [label.text, label.x, label.y, label.width]),
    ).toEqual([
      ["Approve", 950, 114, 130],
      ["Reject", 950, 562, 130],
      ["Otherwise", 950, 786, 130],
    ]);
    expect([
      tile(layout, "pay-vendor").x,
      tile(layout, "pay-vendor").y,
      tile(layout, "rejected").y,
    ]).toEqual([1088, 96, 544]);
    expect(handle(layout, "route>out")).toMatchObject({
      x: 1928,
      y: 144,
      insert: { owner: "root", index: 3 },
      plus: null,
    });
    expect(edge(layout, "route>record-result")).toMatchObject({
      a: { x: 1934, y: 144 },
      b: { x: 2080, y: 144 },
      insert: { owner: "root", index: 3 },
      name: "Insert a step between route and record-result",
      leaves: null,
      enters: "record-result",
    });
    expect(edge(layout, "route>route:path:case 2")).toMatchObject({
      shape: "fan",
      a: { x: 936, y: 144 },
      b: { x: 950, y: 592 },
      insert: null,
      enters: "rejected",
    });
  });

  it("draws a parallel step as a fork bar, one lane per branch and a join bar", () => {
    const layout = layoutLtr(fixture, options());
    expect(tile(layout, "pay-and-notify")).toMatchObject({
      x: 1336,
      y: 96,
      width: 20,
      height: 96,
      shape: "fork",
      owner: "route/case 1",
      index: 1,
      place: "Approve of route",
    });
    expect(
      ["pay-and-notify:path:ledger", "pay-and-notify:path:email"].map((key) => [
        handle(layout, key).x,
        handle(layout, key).y,
        handle(layout, key).label,
      ]),
    ).toEqual([
      [1376, 144, "ledger"],
      [1376, 368, "email"],
    ]);
    expect([
      card(tile(layout, "post-ledger-entry")),
      card(tile(layout, "send-confirmation")),
    ]).toEqual([
      { x: 1508, y: 96, width: 96, height: 96 },
      { x: 1508, y: 320, width: 96, height: 96 },
    ]);
    expect(layout.joins).toEqual([
      {
        id: "pay-and-notify",
        x: 1756,
        y: 96,
        width: 20,
        height: 96,
        label: "All branches done",
      },
    ]);
    expect(handle(layout, "pay-and-notify>out")).toMatchObject({
      x: 1776,
      y: 144,
      plus: { x: 1816, y: 144 },
      insert: { owner: "route/case 1", index: 2 },
    });
    expect(
      edge(layout, "send-confirmation>$join:pay-and-notify"),
    ).toMatchObject({
      shape: "merge",
      a: { x: 1610, y: 368 },
      b: { x: 1756, y: 144 },
      insert: null,
      leaves: "send-confirmation",
      enters: null,
    });
    expect(
      edge(layout, "post-ledger-entry>$join:pay-and-notify"),
    ).toMatchObject({
      shape: "curve",
      a: { x: 1610, y: 144 },
      b: { x: 1756, y: 144 },
    });
  });

  it("ends a Fail lane with the path-ends marker and no way to the join", () => {
    const layout = layoutLtr(fixture, options());
    expect(tile(layout, "rejected")).toMatchObject({
      shape: "octagon",
      after: null,
    });
    expect(layout.terminals).toEqual([{ step: "rejected", x: 1208, y: 592 }]);
    expect(layout.handles.some((item) => item.tile === "rejected")).toBe(false);
    expect(
      layout.edges.filter((item) => item.key.startsWith("rejected>")),
    ).toEqual([]);
  });

  it("shows an empty lane as a slot that adds a step to that path", () => {
    const layout = layoutLtr(fixture, options());
    expect(layout.slots).toEqual([
      {
        owner: "route/default",
        tile: "route",
        x: 1088,
        y: 784,
        label: "Otherwise",
        name: "Add a step to path Otherwise of route",
        insert: { owner: "route/default", index: 0 },
      },
    ]);
    expect(
      edge(layout, "route:path:default>$slot:route/default"),
    ).toMatchObject({
      a: { x: 962, y: 816 },
      b: { x: 1088, y: 816 },
      insert: null,
      leaves: null,
      enters: null,
    });
    expect(edge(layout, "$slot:route/default>$join:route")).toMatchObject({
      shape: "merge",
      a: { x: 1152, y: 816 },
      b: { x: 1922, y: 144 },
    });
  });

  it("puts a + after the last step of every path and of the main sequence, and nowhere else", () => {
    const layout = layoutLtr(fixture, options());
    expect(
      layout.handles
        .filter((item) => item.plus)
        .map((item) => [item.key, item.plus, item.insert, item.name]),
    ).toEqual([
      [
        "post-ledger-entry>out",
        { x: 1644, y: 144 },
        { owner: "pay-and-notify/ledger", index: 1 },
        "Add a step after post-ledger-entry",
      ],
      [
        "send-confirmation>out",
        { x: 1644, y: 368 },
        { owner: "pay-and-notify/email", index: 1 },
        "Add a step after send-confirmation",
      ],
      [
        "pay-and-notify>out",
        { x: 1816, y: 144 },
        { owner: "route/case 1", index: 2 },
        "Add a step after pay-and-notify",
      ],
      [
        "record-result>out",
        { x: 2216, y: 144 },
        { owner: "root", index: 4 },
        "Add a step after record-result",
      ],
    ]);
  });

  it("offers a + on each edge between two steps, inserting between them", () => {
    const layout = layoutLtr(fixture, options());
    expect(
      layout.edges
        .filter((item) => item.insert)
        .map((item) => [item.key, item.insert, item.name]),
    ).toEqual([
      [
        "pay-and-notify:path:ledger>post-ledger-entry",
        { owner: "pay-and-notify/ledger", index: 0 },
        "Insert a step between pay-and-notify and post-ledger-entry",
      ],
      [
        "pay-and-notify:path:email>send-confirmation",
        { owner: "pay-and-notify/email", index: 0 },
        "Insert a step between pay-and-notify and send-confirmation",
      ],
      [
        "pay-vendor>pay-and-notify",
        { owner: "route/case 1", index: 1 },
        "Insert a step between pay-vendor and pay-and-notify",
      ],
      [
        "route:path:case 1>pay-vendor",
        { owner: "route/case 1", index: 0 },
        "Insert a step between route and pay-vendor",
      ],
      [
        "route:path:case 2>rejected",
        { owner: "route/case 2", index: 0 },
        "Insert a step between route and rejected",
      ],
      [
        "prepare-request>approval",
        { owner: "root", index: 1 },
        "Insert a step between prepare-request and approval",
      ],
      [
        "approval>route",
        { owner: "root", index: 2 },
        "Insert a step between approval and route",
      ],
      [
        "route>record-result",
        { owner: "root", index: 3 },
        "Insert a step between route and record-result",
      ],
      [
        "$trigger:manual>prepare-request",
        { owner: "root", index: 0 },
        "Insert a step before prepare-request",
      ],
    ]);
  });

  it("ends the main sequence with End and bounds every tile, label and +", () => {
    const layout = layoutLtr(fixture, options());
    expect(tile(layout, "$end")).toMatchObject({
      x: 2328,
      y: 124,
      width: 40,
      height: 40,
      shape: "end",
      after: null,
    });
    expect(edge(layout, "record-result>$end")).toMatchObject({
      a: { x: 2182, y: 144 },
      b: { x: 2328, y: 144 },
      insert: null,
      leaves: "record-result",
      enters: "$end",
    });
    expect(layout.bounds).toEqual({
      minX: 28,
      minY: 96,
      maxX: 2368,
      maxY: 872,
    });
  });

  it("numbers tiles in document order and names where each step sits", () => {
    const layout = layoutLtr(fixture, options());
    expect(layout.tiles.map((item) => item.id)).toEqual([
      "$trigger:manual",
      "prepare-request",
      "approval",
      "route",
      "pay-vendor",
      "pay-and-notify",
      "post-ledger-entry",
      "send-confirmation",
      "rejected",
      "record-result",
      "$end",
    ]);
    expect(layout.tiles.map((item) => item.order)).toEqual([
      0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
    ]);
    expect(tile(layout, "post-ledger-entry")).toMatchObject({
      owner: "pay-and-notify/ledger",
      parent: "pay-and-notify",
      depth: 2,
      index: 0,
      count: 1,
      place: "ledger of pay-and-notify",
    });
    expect(tile(layout, "rejected")).toMatchObject({
      parent: "route",
      depth: 1,
      place: "Reject of route",
    });
  });

  it("draws each role in its own shape, and a kind it doesn't know as an action", () => {
    const kinds = [
      "action",
      "llm",
      "decisionTable",
      "transform",
      "humanTask",
      "wait",
      "signal",
      "callWorkflow",
      "agent",
      "mystery",
      "fail",
    ];
    const steps = kinds.map((kind, i) => ({ id: `s${i}`, kind }) as Step);
    const layout = layoutLtr(workflowOf(steps), options());
    expect(
      layout.tiles
        .filter((item) => item.step)
        .map((item) => [item.kind, item.shape, item.width]),
    ).toEqual([
      ["action", "square", 96],
      ["llm", "square", 96],
      ["decisionTable", "square", 96],
      ["transform", "square", 96],
      ["humanTask", "square", 96],
      ["wait", "circle", 96],
      ["signal", "circle", 96],
      ["callWorkflow", "stacked", 96],
      ["agent", "wide", 208],
      ["mystery", "square", 96],
      ["fail", "octagon", 96],
    ]);
    expect(tile(layout, "s9").role).toBe("app");
    expect(tile(layout, "s9").x - tile(layout, "s8").x).toBe(208 + 152);
  });

  it("puts an AI agent's sub-node row between its card and its label, and gives its lane more room", () => {
    const slots = [
      { id: "model", label: "Model", required: true },
      { id: "memory", label: "Memory", required: false },
      { id: "tools", label: "Tools", required: false },
      { id: "output", label: "Output", required: false },
    ];
    const agent = { id: "helper", kind: "agent" } as unknown as Step;
    const layout = layoutLtr(
      workflowOf([agent]),
      options({ slots: (kind) => (kind === "agent" ? slots : []) }),
    );
    expect(tile(layout, "helper")).toMatchObject({
      x: 344,
      y: 96,
      width: 208,
      labelY: 264,
    });
    expect(layout.subNodes).toEqual([
      {
        step: "helper",
        chips: [
          {
            id: "model",
            label: "Model*",
            required: true,
            x: 284,
            y: 224,
            handle: { x: 356, y: 192 },
          },
          {
            id: "memory",
            label: "Memory",
            required: false,
            x: 368,
            y: 224,
            handle: { x: 406, y: 192 },
          },
          {
            id: "tools",
            label: "Tools",
            required: false,
            x: 452,
            y: 224,
            handle: { x: 490, y: 192 },
          },
          {
            id: "output",
            label: "Output",
            required: false,
            x: 536,
            y: 224,
            handle: { x: 540, y: 192 },
          },
        ],
      },
    ]);
    const decision = createStep("switch", "route");
    (decision["cases"] as { steps: Step[] }[])[0].steps.push(agent);
    const nested = layoutLtr(
      workflowOf([decision]),
      options({ slots: () => slots }),
    );
    expect(handle(nested, "route:path:default").y).toBe(96 + 288 + 48);
  });

  it("gives a new decision a Case 1 and an Otherwise output, each with an empty lane", () => {
    const layout = layoutLtr(
      workflowOf([createStep("switch", "decision-1")]),
      options(),
    );
    expect(
      layout.handles
        .filter((item) => item.label)
        .map((item) => [item.label, item.name]),
    ).toEqual([
      ["Case 1", "Add a step to path Case 1 of decision-1"],
      ["Otherwise", "Add a step to path Otherwise of decision-1"],
    ]);
    expect(layout.slots.map((slot) => slot.owner)).toEqual([
      "decision-1/case 1",
      "decision-1/default",
    ]);
  });

  it("draws nothing for an empty workflow without a trigger, and a trigger's + when there are no steps", () => {
    expect(layoutLtr(freshWorkflow(), options({ triggers: [] }))).toMatchObject(
      {
        tiles: [],
        handles: [],
        edges: [],
        bounds: { minX: 96, minY: 96, maxX: 192, maxY: 192 },
      },
    );
    const lone = layoutLtr(freshWorkflow(), options());
    expect(lone.tiles.map((item) => item.id)).toEqual(["$trigger:manual"]);
    expect(lone.handles).toEqual([
      {
        key: "$trigger:manual>out",
        tile: "$trigger:manual",
        x: 192,
        y: 144,
        insert: { owner: "root", index: 0 },
        label: null,
        plus: { x: 232, y: 144 },
        name: "Add a step at the start",
      },
    ]);
  });

  it("is a pure function: the same workflow always gives the same layout", () => {
    const first = layoutLtr(fixture, options());
    const second = layoutLtr(structuredClone(fixture), options());
    expect(second).toEqual(first);
    expect(JSON.stringify(second)).toBe(JSON.stringify(first));
  });

  it("lays out every template without overlapping tiles, with edges going right and insertions that exist", () => {
    for (const template of workflowTemplates) {
      const workflow = parse(template.yaml) as Workflow;
      const layout = layoutLtr(workflow, options());
      expect(layoutLtr(workflow, options())).toEqual(layout);
      const model = new StructuredCanvasAdapter();
      model.setSource(template.yaml);
      const owners = model.owners();
      const boxes = layout.tiles.map((item) => {
        if (item.kind === "end")
          return {
            id: item.id,
            left: item.x,
            top: item.y,
            right: item.x + item.width,
            bottom: item.y + item.height,
          };
        const labelLeft = item.x + item.width / 2 - LTR.label / 2;
        return {
          id: item.id,
          left: Math.min(item.x, labelLeft),
          top: item.y,
          right: Math.max(item.x + item.width, labelLeft + LTR.label),
          bottom: item.labelY + LTR.labelHeight,
        };
      });
      for (const [i, a] of boxes.entries())
        for (const b of boxes.slice(i + 1))
          expect(
            a.right <= b.left ||
              b.right <= a.left ||
              a.bottom <= b.top ||
              b.bottom <= a.top,
            `${template.id}: ${a.id} overlaps ${b.id}`,
          ).toBe(true);
      for (const item of layout.edges)
        expect(item.b.x, `${template.id}: ${item.key}`).toBeGreaterThan(
          item.a.x,
        );
      const inserts = [
        ...layout.handles.map((item) => item.insert),
        ...layout.slots.map((item) => item.insert),
        ...layout.edges.flatMap((item) => (item.insert ? [item.insert] : [])),
      ];
      for (const insert of inserts) {
        const list = owners.get(insert.owner);
        expect(list, `${template.id}: ${insert.owner}`).toBeDefined();
        expect(insert.index).toBeGreaterThanOrEqual(0);
        expect(insert.index).toBeLessThanOrEqual(list!.length);
      }
      expect(Object.values(layout.bounds).every(Number.isFinite)).toBe(true);
    }
  });

  it("draws curves between steps and right angles from a group to its outputs", () => {
    const curve: LtrEdge = {
      key: "a>b",
      tile: "a",
      shape: "curve",
      a: { x: 0, y: 0 },
      b: { x: 100, y: 40 },
      insert: null,
      name: null,
      leaves: null,
      enters: null,
    };
    expect(edgePath(curve)).toBe("M 0 0 C 50 0, 50 40, 100 40");
    expect(midpoint(curve)).toEqual({ x: 50, y: 20 });
    expect(edgePath({ ...curve, shape: "fan", b: { x: 14, y: 400 } })).toBe(
      "M 0 0 H 7 V 400 H 14",
    );
  });

  it("brings a path into its join from another row with right angles, turning just before the join", () => {
    const merge: LtrEdge = {
      key: "c>$join:g",
      tile: "c",
      shape: "merge",
      a: { x: 0, y: 400 },
      b: { x: 300, y: 0 },
      insert: null,
      name: null,
      leaves: "c",
      enters: null,
    };
    expect(LTR.merge).toBe(64);
    expect(edgePath(merge)).toBe("M 0 400 H 236 V 0 H 300");
  });

  it("never routes a path into its join across a step, a label, a slot or another path's +", () => {
    let merges = 0;
    for (const [name, workflow] of workflowsToCheck()) {
      const layout = layoutLtr(workflow, options());
      for (const item of layout.edges.filter(
        (edge) => edge.key.includes(">$join:") && edge.a.y !== edge.b.y,
      )) {
        merges++;
        expect(item.shape, `${name}: ${item.key}`).toBe("merge");
        const turn = item.b.x - LTR.merge;
        const points = [
          item.a,
          { x: turn, y: item.a.y },
          { x: turn, y: item.b.y },
          item.b,
        ];
        // At 100% and at the zooms that draw labels larger. Its own "+"
        // sits on it: the + that follows the last step of its path.
        for (const zoom of [1, 0.5, 0.4])
          for (const obstacle of [
            ...obstaclesOf(layout),
            ...drawnLabels(layout, zoom),
          ].filter((other) => other.what !== `${item.tile}>out's +`))
            expect(
              crosses(points, obstacle),
              `${name} at ${zoom}: ${item.key} crosses ${obstacle.what}`,
            ).toBe(false);
      }
    }
    expect(merges).toBeGreaterThan(4);
  });

  it("fans a group out to its outputs clear of every step and label but the group's own, whose text sits on the canvas color", () => {
    let fans = 0;
    for (const [name, workflow] of workflowsToCheck()) {
      const layout = layoutLtr(workflow, options());
      for (const item of layout.edges.filter((edge) => edge.shape === "fan")) {
        fans++;
        const turn = item.a.x + (item.b.x - item.a.x) / 2;
        const points = [
          item.a,
          { x: turn, y: item.a.y },
          { x: turn, y: item.b.y },
          item.b,
        ];
        for (const zoom of [1, 0.5, 0.4])
          for (const obstacle of [
            ...obstaclesOf(layout),
            ...drawnLabels(layout, zoom),
          ].filter((other) => other.what !== `${item.tile}'s label`))
            expect(
              crosses(points, obstacle),
              `${name} at ${zoom}: ${item.key} crosses ${obstacle.what}`,
            ).toBe(false);
      }
    }
    expect(fans).toBeGreaterThan(8);
  });

  it("keeps every label clear of steps, handles, +, slots, joins and other labels at 40% and 50%", () => {
    for (const [name, workflow] of workflowsToCheck()) {
      const layout = layoutLtr(workflow, options());
      for (const zoom of [0.5, 0.4]) {
        const labels = drawnLabels(layout, zoom);
        const obstacles = obstaclesOf(layout);
        labels.forEach((label, i) => {
          for (const other of [...labels.slice(i + 1), ...obstacles])
            expect(
              overlaps(label, other),
              `${name} at ${zoom}: ${label.what} overlaps ${other.what}`,
            ).toBe(false);
        });
      }
    }
  });

  it("drops labels below 40% and draws plain tiles below 30%", () => {
    expect(
      [1, 0.5, 0.45, 0.4, 0.39, 0.3, 0.29, 0.25].map(levelOfDetail),
    ).toEqual([
      "full",
      "full",
      "full",
      "full",
      "compact",
      "compact",
      "minimal",
      "minimal",
    ]);
  });

  it("draws label text larger as the zoom drops, so names never show below 12 px and subtitles below 11 px", () => {
    expect(labelScale(2)).toBe(1);
    expect(labelScale(1)).toBe(1);
    expect(labelScale(12 / 13)).toBe(1);
    expect(labelScale(0.923)).toBeCloseTo(1, 3);
    expect(labelScale(0.5)).toBeCloseTo(1.846, 3);
    expect(labelScale(0.4)).toBeCloseTo(2.308, 3);
    // A name is 13 px (--type-label), a subtitle 12 px (--type-caption).
    for (const zoom of [0.3, 0.4, 0.45, 0.5, 0.6, 0.75, 0.9, 1, 1.5, 2]) {
      const scale = labelScale(zoom);
      expect(scale).toBeGreaterThanOrEqual(1);
      expect(13 * scale * zoom).toBeGreaterThanOrEqual(12 - 1e-9);
      expect(12 * scale * zoom).toBeGreaterThanOrEqual(11);
    }
    // At 40%, the lowest zoom that shows labels, a block of three 16 px
    // lines (a name on two lines, then its subtitle) still ends above the
    // next path down: above its label, which rises from its edge, and so
    // above its tile.
    const drawn = LTR.labelHeight * labelScale(0.4);
    expect(LTR.labelHeight).toBe(3 * LTR.line);
    const nextPathLabel = (lane: number) =>
      lane + LTR.tile / 2 - LTR.pathLabelRise - LTR.line * pathLabelScale(0.4);
    expect(LTR.tile + LTR.labelGap + drawn + 8).toBeLessThan(
      nextPathLabel(LTR.lane),
    );
    expect(nextPathLabel(LTR.lane)).toBeLessThan(LTR.lane + LTR.tile / 2);
    expect(LTR.tile + LTR.subNodeRow + LTR.labelGap + drawn + 8).toBeLessThan(
      nextPathLabel(LTR.agentLane),
    );
  });

  it("widens a step's label block below 100% to its column, less a visible gap", () => {
    expect(labelWidth(2)).toBe(LTR.label);
    expect(labelWidth(1)).toBe(LTR.label);
    expect(labelWidth(1, "fork")).toBe(LTR.label);
    expect(labelWidth(0.99)).toBe(LTR.labelWide);
    expect(labelWidth(0.5)).toBe(LTR.labelWide);
    expect(labelWidth(0.4, "square")).toBe(LTR.labelWide);
    // Two tiles' blocks, one column apart, leave 16 units between them.
    expect(LTR.tile + LTR.edge - LTR.labelWide).toBe(16);
    // A parallel step's bar is 76 units narrower than a tile, and its
    // column too: its block gives up the same 76 units.
    expect(labelWidth(0.5, "fork")).toBe(LTR.labelWide - (LTR.tile - LTR.bar));
    // A 24-character name takes two lines of 12 or more characters at 50%,
    // in a 13 px font drawn larger by labelScale, inside 4 px of padding.
    expect(LTR.labelWide / labelScale(0.5) - 8).toBeGreaterThan(108);
  });

  it("draws path labels and All branches done larger as the zoom drops, never below 10 px", () => {
    expect(pathLabelScale(2)).toBe(1);
    expect(pathLabelScale(1)).toBe(1);
    expect(pathLabelScale(10 / 12)).toBe(1);
    expect(pathLabelScale(0.5)).toBeCloseTo(1.667, 3);
    expect(pathLabelScale(0.4)).toBeCloseTo(2.083, 3);
    // They are 12 px (--type-caption) at 100% and above.
    for (const zoom of [0.3, 0.4, 0.45, 0.5, 0.6, 0.75, 0.9, 1, 1.5, 2]) {
      const scale = pathLabelScale(zoom);
      expect(scale).toBeGreaterThanOrEqual(1);
      expect(12 * scale * zoom).toBeGreaterThanOrEqual(10 - 1e-9);
      expect(scale === 1 || 12 * scale * zoom < 10 + 1e-9).toBe(true);
    }
    // A path label sits above its path's edge, from its output handle's left
    // to just before the path's first step: at 40% it has room for about
    // 10 characters (about 62 px of 12 px text).
    const layout = layoutLtr(fixture, options());
    const otherwise = handle(layout, "route:path:default");
    const label = layout.labels.find((item) => item.owner === "route/default")!;
    expect(label.x).toBe(otherwise.x - 6);
    expect(label.y + LTR.line).toBe(otherwise.y - LTR.pathLabelRise);
    expect(label.x + label.width).toBe(layout.slots[0].x - 8);
    expect(label.width / pathLabelScale(0.4)).toBeGreaterThan(62);
  });

  it("opens a workflow fitted when it fits at 50% or more, else at 50% from the trigger at the left margin", () => {
    const size = { width: 1440, height: 900 };
    const small = { minX: 60, minY: 96, maxX: 1060, maxY: 496 };
    expect(openView(small, size)).toEqual(fitView(small, size));
    expect(openView(small, size).zoom).toBe(1);
    const medium = { minX: 60, minY: 96, maxX: 60 + 1344 / 0.7, maxY: 496 };
    expect(openView(medium, size).zoom).toBeCloseTo(0.7, 10);
    const wide = { minX: 60, minY: 96, maxX: 4060, maxY: 496 };
    expect(openView(wide, size)).toEqual({
      zoom: 0.5,
      pan: { x: 48 - 60 * 0.5, y: (900 - 400 * 0.5) / 2 - 96 * 0.5 },
    });
    const tall = { minX: 60, minY: 96, maxX: 4060, maxY: 3096 };
    expect(openView(tall, size)).toEqual({
      zoom: 0.5,
      pan: { x: 48 - 60 * 0.5, y: 48 - 96 * 0.5 },
    });
    // The vendor payment workflow in a canvas beside both side panels.
    const layout = layoutLtr(fixture, options());
    const view = openView(layout.bounds, { width: 766, height: 670 });
    const trigger = tile(layout, "$trigger:manual");
    expect(view.zoom).toBe(0.5);
    expect(
      (trigger.x + trigger.width / 2 - labelWidth(view.zoom) / 2) * view.zoom +
        view.pan.x,
    ).toBe(48);
    expect(levelOfDetail(view.zoom)).toBe("full");
    // fitView alone would go below 50% for it; the canvas's Fit view uses
    // openView, so it chooses 50% too.
    expect(
      fitView(layout.bounds, { width: 766, height: 670 }).zoom,
    ).toBeLessThan(0.5);
  });

  it("fits the whole workflow, never past 100% and never below 25%", () => {
    expect(
      fitView(
        { minX: 60, minY: 96, maxX: 1060, maxY: 496 },
        { width: 1440, height: 900 },
      ),
    ).toEqual({ zoom: 1, pan: { x: 160, y: 154 } });
    const wide = fitView(
      { minX: 60, minY: 96, maxX: 232060, maxY: 496 },
      { width: 1440, height: 900 },
    );
    expect(wide.zoom).toBe(0.25);
    expect(wide.pan).toEqual({
      x: 48 - 60 * 0.25,
      y: (900 - 400 * 0.25) / 2 - 96 * 0.25,
    });
  });
});
