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
// The canvas geometry, as one pure function: from a workflow to its tiles,
// output handles, edges, empty-lane slots, joins and lane labels, laid out
// left to right. Every size is a constant, never measured from text, so
// font metrics can't move a step, and steps are never placed by hand.
import { branchName } from "../../designer/conditions";
import type { Step, Workflow } from "../../model";
import type { NodeRole } from "../ndv/registry";

/** Sizes in canvas units: one unit is one CSS pixel at 100% zoom. */
export const LTR = {
  originX: 96,
  originY: 96,
  tile: 96,
  agentWidth: 208,
  /** Parallel fork and join bars. */
  bar: 20,
  /** Room after a tile: its edge, the edge's "+" and a branch label. */
  edge: 136,
  lane: 208,
  agentLane: 272,
  subNodeRow: 64,
  label: 168,
  labelGap: 8,
  labelHeight: 48,
  /** "All branches done" under a join bar, centered on it. */
  joinLabel: 120,
  /**
   * A path that joins from another row turns this far before the join:
   * clear of the longest path's last "+" and of the join bar's label.
   */
  merge: 64,
  /** Decision and parallel outputs sit this far right of the tile or fork bar. */
  rail: 20,
  /** A free output's "+", from the handle's center. */
  plus: 40,
  end: 40,
  slot: 64,
  chipWidth: 76,
  chipHeight: 28,
} as const;
const MID = LTR.tile / 2;

export interface Point {
  x: number;
  y: number;
}
/** Where a new step goes: a sequence ("root" or "<group>/<lane>") and an index in it. */
export interface Insertion {
  owner: string;
  index: number;
}
export type TriggerKind =
  | "manual"
  | "webhook"
  | "schedule"
  | "broker"
  | "email"
  | "provider"
  | "called";
export interface TriggerInput {
  id: string;
  kind: TriggerKind;
}
export interface SlotInput {
  id: string;
  label: string;
  required: boolean;
}
export interface LtrOptions {
  /** A step kind's role; a kind without one draws as an app action. */
  role(kind: string): NodeRole | undefined;
  /** An AI agent kind's slots, in order. */
  slots?(kind: string): readonly SlotInput[];
  /** Trigger tiles, stacked in the first column. */
  triggers: readonly TriggerInput[];
}
export type TileShape =
  | "square"
  | "wide"
  | "circle"
  | "octagon"
  | "stacked"
  | "trigger"
  | "end"
  | "fork";
export interface LtrTile {
  /** The step ID; `$trigger:<id>` for a trigger, `$end` for End. */
  id: string;
  /** The step kind, or "trigger" or "end". */
  kind: string;
  role: NodeRole | "end";
  shape: TileShape;
  /** The card, in canvas units. */
  x: number;
  y: number;
  width: number;
  height: number;
  /** The step itself; null for a trigger and End. */
  step: Step | null;
  /** "root" or "<group>/<lane>"; "" for a trigger and End. */
  owner: string;
  index: number;
  /** The decision or parallel step whose lane holds it; null in the main sequence. */
  parent: string | null;
  depth: number;
  /** Position in document order. */
  order: number;
  /** How many steps its sequence has. */
  count: number;
  /** "Main sequence", or a lane and its group: "Approve of route". */
  place: string;
  /** Where a step added right after it goes; null for Fail and End. */
  after: Insertion | null;
  /** The top of its label block. */
  labelY: number;
}
export interface LtrHandle {
  /** "<tile>:out", or "<group>:<lane>" for a decision case or a parallel branch. */
  key: string;
  /** The tile whose output it is. */
  tile: string;
  x: number;
  y: number;
  insert: Insertion;
  /** A decision case's or a parallel branch's label. */
  label: string | null;
  /** The "+" shown when nothing follows this output in its sequence. */
  plus: Point | null;
  name: string;
}
export interface LtrEdge {
  key: string;
  /** The tile whose Tab group holds the edge's "+". */
  tile: string;
  /**
   * "fan": from a group to one of its outputs; "merge": from the end of a
   * path into its group's join on another row. Both are drawn with right angles.
   */
  shape: "curve" | "fan" | "merge";
  a: Point;
  b: Point;
  /** What the edge's "+" inserts; null for an edge without a "+" of its own. */
  insert: Insertion | null;
  name: string | null;
  /** The step a run must finish to take this edge; null from a trigger or a group's output. */
  leaves: string | null;
  /** The step (or "$end") a run reaches over it; null when it ends at a join. */
  enters: string | null;
}
export interface LtrSlot {
  owner: string;
  tile: string;
  x: number;
  y: number;
  label: string;
  name: string;
  insert: Insertion;
}
export interface LtrJoin {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
  label: string;
}
export interface LtrLaneLabel {
  owner: string;
  text: string;
  x: number;
  y: number;
  width: number;
}
export interface LtrTerminal {
  step: string;
  x: number;
  y: number;
}
export interface LtrChip {
  id: string;
  label: string;
  required: boolean;
  x: number;
  y: number;
  handle: Point;
}
export interface LtrSubNodeRow {
  step: string;
  chips: LtrChip[];
}
export interface LtrBounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}
export interface LtrLayout {
  tiles: LtrTile[];
  handles: LtrHandle[];
  edges: LtrEdge[];
  slots: LtrSlot[];
  joins: LtrJoin[];
  labels: LtrLaneLabel[];
  terminals: LtrTerminal[];
  subNodes: LtrSubNodeRow[];
  bounds: LtrBounds;
}

