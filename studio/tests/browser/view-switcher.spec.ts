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
// At 767 px and below the four view tabs fold into one view switcher on the
// workflow name's line, on every view: a 44 px menu button that names the
// view shown and opens the views as radio items. The name keeps its room,
// the settings gear its 44 px target, and the bar its height; the name, the
// gear, the status and the switcher share one center line. From 768 px up
// the tabs stay.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { useNewEditor } from "./canvas-po";
import { DesignerPage } from "./designer-po";
import {
  openStepFixture,
  settingsGear,
  stepFixture,
  yamlFile,
} from "./step-details-po";
import {
  box,
  boxes,
  expectAligned,
  intersects,
  textBox,
} from "./step-geometry";
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
/** Longer than any room the bar has, so the name fills the room it gets. */
const LONG_NAME =
  "order-intake-for-every-region-with-approvals-and-escalations";

/**
 * The step fixture named `name`, opened offline from a file or as a platform
 * draft, on the Designer.
 */
async function openDraft(page: Page, draft: Draft, name = LONG_NAME) {
  const text = readFileSync(stepFixture, "utf8").replace(
    "{name: order-intake,",
    `{name: ${name},`,
  );
  if (draft === "offline") {
    await openStepFixture(page, yamlFile(`${name}.yaml`, text));
    return;
  }
  await useNewEditor(page);
  await connected(page, { capabilities: [...allCapabilities, "compile"] });
  await newWorkflow(page);
  await new DesignerPage(page).setSource(text);
  // A platform draft shows its status on the name's line.
  await expect(page.locator(".editor-identity .status-chip")).toBeVisible();
}

const tabs = (page: Page) =>
  page.getByRole("tablist", { name: "Workflow views" });
const workflowName = (page: Page) => page.locator(".editor-identity h1");
const status = (page: Page) => page.locator(".editor-identity .status-chip");

/** Whether the element itself is what a tap at its center reaches. */
const atOwnCenter = (locator: Locator) =>
  locator.evaluate((element) => {
    const r = element.getBoundingClientRect();
    const hit = document.elementFromPoint(
      r.x + r.width / 2,
      r.y + r.height / 2,
    );
    return !!hit && (hit === element || element.contains(hit));
  });

/** How many lines the lifecycle (Draft → Published → Active) takes. */
const lifecycleLines = (page: Page) =>
  page.locator(".editor-heading .workflow-progress").evaluate((list) => {
    const range = document.createRange();
    range.selectNodeContents(list);
    return new Set(
      [...range.getClientRects()]
        .filter((r) => r.width > 0 && r.height > 0)
        .map((r) => Math.round(r.top + r.height / 2)),
    ).size;
  });

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

      test("the view switcher shares the name's line on every view; the gear and the name keep their room, the bar its height", async ({
        page,
      }) => {
        await openDraft(page, draft);
        await expect(tabs(page)).toBeHidden();
        const commandBars: number[] = [];
        const switcherTops: number[] = [];
        for (const view of VIEWS) {
          await showView(page, view);
          const switcher = viewSwitcher(page);
          await expect(switcher).toHaveAccessibleName(`Workflow view: ${view}`);
          await expect(switcher).toHaveText(view);
          const bar = await box(page.locator(".editor-bar"));
          const heading = await box(page.locator(".editor-heading"));
          const name = await box(workflowName(page));
          const toggle = await box(switcher);
          switcherTops.push(toggle.y);

          // A whole target, on the name's line, inside the bar's padding.
          expect(toggle.height, `${view}: switcher height`).toBeGreaterThan(
            43.5,
          );
          expect(toggle.width, `${view}: switcher width`).toBeGreaterThan(95.5);
          expect(name.middle).toBeGreaterThan(toggle.y);
          expect(name.middle).toBeLessThan(toggle.bottom);
          expect(toggle.right).toBeLessThanOrEqual(bar.right - 11.5);
          if (draft === "connected")
            expectAligned(
              [name.middle, (await box(status(page))).middle],
              `${view}: name and status`,
            );
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
          // One center line: the name, the gear and the switcher.
          expectAligned(
            [name.middle, gear.middle, toggle.middle],
            `${view}: name, gear and switcher`,
          );
          commandBars.push(bar.height);
        }
        // Designer, Source and Outline: the bar as tall as with the tabs.
        for (const height of commandBars)
          expect(Math.abs(height - layout.bar)).toBeLessThanOrEqual(1);
        // The switcher stays put when the view changes, Runs included.
        expectAligned(switcherTops, "switcher top on the four views", 0.5);
        expect(
          await page.evaluate(
            () => document.scrollingElement!.scrollWidth === innerWidth,
          ),
        ).toBe(true);
      });
    });

