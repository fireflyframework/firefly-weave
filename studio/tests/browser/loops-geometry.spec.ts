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
// A loop's geometry at the sizes Studio ships: the frame holds its body with
// 64 units each side, its header band stays at least 24 px and clear of the
// body's first step, every "+" and the picker's buttons are big enough to
// hit (44 px on touch), and nothing scrolls the page sideways.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { openWorkflow } from "./canvas-po";
import { StepDetailsPage, yamlFile } from "./step-details-po";

const ORDERS = yamlFile(
  "orders-geometry.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: orders-geometry, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      orders: {type: array, items: {type: object}}
  outputSchema: {}
  steps:
    - id: orders
      kind: forEach
      items: {ref: /input/orders}
      concurrency: 4
      maxItems: 1000
      collect: all
      body:
        steps:
          - {id: check, kind: transform, value: {ref: /item}}
          - {id: send, kind: transform, value: {object: {sent: {ref: /item}}}}
        output: {ref: /item}
    - {id: summarize, kind: transform, value: {literal: true}}
  output: {literal: null}
`,
);

const box = async (locator: Locator) => (await locator.boundingBox())!;
async function zoomOf(page: Page): Promise<number> {
  const text = await page.locator(".canvas-v2 .zoom-level").textContent();
  return parseInt(text ?? "100", 10) / 100;
}

for (const size of [
  { tag: "1440x900", width: 1440, height: 900, touch: false },
  { tag: "600x500", width: 600, height: 500, touch: false },
  { tag: "360x640", width: 360, height: 640, touch: false },
  { tag: "touch 600x500", width: 600, height: 500, touch: true },
])
  test.describe(`a loop's geometry at ${size.tag}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
    });

    test("the frame, its header, the + and the picker", async ({ page }) => {
      const canvas = await openWorkflow(page, ORDERS);
      const frame = await box(
        canvas.root.locator('.loop-frame[data-frame="orders"]'),
      );
      const header = await box(canvas.tileBody("orders"));
      const first = await box(canvas.tileBody("check"));
      const last = await box(canvas.tileBody("send"));
      const zoom = await zoomOf(page);
      // The body inside the frame, 64 units in from each side.
      expect(first.x - frame.x).toBeGreaterThanOrEqual(64 * zoom - 1);
      expect(
        frame.x + frame.width - (last.x + last.width),
      ).toBeGreaterThanOrEqual(64 * zoom - 1);
      // The header band: at least 24 px, and clear of the first step.
      // At least 24 px (a hair less is the browser's sub-pixel rounding).
      expect(header.height).toBeGreaterThanOrEqual(23.9);
      expect(header.y + header.height).toBeLessThanOrEqual(first.y);
      // Every "+" is 24 canvas units across, at the canvas's zoom.
      for (const name of [
        "Insert a step between orders and summarize",
        "Add a step at the end of For each item of orders",
      ]) {
        const plus = await box(canvas.insertTarget(name));
        expect(plus.width, name).toBeGreaterThanOrEqual(24 * zoom - 1);
        expect(plus.height, name).toBeGreaterThanOrEqual(24 * zoom - 1);
      }
      // The picker in step details: 32 px buttons, 44 on touch.
      const details = new StepDetailsPage(page);
      await details.openWithKeyboard("check");
      // Below 768 px the panes are tabs: show Input.
      if (size.width < 768)
        await details.dialog
          .getByRole("tablist", { name: "Step details panes" })
          .getByRole("tab", { name: "Input" })
          .click();
      const buttons = details.dialog.locator(
        "weave-input-pane weave-iteration-picker .picker-group button:visible",
      );
      await expect(buttons).toHaveCount(3);
      const least = size.touch ? 44 : 32;
      for (const button of await buttons.all()) {
        const each = await box(button);
        expect(each.width).toBeGreaterThanOrEqual(least);
        expect(each.height).toBeGreaterThanOrEqual(least);
      }
      // Nothing scrolls the page sideways.
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBe(true);
    });
  });
