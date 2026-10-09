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
// Arrow keys on the canvas, read from the layout: → follows the flow (into
// a group's first lane, then past the group), ← goes back, ↑ and ↓ jump to
// the nearest step in the lane above or below in the same group.
import type { StepPlace } from "../state/selection";
import type { LtrLayout, LtrTile } from "./layout-ltr";

/** Every step's place; the trigger and End are not steps. */
export function placesOf(layout: LtrLayout): Map<string, StepPlace> {
  return new Map(
    layout.tiles
      .filter((tile) => tile.step)
      .map((tile) => [
        tile.id,
        {
          id: tile.id,
          owner: tile.owner,
          index: tile.index,
          parent: tile.parent,
          order: tile.order,
        },
      ]),
  );
}

export type Direction = "previous" | "next" | "above" | "below";

export function neighbor(
  layout: LtrLayout,
  from: string,
  direction: Direction,
): string | null {
  const tiles = new Map(layout.tiles.map((tile) => [tile.id, tile]));
  const tile = tiles.get(from);
  if (!tile) return null;
  const steps = layout.tiles.filter((item) => item.step);
  const at = (owner: string, index: number) =>
    steps.find((item) => item.owner === owner && item.index === index) ?? null;
  const trigger = layout.tiles.find((item) => item.kind === "trigger") ?? null;
  const end = tiles.get("$end") ?? null;
  if (direction === "next") {
    if (tile.kind === "trigger") return at("root", 0)?.id ?? end?.id ?? null;
    if (tile.kind === "end") return null;
    const firstLane = steps
      .filter((item) => item.parent === tile.id && item.index === 0)
      .sort((a, b) => a.y - b.y)[0];
    if (firstLane) return firstLane.id;
    for (
      let current: LtrTile | undefined = tile;
      current;
      current = current.parent ? tiles.get(current.parent) : undefined
    ) {
      const following = at(current.owner, current.index + 1);
      if (following) return following.id;
      if (!current.parent) return end?.id ?? null;
    }
    return null;
  }
  if (direction === "previous") {
    if (tile.kind === "trigger") return null;
    if (tile.kind === "end") {
      const main = steps.filter((item) => item.owner === "root");
      return main[main.length - 1]?.id ?? trigger?.id ?? null;
    }
    const before = at(tile.owner, tile.index - 1);
    if (before) return before.id;
    return tile.parent ?? trigger?.id ?? null;
  }
  if (!tile.parent) return null;
  const lanes = new Map<string, LtrTile[]>();
  for (const item of steps)
    if (item.parent === tile.parent && item.owner !== tile.owner)
      lanes.set(item.owner, [...(lanes.get(item.owner) ?? []), item]);
  const sign = direction === "above" ? -1 : 1;
  let lane: LtrTile[] | null = null;
  let gap = Infinity;
  for (const list of lanes.values()) {
    const distance = (list[0].y - tile.y) * sign;
    if (distance > 0 && distance < gap) {
      gap = distance;
      lane = list;
    }
  }
  if (!lane) return null;
  const center = tile.x + tile.width / 2;
  return lane.reduce((best, item) =>
    Math.abs(item.x + item.width / 2 - center) <
    Math.abs(best.x + best.width / 2 - center)
      ? item
      : best,
  ).id;
}