// From 481 to 767 px the version, the full status and the whole lifecycle
// line show beside the switcher.
for (const width of [481, 600, 767])
  for (const draft of ["offline", "connected"] as const)
    test.describe(`${draft} draft at ${width}x800`, () => {
      test.use({ viewport: { width, height: 800 } });

      test("the view switcher stands for the tabs; the name, the gear and the lifecycle line keep their room", async ({
        page,
      }) => {
        await openDraft(page, draft);
        await expect(tabs(page)).toBeHidden();
        const switcher = viewSwitcher(page);
        await expect(switcher).toHaveAccessibleName("Workflow view: Designer");
        const name = await box(workflowName(page));
        const gear = settingsGear(page);
        const gearBox = await box(gear);
        const toggle = await box(switcher);
        expect(name.width, "the name's room").toBeGreaterThanOrEqual(80);
        expect([gearBox.width, gearBox.height], "gear").toEqual([44, 44]);
        // Nothing covers the gear: a tap at its center reaches it.
        expect(await atOwnCenter(gear), "gear at its own center").toBe(true);
        expect(await lifecycleLines(page), "lifecycle lines").toBe(1);
        const line = [name.middle, gearBox.middle, toggle.middle];
        if (draft === "connected") line.push((await box(status(page))).middle);
        expectAligned(line, "name, gear, status and switcher");
        expect(
          await page.evaluate(
            () => document.scrollingElement!.scrollWidth === innerWidth,
          ),
        ).toBe(true);
      });
    });

// A short name keeps what follows it (the version from 481 px, the gear,
// the status) beside it; the room left over lies before the switcher.
for (const width of [390, 600])
  for (const draft of ["offline", "connected"] as const)
    test.describe(`a short name, ${draft}, at ${width}x800`, () => {
      test.use({ viewport: { width, height: 800 } });

      test("the gear and the status follow the name", async ({ page }) => {
        await openDraft(page, draft, "order-intake");
        // The name is drawn whole.
        expect(
          await workflowName(page).evaluate(
            (h1) => h1.scrollWidth <= h1.clientWidth,
          ),
        ).toBe(true);
        // From where the name's text ends, each item follows at 8 px.
        let previous = await textBox(workflowName(page));
        const after = await boxes(page.locator(".editor-identity > h1 ~ *"));
        // The version shows from 481 px, the status on a platform draft.
        expect(after.length).toBe(
          1 + (width > 480 ? 1 : 0) + (draft === "connected" ? 1 : 0),
        );
        const gear = await box(settingsGear(page));
        expect(after.some((item) => Math.abs(item.x - gear.x) < 0.5)).toBe(
          true,
        );
        for (const [index, item] of after.entries()) {
          expectAligned([item.x - previous.right, 8], `item ${index + 1}`);
          previous = item;
        }
        const toggle = await box(viewSwitcher(page));
        expect(toggle.x - previous.right).toBeGreaterThan(8);
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
});

// A menu that sets no checked state looks and works as before: its toggle's
// 20 px icon, plain items, and focus on the first item when it opens.
test.describe("the toolbar's More menu at 600x800", () => {
  test.use({ viewport: { width: 600, height: 800 } });

  test("keeps its 20 px icon and its plain items", async ({ page }) => {
    await openStepFixture(page);
    const more = page
      .getByRole("toolbar", { name: "Workflow commands" })
      .getByRole("button", { name: "More", exact: true });
    const icon = await box(more.locator("weave-icon"));
    expect([icon.width, icon.height]).toEqual([20, 20]);
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
  { width: 767, folds: true },
  { width: 768, folds: false },
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
