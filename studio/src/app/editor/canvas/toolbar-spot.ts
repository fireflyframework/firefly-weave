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
// Where a step's hover toolbar goes: above the step, centered, as n8n draws
// it. Its buttons keep a target size on screen at every zoom (24 px with a
// fine pointer, 44 px with a coarse one, never less than 28 canvas units),
// so the toolbar grows in canvas units as the canvas zooms out. "clear" says
// whether the band it takes is free of every other step, name, path label,
// join, empty path, handle, "+", End marker and edge at this zoom; where it
// isn't, the toolbar still sits above its step, drawn over those names
// while it shows, never over its own step's card, badge or name.
import {
  LTR,
  labelScale,
  labelWidth,
  pathLabelScale,
  type LtrLayout,
  type LtrTile,
} from "./layout-ltr";

/**
 * The toolbar at 100%: two 28-unit buttons, a 2-unit gap, padding and
 * border (one button for a chosen trigger).
 */
export const TOOLBAR = {
  width: 64,
  single: 34,
  height: 34,
  button: 28,
} as const;
/** From the toolbar's bottom to the step's top at 50% and up: clear of the corner badge, which rises 10 units. */
export const TOOLBAR_GAP = 14;
/** The corner badge's issue button keeps 44 px on screen (canvas.css, .tile-issue). */
const ISSUE_TARGET = 44;
/** Half the corner badge's height, and the room kept above it. */
const BADGE = { half: 10, room: 4 } as const;
/** The smallest button on screen, in CSS pixels. */
const TARGET = { fine: 24, coarse: 44 } as const;

export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}
export interface ToolbarSpot extends Box {
  /** How much larger than at 100% the toolbar draws, so its buttons keep their size on screen. */
  scale: number;
  /** Nothing else is drawn where the toolbar goes. */
  clear: boolean;
  /** From its bottom to the step's top, in canvas units. */
  gap: number;
}

const meets = (a: Box, b: Box) =>
  a.x < b.x + b.width &&
  b.x < a.x + a.width &&
  a.y < b.y + b.height &&
  b.y < a.y + a.height;
const around = (x: number, y: number, radius: number): Box => ({
  x: x - radius,
  y: y - radius,
  width: radius * 2,
  height: radius * 2,
});

/** How much the toolbar grows at this zoom: never under 1, so never smaller than at 100%. */
export function toolbarScale(zoom: number, coarse: boolean): number {
  const target = coarse ? TARGET.coarse : TARGET.fine;
  return Math.max(1, target / (TOOLBAR.button * zoom));
}

/**
 * From the toolbar's bottom to the step's top. Below 50% the corner badge's
 * issue button grows in canvas units and moves its badge up, so the gap
 * grows to stay clear of it.
 */
export function toolbarGap(
  zoom: number,
  tileHeight: number = LTR.tile,
): number {
  const target = Math.ceil(ISSUE_TARGET / zoom);
  const top = Math.min(-target / 2, tileHeight / 2 - target);
  const rise = -(top + target / 2) + BADGE.half;
  return Math.max(TOOLBAR_GAP, rise + BADGE.room);
}

/** What a toolbar above a step must not cover, at this zoom; the step itself is left out. */
export function toolbarObstacles(
  layout: LtrLayout,
  zoom: number,
  except: string,
): Box[] {
  const label = LTR.labelHeight * labelScale(zoom);
  const path = LTR.line * pathLabelScale(zoom);
  const boxes: Box[] = [];
  for (const tile of layout.tiles) {
    if (tile.id === except) continue;
    const width = Math.max(tile.width, labelWidth(zoom, tile.shape));
    // The card, its corner badge and its name block, as one box.
    boxes.push({
      x: tile.x + tile.width / 2 - width / 2,
      y: tile.y - 10,
      width,
      height: tile.labelY + label - (tile.y - 10),
    });
  }
  for (const join of layout.joins) {
    boxes.push(join);
    boxes.push({
      x: join.x + join.width / 2 - LTR.joinLabel / 2,
      y: join.y + join.height,
      width: LTR.joinLabel,
      height: 3 * LTR.line * labelScale(zoom),
    });
  }
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
  for (const handle of layout.handles) {
    boxes.push(around(handle.x, handle.y, 6));
    const plus = handle.plus ?? { x: handle.x + LTR.plus, y: handle.y };
    boxes.push(around(plus.x, plus.y, 12));
  }
  for (const end of layout.terminals) boxes.push(around(end.x, end.y, 12));
  for (const edge of layout.edges)
    boxes.push({
      x: Math.min(edge.a.x, edge.b.x) - 2,
      y: Math.min(edge.a.y, edge.b.y) - 2,
      width: Math.abs(edge.b.x - edge.a.x) + 4,
      height: Math.abs(edge.b.y - edge.a.y) + 4,
    });
  return boxes;
}

export function toolbarSpot(
  tile: LtrTile,
  layout: LtrLayout,
  zoom: number,
  options: { buttons?: 1 | 2; coarse?: boolean } = {},
): ToolbarSpot {
  const scale = toolbarScale(zoom, options.coarse ?? false);
  const width =
    (options.buttons === 1 ? TOOLBAR.single : TOOLBAR.width) * scale;
  const height = TOOLBAR.height * scale;
  const gap = toolbarGap(zoom, tile.height);
  const box: Box = {
    x: tile.x + tile.width / 2 - width / 2,
    y: tile.y - gap - height,
    width,
    height,
  };
  const clear = !toolbarObstacles(layout, zoom, tile.id).some((other) =>
    meets(box, other),
  );
  return { ...box, scale, clear, gap };
}
