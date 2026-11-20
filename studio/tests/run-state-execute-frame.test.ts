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
// How Execute workflow frames its run in the canvas area the run log leaves:
// the whole workflow at 55% or more, else the loop that holds the step the
// run failed or is at, else that step at 60%.
import { describe, expect, it } from "vitest";
import { frameExecuted } from "../src/app/editor/canvas/run-state";

const area = { width: 800, height: 500 };
/** Where a box lands on screen with a view. */
const onScreen = (
  box: { x: number; y: number; width: number; height: number },
  view: { zoom: number; pan: { x: number; y: number } },
) => ({
  left: box.x * view.zoom + view.pan.x,
  top: box.y * view.zoom + view.pan.y,
  right: (box.x + box.width) * view.zoom + view.pan.x,
  bottom: (box.y + box.height) * view.zoom + view.pan.y,
});
const inside = (shown: ReturnType<typeof onScreen>) =>
  shown.left >= 0 &&
  shown.top >= 0 &&
  shown.right <= area.width &&
  shown.bottom <= area.height;

describe("Execute workflow's framing", () => {
  it("shows the whole workflow at 55% or more, centered", () => {
    // 1,420 wide: 55% fits (781 + 16), 56% doesn't (795.2 + 16).
    const workflow = { x: 0, y: 0, width: 1420, height: 400 };
    const view = frameExecuted(() => workflow, null, null, area);
    expect(view.zoom).toBe(0.55);
    expect(inside(onScreen(workflow, view))).toBe(true);
  });

  it("else shows the loop that holds the step, at 55% or more", () => {
    const workflow = { x: 0, y: 0, width: 3000, height: 400 };
    const loop = { x: 1200, y: 40, width: 1000, height: 300 };
    const step = { x: 1500, y: 100, width: 64, height: 120 };
    const view = frameExecuted(() => workflow, loop, step, area);
    expect(view.zoom).toBe(0.78);
    expect(inside(onScreen(loop, view))).toBe(true);
  });

  it("else shows the step itself, at 60%", () => {
    const workflow = { x: 0, y: 0, width: 4000, height: 400 };
    const loop = { x: 1200, y: 40, width: 2000, height: 300 };
    const step = { x: 1500, y: 100, width: 64, height: 120 };
    const view = frameExecuted(() => workflow, loop, step, area);
    expect(view.zoom).toBe(0.6);
    expect(inside(onScreen(step, view))).toBe(true);
    // Centered across.
    const shown = onScreen(step, view);
    expect(
      Math.abs((shown.left + shown.right) / 2 - area.width / 2),
    ).toBeLessThan(1);
  });
});
