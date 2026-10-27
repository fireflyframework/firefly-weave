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
// Formulas in the decision grid: a result mapped from the input, a rule's
// whole condition in the formula editor, and what a rule can't hold.
import { expect, test } from "@playwright/test";
import { sizes } from "./step-details-po";
import { openPaymentGrid, viewedTable } from "./decision-grid-po";
import { replaceFormula, suggestions } from "./formula-po";

for (const size of sizes)
  test.describe(`formulas in the grid at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a mapped result reads the input", async ({ page }) => {
      const { details, grid } = await openPaymentGrid(page);
      await grid.edit(0, 4, "= input.amount * 1");
      await expect(grid.cell(0, 4)).toHaveAttribute("aria-invalid", "true");
      await expect(grid.root.locator(".dg-problems li")).toHaveCount(1);
      await grid.cell(0, 4).focus();
      await page.keyboard.press("Escape");
      await grid.edit(0, 4, "= input.amount");
      await expect(grid.cell(0, 4)).toContainText("ƒinput.amount");
      await expect(grid.cell(0, 4)).not.toHaveAttribute("aria-invalid");
      await grid.back();
      const table = await viewedTable(page, details);
      expect(table.spec.rules[0].output).toEqual({
        object: {
          approve: { literal: true },
          limit: { ref: "/input/amount" },
        },
      });
    });

    test("a merged condition opens the formula editor and offers only the table's input", async ({
      page,
    }) => {
      const { grid } = await openPaymentGrid(page);
      await grid.cell(2, 1).click();
      await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog", {
        name: "Edit formula: Rule 3 condition",
      });
      await expect(dialog).toBeVisible();
      await replaceFormula(page, dialog, "input.");
      const list = suggestions(page);
      await expect(list.getByRole("option", { name: /amount/ })).toBeVisible();
      await expect(list.getByRole("option", { name: /tier/ })).toBeVisible();
      await expect(list.getByRole("option", { name: /steps/ })).toHaveCount(0);
      await page.keyboard.press("Escape");
      await replaceFormula(
        page,
        dialog,
        "input.amount > 7000 or exists(input.tier)",
      );
      await dialog.getByRole("button", { name: "Apply" }).click();
      await expect(dialog).toHaveCount(0);
      await expect(grid.cell(2, 1)).toContainText(
        "input.amount > 7000 or exists(input.tier)",
      );
      await expect(grid.cell(2, 1)).toBeFocused();
    });

    test("typing into a merged condition opens the formula editor and keeps the rule", async ({
      page,
    }) => {
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => {
        // A thrown edit reaches the console as Angular's error report.
        if (message.type() === "error" && /Error: /.test(message.text()))
          errors.push(message.text());
      });
      const { grid } = await openPaymentGrid(page);
      await grid.cell(2, 1).click();
      await page.keyboard.press("5");
      const dialog = page.getByRole("dialog", {
        name: "Edit formula: Rule 3 condition",
      });
      await expect(dialog).toBeVisible();
      await dialog.getByRole("button", { name: "Cancel" }).first().click();
      await expect(grid.cell(2, 1)).toContainText("input.amount > 5000");
      expect(errors).toEqual([]);
    });

    test("a text operator in a rule is explained", async ({ page }) => {
      const { grid } = await openPaymentGrid(page);
      await grid.cell(2, 1).click();
      await page.keyboard.press("Enter");
      const dialog = page.getByRole("dialog", {
        name: "Edit formula: Rule 3 condition",
      });
      await replaceFormula(page, dialog, 'concat(input.tier, "x") == "goldx"');
      await expect(dialog.getByRole("alert")).toHaveText(
        "Decision rules cannot use text operators yet.",
      );
      await expect(
        dialog.getByRole("button", { name: "Apply" }),
      ).toHaveAttribute("aria-disabled", "true");
      await dialog.getByRole("button", { name: "Cancel" }).first().click();
      await expect(dialog).toHaveCount(0);
      await expect(grid.cell(2, 1)).toContainText("input.amount > 5000");
    });
  });