const SHAPES: Record<NodeRole, TileShape> = {
  trigger: "trigger",
  app: "square",
  "ai-task": "square",
  "ai-agent": "wide",
  rules: "square",
  decision: "square",
  parallel: "fork",
  loop: "square",
  "sub-workflow": "stacked",
  transform: "square",
  human: "square",
  wait: "circle",
  signal: "circle",
  fail: "octagon",
};

const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const stepsOf = (branch: unknown): Step[] => {
  const steps = record(branch)["steps"];
  return Array.isArray(steps) ? (steps as Step[]) : [];
};
const isGroup = (step: Step) =>
  step.kind === "switch" || step.kind === "parallel";

/** A group's lanes in order: a decision's cases then Otherwise, or a parallel step's branches. */
export function lanesOf(step: Step): { name: string; steps: Step[] }[] {
  if (step.kind === "switch")
    return [
      ...(Array.isArray(step["cases"]) ? step["cases"] : []).map(
        (branch: unknown, i) => ({
          name: `case ${i + 1}`,
          steps: stepsOf(branch),
        }),
      ),
      { name: "default", steps: stepsOf(step["default"]) },
    ];
  if (step.kind === "parallel")
    return Object.entries(record(step["branches"])).map(([name, branch]) => ({
      name,
      steps: stepsOf(branch),
    }));
  return [];
}

/** Whether a run can get past these steps: no Fail on the way, and a way through each group. */
export function completes(steps: readonly Step[]): boolean {
  return steps.every((step) => {
    if (step.kind === "fail") return false;
    if (!isGroup(step)) return true;
    const lanes = lanesOf(step);
    return step.kind === "parallel"
      ? lanes.every((lane) => completes(lane.steps))
      : lanes.some((lane) => completes(lane.steps));
  });
}

/** A decision case's label, "Case 2" until its condition is set; a branch's name. */
function laneLabel(step: Step, name: string, workflow: Workflow): string {
  if (step.kind !== "switch") return name;
  const label = branchName(step, name, workflow);
  return label === "Condition not set" ? `Case ${name.slice(5)}` : label;
}

/** An AI agent's slots: diamonds on the card's bottom edge, chips under them. */
function subNodeRow(tile: LtrTile, slots: readonly SlotInput[]): LtrSubNodeRow {
  const spacing = Math.max(LTR.agentWidth / slots.length, LTR.chipWidth + 8);
  const center = tile.x + tile.width / 2;
  return {
    step: tile.id,
    chips: slots.map((slot, i) => {
      const x = center + (i - (slots.length - 1) / 2) * spacing;
      return {
        id: slot.id,
        label: slot.required ? `${slot.label}*` : slot.label,
        required: slot.required,
        x: x - LTR.chipWidth / 2,
        y: tile.y + LTR.tile + 32,
        handle: {
          x: Math.min(Math.max(x, tile.x + 12), tile.x + tile.width - 12),
          y: tile.y + LTR.tile,
        },
      };
    }),
  };
}

