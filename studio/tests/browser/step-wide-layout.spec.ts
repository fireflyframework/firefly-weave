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
// The wide layout of step details: while a decision table is open in the
// grid, Input and Output fold to 48 px rails that open over the grid's edge.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { connected } from "./support";
import {
  openStepFixture,
  stepFixture,
  withLanguageFeatures,
  type StepDetailsPage,
} from "./step-details-po";
import { routeTables } from "./decision-grid-po";

async function openGrid(page: Page): Promise<StepDetailsPage> {
  const details = await openStepFixture(page, stepFixture, async (p) => {
    const result = await connected(p);
    await withLanguageFeatures(p);
    return result;
  });
  await routeTables(page);
  await details.open("score");
  const edit = details
    .field("table")
    .getByRole("button", { name: "Edit table" });
  await expect(edit).not.toHaveAttribute("aria-disabled");
  await edit.click();
  await expect(
    details.dialog.getByRole("button", { name: "Back to the step" }),
  ).toBeVisible();
  return details;
}
const box = async (locator: Locator) => (await locator.boundingBox())!;
/** A width while it may still be drawn: -1 until it is. */
const widthOf = async (locator: Locator) =>
  Math.round((await locator.boundingBox())?.width ?? -1);

test.describe("at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Edit table folds Input and Output to rails", async ({ page }) => {
    const details = await openGrid(page);
    const input = details.dialog.getByRole("button", {
      name: "Show input data",
    });
    const output = details.dialog.getByRole("button", {
      name: "Show output data",
    });
    const main = details.dialog.locator(".sd-main");
    const [left, right, middle] = [
      await box(input),
      await box(output),
      await box(main),
    ];
    expect(Math.abs(left.width - 48)).toBeLessThanOrEqual(0.5);
    expect(Math.abs(right.width - 48)).toBeLessThanOrEqual(0.5);
    expect(left.x + left.width).toBeLessThanOrEqual(middle.x + 0.5);
    expect(middle.x + middle.width).toBeLessThanOrEqual(right.x + 0.5);
    await expect(input).toHaveAttribute("aria-expanded", "false");
    await expect(details.dialog.locator(".sd-input")).toBeHidden();
    await expect(details.dialog.locator(".sd-output")).toBeHidden();
    await expect(details.dialog.locator(".sd-separator")).toHaveCount(0);
  });

  test("a rail opens its pane over the grid and Escape closes it", async ({
    page,
  }) => {
    const details = await openGrid(page);
    const rail = details.dialog.getByRole("button", {
      name: "Show input data",
    });
    const before = await box(details.dialog.locator(".sd-main"));
    await rail.click();
    await expect(rail).toHaveAttribute("aria-expanded", "true");
    const pane = details.dialog.locator(".sd-input");
    await expect(pane).toBeVisible();
    const over = await box(pane);
    const railBox = await box(rail);
    expect(Math.abs(over.x - (railBox.x + railBox.width))).toBeLessThanOrEqual(
      1,
    );
    expect(over.width).toBeGreaterThanOrEqual(240);
    expect(Math.abs(over.y - railBox.y)).toBeLessThanOrEqual(1);
    expect(Math.abs(over.height - railBox.height)).toBeLessThanOrEqual(1);
    // The grid keeps its width under the pane.
    const after = await box(details.dialog.locator(".sd-main"));
    expect(after.width).toBeCloseTo(before.width, 0);
    await page.keyboard.press("Escape");
    await expect(pane).toBeHidden();
    await expect(rail).toBeFocused();
    await expect(details.dialog).toBeVisible();
  });

  test("Back to the step restores the three panes at the person's widths", async ({
    page,
  }) => {
    const details = await openStepFixture(page, stepFixture, async (p) => {
      const result = await connected(p);
      await withLanguageFeatures(p);
      return result;
    });
    await routeTables(page);
    await details.open("score");
    const separator = details.dialog.getByRole("separator", {
      name: "Resize Input",
    });
    await separator.focus();
    const initial = (await separator.getAttribute("aria-valuenow"))!;
    for (let i = 0; i < 3; i++) await page.keyboard.press("ArrowRight");
    await expect(separator).toHaveAttribute(
      "aria-valuenow",
      String(Number(initial) + 48),
    );
    const chosen = await separator.getAttribute("aria-valuenow");
    await expect
      .poll(async () => widthOf(details.dialog.locator(".sd-input")))
      .toBe(Number(chosen));
    await details
      .field("table")
      .getByRole("button", { name: "Edit table" })
      .click();
    const rail = details.dialog.getByRole("button", {
      name: "Show input data",
    });
    await expect(rail).toBeVisible();
    // A rail opens its pane at the width the person chose.
    await rail.click();
    await expect
      .poll(async () => widthOf(details.dialog.locator(".sd-input")))
      .toBe(Number(chosen));
    await rail.click();
    await details.dialog
      .getByRole("button", { name: "Back to the step" })
      .click();
    await expect(details.field("table")).toBeVisible();
    await expect(details.dialog.locator(".sd-separator")).toHaveCount(2);
    await expect(separator).toHaveAttribute("aria-valuenow", chosen!);
    await expect
      .poll(async () => widthOf(details.dialog.locator(".sd-input")))
      .toBe(Number(chosen));
  });
});

test.describe("at 1024x768", () => {
  test.use({ viewport: { width: 1024, height: 768 } });

  test("the data pane folds to one rail", async ({ page }) => {
    const details = await openGrid(page);
    const rail = details.dialog.getByRole("button", { name: "Show data" });
    expect(Math.abs((await box(rail)).width - 48)).toBeLessThanOrEqual(0.5);
    await expect(
      details.dialog.getByRole("button", { name: "Show input data" }),
    ).toHaveCount(0);
    await expect(
      details.dialog.getByRole("tablist", { name: "Data" }),
    ).toHaveCount(0);
    await rail.click();
    await expect(
      details.dialog.getByRole("tablist", { name: "Data" }),
    ).toBeVisible();
    await expect(details.dialog.locator(".sd-input")).toBeVisible();
    const tabs = await box(
      details.dialog.getByRole("tablist", { name: "Data" }),
    );
    const pane = await box(details.dialog.locator(".sd-input"));
    expect(Math.abs(tabs.x - pane.x)).toBeLessThanOrEqual(1);
    expect(pane.y).toBeGreaterThanOrEqual(tabs.y + tabs.height - 1);
    expect(Math.abs(pane.y - (tabs.y + tabs.height))).toBeLessThanOrEqual(1);
    const panes = await box(details.dialog.locator(".sd-panes"));
    expect(pane.height).toBeGreaterThan(300);
    expect(pane.y + pane.height).toBeLessThanOrEqual(
      panes.y + panes.height + 0.5,
    );
    await page.keyboard.press("Escape");
    await expect(details.dialog.locator(".sd-input")).toBeHidden();
    await expect(rail).toBeFocused();
  });
});

test.describe("at 600x500", () => {
  test.use({ viewport: { width: 600, height: 500 } });

  test("the segmented control stays and the grid fills the body", async ({
    page,
  }) => {
    const details = await openGrid(page);
    await expect(
      details.dialog.getByRole("tablist", { name: "Step details panes" }),
    ).toBeVisible();
    await expect(details.dialog.locator(".sd-rail")).toHaveCount(0);
    const grid = await box(details.dialog.locator(".dg"));
    const main = await box(details.dialog.locator(".sd-main"));
    expect(grid.width).toBeGreaterThan(main.width - 2 * 24);
  });
});
