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
  layoutLtr,
  levelOfDetail,
  midpoint,
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
    expect(main.map((id) => tile(layout, id).x)).toEqual([328, 560, 792, 1936]);
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
      ["route:case 1", "route:case 2", "route:default"].map((key) => {
        const output = handle(layout, key);
        return [output.x, output.y, output.label, output.insert];
      }),
    ).toEqual([
      [908, 144, "Approve", { owner: "route/case 1", index: 0 }],
      [908, 560, "Reject", { owner: "route/case 2", index: 0 }],
      [908, 768, "Otherwise", { owner: "route/default", index: 0 }],
    ]);
    expect(
      layout.labels
        .filter((label) => label.owner.startsWith("route/"))
        .map((label) => [label.text, label.x, label.y, label.width]),
    ).toEqual([
      ["Approve", 920, 116, 96],
      ["Reject", 920, 532, 96],
      ["Otherwise", 920, 740, 96],
    ]);
    expect([
      tile(layout, "pay-vendor").x,
      tile(layout, "pay-vendor").y,
      tile(layout, "rejected").y,
    ]).toEqual([1024, 96, 512]);
    expect(handle(layout, "route:out")).toMatchObject({
      x: 1800,
      y: 144,
      insert: { owner: "root", index: 3 },
      plus: null,
    });
    expect(edge(layout, "route>record-result")).toMatchObject({
      a: { x: 1806, y: 144 },
      b: { x: 1936, y: 144 },
      insert: { owner: "root", index: 3 },
      name: "Insert a step between route and record-result",
      leaves: null,
      enters: "record-result",
    });
    expect(edge(layout, "route>route:case 2")).toMatchObject({
      shape: "fan",
      a: { x: 888, y: 144 },
      b: { x: 902, y: 560 },
      insert: null,
      enters: "rejected",
    });
  });

  it("draws a parallel step as a fork bar, one lane per branch and a join bar", () => {
    const layout = layoutLtr(fixture, options());
    expect(tile(layout, "pay-and-notify")).toMatchObject({
      x: 1256,
      y: 96,
      width: 20,
      height: 96,
      shape: "fork",
      owner: "route/case 1",
      index: 1,
      place: "Approve of route",
    });
    expect(
      ["pay-and-notify:ledger", "pay-and-notify:email"].map((key) => [
        handle(layout, key).x,
        handle(layout, key).y,
        handle(layout, key).label,
      ]),
    ).toEqual([
      [1296, 144, "ledger"],
      [1296, 352, "email"],
    ]);
    expect([
      card(tile(layout, "post-ledger-entry")),
      card(tile(layout, "send-confirmation")),
    ]).toEqual([
      { x: 1412, y: 96, width: 96, height: 96 },
      { x: 1412, y: 304, width: 96, height: 96 },
    ]);
    expect(layout.joins).toEqual([
      {
        id: "pay-and-notify",
        x: 1644,
        y: 96,
        width: 20,
        height: 96,
        label: "All branches done",
      },
    ]);
    expect(handle(layout, "pay-and-notify:out")).toMatchObject({
      x: 1664,
      y: 144,
      plus: { x: 1704, y: 144 },
      insert: { owner: "route/case 1", index: 2 },
    });
    expect(
      edge(layout, "send-confirmation>$join:pay-and-notify"),
    ).toMatchObject({
      a: { x: 1514, y: 352 },
      b: { x: 1644, y: 144 },
      insert: null,
      leaves: "send-confirmation",
      enters: null,
    });
  });

  it("ends a Fail lane with the path-ends marker and no way to the join", () => {
    const layout = layoutLtr(fixture, options());
    expect(tile(layout, "rejected")).toMatchObject({
      shape: "octagon",
      after: null,
    });
    expect(layout.terminals).toEqual([{ step: "rejected", x: 1144, y: 560 }]);
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
        x: 1024,
        y: 736,
        label: "Otherwise",
        name: "Add a step to path Otherwise of route",
        insert: { owner: "route/default", index: 0 },
      },
    ]);
    expect(edge(layout, "route:default>$slot:route/default")).toMatchObject({
      a: { x: 914, y: 768 },
      b: { x: 1024, y: 768 },
      insert: null,
      leaves: null,
      enters: null,
    });
    expect(edge(layout, "$slot:route/default>$join:route")).toMatchObject({
      a: { x: 1088, y: 768 },
      b: { x: 1794, y: 144 },
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
        "post-ledger-entry:out",
        { x: 1548, y: 144 },
        { owner: "pay-and-notify/ledger", index: 1 },
        "Add a step after post-ledger-entry",
      ],
      [
        "send-confirmation:out",
        { x: 1548, y: 352 },
        { owner: "pay-and-notify/email", index: 1 },
        "Add a step after send-confirmation",
      ],
      [
        "pay-and-notify:out",
        { x: 1704, y: 144 },
        { owner: "route/case 1", index: 2 },
        "Add a step after pay-and-notify",
      ],
      [
        "record-result:out",
        { x: 2072, y: 144 },
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
        "pay-and-notify:ledger>post-ledger-entry",
        { owner: "pay-and-notify/ledger", index: 0 },
        "Insert a step between pay-and-notify and post-ledger-entry",
      ],
      [
        "pay-and-notify:email>send-confirmation",
        { owner: "pay-and-notify/email", index: 0 },
        "Insert a step between pay-and-notify and send-confirmation",
      ],
      [
        "pay-vendor>pay-and-notify",
        { owner: "route/case 1", index: 1 },
        "Insert a step between pay-vendor and pay-and-notify",
      ],
      [
        "route:case 1>pay-vendor",
        { owner: "route/case 1", index: 0 },
        "Insert a step between route and pay-vendor",
      ],
      [
        "route:case 2>rejected",
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
      x: 2168,
      y: 124,
      width: 40,
      height: 40,
      shape: "end",
      after: null,
    });
    expect(edge(layout, "record-result>$end")).toMatchObject({
      a: { x: 2038, y: 144 },
      b: { x: 2168, y: 144 },
      insert: null,
      leaves: "record-result",
      enters: "$end",
    });
    expect(layout.bounds).toEqual({
      minX: 60,
      minY: 96,
      maxX: 2208,
      maxY: 824,
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
    expect(tile(layout, "s9").x - tile(layout, "s8").x).toBe(208 + 136);
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
      x: 328,
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
            x: 268,
            y: 224,
            handle: { x: 340, y: 192 },
          },
          {
            id: "memory",
            label: "Memory",
            required: false,
            x: 352,
            y: 224,
            handle: { x: 390, y: 192 },
          },
          {
            id: "tools",
            label: "Tools",
            required: false,
            x: 436,
            y: 224,
            handle: { x: 474, y: 192 },
          },
          {
            id: "output",
            label: "Output",
            required: false,
            x: 520,
            y: 224,
            handle: { x: 524, y: 192 },
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
    expect(handle(nested, "route:default").y).toBe(96 + 272 + 48);
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
        key: "$trigger:manual:out",
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

  it("drops labels below 50% and draws plain tiles below 30%", () => {
    expect([1, 0.5, 0.49, 0.3, 0.29, 0.25].map(levelOfDetail)).toEqual([
      "full",
      "full",
      "compact",
      "compact",
      "minimal",
      "minimal",
    ]);
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
