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
import { describe, expect, it } from "vitest";
import {
  TIDY_GAP,
  freeSpot,
  noteObstacles,
  tidyNotes,
  tileBoxes,
  type Box,
  type TidyNote,
} from "../src/app/editor/canvas/tidy";
import type { LtrLayout } from "../src/app/editor/canvas/layout-ltr";

const free = (id: string, box: Box): TidyNote => ({
  id,
  anchored: false,
  ...box,
});
const meets = (a: Box, b: Box) =>
  a.x < b.x + b.width &&
  b.x < a.x + a.width &&
  a.y < b.y + b.height &&
  b.y < a.y + a.height;
const tile: Box = { x: 0, y: 0, width: 100, height: 100 };

describe("tidy layout's note rule", () => {
  it("moves a free note off a step to the nearest free spot to its right", () => {
    expect(
      tidyNotes(
        [free("note-a", { x: 80, y: 0, width: 100, height: 40 })],
        [tile],
      ),
    ).toEqual(new Map([["note-a", { x: 100 + TIDY_GAP, y: 0 }]]));
  });

  it("or below it, when below is nearer", () => {
    expect(
      tidyNotes(
        [free("note-b", { x: 0, y: 90, width: 100, height: 40 })],
        [tile],
      ),
    ).toEqual(new Map([["note-b", { x: 0, y: 100 + TIDY_GAP }]]));
  });

  it("leaves anchored notes and notes clear of every step where they are", () => {
    expect(
      tidyNotes(
        [
          { id: "note-c", anchored: true, x: 0, y: 0, width: 100, height: 40 },
          free("note-d", { x: 200, y: 0, width: 100, height: 40 }),
        ],
        [tile],
      ),
    ).toEqual(new Map());
  });

  it("steps past other steps and notes, and takes the shorter way", () => {
    const tiles: Box[] = [
      tile,
      { x: 116, y: 0, width: 100, height: 100 },
      { x: 0, y: 116, width: 300, height: 100 },
    ];
    const moving = free("note-e", { x: 80, y: 0, width: 100, height: 40 });
    expect(tidyNotes([moving], tiles)).toEqual(
      new Map([["note-e", { x: 232, y: 0 }]]),
    );
    const parked: TidyNote = {
      id: "note-f",
      anchored: true,
      x: 232,
      y: 0,
      width: 100,
      height: 40,
    };
    expect(tidyNotes([moving, parked], tiles)).toEqual(
      new Map([["note-e", { x: 80, y: 232 }]]),
    );
  });

  it("never puts two moved notes on each other", () => {
    const one = free("note-g", { x: 80, y: 0, width: 100, height: 40 });
    const two = free("note-h", { x: 80, y: 0, width: 100, height: 40 });
    const moves = tidyNotes([one, two], [tile]);
    expect(moves).toEqual(
      new Map([
        ["note-g", { x: 116, y: 0 }],
        ["note-h", { x: 80, y: 116 }],
      ]),
    );
    const boxes = [...moves.values()].map((at) => ({
      ...at,
      width: 100,
      height: 40,
    }));
    expect(meets(boxes[0], boxes[1])).toBe(false);
    for (const box of boxes) expect(meets(box, tile)).toBe(false);
  });

  it("counts a step's name under its tile as part of the step", () => {
    const layout = {
      tiles: [{ id: "check", x: 344, y: 96, width: 96, labelY: 200 }],
    } as unknown as LtrLayout;
    expect(tileBoxes(layout)).toEqual([
      { x: 308, y: 96, width: 168, height: 152 },
    ]);
  });

  it("puts a new note where it was asked for when nothing is there", () => {
    expect(freeSpot({ x: 0, y: 116, width: 100, height: 40 }, [tile])).toEqual({
      x: 0,
      y: 116,
    });
  });

  it("else in the nearer free spot, below on a tie, past steps and notes", () => {
    const note = { x: 0, y: 80, width: 100, height: 40 };
    expect(freeSpot(note, [tile])).toEqual({ x: 0, y: 116 });
    const under: Box = { x: 0, y: 116, width: 100, height: 400 };
    expect(freeSpot(note, [tile, under])).toEqual({ x: 116, y: 80 });
    const level = { x: 50, y: 50, width: 100, height: 100 };
    expect(freeSpot(level, [tile])).toEqual({ x: 50, y: 116 });
  });

  it("keeps a new note off names as drawn at this zoom and off every +", () => {
    const layout = {
      tiles: [
        {
          id: "check",
          shape: "square",
          x: 344,
          y: 96,
          width: 96,
          height: 96,
          labelY: 200,
        },
      ],
      joins: [],
      labels: [],
      slots: [{ x: 600, y: 400 }],
      subNodes: [],
      handles: [{ plus: { x: 480, y: 144 } }, { plus: null }],
      edges: [
        {
          insert: { owner: "", index: 1 },
          shape: "curve",
          a: { x: 0, y: 144 },
          b: { x: 200, y: 144 },
        },
        { insert: null, shape: "curve", a: { x: 0, y: 0 }, b: { x: 0, y: 0 } },
      ],
    } as unknown as LtrLayout;
    const [step, slot, plus, edge, ...rest] = noteObstacles(layout, 1);
    expect(step).toEqual({ x: 308, y: 96, width: 168, height: 152 });
    expect(slot).toEqual({ x: 592, y: 400, width: 80, height: 96 });
    expect(plus).toEqual({ x: 468, y: 132, width: 24, height: 24 });
    expect(edge).toEqual({ x: 88, y: 132, width: 24, height: 24 });
    expect(rest).toEqual([]);
    // At 50% names are drawn larger and wider, so their block grows.
    const [half] = noteObstacles(layout, 0.5);
    expect(half.width).toBeGreaterThan(168);
    expect(half.y + half.height).toBeGreaterThan(248);
  });

  // Tidy's work is counted, not timed: every comparison reads the positions
  // of the steps and notes it is given, and the count is the same on any
  // machine, however busy.
  it("tidies 200 notes over 200 steps in bounded work that grows less than cubically", () => {
    const at200 = tidyWork(200);
    expect(at200.moves).toBe(200);
    // Today: about 53 reads per note and step pair. The floor keeps the count
    // honest: if tidy copied its inputs first, the counted boxes would go
    // unread and any algorithm would pass the ceiling.
    expect(at200.reads).toBeGreaterThan(200 * 200);
    expect(at200.reads).toBeLessThan(100 * 200 * 200);
    // Doubling the notes and steps multiplies the work by about 5.4 today;
    // by 4 if it were quadratic, by 8 if cubic.
    expect(at200.reads / tidyWork(100).reads).toBeLessThan(6.5);
  });
});

/** Tidies `count` notes, each covering one of `count` steps on a grid, and counts the position reads. */
function tidyWork(count: number): { reads: number; moves: number } {
  let reads = 0;
  const counted = <T extends Box>(box: T): T => {
    const copy = { ...box };
    for (const key of ["x", "y", "width", "height"] as const) {
      const value = box[key];
      Object.defineProperty(copy, key, {
        enumerable: true,
        get: () => {
          reads++;
          return value;
        },
      });
    }
    return copy;
  };
  const cell = (i: number) => ({
    x: (i % 20) * 248,
    y: Math.floor(i / 20) * 224,
  });
  const tiles = Array.from({ length: count }, (_, i) =>
    counted({ ...cell(i), width: 96, height: 152 }),
  );
  const notes = Array.from({ length: count }, (_, i) =>
    counted(free(`note-${i}`, { ...cell(i), width: 240, height: 120 })),
  );
  const moves = tidyNotes(notes, tiles);
  return { reads, moves: moves.size };
}
