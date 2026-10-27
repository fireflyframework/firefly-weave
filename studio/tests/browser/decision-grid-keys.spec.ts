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
// The decision grid by keyboard: arrows, Tab, Enter, Escape, Alt+Enter,
// Alt+arrows, Ctrl/Cmd+D, pasting from a spreadsheet, and its own Undo.
import { expect, test } from "@playwright/test";
import { parse } from "yaml";
import { sourceText } from "./support";
import { sizes } from "./step-details-po";
import { openPaymentGrid, viewedTable } from "./decision-grid-po";

const ref = (name: string) => ({ ref: `/input/${name}` });
const op = (name: string, ...args: unknown[]) => ({ op: { name, args } });
const lit = (value: unknown) => ({ literal: value });

for (const size of sizes)
  test.describe(`the grid by keyboard at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("builds two rules without a pointer", async ({ page }) => {
      const { details, grid } = await openPaymentGrid(page);
      await grid.cell(1, 0).click();
      const keys = page.keyboard;
      // Rule 2: amount "> 50", tier "silver".
      await keys.press("ArrowRight");
      await expect(grid.cell(1, 1)).toBeFocused();
      await keys.type("> 50");
      await keys.press("Enter");
      await keys.press("Tab");
      await expect(grid.cell(1, 2)).toBeFocused();
      await keys.press("Enter");
      await keys.press("ControlOrMeta+a");
      await keys.type("silver");
      await keys.press("Enter");
      // A new rule below, named, then moved above rule 2 and duplicated.
      await keys.press("Alt+Enter");
      await expect(grid.cell(2, 0)).toBeFocused();
      await keys.press("F2");
      await keys.press("ControlOrMeta+a");
      await keys.type("tiny");
      await keys.press("Enter");
      await keys.press("ArrowRight");
      await keys.type("< 10");
      await keys.press("Enter");
      await keys.press("Alt+ArrowUp");
      await expect(grid.cell(1, 1)).toBeFocused();
      await keys.press("ControlOrMeta+d");
      await expect(grid.cell(2, 1)).toBeFocused();
      await grid.back();
      const table = await viewedTable(page, details);
      expect(table.spec.rules.map((rule) => rule.id)).toEqual([
        "big-gold",
        "tiny",
        "tiny-2",
        "small",
        "large-or-platinum",
      ]);
      expect(table.spec.rules[1].when).toEqual(
        op("lt", ref("amount"), lit(10)),
      );
      expect(table.spec.rules[3].when).toEqual(
        op(
          "and",
          op("gt", ref("amount"), lit(50)),
          op("eq", ref("tier"), lit("silver")),
        ),
      );
    });

    test("Ctrl/Cmd+Enter does nothing in the grid", async ({ page }) => {
      const executed: string[] = [];
      page.on("request", (request) => {
        if (request.url().includes("/studio/local/debug/execute"))
          executed.push(request.url());
      });
      const { grid } = await openPaymentGrid(page);
      await grid.cell(0, 1).click();
      await page.keyboard.press("ControlOrMeta+Enter");
      await grid.cell(0, 1).click();
      await page.keyboard.press("Enter");
      await page.keyboard.press("ControlOrMeta+Enter");
      await page.keyboard.press("Escape");
      await page.waitForTimeout(300);
      expect(executed).toEqual([]);
      await expect(page.getByRole("region", { name: "Run log" })).toHaveCount(
        0,
      );
      await expect(grid.grid).toBeVisible();
    });

    test("Escape cancels the edit first", async ({ page }) => {
      const { details, grid } = await openPaymentGrid(page);
      await grid.cell(0, 1).click();
      await page.keyboard.type("> 99");
      await page.keyboard.press("Escape");
      await expect(grid.cell(0, 1)).toHaveText("> 1000");
      await expect(grid.cell(0, 1)).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(
        grid.root.getByRole("button", { name: "Back to the step" }),
      ).toBeFocused();
      await expect(details.dialog).toBeVisible();
      await expect(grid.root).toContainText("payment-policy 1.2.0 · published");
    });

    test("Tab leaves the grid from its last cell", async ({ page }) => {
      const { grid } = await openPaymentGrid(page);
      await grid.cell(3, 4).click();
      await page.keyboard.press("Tab");
      await expect(grid.root.locator(".dg-add-rule")).toBeFocused();
      await grid.cell(0, 0).click();
      await page.keyboard.press("Shift+Tab");
      await expect(
        grid.root.getByRole("button", { name: "Add result column" }),
      ).toBeFocused();
    });

    test("pasting cells from a spreadsheet fills a block and Undo removes it in one step", async ({
      page,
    }) => {
      const { grid } = await openPaymentGrid(page);
      await grid.cell(1, 1).click();
      await grid.cell(1, 1).evaluate((cell) => {
        const data = new DataTransfer();
        data.setData(
          "text/plain",
          "> 5\tgold\ttrue\t10\r\n< 2\tsilver\tfalse\t20\r\n>= 7\tbronze\ttrue\t30\r\n",
        );
        cell.dispatchEvent(
          new ClipboardEvent("paste", { clipboardData: data, bubbles: true }),
        );
      });
      await expect(grid.cell(3, 1)).toHaveText(">= 7");
      await expect(grid.cell(4, 0)).toHaveText("Otherwise (no rule matches)");
      await expect(grid.cell(2, 2)).toContainText("silver");
      await expect(grid.cell(3, 4)).toHaveText("30");
      await grid.cell(1, 1).focus();
      await page.keyboard.press("ControlOrMeta+z");
      await expect(grid.cell(1, 1)).toHaveText("<= 1000");
      await expect(grid.cell(3, 0)).toHaveText("Otherwise (no rule matches)");
      await expect(grid.cell(2, 1)).toContainText("input.amount > 5000");
    });

    test("Ctrl/Cmd+Z in the grid undoes the last cell, not the workflow", async ({
      page,
    }) => {
      const { details, grid } = await openPaymentGrid(page);
      await grid.edit(0, 1, "> 10");
      await grid.edit(1, 1, "<= 20");
      await expect(grid.root).toContainText("1.2.1 · on this computer");
      await grid.cell(1, 1).focus();
      await page.keyboard.press("ControlOrMeta+z");
      await expect(grid.cell(1, 1)).toHaveText("<= 1000");
      await expect(grid.cell(0, 1)).toHaveText("> 10");
      await expect(grid.root).toContainText("1.2.1 · on this computer");
      await grid.back();
      await details.close();
      expect(parse(await sourceText(page)).spec.steps[0].uses).toBe(
        "payment-policy@1.2.1",
      );
    });
  });
