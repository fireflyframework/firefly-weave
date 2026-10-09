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
// The editor's app navigation: it opens expanded on wide screens, and the
// choice the person makes with its button is kept per viewer.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { openWorkflow, vendorPayment } from "./canvas-po";
import { offline } from "./support";

const NAV_KEY = "ui:weave.editor.navExpanded";
const navButton = (page: Page) =>
  page.getByRole("button", { name: /^(Collapse|Expand) navigation$/ });
const lockup = (page: Page) => page.locator("img.brand-lockup");
const sidebarWidth = async (page: Page) =>
  Math.round((await page.locator(".sidebar").boundingBox())!.width);
const stored = (page: Page) =>
  page.evaluate((key) => localStorage.getItem(key), NAV_KEY);

async function expectExpanded(page: Page) {
  await expect(
    page.getByRole("button", { name: "Collapse navigation", exact: true }),
  ).toHaveAttribute("aria-expanded", "true");
  await expect(lockup(page)).toBeVisible();
  expect(await sidebarWidth(page)).toBe(224);
}
async function expectCollapsed(page: Page) {
  await expect(
    page.getByRole("button", { name: "Expand navigation", exact: true }),
  ).toHaveAttribute("aria-expanded", "false");
  await expect(lockup(page)).toBeHidden();
  expect(await sidebarWidth(page)).toBe(64);
}
/** The whole control sits inside the window. */
async function expectInWindow(page: Page, control: Locator) {
  const box = (await control.boundingBox())!;
  const window = page.viewportSize()!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(window.width);
}
/** No sideways page scroll, and the canvas and the toolbar are whole. */
async function expectFits(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  const tiles = page.locator(".canvas-v2 .tile-node");
  await expect(tiles.first()).toBeVisible();
  expect(await tiles.count()).toBeGreaterThan(0);
  await expectInWindow(page, page.locator(".canvas-v2"));
  await expectInWindow(
    page,
    page.getByRole("button", { name: "Save to file", exact: true }),
  );
}