/** One element of a sequence: where edges enter it and where its output leaves. */
interface Placed {
  id: string;
  in: Point;
  out: Point | null;
  group: boolean;
}
type TileBase = Pick<
  LtrTile,
  | "kind"
  | "step"
  | "owner"
  | "index"
  | "parent"
  | "depth"
  | "count"
  | "place"
  | "after"
>;

export function layoutLtr(workflow: Workflow, options: LtrOptions): LtrLayout {
  const layout: LtrLayout = {
    tiles: [],
    handles: [],
    edges: [],
    slots: [],
    joins: [],
    labels: [],
    terminals: [],
    subNodes: [],
    bounds: { minX: 0, minY: 0, maxX: 0, maxY: 0 },
  };
  const roleOf = (step: Step): NodeRole => options.role(step.kind) ?? "app";
  const agent = (step: Step) => !isGroup(step) && roleOf(step) === "ai-agent";
  const widths = new Map<Step, number>();
  const heights = new Map<Step, number>();
  const laneWidth = (steps: readonly Step[]): number =>
    steps.length
      ? steps.reduce((sum, step) => sum + advance(step), 0)
      : LTR.slot + LTR.edge;
  const laneHeight = (steps: readonly Step[]): number =>
    Math.max(LTR.lane, ...steps.map(height));
  function advance(step: Step): number {
    let width = widths.get(step);
    if (width === undefined) {
      const inner = Math.max(
        0,
        ...lanesOf(step).map((lane) => laneWidth(lane.steps)),
      );
      width =
        step.kind === "parallel"
          ? 2 * (LTR.bar + LTR.edge) + inner
          : step.kind === "switch"
            ? LTR.tile + 2 * LTR.edge + inner
            : (agent(step) ? LTR.agentWidth : LTR.tile) + LTR.edge;
      widths.set(step, width);
    }
    return width;
  }
  function height(step: Step): number {
    let value = heights.get(step);
    if (value === undefined) {
      value = isGroup(step)
        ? Math.max(
            LTR.lane,
            lanesOf(step).reduce(
              (sum, lane) => sum + laneHeight(lane.steps),
              0,
            ),
          )
        : agent(step)
          ? LTR.agentLane
          : LTR.lane;
      heights.set(step, value);
    }
    return value;
  }
  let order = 0;

  function sequence(
    steps: readonly Step[],
    owner: string,
    parent: string | null,
    depth: number,
    left: number,
    top: number,
    place: string,
  ): { placed: Placed[]; end: number } {
    const placed: Placed[] = [];
    let x = left;
    steps.forEach((step, index) => {
      const base: TileBase = {
        kind: step.kind,
        step,
        owner,
        index,
        parent,
        depth,
        count: steps.length,
        place,
        after: step.kind === "fail" ? null : { owner, index: index + 1 },
      };
      if (isGroup(step)) placed.push(group(step, base, x, top));
      else {
        const role = roleOf(step);
        const width = agent(step) ? LTR.agentWidth : LTR.tile;
        const slots = agent(step) ? (options.slots?.(step.kind) ?? []) : [];
        const tile: LtrTile = {
          ...base,
          id: step.id,
          role,
          shape: SHAPES[role] === "fork" ? "square" : SHAPES[role],
          x,
          y: top,
          width,
          height: LTR.tile,
          order: order++,
          labelY:
            top + LTR.tile + (slots.length ? LTR.subNodeRow : 0) + LTR.labelGap,
        };
        layout.tiles.push(tile);
        if (slots.length) layout.subNodes.push(subNodeRow(tile, slots));
        const fail = step.kind === "fail";
        if (fail)
          layout.terminals.push({
            step: step.id,
            x: x + width + 24,
            y: top + MID,
          });
        placed.push({
          id: step.id,
          in: { x, y: top + MID },
          out: fail ? null : { x: x + width, y: top + MID },
          group: false,
        });
      }
      x += advance(step);
    });
    placed.forEach((item, index) => {
      if (!item.out) return;
      const next = placed[index + 1];
      const insert = { owner, index: index + 1 };
      layout.handles.push({
        key: `${item.id}:out`,
        tile: item.id,
        x: item.out.x,
        y: item.out.y,
        insert,
        label: null,
        plus: next ? null : { x: item.out.x + LTR.plus, y: item.out.y },
        name: `Add a step after ${item.id}`,
      });
      if (next)
        layout.edges.push({
          key: `${item.id}>${next.id}`,
          tile: item.id,
          shape: "curve",
          a: { x: item.out.x + 6, y: item.out.y },
          b: next.in,
          insert,
          name: `Insert a step between ${item.id} and ${next.id}`,
          leaves: item.group ? null : item.id,
          enters: next.id,
        });
    });
    return { placed, end: x };
  }

  function group(step: Step, base: TileBase, x: number, top: number): Placed {
    const parallel = step.kind === "parallel";
    const width = parallel ? LTR.bar : LTR.tile;
    const role = roleOf(step);
    layout.tiles.push({
      ...base,
      id: step.id,
      role,
      shape: parallel ? "fork" : "square",
      x,
      y: top,
      width,
      height: LTR.tile,
      order: order++,
      labelY: top + LTR.tile + LTR.labelGap,
    });
    const lanes = lanesOf(step);
    const right = x + width;
    const lanesLeft = right + LTR.edge;
    const joinX =
      lanesLeft + Math.max(0, ...lanes.map((lane) => laneWidth(lane.steps)));
    const join: Point = { x: parallel ? joinX : joinX - 6, y: top + MID };
    let laneTop = top;
    for (const lane of lanes) {
      const owner = `${step.id}/${lane.name}`;
      const label = laneLabel(step, lane.name, workflow);
      const y = laneTop + MID;
      const output: LtrHandle = {
        key: `${step.id}:${lane.name}`,
        tile: step.id,
        x: right + LTR.rail,
        y,
        insert: { owner, index: 0 },
        label,
        plus: null,
        name: `Add a step to path ${label} of ${step.id}`,
      };
      layout.handles.push(output);
      const first = lane.steps[0]?.id ?? null;
      layout.edges.push({
        key: `${step.id}>${output.key}`,
        tile: step.id,
        shape: "fan",
        a: { x: right, y: top + MID },
        b: { x: output.x - 6, y },
        insert: null,
        name: null,
        leaves: null,
        enters: first,
      });
      layout.labels.push({
        owner,
        text: label,
        x: output.x + 12,
        y: y - 28,
        width: lanesLeft - output.x - 20,
      });
      if (!first) {
        layout.slots.push({
          owner,
          tile: step.id,
          x: lanesLeft,
          y: y - LTR.slot / 2,
          label,
          name: output.name,
          insert: output.insert,
        });
        layout.edges.push({
          key: `${output.key}>$slot:${owner}`,
          tile: step.id,
          shape: "curve",
          a: { x: output.x + 6, y },
          b: { x: lanesLeft, y },
          insert: null,
          name: null,
          leaves: null,
          enters: null,
        });
        layout.edges.push({
          key: `$slot:${owner}>$join:${step.id}`,
          tile: step.id,
          shape: y === join.y ? "curve" : "merge",
          a: { x: lanesLeft + LTR.slot, y },
          b: join,
          insert: null,
          name: null,
          leaves: null,
          enters: null,
        });
      } else {
        const inner = sequence(
          lane.steps,
          owner,
          step.id,
          base.depth + 1,
          lanesLeft,
          laneTop,
          `${label} of ${step.id}`,
        );
        layout.edges.push({
          key: `${output.key}>${first}`,
          tile: step.id,
          shape: "curve",
          a: { x: output.x + 6, y },
          b: inner.placed[0].in,
          insert: output.insert,
          name: `Insert a step between ${step.id} and ${first}`,
          leaves: null,
          enters: first,
        });
        const last = inner.placed[inner.placed.length - 1];
        if (last.out && completes(lane.steps))
          layout.edges.push({
            key: `${last.id}>$join:${step.id}`,
            tile: last.id,
            shape: last.out.y === join.y ? "curve" : "merge",
            a: { x: last.out.x + 6, y: last.out.y },
            b: join,
            insert: null,
            name: null,
            leaves: last.id,
            enters: null,
          });
      }
      laneTop += laneHeight(lane.steps);
    }
    if (parallel)
      layout.joins.push({
        id: step.id,
        x: joinX,
        y: top,
        width: LTR.bar,
        height: LTR.tile,
        label: "All branches done",
      });
    const through = parallel
      ? lanes.every((lane) => completes(lane.steps))
      : lanes.some((lane) => completes(lane.steps));
    return {
      id: step.id,
      in: { x, y: top + MID },
      out: through
        ? { x: parallel ? joinX + LTR.bar : joinX, y: top + MID }
        : null,
      group: true,
    };
  }

  const steps = workflow.spec.steps ?? [];
  const triggers = options.triggers;
  triggers.forEach((trigger, i) => {
    const y = LTR.originY + i * LTR.lane;
    layout.tiles.push({
      id: `$trigger:${trigger.id}`,
      kind: "trigger",
      role: "trigger",
      shape: "trigger",
      x: LTR.originX,
      y,
      width: LTR.tile,
      height: LTR.tile,
      step: null,
      owner: "",
      index: i,
      parent: null,
      depth: 0,
      order: order++,
      count: triggers.length,
      place: "",
      after: { owner: "root", index: 0 },
      labelY: y + LTR.tile + LTR.labelGap,
    });
  });
  const left = LTR.originX + (triggers.length ? LTR.tile + LTR.edge : 0);
  const root = sequence(
    steps,
    "root",
    null,
    0,
    left,
    LTR.originY,
    "Main sequence",
  );
  const first = root.placed[0];
  triggers.forEach((trigger, i) => {
    const id = `$trigger:${trigger.id}`;
    const out = {
      x: LTR.originX + LTR.tile,
      y: LTR.originY + i * LTR.lane + MID,
    };
    layout.handles.push({
      key: `${id}:out`,
      tile: id,
      x: out.x,
      y: out.y,
      insert: { owner: "root", index: 0 },
      label: null,
      plus: first ? null : { x: out.x + LTR.plus, y: out.y },
      name: "Add a step at the start",
    });
    if (first)
      layout.edges.push({
        key: `${id}>${first.id}`,
        tile: id,
        shape: "curve",
        a: { x: out.x + 6, y: out.y },
        b: first.in,
        insert: { owner: "root", index: 0 },
        name: `Insert a step before ${first.id}`,
        leaves: null,
        enters: first.id,
      });
  });
  if (steps.length) {
    const end = { x: root.end, y: LTR.originY + MID };
    layout.tiles.push({
      id: "$end",
      kind: "end",
      role: "end",
      shape: "end",
      x: end.x,
      y: end.y - LTR.end / 2,
      width: LTR.end,
      height: LTR.end,
      step: null,
      owner: "",
      index: 0,
      parent: null,
      depth: 0,
      order: order++,
      count: 1,
      place: "",
      after: null,
      labelY: end.y + LTR.end / 2 + LTR.labelGap,
    });
    const last = root.placed[root.placed.length - 1];
    if (last.out)
      layout.edges.push({
        key: `${last.id}>$end`,
        tile: last.id,
        shape: "curve",
        a: { x: last.out.x + 6, y: last.out.y },
        b: end,
        insert: null,
        name: null,
        leaves: last.id,
        enters: "$end",
      });
  }

  const xs: number[] = [];
  const ys: number[] = [];
  const rights: number[] = [];
  const bottoms: number[] = [];
  const add = (x: number, y: number, right: number, bottom: number) => {
    xs.push(x);
    ys.push(y);
    rights.push(right);
    bottoms.push(bottom);
  };
  for (const tile of layout.tiles) {
    if (tile.kind === "end") {
      add(tile.x, tile.y, tile.x + tile.width, tile.y + tile.height);
      continue;
    }
    const labelLeft = tile.x + tile.width / 2 - LTR.label / 2;
    add(
      Math.min(tile.x, labelLeft),
      tile.y,
      Math.max(tile.x + tile.width, labelLeft + LTR.label),
      tile.labelY + LTR.labelHeight,
    );
  }
  for (const output of layout.handles)
    if (output.plus)
      add(
        output.plus.x - 12,
        output.plus.y - 12,
        output.plus.x + 12,
        output.plus.y + 12,
      );
  for (const slot of layout.slots)
    add(slot.x, slot.y, slot.x + LTR.slot, slot.y + LTR.slot + 24);
  for (const join of layout.joins)
    add(
      join.x + join.width / 2 - LTR.label / 2,
      join.y,
      join.x + join.width / 2 + LTR.label / 2,
      join.y + join.height + LTR.labelGap + 16,
    );
  for (const label of layout.labels)
    add(label.x, label.y, label.x + label.width, label.y + 16);
  for (const row of layout.subNodes)
    for (const chip of row.chips)
      add(chip.x, chip.y, chip.x + LTR.chipWidth, chip.y + LTR.chipHeight);
  layout.bounds = xs.length
    ? {
        minX: Math.min(...xs),
        minY: Math.min(...ys),
        maxX: Math.max(...rights),
        maxY: Math.max(...bottoms),
      }
    : {
        minX: LTR.originX,
        minY: LTR.originY,
        maxX: LTR.originX + LTR.tile,
        maxY: LTR.originY + LTR.tile,
      };
  return layout;
}

