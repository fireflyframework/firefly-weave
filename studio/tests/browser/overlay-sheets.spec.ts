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
// Side panels that cover the page are modal side sheets: focus moves in when
// they open, Tab stays inside, Escape or a click beside them closes them and
// returns focus to the control that opened them, and what they cover is
// inert. Where a panel sits beside the content it stays non-modal. Covers the
// inspector, the step palette popover and a list's detail panel; the
// simulation panel is covered in designer-simulation.spec.ts.
import { test, expect, Locator, Page } from "@playwright/test";
import { connected, insertStep, newWorkflow, offline } from "./support";
import { DesignerPage } from "./designer-po";

interface Size {
  tag: string;
  width: number;
  height: number;
  scale?: number;
  /** Whether the inspector and the palette cover the canvas here. */
  covers: boolean;
}
const sizes: Size[] = [
  { tag: "360x740", width: 360, height: 740, covers: true },
  { tag: "600x500", width: 600, height: 500, covers: true },
  // A 1280x720 window at 200% browser zoom.
  { tag: "1280x720-zoom200", width: 640, height: 360, scale: 2, covers: true },
  { tag: "1440x900", width: 1440, height: 900, covers: false },
];

/** What has focus: inside the element, or a description of where it is. */
const focusInside = (sheet: Locator) =>
  sheet.evaluate((element) => element.contains(document.activeElement));
/** Presses Tab (or Shift+Tab) and records whether focus stayed inside. */
async function tabStaysInside(page: Page, sheet: Locator, presses: number) {
  const outside: string[] = [];
  for (const key of ["Tab", "Shift+Tab"])
    for (let i = 0; i < presses; i++) {
      await page.keyboard.press(key);
      if (!(await focusInside(sheet)))
        outside.push(
          await page.evaluate(
            () =>
              `${document.activeElement?.tagName}.${document.activeElement?.className}`,
          ),
        );
    }
  return outside;
}
/** The elements outside the sheet that are not inert and can take focus. */
const reachableOutside = (sheet: Locator) =>
  sheet.evaluate((element) =>
    [
      ...document.querySelectorAll<HTMLElement>(
        "button, a[href], input, select, textarea, [tabindex]",
      ),
    ]
      .filter(
        (e) =>
          !element.contains(e) &&
          !e.closest("[inert]") &&
          e.tabIndex >= 0 &&
          e.getClientRects().length > 0 &&
          // Live regions and dialogs stay usable on purpose.
          !e.closest('[role="status"], [role="alert"], weave-modal'),
      )
      .map(
        (e) =>
          e.getAttribute("aria-label") || e.textContent?.trim() || e.tagName,
      ),
  );

