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
// Selecting several steps on the left-to-right canvas: Shift and Ctrl or
// Command clicks, the selection box, the selection toolbar, and the
// minimap that shows while the view moves.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { openWorkflow, type CanvasPage } from "./canvas-po";

const MINIMAP_KEY = "ui:weave.canvas.minimap";

/** Draws a selection box from above and left of one step to the middle of another. */
async function boxSelect(canvas: CanvasPage, from: string, to: string) {
  const a = (await canvas.tileBody(from).boundingBox())!;
  const b = (await canvas.tileBody(to).boundingBox())!;
  await canvas.page.mouse.move(a.x - 16, a.y - 24);
  await canvas.page.mouse.down();
  await canvas.page.mouse.move(b.x + b.width - 8, b.y + b.height / 2, {
    steps: 12,
  });
  await expect(canvas.root.locator(".f-selection-area")).toBeVisible();
  await canvas.page.mouse.up();
}

/** Clicks the middle of a button the way a person does, also when it is aria-disabled. */
async function press(page: Page, button: Locator) {
  const box = (await button.boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
}

test.describe("selecting steps", () => {
  let pageErrors: string[] = [];
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
  });
  test.afterEach(() => {
    expect(pageErrors).toEqual([]);
  });

  test("Shift and Ctrl or Command add to the selection, and a click on empty canvas clears it", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("prepare-request").click();
    await canvas.tileBody("approval").click({ modifiers: ["Shift"] });
    await expect(canvas.tileBody("prepare-request")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(canvas.tileBody("approval")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const bar = canvas.root.getByRole("toolbar", { name: "Selected steps" });
    await expect(bar).toContainText("2 steps");
    await canvas.tileBody("approval").click({ modifiers: ["ControlOrMeta"] });
    await expect(canvas.tileBody("approval")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    await expect(bar).toHaveCount(0);
    const spot = await canvas.emptySpot();
    await page.mouse.click(spot.x, spot.y);
    await expect(canvas.tileBody("prepare-request")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  test("a box drawn on empty canvas selects the steps it touches; Duplicate copies that run and Delete removes it", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await boxSelect(canvas, "prepare-request", "approval");
    const bar = canvas.root.getByRole("toolbar", { name: "Selected steps" });
    await expect(bar).toContainText("2 steps");
    await bar.getByRole("button", { name: "Duplicate", exact: true }).click();
    expect(await canvas.rootSteps()).toEqual([
      "prepare-request",
      "approval",
      "prepare-request-1",
      "approval-1",
      "route",
      "record-result",
    ]);
    await canvas.tileBody("prepare-request-1").click();
    await canvas.tileBody("approval-1").click({ modifiers: ["Shift"] });
    await canvas.root
      .getByRole("toolbar", { name: "Selected steps" })
      .getByRole("button", { name: "Delete", exact: true })
      .click();
    await expect(page.locator(".toast")).toContainText("Deleted 2 steps.");
    expect(await canvas.rootSteps()).toEqual([
      "prepare-request",
      "approval",
      "route",
      "record-result",
    ]);
  });

  test("a box drawn with Shift adds just the steps it touches, and pressing a step's name changes nothing", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await boxSelect(canvas, "prepare-request", "approval");
    const bar = canvas.root.getByRole("toolbar", { name: "Selected steps" });
    await expect(bar).toContainText("2 steps");
    await canvas.tileBody("approval").click({ modifiers: ["ControlOrMeta"] });
    await expect(bar).toHaveCount(0);
    await page.keyboard.down("Shift");
    await boxSelect(canvas, "route", "route");
    await page.keyboard.up("Shift");
    await expect(bar).toContainText("2 steps");
    for (const [id, pressed] of [
      ["prepare-request", "true"],
      ["approval", "false"],
      ["route", "true"],
      ["pay-vendor", "true"],
    ])
      await expect(canvas.tileBody(id)).toHaveAttribute(
        "aria-pressed",
        pressed,
      );
    await canvas.tile("route").locator(".tile-label strong").click();
    await expect(bar).toContainText("2 steps");
  });

  test("a selection that isn't one run can't be duplicated, and says why", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("prepare-request").click();
    await canvas.tileBody("route").click({ modifiers: ["Shift"] });
    const duplicate = canvas.root
      .getByRole("toolbar", { name: "Selected steps" })
      .getByRole("button", { name: "Duplicate", exact: true });
    await expect(duplicate).toHaveAttribute("aria-disabled", "true");
    await expect(duplicate).toHaveAccessibleDescription(
      "Duplicate works on steps next to each other in one path. Select a single run of steps.",
    );
    // An aria-disabled button still takes a click and says why.
    await press(page, duplicate);
    await expect(page.locator(".toast")).toContainText(
      "Duplicate works on steps next to each other in one path.",
    );
  });

  test("selecting a decision selects every step inside it", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("route").click();
    for (const id of [
      "pay-vendor",
      "pay-and-notify",
      "post-ledger-entry",
      "rejected",
    ])
      await expect(canvas.tileBody(id)).toHaveAttribute("aria-pressed", "true");
    await expect(canvas.tileBody("record-result")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  test("Ctrl or Command and a click on a step inside a selected decision takes out just that step", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("route").click();
    await canvas.tileBody("pay-vendor").click({ modifiers: ["ControlOrMeta"] });
    for (const id of ["route", "pay-vendor"])
      await expect(canvas.tileBody(id)).toHaveAttribute(
        "aria-pressed",
        "false",
      );
    for (const id of [
      "pay-and-notify",
      "post-ledger-entry",
      "send-confirmation",
      "rejected",
    ])
      await expect(canvas.tileBody(id)).toHaveAttribute("aria-pressed", "true");
    // The decision gave way to the steps still selected inside it: the
    // parallel step (with its own steps) and rejected.
    const bar = canvas.root.getByRole("toolbar", { name: "Selected steps" });
    await expect(bar).toContainText("2 steps");
    await canvas.tileBody("pay-vendor").click({ modifiers: ["ControlOrMeta"] });
    await expect(canvas.tileBody("pay-vendor")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(canvas.tileBody("route")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    await expect(bar).toContainText("3 steps");
  });

  test("undo drops removed steps from the selection", async ({ page }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("prepare-request").click();
    await canvas.tileBody("approval").click({ modifiers: ["Shift"] });
    const bar = canvas.root.getByRole("toolbar", { name: "Selected steps" });
    await bar.getByRole("button", { name: "Duplicate", exact: true }).click();
    await canvas.tileBody("prepare-request-1").click();
    await canvas.tileBody("approval-1").click({ modifiers: ["Shift"] });
    await expect(bar).toContainText("2 steps");
    await page
      .getByRole("toolbar", { name: "Workflow commands" })
      .getByRole("button", { name: "Undo", exact: true })
      .click();
    await expect(canvas.tile("approval-1")).toHaveCount(0);
    await expect(bar).toHaveCount(0);
    await expect(canvas.root.locator('.tile-body[tabindex="0"]')).toHaveCount(
      1,
    );
  });

  test("while the source doesn't parse, the selection toolbar's commands are disabled and say why, and the box still selects", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await page.getByRole("tab", { name: "Source", exact: true }).click();
    const source = page.getByRole("textbox", { name: "Workflow source" });
    await source.fill(`${await source.inputValue()}\n  broken: [`);
    await page
      .getByRole("button", { name: "Apply changes", exact: true })
      .click();
    await page.getByRole("tab", { name: "Designer", exact: true }).click();
    await canvas.ready();
    await boxSelect(canvas, "prepare-request", "approval");
    const bar = canvas.root.getByRole("toolbar", { name: "Selected steps" });
    await expect(bar).toContainText("2 steps");
    for (const name of ["Duplicate", "Delete"]) {
      const button = bar.getByRole("button", { name, exact: true });
      await expect(button).toHaveAttribute("aria-disabled", "true");
      await expect(button).toHaveAccessibleDescription(
        "Fix the source before changing steps.",
      );
    }
    await press(page, bar.getByRole("button", { name: "Delete", exact: true }));
    await expect(page.locator(".toast")).toContainText(
      "Fix the source before changing steps.",
    );
    await expect(canvas.tile("prepare-request")).toHaveCount(1);
    await expect(canvas.tile("approval")).toHaveCount(1);
  });

  test("the minimap shows while the view moves, stays with Show minimap, and moves the view", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    const minimap = canvas.root.locator("f-minimap");
    await expect(minimap).toHaveCSS("opacity", "0");
    const area = (await canvas.root.boundingBox())!;
    await page.mouse.move(area.x + area.width / 2, area.y + area.height / 2);
    await page.mouse.wheel(0, 120);
    await expect(minimap).toHaveCSS("opacity", "1");
    await expect(minimap).toHaveCSS("opacity", "0", { timeout: 4000 });
    const toggle = canvas.root.getByRole("button", {
      name: "Show minimap",
      exact: true,
    });
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
    await expect(minimap).toHaveCSS("opacity", "1");
    expect(
      await page.evaluate((key) => localStorage.getItem(key), MINIMAP_KEY),
    ).toBe("true");
    const before = (await canvas.tileBody("prepare-request").boundingBox())!.x;
    const map = (await minimap.boundingBox())!;
    await page.mouse.click(map.x + map.width - 8, map.y + map.height / 2);
    await expect
      .poll(
        async () => (await canvas.tileBody("prepare-request").boundingBox())!.x,
      )
      .not.toBe(before);
  });

  test("keeps Show minimap for the session, and says so once, when the browser refuses to save it", async ({
    page,
  }) => {
    await page.addInitScript((key) => {
      const setItem = Storage.prototype.setItem;
      Storage.prototype.setItem = function (name: string, value: string) {
        if (name === key)
          throw new DOMException("Blocked", "QuotaExceededError");
        return setItem.call(this, name, value);
      };
    }, MINIMAP_KEY);
    const canvas = await openWorkflow(page);
    const minimap = canvas.root.locator("f-minimap");
    const toggle = canvas.root.getByRole("button", {
      name: "Show minimap",
      exact: true,
    });
    const notice = page.getByText(
      "Studio couldn't keep this choice in this browser, so it lasts until Studio closes.",
    );
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
    await expect(minimap).toHaveCSS("opacity", "1");
    await expect(notice).toHaveCount(1);
    await page.getByRole("button", { name: "Dismiss notification" }).click();
    await expect(notice).toHaveCount(0);
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-pressed", "false");
    await expect(minimap).toHaveCSS("opacity", "0");
    await expect(notice).toHaveCount(0);
  });

  test("hides the minimap below 768 px", async ({ page }) => {
    await page.addInitScript(
      (key) => localStorage.setItem(key, "true"),
      MINIMAP_KEY,
    );
    const canvas = await openWorkflow(page);
    const minimap = canvas.root.locator("f-minimap");
    // At 1440 px the minimap is there, kept open by Show minimap.
    await expect(minimap).toHaveCount(1);
    await expect(minimap).toBeVisible();
    await expect(minimap).toHaveCSS("opacity", "1");
    await page.setViewportSize({ width: 600, height: 500 });
    await expect(canvas.root).toBeVisible();
    await expect(minimap).toBeHidden();
  });

  test("keeps the minimap clear of the canvas tools at different window widths", async ({
    page,
  }) => {
    await page.addInitScript(
      (key) => localStorage.setItem(key, "true"),
      MINIMAP_KEY,
    );
    for (const width of [820, 1440]) {
      await page.setViewportSize({ width, height: 800 });
      const canvas = await openWorkflow(page);
      const minimap = (await canvas.root.locator("f-minimap").boundingBox())!;
      const tools = (await canvas.root
        .getByRole("toolbar", { name: "Canvas view" })
        .boundingBox())!;
      const apart =
        minimap.x + minimap.width <= tools.x ||
        minimap.y + minimap.height <= tools.y;
      expect(apart, `at ${width} px`).toBe(true);
    }
  });

  test("at 600x500 the selection toolbar sits at the bottom of the canvas, in view, without sideways scroll", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 600, height: 500 });
    const canvas = await openWorkflow(page);
    await boxSelect(canvas, "prepare-request", "approval");
    const bar = canvas.root.getByRole("toolbar", { name: "Selected steps" });
    await expect(bar).toContainText("2 steps");
    const area = (await canvas.root.boundingBox())!;
    const box = (await bar.boundingBox())!;
    expect(box.y).toBeGreaterThan(area.y + area.height / 2);
    expect(box.y + box.height).toBeLessThanOrEqual(area.y + area.height);
    expect(box.x).toBeGreaterThanOrEqual(area.x);
    expect(box.x + box.width).toBeLessThanOrEqual(area.x + area.width);
    for (const name of ["Duplicate", "Delete"])
      await expect(
        bar.getByRole("button", { name, exact: true }),
      ).toBeInViewport();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth,
      ),
    ).toBeLessThanOrEqual(0);
  });
});
