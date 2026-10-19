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
// Edit formula: the dialog with the input's fields, the formula and its
// Result; drops into it; Escape; conditions edited as a formula.
import { expect, test } from "@playwright/test";
import {
  formulaBox,
  formulaText,
  formulasFixture,
  replaceFormula,
  setTestInput,
  suggestions,
} from "./formula-po";
import {
  openStepFixture,
  sizes,
  withLanguageFeatures,
} from "./step-details-po";

for (const size of sizes)
  test.describe(`Edit formula at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test.beforeEach(async ({ page }) => {
      await withLanguageFeatures(page);
    });

    test("⤢ opens the formula with the input's fields beside it and its Result", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await setTestInput(page, details, {
        "Customer ID": "c-1042",
        Amount: "1500",
      });
      await details.openWithKeyboard("flag");
      await details
        .field("value")
        .getByRole("button", { name: "Edit formula" })
        .click();
      const dialog = page.getByRole("dialog", { name: "Edit formula: Fields" });
      await expect(dialog).toBeVisible();
      await expect(
        dialog.getByRole("textbox", { name: "Search fields of earlier steps" }),
      ).toBeVisible();
      await expect(formulaBox(dialog)).toHaveAttribute(
        "data-text",
        "input.amount > 1000",
      );
      await expect(
        dialog.getByRole("region", { name: "Result of Fields" }),
      ).toContainText("true");
      await replaceFormula(page, dialog, "input.amount > 2000");
      await expect(
        dialog.getByRole("region", { name: "Result of Fields" }),
      ).toContainText("false");
      await dialog.getByRole("button", { name: "Done" }).last().click();
      await expect(dialog).toHaveCount(0);
      await expect(formulaBox(details.field("value"))).toHaveAttribute(
        "data-text",
        "input.amount > 2000",
      );
      await expect(
        details.field("value").getByRole("button", { name: "Edit formula" }),
      ).toBeFocused();
    });

    test("Edit formula says why a formula doesn't apply, in its Result", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      await details
        .field("value")
        .getByRole("button", { name: "Edit formula" })
        .click();
      const dialog = page.getByRole("dialog", { name: "Edit formula: Fields" });
      await replaceFormula(page, dialog, "input.amount >");
      // The dialog has no line under the formula, so the Result says it, once.
      await expect(
        dialog.getByText("The formula ends too early. Add a value after `>`."),
      ).toHaveCount(1);
      await expect(
        dialog.getByRole("region", { name: "Result of Fields" }),
      ).toContainText("The formula ends too early. Add a value after `>`.");
    });

    test("the field menu's Edit formula opens the same dialog", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      await details
        .field("value")
        .getByRole("button", { name: "Field menu for Fields" })
        .click();
      await page.getByRole("menuitem", { name: /Edit formula/ }).click();
      await expect(
        page.getByRole("dialog", { name: "Edit formula: Fields" }),
      ).toBeVisible();
    });

    test("a field dragged from the dialog's Input column lands in its formula", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      await details
        .field("value")
        .getByRole("button", { name: "Edit formula" })
        .click();
      const dialog = page.getByRole("dialog", { name: "Edit formula: Fields" });
      await replaceFormula(page, dialog, "");
      await dialog
        .getByRole("combobox", { name: "Input source" })
        .selectOption("input");
      await dialog
        .getByRole("listitem", { name: /^amount, / })
        .first()
        .dragTo(formulaText(dialog));
      await expect(formulaBox(dialog)).toHaveAttribute(
        "data-text",
        "input.amount",
      );
    });

    test("suggestions in Edit formula are drawn above the dialog in every row", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      await details
        .field("value")
        .getByRole("button", { name: "Edit formula" })
        .click();
      const dialog = page.getByRole("dialog", { name: "Edit formula: Fields" });
      await replaceFormula(page, dialog, "input.");
      const list = suggestions(page);
      await expect(list).toBeVisible();
      // The list's layer stacks above the dialog's backdrop, and every row
      // that shows is what the pointer hits.
      await expect
        .poll(() =>
          list.evaluate((element) => {
            const rows = [
              ...element.querySelectorAll<HTMLElement>('[role="option"]'),
            ].filter((row) => {
              const box = row.getBoundingClientRect();
              const frame = element.getBoundingClientRect();
              return box.top >= frame.top && box.bottom <= frame.bottom;
            });
            return (
              rows.length > 0 &&
              rows.every((row) => {
                const box = row.getBoundingClientRect();
                return (
                  document
                    .elementFromPoint(
                      box.x + box.width / 2,
                      box.y + box.height / 2,
                    )
                    ?.closest('[role="option"]') === row
                );
              })
            );
          }),
        )
        .toBe(true);
      // It stacks above every layer Studio draws in <body> over the page (the
      // side sheets' scrim and panel, and the dialogs' backdrop) and below
      // the skip link, which must stay reachable over everything.
      const layers = await list.evaluate((element) => {
        const probe = document.createElement("div");
        probe.style.visibility = "hidden";
        document.body.append(probe);
        const zOf = (name: string) => {
          const layer = document.createElement("div");
          layer.className = name;
          probe.append(layer);
          return Number(getComputedStyle(layer).zIndex);
        };
        const skip = zOf("skip-link");
        const others = ["sheet-scrim", "sheet-modal", "modal-backdrop"].map(
          (name) => {
            const layer = document.createElement("div");
            layer.className = name;
            probe.append(layer);
            return Number(getComputedStyle(layer).zIndex);
          },
        );
        probe.remove();
        return {
          list: Number(getComputedStyle(element).zIndex),
          inBody: element.closest("body > .formula-suggestions") !== null,
          others,
          skip,
        };
      });
      expect(layers.inBody).toBe(true);
      for (const other of layers.others)
        expect(layers.list).toBeGreaterThan(other);
      expect(layers.list).toBeLessThan(layers.skip);
    });

    test("Escape in Edit formula's search clears it, then closes the dialog", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      await details
        .field("value")
        .getByRole("button", { name: "Edit formula" })
        .click();
      const dialog = page.getByRole("dialog", { name: "Edit formula: Fields" });
      const search = dialog.getByRole("textbox", {
        name: "Search fields of earlier steps",
      });
      await search.fill("cust");
      await expect(dialog.locator(".data-tree > li")).toHaveCount(1);
      await page.keyboard.press("Escape");
      await expect(search).toHaveValue("");
      await expect(search).toBeFocused();
      await expect(dialog).toBeVisible();
      await expect(dialog.locator(".data-tree > li")).toHaveCount(2);
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await expect(details.dialog).toBeVisible();
    });

    test("Escape in Edit formula closes the list, then the dialog", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      await details
        .field("value")
        .getByRole("button", { name: "Edit formula" })
        .click();
      const dialog = page.getByRole("dialog", { name: "Edit formula: Fields" });
      await replaceFormula(page, dialog, "input.");
      await expect(suggestions(page)).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(suggestions(page)).toHaveCount(0);
      await expect(dialog).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await expect(details.dialog).toBeVisible();
    });

    test("a decision's conditions open as a formula and go back to rows", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("route");
      const when = details.dialog.locator('[data-param="case.when"]').first();
      await when
        .getByRole("button", { name: "Field menu for Runs when" })
        .click();
      await page.getByRole("menuitem", { name: /Edit as formula/ }).click();
      await expect(formulaBox(when)).toHaveAttribute(
        "data-text",
        "input.amount > 1000 or not exists(input.email)",
      );
      await expect(formulaText(when)).toBeFocused();
      await when
        .getByRole("button", { name: "Field menu for Runs when" })
        .click();
      await page.getByRole("menuitem", { name: /Edit as rows/ }).click();
      await expect(formulaBox(when)).toHaveCount(0);
    });
  });
