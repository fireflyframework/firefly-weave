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
import type {
  Branch,
  Node,
  Placeholder,
  Point,
  Step,
  Target,
  Workflow,
} from "../model";
import { branchName, parseCondition } from "./conditions";

export const laneGeometry = {
  nodeWidth: 240,
  nodeHeight: 72,
  rowPitch: 112,
  laneGap: 48,
} as const;
export interface Lane {
  owner: string;
  label: string;
  point: Point;
  width: number;
  height: number;
  terminal: boolean;
}
export interface LaneGroup {
  id: string;
  point: Point;
  width: number;
  height: number;
  split: Point;
  join: Point;
  lanes: Lane[];
}
export interface LaneLayout {
  nodes: Node[];
  placeholders: Placeholder[];
  groups: LaneGroup[];
  junctions: Record<string, Point>;
  terminals: { step: string; point: Point }[];
  edges: { from: string; to: string }[];
  targets: Target[];
  boundaries: { start: Point; end: Point };
}
const children = (step: Step): [string, Branch][] =>
  step.kind === "parallel"
    ? Object.entries(step["branches"] as Record<string, Branch>)
    : step.kind === "switch"
      ? [
          ...(step["cases"] as Branch[]).map(
            (branch, index): [string, Branch] => [`case ${index + 1}`, branch],
          ),
          ["default", step["default"] as Branch],
        ]
      : [];
const completes = (steps: Step[]): boolean =>
  steps.every((step) => {
    if (step.kind === "fail") return false;
    const branches = children(step);
    return (
      !branches.length ||
      (step.kind === "parallel"
        ? branches.every(([, branch]) => completes(branch.steps))
        : branches.some(([, branch]) => completes(branch.steps)))
    );
  });

