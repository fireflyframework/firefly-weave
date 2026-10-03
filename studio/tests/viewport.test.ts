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
// The canvas viewport (W3-1): a readable fit on open, "Fit all" down to
// 40 %, and zooming that keeps the point under the pointer in place.
import { describe, expect, it } from "vitest";
import { StructuredCanvasAdapter, kinds } from "../src/app/model";
import {
  FIT_ALL_MIN,
  MIN_ZOOM,
  fitAll,
  readableFit,
  reveal,
  usableViewport,
  wheelFactor,
  zoomAt,
} from "../src/app/designer/viewport";

/** A 12-step workflow's extent, as the designer measures it. */
function twelveSteps() {
  const m = new StructuredCanvasAdapter();
  for (let i = 0; i < 12; i++) m.insert(kinds[i % 2 ? 1 : 4]);
  const points = [
    ...m.nodes().map((n) => n.point),
    m.boundaries().start,
    m.boundaries().end,
  ];
  return {
    bounds: {
      minX: Math.min(...points.map((p) => p.x)),
      minY: Math.min(...points.map((p) => p.y)),
      maxX: Math.max(...points.map((p) => p.x)) + 208,
      maxY: Math.max(...points.map((p) => p.y)) + 64,
    },
    spine: m.boundaries().start.x + 104,
  };
}

describe("readable fit", () => {
  it("opens a 12-step workflow at 75 % or more, Start near the top", () => {
    const { bounds, spine } = twelveSteps();
    // The canvas of a 1280x720 window, and of the same window at 200 %.
    for (const view of [
      { width: 606, height: 520 },
      { width: 290, height: 180 },
    ]) {
      const fit = readableFit(bounds, view, spine);
      expect(fit.zoom).toBeGreaterThanOrEqual(0.75);
      expect(fit.zoom).toBeLessThanOrEqual(1);
      expect(bounds.minY * fit.zoom + fit.pan.y).toBeCloseTo(32);
      // A 12 px title stays at least 9 px; the designer scales it to 12.
      expect(12 / fit.zoom).toBeGreaterThanOrEqual(12);
    }
  });
  it("centers a graph that fits the width", () => {
    const fit = readableFit(
      { minX: 0, minY: 0, maxX: 400, maxY: 2000 },
      { width: 1000, height: 500 },
      100,
    );
    expect(fit.zoom).toBe(1);
    expect(fit.pan.x).toBe(300);
  });
  it("never pushes a wide graph's left edge off the canvas", () => {
    const fit = readableFit(
      { minX: 96, minY: 24, maxX: 2000, maxY: 900 },
      { width: 600, height: 500 },
      200,
    );
    expect(fit.zoom).toBe(0.75);
    expect(96 * fit.zoom + fit.pan.x).toBeLessThanOrEqual(24);
  });
});

describe("fit all", () => {
  it("shows the whole graph down to 40 %", () => {
    const { bounds } = twelveSteps();
    const fit = fitAll(bounds, { width: 606, height: 520 });
    expect(fit.zoom).toBeGreaterThanOrEqual(FIT_ALL_MIN);
    const tiny = fitAll(bounds, { width: 200, height: 150 });
    expect(tiny.zoom).toBe(FIT_ALL_MIN);
  });
});

describe("zooming", () => {
  it("keeps the point under the pointer within 2 px", () => {
    const start = { zoom: 0.8, pan: { x: 40, y: -120 } };
    const at = { x: 310, y: 205 };
    const world = {
      x: (at.x - start.pan.x) / start.zoom,
      y: (at.y - start.pan.y) / start.zoom,
    };
    const next = zoomAt(start, wheelFactor(100), at);
    expect(Math.abs(world.x * next.zoom + next.pan.x - at.x)).toBeLessThan(2);
    expect(Math.abs(world.y * next.zoom + next.pan.y - at.y)).toBeLessThan(2);
  });
  it("changes by less than 20 % for one wheel step", () => {
    expect(Math.abs(1 - wheelFactor(100))).toBeLessThan(0.2);
    expect(Math.abs(1 - wheelFactor(-100))).toBeLessThan(0.2);
  });
  it("stays between 25 % and 200 %", () => {
    const view = { zoom: 0.3, pan: { x: 0, y: 0 } };
    expect(zoomAt(view, 0.1, { x: 0, y: 0 }).zoom).toBe(MIN_ZOOM);
    expect(zoomAt({ ...view, zoom: 1.9 }, 3, { x: 0, y: 0 }).zoom).toBe(2);
  });
});

describe("revealing a step", () => {
  it("pans a step 48 px inside the canvas, and only when needed", () => {
    const view = { zoom: 1, pan: { x: 0, y: 0 } };
    const size = { width: 600, height: 400 };
    expect(
      reveal(view, { x: 100, y: 100, width: 208, height: 64 }, size),
    ).toEqual({ x: 0, y: 0 });
    const below = reveal(
      view,
      { x: 100, y: 900, width: 208, height: 64 },
      size,
    );
    expect(900 + 64 + below.y).toBe(400 - 48);
    const left = reveal(
      view,
      { x: -300, y: 100, width: 208, height: 64 },
      size,
    );
    expect(-300 + left.x).toBe(48);
  });
  it("keeps only usable stored viewports", () => {
    expect(usableViewport({ x: 10, y: 20, zoom: 0.8 })).toEqual({
      zoom: 0.8,
      pan: { x: 10, y: 20 },
    });
    expect(usableViewport({ x: 10, y: 20, zoom: 9 })).toBeNull();
    expect(usableViewport({ x: "a", y: 0, zoom: 1 })).toBeNull();
    expect(usableViewport(null)).toBeNull();
  });
});
