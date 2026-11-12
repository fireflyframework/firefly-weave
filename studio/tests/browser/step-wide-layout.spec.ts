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
// Table view of step details: while a decision table is open in the grid,
// Input, Parameters and Output stay side by side at widths of their own, a
// pane the person folds becomes a 48 px rail, and below 1280 px the Data
// pane sits beside the grid with Output first.
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

  test("Edit table keeps Input, Parameters and Output side by side", async ({
    page,
  }) => {
    const details = await openGrid(page);
    const [input, main, output] = [
      await box(details.dialog.locator(".sd-input")),
      await box(details.dialog.locator(".sd-main")),
      await box(details.dialog.locator(".sd-output")),
    ];
    expect(input.x + input.width).toBeLessThanOrEqual(main.x + 0.5);
    expect(main.x + main.width).toBeLessThanOrEqual(output.x + 0.5);
    await expect(details.dialog.locator(".sd-rail")).toHaveCount(0);
    await expect(details.dialog.locator(".sd-separator")).toHaveCount(2);
    await expect(
      details.dialog.locator(".sd-input weave-table-input-pane"),
    ).toBeVisible();
    await expect(
      details.dialog.locator(".sd-output weave-table-output-pane"),
    ).toBeVisible();
  });

  test("Fold folds a pane to its rail and the rail opens it again, by keyboard", async ({
    page,
  }) => {
    const details = await openGrid(page);
    const fold = details.dialog.getByRole("button", { name: "Fold Output" });
    await fold.focus();
    await page.keyboard.press("Enter");
    const rail = details.dialog.getByRole("button", { name: "Show Output" });
    await expect(rail).toBeFocused();
    expect(Math.abs((await box(rail)).width - 48)).toBeLessThanOrEqual(0.5);
    await expect(details.dialog.locator(".sd-output")).toBeHidden();
    await expect(details.dialog.locator(".sd-separator")).toHaveCount(1);
    const main = await box(details.dialog.locator(".sd-main"));
    expect(main.x + main.width).toBeLessThanOrEqual((await box(rail)).x + 0.5);
    await page.keyboard.press("Enter");
    await expect(details.dialog.locator(".sd-output")).toBeVisible();
    await expect(fold).toBeFocused();
    await expect(details.dialog.locator(".sd-rail")).toHaveCount(0);
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
    // Table view uses widths of its own.
    await expect(
      details.dialog.locator(".sd-input weave-table-input-pane"),
    ).toBeVisible();
    expect(await widthOf(details.dialog.locator(".sd-input"))).not.toBe(
      Number(chosen),
    );
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

  test("the Data pane starts folded, and opens beside the grid with Output first", async ({
    page,
  }) => {
    const details = await openGrid(page);
    const rail = details.dialog.getByRole("button", { name: "Show data" });
    expect(Math.abs((await box(rail)).width - 48)).toBeLessThanOrEqual(0.5);
    await expect(
      details.dialog.getByRole("tablist", { name: "Data" }),
    ).toHaveCount(0);
    await rail.click();
    const tabs = details.dialog.getByRole("tablist", { name: "Data" });
    await expect(tabs.getByRole("tab", { name: "Output" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const pane = await box(details.dialog.locator(".sd-output"));
    const strip = await box(tabs);
    const main = await box(details.dialog.locator(".sd-main"));
    expect(main.x + main.width).toBeLessThanOrEqual(pane.x + 0.5);
    // The tab strip sits inset by --space-2 from the pane's edges.
    expect(strip.x - pane.x).toBeCloseTo(8, 0);
    expect(pane.y).toBeGreaterThanOrEqual(strip.y + strip.height - 1);
    const panes = await box(details.dialog.locator(".sd-panes"));
    expect(pane.height).toBeGreaterThan(300);
    expect(pane.y + pane.height).toBeLessThanOrEqual(
      panes.y + panes.height + 0.5,
    );
    await tabs.getByRole("tab", { name: "Input" }).click();
    await expect(
      details.dialog.locator(".sd-input weave-table-input-pane"),
    ).toBeVisible();
    await expect(details.dialog.locator(".sd-output")).toBeHidden();
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
