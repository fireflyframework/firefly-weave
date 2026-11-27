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
// A transaction's screens measured at a desktop, a narrow window and a phone,
// and on touch: the frame with how each step is undone and the point of no
// return, How it's undone in Settings with its operations, and an empty
// transaction's slot. A fine pointer from 768 px gets the desktop sizes; a
// coarse pointer or a narrower window gets 44 px targets.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { CanvasPage } from "./canvas-po";
import { hasPython } from "./local-authoring";
import {
  box,
  clippedText,
  density,
  intersects,
  type Box,
} from "./step-geometry";
import {
  ADD_STEP,
  HOLD_STEP,
  moveWorkflow,
  NOTIFY_STEP,
  openConnectedMove,
  openInside,
  openMove,
  SUBMIT_STEP,
} from "./transaction-po";

const SIZES = [
  { tag: "1440x900", width: 1440, height: 900, touch: false },
  { tag: "600x500", width: 600, height: 500, touch: false },
  { tag: "360x640", width: 360, height: 640, touch: false },
  { tag: "600x500 touch", width: 600, height: 500, touch: true },
];
/** hold reserves, add reverses, submit can't be undone, notify comes after it. */
const MOVE = moveWorkflow(
  HOLD_STEP() + ADD_STEP + SUBMIT_STEP + NOTIFY_STEP + "  irreversible: true\n",
);
/** move with nothing in it yet. */
const EMPTY = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: move-between-systems, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - id: move
      kind: transaction
      steps: []
      output: {literal: null}
  output: {literal: null}
`;

const inside = (outer: Box, inner: Box) =>
  inner.x >= outer.x - 0.5 &&
  inner.right <= outer.right + 0.5 &&
  inner.y >= outer.y - 0.5 &&
  inner.bottom <= outer.bottom + 0.5;
/** The page never scrolls sideways. */
async function expectNoPageScroll(page: Page) {
  const [scroll, width] = await page.evaluate(() => [
    document.scrollingElement!.scrollWidth,
    innerWidth,
  ]);
  expect(scroll).toBeLessThanOrEqual(width);
}
/**
 * The sides of the target's center, at `by` px, that a tap doesn't reach: what
 * its hit area takes, whatever draws it. A control that inserts at the same
 * place counts.
 */
function missed(target: Locator, by: number): Promise<string[]> {
  return target.evaluate((element, by) => {
    const r = element.getBoundingClientRect();
    const [x, y] = [r.x + r.width / 2, r.y + r.height / 2];
    const place = (node: Element | null | undefined) =>
      node
        ? `${node.getAttribute("data-insert-owner")}#${node.getAttribute("data-insert-index")}`
        : null;
    const sides: [number, number, string][] = [
      [-by, 0, "left"],
      [by, 0, "right"],
      [0, -by, "above"],
      [0, by, "below"],
    ];
    return sides.flatMap(([dx, dy, side]) => {
      const hit = document.elementFromPoint(x + dx, y + dy);
      if (hit && element.contains(hit)) return [];
      const same = place(hit?.closest("[data-insert-owner]"));
      if (same && same === place(element)) return [];
      return [`${side}: ${hit ? hit.className : "nothing"}`];
    });
  }, by);
}
/** Pans the canvas with the wheel, as a person does, until the target is in its middle. */
async function panIntoView(page: Page, canvas: CanvasPage, target: Locator) {
  const area = await box(canvas.root);
  const at = await box(target);
  const by = Math.round(at.x + at.width / 2 - (area.x + area.width / 2));
  await page.mouse.move(area.x + area.width / 2, area.middle);
  await page.mouse.wheel(by, 0);
  await expect
    .poll(async () => Math.abs((await box(target)).x - (at.x - by)))
    .toBeLessThan(3);
}
/** Zooms the canvas in with the wheel until the point of no return's label draws. */
async function untilLabelled(page: Page, canvas: CanvasPage, label: Locator) {
  const body = await box(canvas.root);
  await expect(async () => {
    if (await label.count()) return;
    await page.mouse.move(body.x + body.width / 2, body.y + body.height / 2);
    await page.keyboard.down("Control");
    await page.mouse.wheel(0, -40);
    await page.keyboard.up("Control");
    await expect(label).toHaveCount(1, { timeout: 500 });
  }).toPass({ timeout: 15_000 });
}

