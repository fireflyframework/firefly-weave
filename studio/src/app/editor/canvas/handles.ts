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
// Letting go of an edge dragged from an output handle. Near a step, a
// handle, a "+", an empty-lane slot or a join (60 px on screen, as in n8n)
// the drop is refused, because steps run in the order of their sequence; on
// empty canvas it opens the step picker at that handle's insertion point.
import { LTR, midpoint, type LtrLayout, type Point } from "./layout-ltr";

export const CONNECT_RADIUS = 60;
/** The hit area of a "+", centered on its point. */
const HIT_AREA = 24;
export const DROP_REFUSED = "Steps run in order. Use + to insert.";

/** From a point to a rectangle; 0 inside it. */
export function distanceToRect(
  point: Point,
  rect: { x: number; y: number; width: number; height: number },
): number {
  const dx = Math.max(rect.x - point.x, 0, point.x - (rect.x + rect.width));
  const dy = Math.max(rect.y - point.y, 0, point.y - (rect.y + rect.height));
  return Math.hypot(dx, dy);
}

/** A square of `size` around a point; a point itself when `size` is 0. */
function around(point: Point, size: number) {
  return {
    x: point.x - size / 2,
    y: point.y - size / 2,
    width: size,
    height: size,
  };
}

/** `point` in canvas units; the reach is 60 screen pixels at this zoom. */
export function dropOutcome(
  point: Point,
  layout: LtrLayout,
  zoom: number,
): "open" | "refused" {
  const reach = CONNECT_RADIUS / zoom;
  const near = (rect: {
    x: number;
    y: number;
    width: number;
    height: number;
  }) => distanceToRect(point, rect) <= reach;
  const obstacles = [
    ...layout.tiles,
    ...layout.joins,
    ...layout.slots.map((slot) => ({
      x: slot.x,
      y: slot.y,
      width: LTR.slot,
      height: LTR.slot,
    })),
    ...layout.handles.map((handle) => around(handle, 0)),
    ...layout.handles.flatMap((handle) =>
      handle.plus ? [around(handle.plus, HIT_AREA)] : [],
    ),
    ...layout.edges.flatMap((edge) =>
      edge.insert ? [around(midpoint(edge), HIT_AREA)] : [],
    ),
  ];
  return obstacles.some(near) ? "refused" : "open";
}