/** Deterministic canvas geometry. It never edits steps or the saved document. */
export function computeLaneLayout(
  workflow: Workflow,
  positions: Readonly<Record<string, Point>> = {},
): LaneLayout {
  const { nodeWidth, nodeHeight, rowPitch, laneGap } = laneGeometry;
  const nodes: Node[] = [],
    placeholders: Placeholder[] = [],
    groups: LaneGroup[] = [];
  const junctions: Record<string, Point> = {};
  const terminals: LaneLayout["terminals"] = [];
  const targets: Target[] = [];
  const widths = new Map<Step, number>();
  const sequenceWidth = (steps: Step[]): number =>
    Math.max(nodeWidth, ...steps.map(stepWidth));
  const stepWidth = (step: Step): number => {
    const cached = widths.get(step);
    if (cached !== undefined) return cached;
    const branches = children(step);
    const width = branches.length
      ? branches.reduce(
          (sum, [, branch]) => sum + sequenceWidth(branch.steps),
          0,
        ) +
        laneGap * (branches.length - 1)
      : nodeWidth;
    widths.set(step, width);
    return width;
  };
  const rootWidth = sequenceWidth(workflow.spec.steps);
  const ends = new Map<string, Point>();
  const laneLabels = new Map<string, string>();
  const laneLocations = new Map<string, string>();
  const visit = (
    steps: Step[],
    owner: string,
    depth: number,
    left: number,
    width: number,
    top: number,
  ): number => {
    let y = top;
    for (let index = 0; index < steps.length; index++) {
      const step = steps[index];
      const point = positions[step.id] ?? {
        x: left + (width - nodeWidth) / 2,
        y,
      };
      const next = steps[index + 1];
      const routed =
        next?.kind === "switch" &&
        (next["cases"] as Branch[]).some((branch) =>
          parseCondition(branch["when"])?.rows.some(
            (row) => row.ref === `/steps/${step.id}/output/decision`,
          ),
        );
      const answers = Array.isArray(step["decisions"])
        ? step["decisions"].map(String)
        : ["approve", "reject"];
      const answerPrompt =
        step.kind === "humanTask" && !routed
          ? `Branch on the answer (${answers.join(" / ")})`
          : undefined;
      const extra = answerPrompt ? 72 : 0;
      nodes.push({
        step,
        owner,
        index,
        depth,
        point,
        ...(answerPrompt ? { answerPrompt } : {}),
      });
      const branches = children(step);
      if (!branches.length) {
        ends.set(step.id, {
          x: point.x + nodeWidth / 2,
          y: point.y + nodeHeight + extra,
        });
        if (step.kind === "fail")
          terminals.push({
            step: step.id,
            point: { x: point.x + nodeWidth / 2, y: point.y + nodeHeight + 16 },
          });
        y += rowPitch + extra;
        continue;
      }
      const span = stepWidth(step);
      const laneTop = point.y + 200;
      let laneLeft = point.x + nodeWidth / 2 - span / 2;
      const lanes: Lane[] = [];
      let bottom = laneTop + rowPitch;
      for (const [name, branch] of branches) {
        const child = `${step.id}/${name}`;
        const laneWidth = sequenceWidth(branch.steps);
        const label = branchName(step, name, workflow);
        laneLabels.set(
          child,
          label === "Condition not set" ? `Case ${name.slice(5)}` : label,
        );
        laneLocations.set(
          child,
          `${label === "Condition not set" ? `Case ${name.slice(5)}` : label} of ${step.id}`,
        );
        const end = visit(
          branch.steps,
          child,
          depth + 1,
          laneLeft,
          laneWidth,
          laneTop,
        );
        bottom = Math.max(bottom, end);
        lanes.push({
          owner: child,
          label,
          point: { x: laneLeft, y: laneTop - 100 },
          width: laneWidth,
          height: 0,
          terminal: !completes(branch.steps),
        });
        if (!branch.steps.length)
          placeholders.push({
            owner: child,
            parent: step.id,
            branch: label,
            point: { x: laneLeft + (laneWidth - 208) / 2, y: laneTop },
          });
        laneLeft += laneWidth + laneGap;
      }
      const split = { x: point.x + nodeWidth / 2, y: point.y + 88 };
      const join = { x: split.x, y: bottom + 16 };
      for (const lane of lanes) lane.height = join.y - lane.point.y;
      groups.push({
        id: step.id,
        point: { x: split.x - span / 2 - 16, y: split.y },
        width: span + 32,
        height: join.y - split.y + 16,
        split,
        join,
        lanes,
      });
      junctions[`$split:${step.id}`] = split;
      junctions[`$join:${step.id}`] = join;
      ends.set(step.id, join);
      y = join.y + 64;
    }
    return y;
  };
  const bottom = visit(workflow.spec.steps, "root", 0, 96, rootWidth, 128);
  const rootX = 96 + (rootWidth - nodeWidth) / 2;
  const first = nodes.find((node) => node.owner === "root");
  const boundaries = {
    start: {
      x: (first?.point.x ?? rootX) + (nodeWidth - 208) / 2,
      y: (first?.point.y ?? 128) - 104,
    },
    end: {
      x: rootX + (nodeWidth - 208) / 2,
      y: Math.max(256, bottom + 16, ...nodes.map((node) => node.point.y + 128)),
    },
  };
  const owners = new Map<string, Node[]>();
  for (const node of nodes)
    owners.set(node.owner, [...(owners.get(node.owner) ?? []), node]);
  const continuation = (node: Node): string =>
    owners.get(node.owner)?.[node.index + 1]?.step.id ??
    (node.owner === "root"
      ? "$end"
      : `$join:${node.owner.slice(0, node.owner.indexOf("/"))}`);
  const edges: LaneLayout["edges"] = [
    { from: "$start", to: first?.step.id ?? "$end" },
  ];
  for (const node of nodes) {
    const branches = children(node.step);
    if (node.step.kind === "fail") continue;
    if (!branches.length)
      edges.push({ from: node.step.id, to: continuation(node) });
    else {
      const split = `$split:${node.step.id}`,
        join = `$join:${node.step.id}`;
      edges.push({ from: node.step.id, to: split });
      for (const [name] of branches) {
        const owner = `${node.step.id}/${name}`;
        const first = owners.get(owner)?.[0];
        edges.push({ from: split, to: first?.step.id ?? `$empty:${owner}` });
        if (!first) edges.push({ from: `$empty:${owner}`, to: join });
      }
      if (completes([node.step]))
        edges.push({ from: join, to: continuation(node) });
    }
  }
  for (const [owner, own] of owners) {
    const lane = laneLabels.get(owner);
    targets.push({
      owner,
      index: 0,
      point: { x: own[0].point.x + nodeWidth / 2, y: own[0].point.y - 20 },
      label:
        owner === "root"
          ? "Add a step here, at the start"
          : `Add a step here, at the start of ${laneLocations.get(owner)}`,
    });
    for (const node of own) {
      if (!completes([node.step])) continue;
      const end = ends.get(node.step.id)!;
      targets.push({
        owner,
        index: node.index + 1,
        point: { x: end.x, y: end.y + (children(node.step).length ? 32 : 20) },
        label: lane
          ? `Add a step after ${node.step.id}, in ${lane}`
          : `Add a step here, after ${node.step.id}`,
        ...(lane && node.index === own.length - 1 ? { lane } : {}),
      });
    }
  }
  for (const placeholder of placeholders)
    targets.push({
      owner: placeholder.owner,
      index: 0,
      point: { x: placeholder.point.x + 104, y: placeholder.point.y + 24 },
      label: `Add a step here, in ${laneLocations.get(placeholder.owner)}`,
      empty: placeholder.branch,
    });
  if (!nodes.length)
    targets.push({
      owner: "root",
      index: 0,
      point: { x: rootX + nodeWidth / 2, y: 108 },
      label: "Add a step here, at the start",
    });
  return {
    nodes,
    placeholders,
    groups,
    junctions,
    terminals,
    edges,
    targets,
    boundaries,
  };
}