for (const size of sizes)
  test.describe(size.tag, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      deviceScaleFactor: size.scale ?? 1,
    });

    test("the inspector is a modal sheet only where it covers the canvas", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      await insertStep(page, "Wait for time");
      const designer = new DesignerPage(page);
      const node = page.locator('[data-step="wait-1"] .node-body');
      if (size.covers) {
        // Inserting on a narrow layout leaves the canvas visible.
        await designer.fit();
        await node.click();
        const sheet = page.getByRole("dialog", { name: "Inspector" });
        await expect(sheet).toBeVisible();
        await expect(sheet).toHaveAttribute("aria-modal", "true");
        // Focus moved in, onto the panel's heading.
        await expect(
          sheet.getByRole("heading", { name: "Wait for time" }),
        ).toBeFocused();
        // The canvas, toolbar and navigation under it are inert (the
        // toolbar through the editor bar that holds it).
        for (const covered of [
          ".canvas-panel",
          ".editor-toolbar",
          ".sidebar",
          ".topbar",
        ])
          expect(
            await page
              .locator(covered)
              .evaluate((element) => !!element.closest("[inert]")),
            `${covered} is inert`,
          ).toBe(true);
        expect(await reachableOutside(sheet)).toEqual([]);
        expect(await tabStaysInside(page, sheet, 14)).toEqual([]);
        // Escape closes it and returns focus to the step.
        await page.keyboard.press("Escape");
        await expect(sheet).toBeHidden();
        await expect(node).toBeFocused();
        await expect(page.locator("[inert]")).toHaveCount(0);
        await expect(page.locator(".sheet-scrim")).toHaveCount(0);
        // A click beside the sheet closes it too, without reaching the page.
        await node.click();
        await expect(sheet).toBeVisible();
        await page.locator(".sheet-scrim").click({ position: { x: 4, y: 4 } });
        await expect(sheet).toBeHidden();
        await expect(page).toHaveURL(/\/designer$/);
        await expect(node).toBeFocused();
      } else {
        await node.click();
        const panel = page.getByRole("complementary", { name: "Inspector" });
        await expect(panel).toBeVisible();
        await expect(panel).not.toHaveAttribute("aria-modal", "true");
        await expect(page.locator("[inert]")).toHaveCount(0);
        await expect(page.locator(".sheet-scrim")).toHaveCount(0);
        // Focus stays on the step the person clicked.
        await expect(node).toBeFocused();
        // The canvas stays usable beside it.
        await page.getByRole("button", { name: "Fit all" }).click();
        await expect(panel).toBeVisible();
      }
    });

    test("the step palette popover is a modal sheet", async ({ page }) => {
      await offline(page);
      await newWorkflow(page);
      const insert = page.getByRole("button", { name: "Insert step" });
      if (!size.covers) {
        // Wide layouts dock the palette beside the canvas.
        await expect(insert).toBeHidden();
        const palette = page.getByRole("complementary", { name: "Steps" });
        await expect(palette).toBeVisible();
        await expect(palette).not.toHaveAttribute("aria-modal", "true");
        return;
      }
      await insert.click();
      const sheet = page.getByRole("dialog", { name: "Steps" });
      await expect(sheet).toBeVisible();
      await expect(
        sheet.getByRole("textbox", { name: "Search steps" }),
      ).toBeFocused();
      await expect(page.locator(".canvas-panel")).toHaveAttribute("inert", "");
      expect(await reachableOutside(sheet)).toEqual([]);
      expect(await tabStaysInside(page, sheet, 12)).toEqual([]);
      await page.keyboard.press("Escape");
      await expect(sheet).toBeHidden();
      await expect(insert).toBeFocused();
      // Choosing a step closes it and adds the step.
      await insert.click();
      await sheet
        .locator(".palette-step")
        .filter({ hasText: "Transform" })
        .click();
      await expect(sheet).toBeHidden();
      await expect(page.locator('[data-step="transform-1"]')).toHaveCount(1);
      await expect(page.locator("[inert]")).toHaveCount(0);
    });

    test("a run's detail is a modal sheet where it floats over the list", async ({
      page,
    }) => {
      const runs = ["104", "105"].map((n) => ({
        id: `00000000-0000-4000-8000-000000000${n}`,
        business_key: `expense-2026-${n}`,
        state: { status: "waiting", active: [] },
      }));
      await connected(page);
      await page.route("**/environments/development/runs?*", (r) =>
        r.fulfill({ json: { items: runs, next_cursor: null } }),
      );
      for (const run of runs)
        await page.route(`**/environments/development/runs/${run.id}`, (r) =>
          r.fulfill({ json: run }),
        );
      await page.route("**/runs/*/lifecycle", (r) =>
        r.fulfill({ json: { archived: false, purged: false, revision: 1 } }),
      );
      await page.route("**/runs/*/history?*", (r) =>
        r.fulfill({ json: { events: [], next_cursor: null } }),
      );
      await page.getByRole("button", { name: "Runs", exact: true }).click();
      // A row opens with its one button; focus returns to that button.
      const row = page.locator(".resource-row .row-open").first();
      await row.click();
      const heading = page.locator("#record-detail-title");
      await expect(heading).toBeVisible();
      const detail = page.locator(".record-detail");
      if (size.covers) {
        await expect(detail).toHaveAttribute("role", "dialog");
        await expect(detail).toHaveAttribute("aria-modal", "true");
        await expect(heading).toBeFocused();
        await expect(page.locator(".resource-table")).toHaveAttribute(
          "inert",
          "",
        );
        expect(await reachableOutside(detail)).toEqual([]);
        expect(await tabStaysInside(page, detail, 10)).toEqual([]);
        await page.keyboard.press("Escape");
        await expect(detail).toHaveCount(0);
        await expect(row).toBeFocused();
        await expect(page.locator("[inert]")).toHaveCount(0);
        // The close button returns focus the same way.
        await row.click();
        await page.getByRole("button", { name: "Close detail" }).click();
        await expect(detail).toHaveCount(0);
        await expect(row).toBeFocused();
      } else {
        // Beside the list: no dialog, the list stays usable.
        await expect(detail).not.toHaveAttribute("role", "dialog");
        await expect(row).toBeFocused();
        await expect(page.locator("[inert]")).toHaveCount(0);
        await page.locator(".resource-row").nth(1).click();
        await expect(heading).toContainText("expense-2026-105");
      }
    });
  });
