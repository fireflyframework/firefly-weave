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
// At 480 px and below the four view tabs fold into one view switcher on the
// workflow name's row, on every view: a 44 px menu button that names the
// view shown and opens the views as radio items. The name gets the room the
// tabs took, the settings gear keeps its 44 px target, and the bar keeps its
// height. From 481 px up the tabs stay.
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { useNewEditor } from "./canvas-po";
import { DesignerPage } from "./designer-po";
import { openStepFixture, settingsGear, stepFixture } from "./step-details-po";
import { box, boxes, intersects } from "./step-geometry";
import {
  allCapabilities,
  connected,
  newWorkflow,
  showView,
  viewSwitcher,
  type WorkflowView,
} from "./support";

const VIEWS: WorkflowView[] = ["Designer", "Source", "Outline", "Runs"];
type Draft = "offline" | "connected";

/** A workflow file opened offline, or a platform draft, on the Designer. */
async function openDraft(page: Page, draft: Draft) {
  if (draft === "offline") {
    await openStepFixture(page);
    return;
  }
  await useNewEditor(page);
  await connected(page, { capabilities: [...allCapabilities, "compile"] });
  await newWorkflow(page);
  await new DesignerPage(page).setSource(readFileSync(stepFixture, "utf8"));
  // A platform draft shows its status dot on the name's row.
  await expect(page.locator(".editor-identity .status-chip")).toBeVisible();
}

const tabs = (page: Page) =>
  page.getByRole("tablist", { name: "Workflow views" });

// The room the UX design gives the name (the bar's content width less the
// gear, the status dot and a 96 px switcher), and the bar's height on the
// views with commands, as it was with the tabs.
const layouts = [
  {
    tag: "360x640",
    width: 360,
    height: 640,
    bar: 111,
    name: {
      offline: { Designer: 116, Runs: 168 },
      connected: { Designer: 90, Runs: 142 },
    },
  },
  {
    tag: "390x844",
    width: 390,
    height: 844,
    bar: 119,
    name: {
      offline: { Designer: 146, Runs: 198 },
      connected: { Designer: 120, Runs: 172 },
    },
  },
] as const;

for (const layout of layouts)
  for (const draft of ["offline", "connected"] as const)
    test.describe(`${draft} draft at ${layout.tag}`, () => {
      test.use({ viewport: { width: layout.width, height: layout.height } });

      test("the view switcher shares the name's row on every view; the gear and the name keep their room, the bar its height", async ({
        page,
      }) => {
        await openDraft(page, draft);
        await expect(tabs(page)).toBeHidden();
        const commandBars: number[] = [];
        for (const view of VIEWS) {
          await showView(page, view);
          const switcher = viewSwitcher(page);
          await expect(switcher).toHaveAccessibleName(`Workflow view: ${view}`);
          await expect(switcher).toHaveText(view);
          const bar = await box(page.locator(".editor-bar"));
          const heading = await box(page.locator(".editor-heading"));
          const name = await box(page.locator(".editor-identity h1"));
          const toggle = await box(switcher);

          // A whole target, on the name's row, inside the bar's padding.
          expect(toggle.height, `${view}: switcher height`).toBeGreaterThan(
            43.5,
          );
          expect(toggle.width, `${view}: switcher width`).toBeGreaterThan(95.5);
          expect(name.middle).toBeGreaterThan(toggle.y);
          expect(name.middle).toBeLessThan(toggle.bottom);
          expect(toggle.right).toBeLessThanOrEqual(bar.right - 11.5);
          // Nothing on the name's row overlaps another.
          const row = await boxes(page.locator(".editor-identity > *"));
          for (const item of row)
            expect(intersects(item, toggle), `${view}: overlaps`).toBe(false);
          for (const [index, item] of row.entries())
            for (const other of row.slice(index + 1))
              expect(intersects(item, other), `${view}: row overlaps`).toBe(
                false,
              );

          if (view === "Designer" || view === "Runs")
            expect(
              name.width,
              `${view}: the name's room`,
            ).toBeGreaterThanOrEqual(layout.name[draft][view] - 0.5);
          else expect(name.width).toBeGreaterThanOrEqual(80);

          if (view === "Runs") {
            // No commands, no gear, and no row of tabs: the bar ends with
            // the name's row.
            await expect(settingsGear(page)).toHaveCount(0);
            await expect(page.locator(".editor-toolbar")).toHaveCount(0);
            expect(
              bar.bottom - Math.max(heading.bottom, toggle.bottom),
            ).toBeLessThanOrEqual(9);
            continue;
          }
          const gear = await box(settingsGear(page));
          expect([gear.width, gear.height], `${view}: gear`).toEqual([44, 44]);
          expect(Math.abs(gear.middle - name.middle)).toBeLessThanOrEqual(1);
          // The switcher fits the heading's height: the row grows no taller.
          expect(toggle.y).toBeGreaterThanOrEqual(heading.y - 0.5);
          expect(toggle.bottom).toBeLessThanOrEqual(heading.bottom + 0.5);
          commandBars.push(bar.height);
        }
        // Designer, Source and Outline: the bar as tall as with the tabs.
        for (const height of commandBars)
          expect(Math.abs(height - layout.bar)).toBeLessThanOrEqual(1);
        expect(
          await page.evaluate(
            () => document.scrollingElement!.scrollWidth === innerWidth,
          ),
        ).toBe(true);
      });
    });

