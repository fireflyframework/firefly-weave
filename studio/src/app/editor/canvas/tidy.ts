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
// Tidy layout's note rule: a free note that covers a step moves to the
// nearest free spot to its right or below it, clear of every step and every
// other note. Anchored notes stay with their step. A note Shift+S adds uses
// the same search to start in a free spot. Pure, so it is tested without a
// canvas.
import {
  LTR,
  labelScale,
  labelWidth,
  midpoint,
  pathLabelScale,
  type LtrLayout,
  type Point,
} from "./layout-ltr";

export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}
export interface TidyNote extends Box {
  id: string;
  anchored: boolean;
}
/** The room a moved note keeps from what it was moved past. */
export const TIDY_GAP = 16;

const meets = (a: Box, b: Box, gap: number) =>
  a.x < b.x + b.width + gap &&
  b.x < a.x + a.width + gap &&
  a.y < b.y + b.height + gap &&
  b.y < a.y + a.height + gap;

/** Each tile with its name block under it: what a note must not cover. */
export function tileBoxes(layout: LtrLayout): Box[] {
  return layout.tiles.map((tile) => {
    const width = Math.max(tile.width, LTR.label);
    return {
      x: tile.x + tile.width / 2 - width / 2,
      y: tile.y,
      width,
      height: tile.labelY + LTR.labelHeight - tile.y,
    };
  });
}

/**
 * What a note Shift+S adds must not cover, at this zoom: each step with its
 * name block as drawn (names grow below 92%), "All branches done" and its
 * label, path labels, empty paths with their "Add a step", an agent's slots
 * and every "+", shown or shown on hover. Edges may run behind a note.
 */
export function noteObstacles(layout: LtrLayout, zoom: number): Box[] {
  const label = LTR.labelHeight * labelScale(zoom);
  const around = (at: Point, half: number): Box => ({
    x: at.x - half,
    y: at.y - half,
    width: 2 * half,
    height: 2 * half,
  });
  const boxes: Box[] = layout.tiles.map((tile) => {
    const width = Math.max(
      tile.width,
      tile.shape === "end" ? LTR.end * 2 : labelWidth(zoom, tile.shape),
    );
    return {
      x: tile.x + tile.width / 2 - width / 2,
      y: tile.y,
      width,
      height: tile.labelY + label - tile.y,
    };
  });
  for (const join of layout.joins)
    boxes.push(join, {
      x: join.x + join.width / 2 - LTR.joinLabel / 2,
      y: join.y + join.height,
      width: LTR.joinLabel,
      height: 3 * LTR.line * pathLabelScale(zoom),
    });
  const path = LTR.line * pathLabelScale(zoom);
  for (const item of layout.labels)
    boxes.push({
      x: item.x,
      y: item.y - path,
      width: item.width,
      height: path,
    });
  for (const slot of layout.slots)
    boxes.push({
      x: slot.x + LTR.slot / 2 - LTR.slotLabel / 2,
      y: slot.y,
      width: LTR.slotLabel,
      height: LTR.slot + 2 * LTR.line * labelScale(zoom),
    });
  for (const row of layout.subNodes)
    for (const chip of row.chips)
      boxes.push({
        x: chip.x,
        y: chip.y,
        width: LTR.chipWidth,
        height: LTR.chipHeight,
      });
  for (const handle of layout.handles)
    if (handle.plus) boxes.push(around(handle.plus, 12));
  for (const edge of layout.edges)
    if (edge.insert) boxes.push(around(midpoint(edge), 12));
  return boxes;
}

/** Slides a box right (or down) past every box it meets, keeping the gap. */
function slide(box: Box, blockers: readonly Box[], axis: "x" | "y"): Box {
  let at = box;
  // Each jump leaves one blocker behind for good, so this ends.
  for (let jumps = 0; jumps <= blockers.length; jumps++) {
    const hit = blockers.find((blocker) => meets(at, blocker, TIDY_GAP));
    if (!hit) return at;
    at =
      axis === "x"
        ? { ...at, x: hit.x + hit.width + TIDY_GAP }
        : { ...at, y: hit.y + hit.height + TIDY_GAP };
  }
  return at;
}

/** Where each free note that covers a step goes; notes that stay are left out. */
export function tidyNotes(
  notes: readonly TidyNote[],
  tiles: readonly Box[],
): Map<string, Point> {
  const moving = notes.filter(
    (note) => !note.anchored && tiles.some((tile) => meets(note, tile, 0)),
  );
  const settled: Box[] = notes.filter((note) => !moving.includes(note));
  const moves = new Map<string, Point>();
  for (const note of moving) {
    const blockers = [...tiles, ...settled];
    const right = slide(note, blockers, "x");
    const below = slide(note, blockers, "y");
    const best = below.y - note.y < right.x - note.x ? below : right;
    moves.set(note.id, { x: Math.round(best.x), y: Math.round(best.y) });
    settled.push(best);
  }
  return moves;
}

/**
 * Where a new note goes: where it was asked for when that spot meets nothing
 * (keeping the gap), else the nearer of the free spots below it and to its
 * right; below on a tie, so a step's note stays under it where it can.
 */
export function freeSpot(box: Box, blockers: readonly Box[]): Point {
  if (!blockers.some((blocker) => meets(box, blocker, TIDY_GAP)))
    return { x: Math.round(box.x), y: Math.round(box.y) };
  const right = slide(box, blockers, "x");
  const below = slide(box, blockers, "y");
  const best = below.y - box.y <= right.x - box.x ? below : right;
  return { x: Math.round(best.x), y: Math.round(best.y) };
}
