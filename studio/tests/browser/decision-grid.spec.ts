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
// The decision table grid: a published table opens as cells, the first
// change starts its next version on this computer, cells write the rule's
// condition, and what the cells can't show stays as it was.
import { expect, test, type Page } from "@playwright/test";
import { parse } from "yaml";
import { connected, sourceText } from "./support";
import {
  openStepFixture,
  sizes,
  withLanguageFeatures,
  yamlFile,
  type StepDetailsPage,
} from "./step-details-po";
import {
  DecisionGridPage,
  paymentPolicy,
  routeTables,
} from "./decision-grid-po";

const orders = yamlFile(
  "orders.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: { name: orders, version: 1.0.0 }
spec:
  inputSchema:
    type: object
    properties:
      amount: { type: number }
      tier: { type: string }
  outputSchema: { type: object }
  steps:
    - id: score
      kind: decisionTable
      uses: payment-policy@1.2.0
      with: { literal: {} }
  output: { literal: {} }
`,
);

async function openGrid(
  page: Page,
): Promise<{ details: StepDetailsPage; grid: DecisionGridPage }> {
  const details = await openStepFixture(page, orders, async (p) => {
    const result = await connected(p);
    await withLanguageFeatures(p);
    return result;
  });
  await routeTables(page, [paymentPolicy("1.2.0")]);
  await details.open("score");
  const grid = new DecisionGridPage(page);
  await grid.open();
  return { details, grid };
}

/** The table's document as the Table field's viewer shows it. */
async function viewedTable(page: Page, details: StepDetailsPage) {
  await details.field("table").getByRole("button", { name: "Open" }).click();
  const viewer = page.getByRole("dialog", { name: /^View payment-policy@/ });
  const text = await viewer.locator("pre").innerText();
  await viewer.getByRole("button", { name: "Close" }).click();
  return parse(text) as {
    metadata: { version: string };
    spec: Record<string, unknown> & {
      rules: { id: string; when: unknown }[];
    };
  };
}

// Columns: 0 rule name, 1 amount, 2 tier, 3 approve, 4 limit.
for (const size of sizes)
  test.describe(`the decision table grid at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a published table opens read as cells, with its Otherwise row", async ({
      page,
    }) => {
      const { grid } = await openGrid(page);
      await expect(grid.grid).toHaveAccessibleName(
        "Rules of payment-policy 1.2.0",
      );
      await expect(grid.root).toContainText("payment-policy 1.2.0 · published");
      await expect(grid.cell(0, 0)).toHaveText("big-gold");
      await expect(grid.cell(0, 1)).toHaveText("> 1000");
      await expect(grid.cell(0, 2)).toContainText("gold");
      await expect(grid.cell(1, 2)).toHaveText("-");
      await expect(grid.cell(0, 3)).toHaveText("true");
      await expect(grid.cell(0, 4)).toHaveText("5000");
      // One formula cell across the inputs.
      await expect(grid.cell(2, 1)).toContainText(
        'ƒinput.amount > 5000 or input.tier == "platinum"',
      );
      await expect(grid.cell(2, 2)).toHaveCount(0);
      await expect(grid.cell(3, 0)).toHaveText("Otherwise (no rule matches)");
      await expect(grid.cell(3, 3)).toHaveText("false");
      await expect(grid.cell(3, 4)).toHaveText("0");
      await expect(
        grid.root.getByText(
          "payment-policy 1.2.0 is published. Your first change starts the next version on this computer.",
        ),
      ).toBeVisible();
    });

    test("the first change starts the next version on this computer and moves the step to it", async ({
      page,
    }) => {
      const { details, grid } = await openGrid(page);
      await grid.edit(1, 1, "<= 900");
      await expect(grid.root).toContainText(
        "payment-policy 1.2.1 · on this computer",
      );
      await expect(grid.cell(1, 1)).toHaveText("<= 900");
      await grid.back();
      await details.close();
      expect(parse(await sourceText(page)).spec.steps[0].uses).toBe(
        "payment-policy@1.2.1",
      );
      // The workflow's Undo moves the step back; the draft stays.
      await page.keyboard.press("ControlOrMeta+z");
      await expect
        .poll(async () => parse(await sourceText(page)).spec.steps[0].uses)
        .toBe("payment-policy@1.2.0");
      await details.open("score");
      const table = details.field("table");
      await table.getByRole("radio", { name: "From list" }).click();
      await table.getByRole("combobox", { name: "Table" }).click();
      await expect(
        page.getByRole("option", {
          name: "payment-policy@1.2.1 · on this computer",
        }),
      ).toBeVisible();
      await page.keyboard.press("Escape");
    });

    test("a published version whose next version is already a draft says so", async ({
      page,
    }) => {
      const { details, grid } = await openGrid(page);
      await grid.edit(1, 1, "<= 900");
      await expect(grid.root).toContainText("1.2.1 · on this computer");
      await grid.back();
      const table = details.field("table");
      await table.getByRole("radio", { name: "From list" }).click();
      await table.getByRole("combobox", { name: "Table" }).click();
      await page
        .getByRole("option", { name: "payment-policy@1.2.0", exact: true })
        .click();
      await grid.open();
      await grid.edit(1, 1, "<= 800");
      await expect(grid.root).toContainText(
        "payment-policy 1.2.1 is already a draft on this computer. Choose it in Table to keep editing it.",
      );
      await expect(grid.root).toContainText("payment-policy 1.2.0 · published");
      await expect(grid.cell(1, 1)).toHaveText("<= 1000");
    });

    test("typing a cell writes the rule's condition", async ({ page }) => {
      const { details, grid } = await openGrid(page);
      await grid.edit(0, 1, "> 5000");
      await expect(grid.root).toContainText("1.2.1 · on this computer");
      await grid.back();
      const table = await viewedTable(page, details);
      expect(table.metadata.version).toBe("1.2.1");
      expect(table.spec.rules[0].when).toEqual({
        op: {
          name: "and",
          args: [
            {
              op: {
                name: "gt",
                args: [{ ref: "/input/amount" }, { literal: 5000 }],
              },
            },
            {
              op: {
                name: "eq",
                args: [{ ref: "/input/tier" }, { literal: "gold" }],
              },
            },
          ],
        },
      });
    });

    test("a cell that doesn't read stays with its message and isn't written", async ({
      page,
    }) => {
      const { grid } = await openGrid(page);
      await grid.edit(0, 1, ">> 1000");
      const cell = grid.cell(0, 1);
      await expect(cell.locator(".dg-cell-text")).toHaveText(">> 1000");
      await expect(cell).toHaveAttribute("aria-invalid", "true");
      await expect(grid.root.locator(".dg-problems")).toContainText(
        />>\s+isn't a test; try\s+> 1000\s*\./,
      );
      // Nothing was written: the step still uses the published version.
      await expect(grid.root).toContainText("payment-policy 1.2.0 · published");
      await cell.focus();
      await page.keyboard.press("Escape");
      await expect(cell.locator(".dg-cell-text")).toHaveText("> 1000");
      await expect(cell).not.toHaveAttribute("aria-invalid");
      await expect(grid.root.locator(".dg-problems")).toHaveCount(0);
    });

    test("a rule the grid can't split stays as it was until edited", async ({
      page,
    }) => {
      const { details, grid } = await openGrid(page);
      await grid.edit(0, 1, "> 2000");
      await grid.addRule();
      await expect(grid.cell(4, 0)).toBeVisible();
      await grid.back();
      const table = await viewedTable(page, details);
      const original = paymentPolicy("1.2.0").spec["rules"] as {
        id: string;
        when: unknown;
      }[];
      const kept = table.spec.rules.find(
        (rule) => rule.id === "large-or-platinum",
      );
      expect(JSON.stringify(kept)).toBe(JSON.stringify(original[2]));
    });

    test("adding an input column asks for its name and type", async ({
      page,
    }) => {
      const { details, grid } = await openGrid(page);
      await grid.addColumn("input", "country", "Text");
      await expect(
        grid.grid.getByRole("columnheader", { name: /country/ }),
      ).toContainText("country · Text");
      await grid.back();
      const table = await viewedTable(page, details);
      expect(
        (table.spec["inputSchema"] as { properties: Record<string, unknown> })
          .properties["country"],
      ).toEqual({ type: "string" });
    });

    test("renaming a column rewrites every rule", async ({ page }) => {
      const { grid } = await openGrid(page);
      await grid.grid
        .getByRole("button", { name: "Column menu for amount" })
        .click();
      await page.getByRole("menuitem", { name: "Rename" }).click();
      const dialog = page.getByRole("dialog", { name: "Rename amount" });
      await dialog.getByLabel("Name").fill("total");
      await dialog.getByRole("button", { name: "Rename column" }).click();
      await expect(
        grid.grid.getByRole("columnheader", { name: /total/ }),
      ).toBeVisible();
      await expect(grid.cell(2, 1)).toContainText("input.total > 5000");
      await expect(grid.cell(0, 1)).toHaveText("> 1000");
    });

    test("Collect all removes Otherwise and Undo brings it back", async ({
      page,
    }) => {
      const { grid } = await openGrid(page);
      await grid.policy("Collect all");
      await expect(grid.cell(3, 0)).toHaveCount(0);
      const toast = page.getByText("Removed the Otherwise result.");
      await expect(toast).toBeVisible();
      await page
        .locator(".toast", { hasText: "Removed the Otherwise result." })
        .getByRole("button", { name: "Undo", exact: true })
        .click();
      await expect(grid.cell(3, 0)).toHaveText("Otherwise (no rule matches)");
    });

    test("the problems line explains each problem and goes to its cell", async ({
      page,
    }) => {
      const { grid } = await openGrid(page);
      await grid.edit(1, 0, "big-gold");
      await grid.edit(0, 1, "-");
      await grid.edit(0, 2, "-");
      const problems = grid.root.locator(".dg-problems");
      await expect(problems).toContainText(
        "Rule 3 can never match: rule 1 always matches.",
      );
      await expect(problems).toContainText("big-gold");
      const item = problems.locator("li", {
        hasText: "Rule 3 can never match",
      });
      await item.getByRole("button", { name: "Go to cell" }).click();
      await expect(grid.cell(2, 0)).toBeFocused();
    });
  });
