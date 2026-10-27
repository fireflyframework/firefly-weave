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
// The decision table step's own fields: Table with Edit table and New table,
// then one row per required table input; optional inputs behind Add option.
import { expect, test, type Page } from "@playwright/test";
import { parse } from "yaml";
import { connected, sourceText } from "./support";
import {
  openStepFixture,
  sizes,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";
import { routeTables } from "./decision-grid-po";

const workflow = (step: string) =>
  yamlFile(
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
${step}
  output: { literal: {} }
`,
  );
const fresh = workflow(`    - id: score
      kind: decisionTable
      uses: ""
      with: { ref: /input }`);
const chosen = workflow(`    - id: score
      kind: decisionTable
      uses: payment-policy@1.0.0
      with: { literal: {} }`);

const connectedStart = async (page: Page) => {
  const result = await connected(page);
  await withLanguageFeatures(page);
  return result;
};

for (const size of sizes)
  test.describe(`the decision table step at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a new decision table step shows Table with Edit table and New table", async ({
      page,
    }) => {
      const details = await openStepFixture(page, fresh);
      await details.open("score");
      await expect(details.topFields()).toHaveCount(1);
      const table = details.field("table");
      const edit = table.getByRole("button", { name: "Edit table" });
      await expect(edit).toHaveAttribute("aria-disabled", "true");
      await expect(table).toContainText("Choose a table first.");
      await expect(
        table.getByRole("button", { name: "New table" }),
      ).not.toHaveAttribute("aria-disabled");
      // A command, not a field: the label row stays one line.
      const label = await table
        .locator(".param-label-row, label")
        .first()
        .boundingBox();
      const button = await edit.boundingBox();
      expect(button!.y).toBeGreaterThan(label!.y + label!.height - 0.5);
    });

    test("choosing a table shows its required inputs and offers the optional ones", async ({
      page,
    }) => {
      const details = await openStepFixture(page, chosen, connectedStart);
      await routeTables(page);
      await details.open("score");
      await expect(details.field("input.amount")).toBeVisible();
      await expect(details.topFields()).toHaveCount(2);
      await expect(details.field("input.tier")).toHaveCount(0);
      await details.dialog.getByRole("button", { name: "Add option" }).click();
      await expect(
        details.dialog.getByRole("menuitem", { name: /^tier\b/i }),
      ).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(
        details.field("table").getByRole("button", { name: "Edit table" }),
      ).not.toHaveAttribute("aria-disabled");
    });

    test("New table opens the grid on a table named after the step", async ({
      page,
    }) => {
      const details = await openStepFixture(page, fresh);
      await details.open("score");
      await details
        .field("table")
        .getByRole("button", { name: "New table" })
        .click();
      await expect(
        details.dialog.getByRole("heading", {
          name: "orders.score 1.0.0 · on this computer",
        }),
      ).toBeVisible();
      await details.dialog
        .getByRole("button", { name: "Back to the step" })
        .click();
      await expect(details.field("table")).toBeVisible();
      await details.close();
      expect(parse(await sourceText(page)).spec.steps[0].uses).toBe(
        "orders.score@1.0.0",
      );
    });
  });
