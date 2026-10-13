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

  it("tidies 200 notes over 200 steps quickly", () => {
    const tiles = Array.from({ length: 200 }, (_, i) => ({
      x: (i % 20) * 248,
      y: Math.floor(i / 20) * 224,
      width: 96,
      height: 152,
    }));
    const notes = Array.from({ length: 200 }, (_, i) =>
      free(`note-${i}`, {
        x: (i % 20) * 248,
        y: Math.floor(i / 20) * 224,
        width: 240,
        height: 120,
      }),
    );
    const started = performance.now();
    const moves = tidyNotes(notes, tiles);
    expect(performance.now() - started).toBeLessThan(200);
    expect(moves.size).toBe(200);
  });
});
