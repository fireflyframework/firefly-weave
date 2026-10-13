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
// other note. Anchored notes stay with their step. Pure, so it is tested
// without a canvas.
import { LTR, type LtrLayout, type Point } from "./layout-ltr";

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
