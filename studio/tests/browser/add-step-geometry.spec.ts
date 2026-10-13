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
// The Add a step panel's alignment and density, measured: one left edge 16 px
// inside the panel's border for the title, subtitle, search, group captions
// and entry tiles; 56 px entries (one line more when a description wraps);
// 36 px search and 32 px buttons on a desktop with a fine pointer, 44 px below
// 768 px; nothing clipped.
import { expect, test } from "@playwright/test";
import { AddStepPanel } from "./add-step-po";
import { openNewWorkflow } from "./canvas-po";
import { box, clippedText } from "./step-geometry";

for (const size of [
  { width: 1440, height: 900, control: 36, button: 32 },
  { width: 600, height: 500, control: 44, button: 44 },
])
  test(`the Add a step panel lines up and keeps its density at ${size.width}x${size.height}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: size.width, height: size.height });
    const canvas = await openNewWorkflow(page);
    await canvas.insertTarget("Add first step").click();
    const panel = new AddStepPanel(page);
    await expect(panel.title).toHaveText("What triggers this workflow?");
    await panel.settled();
    const root = await box(panel.root);
    if (size.width >= 768) {
      expect(Math.round(root.width)).toBe(384);
      expect(Math.round(size.width - (root.x + root.width))).toBe(0);
    } else {
      expect(Math.round(root.x)).toBe(0);
      expect(Math.round(root.width)).toBe(size.width);
    }
    const lefts = await panel.root.evaluate((element) =>
      [
        ".add-step-title",
        ".add-step-subtitle",
        ".add-step-search",
        ".add-step-group",
        ".add-step-entry .entry-tile",
      ].map((selector) => {
        const first = element.querySelector(selector)!;
        const style = getComputedStyle(first);
        const rect = first.getBoundingClientRect();
        // A caption's text starts after its padding.
        return Math.round(
          rect.left +
            (selector === ".add-step-group"
              ? parseFloat(style.paddingLeft)
              : 0),
        );
      }),
    );
    // Measured inside the panel's left border, as the design measures it.
    const inner =
      root.x + (await panel.root.evaluate((element) => element.clientLeft));
    expect(lefts.map((left) => left - Math.round(inner))).toEqual([
      16, 16, 16, 16, 16,
    ]);
    // 56 px rows; a description that wraps (a reason an entry can't run
    // yet, read whole on touch) adds its second line and nothing else.
    const rows = await panel.root
      .locator(".add-step-entry")
      .evaluateAll((entries) =>
        entries.map((entry) => {
          const description = entry.querySelector(".entry-description")!;
          const line = parseFloat(getComputedStyle(description).lineHeight);
          const lines = Math.round(
            description.getBoundingClientRect().height / line,
          );
          return {
            height: Math.round(entry.getBoundingClientRect().height),
            expected: 56 + (lines - 1) * line,
          };
        }),
      );
    expect(rows.length).toBe(8);
    for (const row of rows) expect(row.height).toBe(row.expected);
    expect(rows.filter((row) => row.height === 56).length).toBeGreaterThan(5);
    expect(
      Math.round((await box(panel.root.locator(".add-step-search"))).height),
    ).toBe(size.control);
    expect(
      Math.round((await box(panel.root.locator(".add-step-close"))).height),
    ).toBe(size.button);
    expect(await clippedText(panel.root, "")).toEqual([]);
    const scroll = await page.evaluate(
      () => document.documentElement.scrollWidth - innerWidth,
    );
    expect(scroll).toBeLessThanOrEqual(0);
  });
