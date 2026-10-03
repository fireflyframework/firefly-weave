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
import { expect, type Locator, type Page } from "@playwright/test";

/** Check the rendered bounds, including panels larger than the viewport. */
export async function expectInViewport(element: Locator, inset = 0) {
  const box = await element.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    return {
      left: rect.left,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      width: innerWidth,
      height: innerHeight,
    };
  });
  expect(box.left).toBeGreaterThanOrEqual(inset - 1);
  expect(box.top).toBeGreaterThanOrEqual(inset - 1);
  expect(box.right).toBeLessThanOrEqual(box.width - inset + 1);
  expect(box.bottom).toBeLessThanOrEqual(box.height - inset + 1);
}

/** Real hit tests respect top-layer popovers that escape an overflow ancestor. */
export async function expectNotClippedBy(element: Locator, container: Locator) {
  await expect(container).toBeVisible();
  const visible = await element.evaluate((node) => {
    const rect = node.getBoundingClientRect();
    return [
      [rect.left + 2, rect.top + 2],
      [rect.right - 2, rect.top + 2],
      [rect.left + 2, rect.bottom - 2],
      [rect.right - 2, rect.bottom - 2],
    ].every(([x, y]) => {
      const hit = document.elementFromPoint(x, y);
      return !!hit && node.contains(hit);
    });
  });
  expect(
    visible,
    "The field or overlay must receive pointer input at every corner",
  ).toBe(true);
}

/** Sample all rectangles in one browser frame so canvas animation cannot skew them. */
export async function expectNoOverlap(elements: Locator) {
  const boxes = await elements.evaluateAll((nodes) =>
    nodes.map((node) => {
      const r = node.getBoundingClientRect();
      return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
    }),
  );
  for (let i = 0; i < boxes.length; i++)
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i],
        b = boxes[j];
      expect(
        Math.min(a.right, b.right) - Math.max(a.left, b.left) <= 1 ||
          Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) <= 1,
        `Controls ${i + 1} and ${j + 1} must not overlap`,
      ).toBe(true);
    }
}

/** Count actual keyboard stops in a region, including roving-tabindex widgets. */
export async function tabStops(
  page: Page,
  region: Locator,
  maximum = 200,
): Promise<number> {
  await region.evaluate((node) => {
    node.setAttribute("data-tab-audit", "");
    node
      .querySelectorAll("[data-tab-visited]")
      .forEach((child) => child.removeAttribute("data-tab-visited"));
  });
  let count = 0;
  try {
    for (let i = 0; i < maximum; i++) {
      const state = await region.evaluate((node) => {
        const active = document.activeElement;
        if (!active || !node.contains(active)) return "outside";
        if (active.hasAttribute("data-tab-visited")) return "repeat";
        active.setAttribute("data-tab-visited", "");
        return "inside";
      });
      if (state !== "inside") return count;
      count++;
      await page.keyboard.press("Tab");
    }
    throw new Error(
      "Keyboard focus did not leave or cycle within the inspection limit",
    );
  } finally {
    await region.evaluate((node) => {
      node.removeAttribute("data-tab-audit");
      node
        .querySelectorAll("[data-tab-visited]")
        .forEach((child) => child.removeAttribute("data-tab-visited"));
    });
  }
}