/**
 * An edge's SVG path: a smooth curve, or right angles for a group's fan and
 * for a path that joins from another row. Those run along their own row,
 * turn at one x shared by every path into that join, and enter it level.
 */
export function edgePath(edge: LtrEdge): string {
  const { a, b } = edge;
  if (edge.shape === "fan") {
    const x = a.x + (b.x - a.x) / 2;
    return `M ${a.x} ${a.y} H ${x} V ${b.y} H ${b.x}`;
  }
  if (edge.shape === "merge") {
    const x = Math.max(a.x, b.x - LTR.merge);
    return `M ${a.x} ${a.y} H ${x} V ${b.y} H ${b.x}`;
  }
  const dx = Math.max(24, Math.abs(b.x - a.x) / 2);
  return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
}

/** Where an edge's "+" sits: the curve's middle. */
export function midpoint(edge: LtrEdge): Point {
  return { x: (edge.a.x + edge.b.x) / 2, y: (edge.a.y + edge.b.y) / 2 };
}

export type LevelOfDetail = "full" | "compact" | "minimal";
/**
 * Below 40% tiles drop their labels; below 30% they are plain rectangles.
 * A workflow opens at 50% or more (openView), so it opens with its names.
 */
export function levelOfDetail(zoom: number): LevelOfDetail {
  return zoom < 0.3 ? "minimal" : zoom < 0.4 ? "compact" : "full";
}

