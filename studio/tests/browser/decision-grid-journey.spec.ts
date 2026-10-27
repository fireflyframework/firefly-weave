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
// Building business rules from nothing, with no platform: a Decision table
// step, New table, its columns, three rules by keyboard, Exactly one with
// an overlap found and fixed, Try an input checked by Weave, then Execute
// step answering from the table kept on this computer.
import { expect, test } from "@playwright/test";
import { parse, stringify } from "yaml";
import { insertStep, newWorkflow, offline } from "./support";
import { StepDetailsPage } from "./step-details-po";
import { DecisionGridPage } from "./decision-grid-po";
import { shownExecute } from "./execute-po";
import { outputPane } from "./test-data-po";
import { hasPython, localAuthoring } from "./local-authoring";

test.skip(
  !hasPython,
  "Weave's check and Execute step run the repository's Python.",
);

for (const size of [
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "600x500", width: 600, height: 500 },
])
  test.describe(`a decision table from nothing at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("builds, tries and executes a table by keyboard, offline", async ({
      page,
    }) => {
      test.slow();
      await offline(page);
      await localAuthoring(page);
      // Nothing chosen: the new editor is the default.
      expect(
        await page.evaluate(() => localStorage.getItem("ui:weave.editorNext")),
      ).toBeNull();
      await newWorkflow(page);
      await insertStep(page, "Decision table");
      const details = new StepDetailsPage(page);
      await expect(details.dialog).toBeVisible();
      await details
        .field("table")
        .getByRole("button", { name: "New table" })
        .click();
      const grid = new DecisionGridPage(page);
      await expect(grid.grid).toBeVisible();
      await expect(grid.root).toContainText("· on this computer");
      await grid.addColumn("input", "amount", "Number");
      await grid.addColumn("input", "tier", "Text");
      await grid.addColumn("result", "approve", "Yes/No");
      await grid.addColumn("result", "limit", "Number");
      const keys = page.keyboard;
      // Rule 1: big gold amounts.
      await grid.cell(0, 1).click();
      for (const [text, move] of [
        ["> 1000", "Tab"],
        ["gold", "Tab"],
        ["true", "Tab"],
        ["5000", null],
      ] as const) {
        await keys.type(text);
        await keys.press("Enter");
        if (move) await keys.press(move);
      }
      // Rule 2: small amounts.
      await keys.press("Alt+Enter");
      await expect(grid.cell(1, 0)).toBeFocused();
      await keys.press("ArrowRight");
      for (const [text, move] of [
        ["<= 1000", "Tab"],
        [null, "Tab"],
        ["true", "Tab"],
        ["1000", null],
      ] as const) {
        if (text) {
          await keys.type(text);
          await keys.press("Enter");
        }
        if (move) await keys.press(move);
      }
      // Rule 3: anything above 500, which overlaps both.
      await keys.press("Alt+Enter");
      await expect(grid.cell(2, 0)).toBeFocused();
      await keys.press("ArrowRight");
      for (const [text, move] of [
        ["> 500", "Tab"],
        [null, "Tab"],
        ["false", "Tab"],
        ["0", null],
      ] as const) {
        if (text) {
          await keys.type(text);
          await keys.press("Enter");
        }
        if (move) await keys.press(move);
      }
      await grid.policy("Exactly one");
      const problems = grid.root.locator(".dg-problems");
      await expect(problems).toContainText("can both match");
      // Fix: rule 3 is for big amounts that aren't gold.
      await grid.edit(2, 1, "> 1000");
      await grid.edit(2, 2, "silver");
      await expect(problems).toHaveCount(0);
      // Try an input: live, then Weave's check on this computer.
      await grid.root.getByRole("button", { name: "Try an input" }).click();
      const panel = grid.root.getByRole("region", { name: "Try an input" });
      await panel.getByLabel("amount").fill("2000");
      await panel.getByLabel("tier").fill("gold");
      await expect(panel).toContainText("Matched: rule-1");
      await expect(
        panel.locator(".pill", { hasText: "Checked by Weave" }),
      ).toBeVisible();
      await grid.root
        .getByRole("button", { name: "Close Try an input" })
        .click();
      await grid.back();
      // The step gives the table fixed values (as YAML), then executes.
      await details.dialog
        .getByRole("button", { name: /^More actions for / })
        .click();
      await page
        .getByRole("menuitem", { name: "Edit as YAML", exact: true })
        .click();
      const yaml = page.getByRole("textbox", { name: "Step YAML" });
      const step = parse(await yaml.inputValue()) as Record<string, unknown>;
      step["with"] = { literal: { amount: 2000, tier: "gold" } };
      await yaml.fill(stringify(step));
      await page.getByRole("button", { name: "Apply" }).click();
      await expect(yaml).toHaveCount(0);
      await (await shownExecute(details)).click();
      const pane = await outputPane(details);
      await expect(pane).toContainText("5000");
    });
  });