test.describe("the view switcher at 390x844", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("opens on the view shown, moves with the keys, and names the view it shows", async ({
    page,
  }) => {
    await openStepFixture(page);
    const switcher = viewSwitcher(page);
    await expect(switcher).toHaveAccessibleName("Workflow view: Designer");
    await expect(switcher).toHaveAttribute("aria-haspopup", "menu");
    await expect(switcher).toHaveAttribute("aria-expanded", "false");
    const menu = page.getByRole("menu");
    const items = menu.getByRole("menuitemradio");

    // Enter opens the views as radio items, the one shown checked.
    await switcher.focus();
    await page.keyboard.press("Enter");
    await expect(menu).toHaveAccessibleName("Workflow view: Designer");
    await expect(switcher).toHaveAttribute("aria-expanded", "true");
    await expect(items).toHaveText(VIEWS);
    await expect(menu.getByRole("menuitem")).toHaveCount(0);
    for (const [index, checked] of [true, false, false, false].entries())
      await expect(items.nth(index)).toHaveAttribute(
        "aria-checked",
        String(checked),
      );
    await expect(items.nth(0)).toBeFocused();

    // Down twice and Enter: Outline shows, the menu closes, focus returns.
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowDown");
    await expect(items.nth(2)).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(menu).toHaveCount(0);
    await expect(switcher).toBeFocused();
    await expect(switcher).toHaveAccessibleName("Workflow view: Outline");
    await expect(switcher).toHaveText("Outline");
    await expect(page.locator("#editor-view")).toHaveAttribute(
      "aria-labelledby",
      "view-tab-outline",
    );

    // Opened again, the menu starts on Outline, not on the first view.
    await page.keyboard.press("ArrowDown");
    await expect(items.nth(2)).toBeFocused();
    await expect(items.nth(2)).toHaveAttribute("aria-checked", "true");
    await expect(items.nth(0)).toHaveAttribute("aria-checked", "false");
    // End and Home reach the last and first views; Escape changes nothing.
    await page.keyboard.press("End");
    await expect(items.nth(3)).toBeFocused();
    await page.keyboard.press("Home");
    await expect(items.nth(0)).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(menu).toHaveCount(0);
    await expect(switcher).toBeFocused();
    await expect(switcher).toHaveAccessibleName("Workflow view: Outline");

    // The view shown, picked again, only closes the menu.
    await page.keyboard.press("Enter");
    await expect(items.nth(2)).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(menu).toHaveCount(0);
    await expect(switcher).toBeFocused();
    await expect(switcher).toHaveAccessibleName("Workflow view: Outline");

    // Tab leaves and closes the menu.
    await page.keyboard.press("Enter");
    await expect(menu).toBeVisible();
    await page.keyboard.press("Tab");
    await expect(menu).toHaveCount(0);
    await expect(switcher).toHaveAttribute("aria-expanded", "false");
  });

  test("the menu's views are 44 px targets with one check on the view shown", async ({
    page,
  }) => {
    await openStepFixture(page);
    await viewSwitcher(page).click();
    const items = page.getByRole("menu").getByRole("menuitemradio");
    await expect(items).toHaveCount(4);
    const rows = await boxes(items);
    const labels = await boxes(items.locator("span"));
    for (const [index, item] of rows.entries()) {
      expect(item.height, `${VIEWS[index]} height`).toBeGreaterThan(43.5);
      // The labels start on one edge: the check has a column of its own.
      expect(Math.abs(labels[index].x - labels[0].x)).toBeLessThanOrEqual(0.5);
    }
    const checks = page.getByRole("menu").locator("weave-icon");
    await expect(checks).toHaveCount(1);
    await expect(items.nth(0).locator("weave-icon")).toHaveCount(1);
    const check = await box(checks);
    expect(check.width).toBe(16);
    expect(check.x).toBeGreaterThan(labels[0].right);
    expect(intersects(check, labels[0])).toBe(false);
  });

  test("a view change Source refuses keeps the switcher on Source", async ({
    page,
  }) => {
    await openStepFixture(page);
    await showView(page, "Source");
    const source = page.getByRole("textbox", { name: "Workflow source" });
    const typed = `${await source.inputValue()}\n  broken: [`;
    await source.fill(typed);
    await showView(page, "Designer");
    const question = page.getByRole("dialog", {
      name: "Apply the changes typed in Source?",
    });
    await question.getByRole("button", { name: "Keep editing" }).click();
    await expect(question).toHaveCount(0);
    await expect(viewSwitcher(page)).toHaveAccessibleName(
      "Workflow view: Source",
    );
    await expect(source).toHaveValue(typed);
  });

  test("the toolbar's More menu keeps its plain items", async ({ page }) => {
    await openStepFixture(page);
    const more = page
      .getByRole("toolbar", { name: "Workflow commands" })
      .getByRole("button", { name: "More", exact: true });
    await more.focus();
    await page.keyboard.press("Enter");
    const menu = page.getByRole("menu", { name: "More" });
    const items = menu.locator("button");
    await expect(items.first()).toBeFocused();
    const count = await items.count();
    expect(count).toBeGreaterThan(1);
    await expect(menu.getByRole("menuitem")).toHaveCount(count);
    await expect(menu.getByRole("menuitemradio")).toHaveCount(0);
    await expect(menu.locator("[aria-checked]")).toHaveCount(0);
    await expect(menu.locator("weave-icon")).toHaveCount(0);
    for (const item of await boxes(items)) expect(item.height).toBe(36);
  });
});

for (const size of [
  { width: 480, folds: true },
  { width: 481, folds: false },
])
  test.describe(`at ${size.width} px`, () => {
    test.use({ viewport: { width: size.width, height: 800 } });

    test(
      size.folds
        ? "the views fold into the switcher"
        : "the tabs stay, Runs with its icon",
      async ({ page }) => {
        await openStepFixture(page);
        if (size.folds) {
          await expect(tabs(page)).toBeHidden();
          await expect(viewSwitcher(page)).toBeVisible();
          return;
        }
        await expect(viewSwitcher(page)).toBeHidden();
        await expect(tabs(page).getByRole("tab")).toHaveText(VIEWS);
        await expect(page.locator("#view-tab-runs weave-icon")).toBeVisible();
      },
    );
  });