/** The whole workflow in view, 48 px from the edges, between `min` and `max` zoom. */
export function fitView(
  bounds: LtrBounds,
  size: { width: number; height: number },
  min = 0.25,
  max = 1,
): { zoom: number; pan: Point } {
  const margin = 48;
  const width = Math.max(1, bounds.maxX - bounds.minX);
  const height = Math.max(1, bounds.maxY - bounds.minY);
  const roomX = Math.max(1, size.width - 2 * margin);
  const roomY = Math.max(1, size.height - 2 * margin);
  const zoom = Math.min(
    max,
    Math.max(min, Math.min(roomX / width, roomY / height)),
  );
  return {
    zoom,
    pan: {
      x:
        width * zoom <= roomX
          ? (size.width - width * zoom) / 2 - bounds.minX * zoom
          : margin - bounds.minX * zoom,
      y:
        height * zoom <= roomY
          ? (size.height - height * zoom) / 2 - bounds.minY * zoom
          : margin - bounds.minY * zoom,
    },
  };
}

/**
 * The view a workflow opens with: fitted when it fits at 50% or more;
 * otherwise 50%, the trigger column at the left margin and the workflow
 * centered vertically when its height fits (else its top at the top
 * margin). The rest is a pan away; Fit view still shows everything.
 */
export function openView(
  bounds: LtrBounds,
  size: { width: number; height: number },
): { zoom: number; pan: Point } {
  return fitView(bounds, size, 0.5);
}
