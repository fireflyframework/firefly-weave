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
// The screens of calling a workflow, measured at a desktop, a tablet and a
// phone width and on touch: the call step's Open workflow and its choices,
// the Workflow list, the Called by a workflow trigger, the run log's called
// workflows and the child run card. A fine pointer from 768 px gets the
// desktop sizes; a coarse pointer or a narrower window gets 44 px targets.
import { expect, test, type Page } from "@playwright/test";
import {
  openCallRun,
  openCallRunLog,
  openCalledBy,
  openCallStep,
  workflowOptions,
} from "./calls-po";
import { hasPython } from "./local-authoring";
import { box, boxes, clippedText, density } from "./step-geometry";

const SIZES = [
  { tag: "1440x900", use: { viewport: { width: 1440, height: 900 } } },
  { tag: "768x1024", use: { viewport: { width: 768, height: 1024 } } },
  { tag: "360x640", use: { viewport: { width: 360, height: 640 } } },
  {
    tag: "600x500 touch",
    use: {
      viewport: { width: 600, height: 500 },
      hasTouch: true,
      isMobile: true,
    },
  },
];

/** The page never scrolls sideways. */
async function expectNoPageScroll(page: Page) {
  const [scroll, width] = await page.evaluate(() => [
    document.scrollingElement!.scrollWidth,
    innerWidth,
  ]);
  expect(scroll).toBeLessThanOrEqual(width);
}
const near = (actual: number, expected: number, what: string) =>
  expect(Math.abs(actual - expected), `${what}: ${actual}`).toBeLessThanOrEqual(
    0.5,
  );

for (const size of SIZES)
  test.describe(`Calling a workflow at ${size.tag}`, () => {
    test.use(size.use);

    test("the call step: Open workflow beside its Workflow or under it, and its choices", async ({
      page,
    }) => {
      const details = await openCallStep(page);
      const { touch } = await density(page);
      const field = details.field("workflow");
      const select = await box(field.getByRole("combobox"));
      const open = await box(
        field.getByRole("button", { name: "Open workflow" }),
      );
      near(open.height, touch ? 44 : 32, "Open workflow height");
      if (touch) {
        // Under the hint, at the right edge of the control and its choices button.
        const control = await box(field.locator("weave-resource-field"));
        expect(open.y).toBeGreaterThanOrEqual(select.bottom);
        expect(Math.abs(open.right - control.right)).toBeLessThanOrEqual(1);
      } else {
        // Beside the select, on its bottom edge.
        expect(open.x).toBeGreaterThan(select.right);
        expect(Math.abs(open.bottom - select.bottom)).toBeLessThanOrEqual(1);
      }
      // When it runs: two 36 px segments on one line, or two equal 44 px ones on touch.
      const segments = await boxes(details.field("mode").getByRole("radio"));
      expect(segments).toHaveLength(2);
      for (const segment of segments)
        near(segment.height, touch ? 44 : 36, "segment height");
      if (touch)
        expect(
          Math.abs(segments[0].width - segments[1].width),
        ).toBeLessThanOrEqual(1);
      else expect(segments[1].y).toBe(segments[0].y);
      expect(await clippedText(details.dialog)).toEqual([]);
      await expectNoPageScroll(page);
    });

    test("the Workflow list: versions as rows, in the window", async ({
      page,
    }) => {
      const details = await openCallStep(page);
      const { touch } = await density(page);
      const options = await workflowOptions(page, details);
      const rows = await boxes(options.getByRole("option"));
      expect(rows.length).toBeGreaterThan(3);
      for (const row of rows)
        expect(row.height, "version row").toBeGreaterThanOrEqual(
          touch ? 44 : 32,
        );
      const list = await box(options);
      const { width } = page.viewportSize()!;
      expect(list.x).toBeGreaterThanOrEqual(0);
      expect(list.right).toBeLessThanOrEqual(width);
      await expectNoPageScroll(page);
    });

    test("the Called by a workflow trigger: options, names and callers", async ({
      page,
    }) => {
      const details = await openCalledBy(page);
      const { touch } = await density(page);
      const dialog = details.dialog;
      for (const card of await boxes(dialog.locator(".cb-card")))
        expect(card.height, "option").toBeGreaterThanOrEqual(touch ? 44 : 38);
      for (const chip of await boxes(dialog.locator(".cb-chip")))
        near(chip.height, touch ? 44 : 28, "name chip height");
      for (const remove of await boxes(dialog.locator(".cb-chip-remove"))) {
        near(remove.height, touch ? 44 : 24, "Remove height");
        near(remove.width, touch ? 44 : 24, "Remove width");
      }
      const callers = await boxes(dialog.locator(".cb-caller"));
      expect(callers).toHaveLength(4);
      for (const caller of callers)
        expect(caller.height, "caller row").toBeGreaterThanOrEqual(
          touch ? 56 : 44,
        );
      for (const open of await boxes(dialog.locator(".cb-open")))
        near(open.height, touch ? 44 : 32, "Open height");
      // Input and result side by side, or one under the other.
      const [input, result] = await boxes(dialog.locator(".cb-box"));
      if (touch) expect(result.y).toBeGreaterThanOrEqual(input.bottom);
      else expect(Math.abs(result.y - input.y)).toBeLessThanOrEqual(1);
      await expectNoPageScroll(page);
    });

    test("the run log: a called workflow's row and its steps step in by level", async ({
      page,
    }) => {
      test.skip(!hasPython, "Execute runs on the Studio host's Python.");
      const log = await openCallRunLog(page);
      const { touch } = await density(page);
      // The panel's content edge, inside its border.
      const panel = await box(log);
      panel.x += await log.evaluate((element) => element.clientLeft);
      const [notify, profile] = await boxes(log.locator(".run-log-frame-row"));
      near(notify.height, touch ? 44 : 36, "called workflow row height");
      near(notify.x - panel.x, touch ? 16 : 28, "called workflow indent");
      const level = profile.x - notify.x;
      // The steps' rule is 2 px; the next called workflow sits 22 px further in (16 on a phone).
      expect(level).toBeGreaterThan(0);
      expect(notify.right).toBeLessThanOrEqual(panel.right);
      expect(profile.right).toBeLessThanOrEqual(panel.right + 0.5);
      await expectNoPageScroll(page);
    });

    test("the child run card: Open run beside its title", async ({ page }) => {
      const { card } = await openCallRun(page);
      const { touch } = await density(page);
      const open = await box(card.getByRole("button", { name: "Open run" }));
      near(open.height, touch ? 44 : 32, "Open run height");
      const whole = await box(card);
      expect(open.right).toBeLessThanOrEqual(whole.right);
      await expectNoPageScroll(page);
    });
  });
