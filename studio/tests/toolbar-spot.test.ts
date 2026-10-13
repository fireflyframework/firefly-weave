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
import { layoutLtr, type LtrLayout } from "../src/app/editor/canvas/layout-ltr";
import {
  TOOLBAR,
  TOOLBAR_GAP,
  toolbarGap,
  toolbarObstacles,
  toolbarScale,
  toolbarSpot,
} from "../src/app/editor/canvas/toolbar-spot";
import { builtInKinds } from "../src/app/editor/ndv/kinds/builtin";
import { llmKind } from "../src/app/editor/ndv/kinds/llm";
import type { NodeRole } from "../src/app/editor/ndv/registry";
import type { Workflow } from "../src/app/model";

const roles = new Map<string, NodeRole>(
  [...builtInKinds, llmKind].map((kind) => [kind.kind, kind.role]),
);
const fixture = parse(
  readFileSync(
    new URL("./fixtures/vendor-payment-approval.yaml", import.meta.url),
    "utf8",
  ),
) as Workflow;
const layout: LtrLayout = layoutLtr(fixture, {
  role: (kind) =>
    roles.get(kind) ?? (kind === "agent" ? "ai-agent" : undefined),
  triggers: [
    { id: "manual", kind: "manual" },
    { id: "trigger-1b2c3d4e", kind: "webhook" },
  ],
});
const steps = layout.tiles.filter((tile) => tile.step);
const meets = (
  a: { x: number; y: number; width: number; height: number },
  b: { x: number; y: number; width: number; height: number },
) =>
  a.x < b.x + b.width &&
  b.x < a.x + a.width &&
  a.y < b.y + b.height &&
  b.y < a.y + a.height;
const ZOOMS = [0.4, 0.5, 0.75, 1, 1.5];

describe("where a step's toolbar goes", () => {
  it("sits above the first step, centered, clear of its corner badge", () => {
    const first = steps.find((tile) => tile.id === "prepare-request")!;
    const scale = toolbarScale(0.5, false);
    expect(toolbarSpot(first, layout, 0.5)).toEqual({
      x: first.x + first.width / 2 - (TOOLBAR.width * scale) / 2,
      y: first.y - TOOLBAR_GAP - TOOLBAR.height * scale,
      width: TOOLBAR.width * scale,
      height: TOOLBAR.height * scale,
      scale,
      clear: true,
      gap: TOOLBAR_GAP,
    });
  });

  it("keeps its buttons 24 px wide on screen, 44 with a coarse pointer, and never smaller than at 100%", () => {
    for (const zoom of ZOOMS) {
      for (const [coarse, minimum] of [
        [false, 24],
        [true, 44],
      ] as const) {
        const scale = toolbarScale(zoom, coarse);
        expect(scale, `${zoom}`).toBeGreaterThanOrEqual(1);
        expect(
          TOOLBAR.button * scale * zoom,
          `${zoom} ${coarse}`,
        ).toBeGreaterThanOrEqual(minimum - 1e-9);
      }
    }
    expect(toolbarScale(1, false)).toBe(1);
  });

  it("stays above its step where the band above is taken, and says it isn't clear", () => {
    const taken = steps.filter((tile) => !toolbarSpot(tile, layout, 0.5).clear);
    expect(taken.length).toBeGreaterThan(0);
    for (const tile of taken) {
      const spot = toolbarSpot(tile, layout, 0.5);
      expect(spot.y + spot.height).toBe(tile.y - toolbarGap(0.5, tile.height));
      expect(spot.x + spot.width / 2).toBe(tile.x + tile.width / 2);
    }
  });

  it("never calls a band clear when anything else is in it, at every zoom", () => {
    for (const zoom of ZOOMS)
      for (const coarse of [false, true])
        for (const tile of steps) {
          const spot = toolbarSpot(tile, layout, zoom, { coarse });
          const others = toolbarObstacles(layout, zoom, tile.id).filter(
            (other) => meets(spot, other),
          );
          expect(spot.clear, `${tile.id} at ${zoom}`).toBe(others.length === 0);
        }
  });

  it("rises above the corner badge, which moves up as the canvas zooms out", () => {
    expect(toolbarGap(0.5)).toBe(TOOLBAR_GAP);
    expect(toolbarGap(1)).toBe(TOOLBAR_GAP);
    for (const zoom of [0.25, 0.3, 0.4, 0.45]) {
      // The issue button keeps 44 px on screen, centered on the corner
      // unless that would cross the tile's middle (canvas.css, .tile-issue).
      const target = Math.ceil(44 / zoom);
      const top = Math.min(-target / 2, 48 - target);
      const badgeTop = top + target / 2 - 10;
      expect(-toolbarGap(zoom), `${zoom}`).toBeLessThanOrEqual(badgeTop - 4);
    }
    expect(toolbarGap(0.4)).toBeGreaterThan(TOOLBAR_GAP);
  });

  it("has more room above at 100 percent than at 40", () => {
    const clear = (zoom: number) =>
      steps.filter((tile) => toolbarSpot(tile, layout, zoom).clear).length;
    expect(clear(1)).toBeGreaterThanOrEqual(clear(0.4));
    expect(clear(1)).toBeGreaterThan(0);
  });

  it("gives a chosen trigger's toolbar one button's width, above its tile", () => {
    const trigger = layout.tiles.find(
      (tile) => tile.id === "$trigger:trigger-1b2c3d4e",
    )!;
    for (const zoom of ZOOMS) {
      const spot = toolbarSpot(trigger, layout, zoom, { buttons: 1 });
      expect(spot.width).toBeCloseTo(TOOLBAR.single * spot.scale, 6);
      expect(spot.y + spot.height).toBeLessThanOrEqual(
        trigger.y - toolbarGap(zoom),
      );
    }
  });
});