for (const size of SIZES)
  test.describe(`a transaction at ${size.tag}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      ...(size.touch ? { hasTouch: true, isMobile: true } : {}),
    });
    /** A tap target: 44 px on touch and below 768 px, else the desktop size. */
    const target = size.touch || size.width < 768 ? 44 : 36;

    test("each step's undo word, its badge and the point of no return stay readable inside the frame", async ({
      page,
    }) => {
      await openMove(page, MOVE);
      const canvas = new CanvasPage(page);
      await canvas.ready();
      await canvas.closeInspector();
      const label = canvas.root.locator('.point-label[data-point="submit"]');
      await untilLabelled(page, canvas, label);
      expect(await canvas.zoomPercent()).toBeGreaterThanOrEqual(55);
      await expectNoPageScroll(page);
      const frame = await box(
        canvas.root.locator('.loop-frame[data-frame="move"]'),
      );
      for (const [id, word] of [
        ["hold", "Reserved"],
        ["add", "Reversible"],
        ["submit", "Can't be undone"],
      ]) {
        const tile = canvas.tile(id);
        await expect(tile.locator(".tile-undo")).toHaveText(word);
        expect(await clippedText(tile.locator(".tile-undo"))).toEqual([]);
        // The badge sits on the card's lower-left corner, over no text.
        const card = await box(canvas.tileBody(id));
        const badge = await box(tile.locator(".undo-badge"));
        expect(
          Math.abs(badge.x + badge.width / 2 - card.x),
        ).toBeLessThanOrEqual(badge.width + 1);
        expect(
          Math.abs(badge.y + badge.height / 2 - card.bottom),
        ).toBeLessThanOrEqual(badge.height + 1);
        expect(inside(frame, badge)).toBe(true);
        expect(intersects(badge, await box(tile.locator(".tile-undo")))).toBe(
          false,
        );
      }
      // The line runs inside the frame; its label sits above the row, on one
      // line of at least 11 px, over no step.
      const line = await box(
        canvas.root.locator('.point-line[data-point="submit"]'),
      );
      expect(inside(frame, line)).toBe(true);
      const shown = await box(label);
      expect(inside(frame, shown)).toBe(true);
      expect(shown.height).toBeGreaterThanOrEqual(11);
      expect(shown.bottom).toBeLessThanOrEqual(
        (await box(canvas.tileBody("submit"))).y,
      );
      for (const id of ["hold", "add", "submit", "notify"])
        expect(intersects(shown, await box(canvas.tileBody(id)))).toBe(false);
      expect(await clippedText(label)).toEqual([]);
    });

    test("How it's undone and its operations fit the column, each control wide enough to press", async ({
      page,
    }) => {
      test.skip(!hasPython, "Needs the repository's Python environment.");
      // The operations list the project's actions: on a platform.
      const details = await openConnectedMove(page, MOVE);
      await openInside(details, "hold");
      await details.tab("Settings").click();
      const field = details.field("undo");
      const choice = field.getByRole("combobox", { name: "How it's undone" });
      await expect(choice).toHaveValue("Reserve, then confirm");
      await choice.scrollIntoViewIfNeeded();
      const column = await box(field);
      for (const control of [
        choice,
        details.dialog.getByRole("combobox", {
          name: "Confirm with",
          exact: true,
        }),
        details.dialog.getByRole("combobox", {
          name: "Release with",
          exact: true,
        }),
      ]) {
        await control.scrollIntoViewIfNeeded();
        const shown = await box(control);
        expect(shown.height).toBeGreaterThanOrEqual(target - 0.5);
        expect(shown.right).toBeLessThanOrEqual(
          (await box(details.dialog)).right + 0.5,
        );
      }
      expect(column.width).toBeLessThanOrEqual(size.width);
      const [scroll, width] = await details.dialog.evaluate((node) => [
        node.scrollWidth,
        node.clientWidth,
      ]);
      expect(scroll).toBeLessThanOrEqual(width);
      await expectNoPageScroll(page);
    });

    test("an empty transaction's Add a step slot takes a 44 px tap on touch", async ({
      page,
    }) => {
      await openMove(page, EMPTY);
      const canvas = new CanvasPage(page);
      await canvas.ready();
      await canvas.closeInspector();
      const slot = canvas.insertTarget("Add a step to transaction move");
      await panIntoView(page, canvas, slot);
      // On touch, a tap 21 px from its center in any direction still adds
      // there, whatever the zoom draws it at.
      if ((await density(page)).touch)
        expect(await missed(slot, 21)).toEqual([]);
      await expectNoPageScroll(page);
    });
  });
