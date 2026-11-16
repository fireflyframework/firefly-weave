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
// The Runs tab's layout: the list's width per window, row and control
// heights for a fine pointer and for touch, the header and banner clear of
// the canvas's first step, no clipped text, no sideways page scroll, a list
// that scrolls in its own box, and targets of at least 24 px (44 on touch).
import { expect, test, type Page } from "@playwright/test";
import {
  listedKinds,
  olderRuns,
  openRunsTab,
  RUN_IDS,
  runFixture,
  type RunsPage,
} from "./runs-po";
import { box, boxes, clippedText, intersects } from "./step-geometry";

const layouts = [
  { tag: "1440x900", width: 1440, height: 900, list: 320, touch: false },
  { tag: "1024x768", width: 1024, height: 768, list: 280, touch: false },
  { tag: "600x500", width: 600, height: 500, list: "full", touch: false },
  { tag: "360x640", width: 360, height: 640, list: "full", touch: false },
  { tag: "touch 600x500", width: 600, height: 500, list: "full", touch: true },
] as const;

async function choose(page: Page, runs: RunsPage, id: string) {
  const back = page.getByRole("button", { name: "Back to runs" });
  if (await back.isVisible()) await back.click();
  await runs.row(id).click();
  await expect(runs.canvas.locator(".run-header .run-caption")).toBeVisible();
}

for (const layout of layouts)
  test.describe(layout.tag, () => {
    test.use({
      viewport: { width: layout.width, height: layout.height },
      hasTouch: layout.touch,
      isMobile: false,
    });
    const coarse = layout.touch || layout.width < 768;

    test("the list's width, rows, controls and targets", async ({ page }) => {
      // Enough runs that the list scrolls.
      const { runs } = await openRunsTab(page, {
        runs: [...listedKinds.map(runFixture), ...olderRuns(20)],
      });
      await expect(runs.rows.first()).toBeVisible();
      const back = page.getByRole("button", { name: "Back to runs" });
      if (await back.isVisible()) await back.click();
      const list = await box(page.locator("weave-runs-list"));
      const view = await box(page.locator("weave-runs-view"));
      if (layout.list === "full")
        expect(Math.abs(list.width - view.width)).toBeLessThanOrEqual(0.5);
      else expect(Math.abs(list.width - layout.list)).toBeLessThanOrEqual(0.5);
      for (const row of await boxes(runs.rows))
        expect(row.height).toBeCloseTo(coarse ? 56 : 48, 1);
      for (const control of await boxes(
        runs.panel.locator(".runs-filters-button, .runs-switch"),
      ))
        expect(control.height).toBeGreaterThanOrEqual(coarse ? 44 : 36);
      for (const icon of await boxes(
        runs.panel.locator("button.icon-button"),
      )) {
        expect(icon.width).toBeGreaterThanOrEqual(coarse ? 44 : 32);
        expect(icon.height).toBeGreaterThanOrEqual(coarse ? 44 : 32);
      }
      // Every control of the list is a target of 24 px at least (44 on touch).
      for (const target of await boxes(
        runs.panel.locator("button:not(.run-row), [role=switch]"),
      )) {
        expect(target.width).toBeGreaterThanOrEqual(coarse ? 44 : 24);
        expect(target.height).toBeGreaterThanOrEqual(coarse ? 44 : 24);
      }
      // The list scrolls in its own box; the page never scrolls.
      const scroller = runs.panel.locator(".runs-scroll");
      expect(
        await scroller.evaluate((e) => e.scrollHeight > e.clientHeight),
      ).toBe(true);
      expect(
        await page.evaluate(
          () => document.scrollingElement!.scrollWidth === innerWidth,
        ),
      ).toBe(true);
      expect(await clippedText(runs.panel, ".run-row")).toEqual([]);
    });

    test("the header and banner stay clear of the canvas's first step", async ({
      page,
    }) => {
      const { runs } = await openRunsTab(page);
      await expect(runs.rows.first()).toBeVisible();
      for (const id of [RUN_IDS.failed, RUN_IDS.older]) {
        await choose(page, runs, id);
        const header = await box(runs.canvas.locator(".run-header"));
        const first = await box(
          runs.canvas.locator('.tile-node[data-tile="fetch"]'),
        );
        expect(intersects(header, first)).toBe(false);
        const banner = runs.canvas.locator(".run-banner");
        if (await banner.count())
          expect(intersects(await box(banner), first)).toBe(false);
        expect(await clippedText(runs.canvas, ".run-header")).toEqual([]);
        if (await banner.count())
          expect(await clippedText(runs.canvas, ".run-banner")).toEqual([]);
        expect(
          await page.evaluate(
            () => document.scrollingElement!.scrollWidth === innerWidth,
          ),
        ).toBe(true);
      }
    });
  });

test.describe("360x640, a run that is still going", () => {
  test.use({ viewport: { width: 360, height: 640 } });

  test("the title keeps one line, and the live line leads the caption", async ({
    page,
  }) => {
    const { runs } = await openRunsTab(page);
    await expect(runs.rows.first()).toBeVisible();
    await choose(page, runs, RUN_IDS.running);
    const header = runs.canvas.locator(".run-header");
    const title = await box(header.locator("h2"));
    const pill = await box(header.locator(".run-pill"));
    // The ID, Copy run ID and the status share the first line.
    expect(Math.abs(title.middle - pill.middle)).toBeLessThanOrEqual(2);
    expect(title.height).toBeLessThanOrEqual(28);
    const live = header.locator(".run-caption .run-follow");
    await expect(live).toBeVisible();
    await expect(live).toHaveText("Following this run");
    expect(await clippedText(runs.canvas, ".run-header")).toEqual([]);
  });
});