test.describe("the editor's navigation", () => {
  test("opens expanded at 1440 px, and a collapse is still there after a reload", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openWorkflow(page);
    await expectExpanded(page);
    expect(await stored(page)).toBeNull();

    await navButton(page).click();
    await expectCollapsed(page);
    expect(await stored(page)).toBe("false");

    // A reload and the workflow opened again: the choice wins over the width.
    await page.reload();
    await openWorkflow(page);
    await expectCollapsed(page);
  });

  test("opens as the rail at 1280 px, and an expand is still there after a reload", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1280, height: 720 });
    await openWorkflow(page);
    await expectCollapsed(page);

    await navButton(page).click();
    await expectExpanded(page);
    expect(await stored(page)).toBe("true");

    await page.reload();
    await openWorkflow(page);
    await expectExpanded(page);
  });

  test("follows the window width until the person chooses", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openWorkflow(page);
    await expectExpanded(page);
    await page.setViewportSize({ width: 1439, height: 900 });
    await expectCollapsed(page);
    await page.setViewportSize({ width: 1920, height: 1080 });
    await expectExpanded(page);

    // After the person chooses, the width no longer decides.
    await navButton(page).click();
    await expectCollapsed(page);
    await page.setViewportSize({ width: 1440, height: 900 });
    await expectCollapsed(page);
    await navButton(page).click();
    await expectExpanded(page);
    await page.setViewportSize({ width: 1366, height: 768 });
    await expectExpanded(page);
  });

  for (const [width, height, choose] of [
    [1280, 720, true],
    [1440, 900, false],
    [1920, 1080, false],
  ] as const)
    test(`never scrolls the page sideways with the navigation expanded and a step open at ${width}x${height}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height });
      const canvas = await openWorkflow(page);
      // At 1280 the navigation opens as the rail: the person expands it.
      if (choose) await navButton(page).click();
      await expectExpanded(page);
      await canvas.tileBody("prepare-request").click();
      await expect(
        page
          .getByRole("complementary", { name: "Inspector" })
          .or(page.getByRole("dialog", { name: "Inspector" })),
      ).toBeVisible();
      await expectFits(page);
    });

  test("keeps the rail and offers no toggle below 900 px, whatever was chosen", async ({
    page,
  }) => {
    await page.addInitScript(
      (key) => localStorage.setItem(key, "true"),
      NAV_KEY,
    );
    await page.setViewportSize({ width: 820, height: 1000 });
    await openWorkflow(page);
    // The rail is all that can show here: no button claims otherwise.
    expect(await sidebarWidth(page)).toBe(64);
    await expect(lockup(page)).toBeHidden();
    await expect(navButton(page)).toHaveCount(0);
    await expectFits(page);
    // The saved choice is kept for wider windows.
    expect(await stored(page)).toBe("true");
    await page.setViewportSize({ width: 900, height: 1000 });
    await expectExpanded(page);
    await expectFits(page);
    await page.setViewportSize({ width: 600, height: 700 });
    await expect(navButton(page)).toHaveCount(0);
    expect(await sidebarWidth(page)).toBeLessThanOrEqual(64);
    expect(await stored(page)).toBe("true");
  });

  test("keeps the toggle below 900 px in the other views", async ({ page }) => {
    await page.setViewportSize({ width: 820, height: 1000 });
    await offline(page);
    await expect(navButton(page)).toHaveCount(1);
  });

  test("opens the classic editor expanded at 1440 px too", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await offline(page);
    await page
      .getByLabel("Choose a workflow file")
      .setInputFiles(vendorPayment);
    await expect(page.locator(".editor-bar")).toBeVisible();
    await expect(page.locator(".canvas-v2")).toHaveCount(0);
    await expect(
      page.getByLabel("Workflow canvas", { exact: true }),
    ).toBeVisible();
    await expectExpanded(page);
    await navButton(page).click();
    await expectCollapsed(page);
    await page.reload();
    await offline(page);
    await page
      .getByLabel("Choose a workflow file")
      .setInputFiles(vendorPayment);
    await expect(page.locator(".editor-bar")).toBeVisible();
    await expectCollapsed(page);
  });

  test("leaves the other views to their own, unsaved choice", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await offline(page);
    // Home opens expanded, as before.
    await expectExpanded(page);
    await navButton(page).click();
    await expectCollapsed(page);
    expect(await stored(page)).toBeNull();
    // The editor decides for itself: its width default, not Home's choice.
    await page
      .getByLabel("Choose a workflow file")
      .setInputFiles(vendorPayment);
    await expect(page.locator(".editor-bar")).toBeVisible();
    await expectExpanded(page);
    // And Home keeps what the person chose there.
    await page.getByRole("button", { name: "Home", exact: true }).click();
    await expectCollapsed(page);
    expect(await stored(page)).toBeNull();
  });

  test("keeps working for the session, and says so once, when the browser refuses to save", async ({
    page,
  }) => {
    await page.addInitScript((key) => {
      const setItem = Storage.prototype.setItem;
      Storage.prototype.setItem = function (name: string, value: string) {
        if (name === key)
          throw new DOMException("Blocked", "QuotaExceededError");
        return setItem.call(this, name, value);
      };
    }, NAV_KEY);
    await page.setViewportSize({ width: 1440, height: 900 });
    await openWorkflow(page);
    await expectExpanded(page);
    const notice = page.getByText(
      "Studio couldn't keep this choice in this browser, so it lasts until Studio closes.",
    );
    await navButton(page).click();
    await expectCollapsed(page);
    await expect(notice).toHaveCount(1);
    await page.getByRole("button", { name: "Dismiss notification" }).click();
    await expect(notice).toHaveCount(0);
    // The width no longer decides, and no second notice appears.
    await page.setViewportSize({ width: 1920, height: 1080 });
    await expectCollapsed(page);
    await navButton(page).click();
    await expectExpanded(page);
    await expect(notice).toHaveCount(0);
  });
});
