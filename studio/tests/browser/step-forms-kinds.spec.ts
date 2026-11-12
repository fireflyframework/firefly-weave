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
// The fields a new step of every kind shows by default, counted as a person
// sees them on its Parameters tab.
import { expect, test } from "@playwright/test";
import {
  StepDetailsPage,
  addCommonStep,
  openStepFixture,
  settingsGear,
  sizes,
  withLanguageFeatures,
  withNewEditor,
  yamlFile,
} from "./step-details-po";
import { connected, insertStep, newWorkflow, offline } from "./support";
import { paymentPolicy, routeTables } from "./decision-grid-po";

const twoRequired = yamlFile(
  "orders.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: { name: orders, version: 1.0.0 }
spec:
  inputSchema: { type: object }
  outputSchema: { type: object }
  steps:
    - id: score
      kind: decisionTable
      uses: payment-policy@1.0.0
      with: { literal: {} }
  output: { literal: {} }
`,
);

/** Add a step's entries for each kind and their default field counts. A new action step has no action yet, so it shows Action alone; its connection and required inputs follow the chosen action. */
const kinds: [string, number][] = [
  ["Call an action", 1],
  ["Decision table", 1],
  ["AI task", 2],
  ["Transform", 1],
  ["Decision", 1],
  ["Parallel", 1],
  ["Wait for time", 1],
  ["Wait for signal", 1],
  ["Approve or reject", 3],
  ["Stop with error", 2],
];
const common: [string, number][] = [
  ["Call an API", 3],
  ["Send email", 4],
  ["Reply to an email", 4],
  ["List files", 2],
  ["Read a file", 2],
  ["Download a file", 2],
  ["Write a file", 3],
  ["Move a file", 3],
  ["Delete a file", 2],
];

/** Each kind's Settings rows, by their field IDs, as the step configuration table lists them. */
const settings: [string, string[]][] = [
  ["Call an action", ["onError", "notes", "about"]],
  ["Decision table", ["onError", "notes", "about"]],
  ["AI task", ["timeout", "onError", "notes", "about"]],
  ["Transform", ["onError", "notes", "about"]],
  ["Decision", ["onError", "notes", "about"]],
  ["Parallel", ["onError", "notes", "about"]],
  ["Wait for time", ["notes", "about"]],
  ["Wait for signal", ["timeout", "onError", "notes", "about"]],
  ["Approve or reject", ["due", "expiry", "onError", "notes", "about"]],
  ["Stop with error", ["notes", "about"]],
];
/** One message per kind, shown once its first required field is left empty. */
const messages: [string, string, string][] = [
  ["Call an action", "action", "Choose an action to call."],
  ["Decision table", "table", "Choose a decision table."],
  [
    "Wait for signal",
    "name",
    "Choose a signal name; names are unique in a workflow.",
  ],
  ["Stop with error", "message", "Write a message the run will report."],
];

for (const size of sizes)
  test.describe(`default fields at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a new step of each kind shows its default fields", async ({
      page,
    }) => {
      test.slow();
      await withNewEditor(page);
      await offline(page);
      await newWorkflow(page);
      const details = new StepDetailsPage(page);
      for (const [label, count] of kinds) {
        await insertStep(page, label);
        await expect(details.dialog, label).toBeVisible();
        await expect(details.topFields(), label).toHaveCount(count);
        await details.close();
      }
      for (const [label, count] of common) {
        await addCommonStep(page, label);
        await expect(details.dialog, label).toBeVisible();
        await expect(details.topFields(), label).toHaveCount(count);
        await details.close();
      }
      await details.trigger().dblclick();
      await expect(details.topFields(), "Manual form trigger").toHaveCount(1);
      await details.close();
      await settingsGear(page).click();
      await expect(
        page
          .getByRole("dialog", { name: "Workflow settings" })
          .locator(".param-form > [data-param]"),
      ).toHaveCount(2);
    });

    test("a new step of each kind shows its Settings rows", async ({
      page,
    }) => {
      test.slow();
      await withNewEditor(page);
      await offline(page);
      await newWorkflow(page);
      const details = new StepDetailsPage(page);
      for (const [label, rows] of settings) {
        await insertStep(page, label);
        await details.tab("Settings").click();
        const panel = details.dialog.locator("#sd-panel-settings");
        await expect(panel.locator("[data-param]").first()).toBeVisible();
        const ids = await panel
          .locator("[data-param]")
          .evaluateAll((items) =>
            items
              .filter((item) => !item.parentElement?.closest("[data-param]"))
              .map((item) => item.getAttribute("data-param")),
          );
        expect(ids, label).toEqual(rows);
        await details.close();
      }
    });

    test("an empty required field says what it needs, in the words of its kind", async ({
      page,
    }) => {
      test.slow();
      await withNewEditor(page);
      await offline(page);
      await newWorkflow(page);
      const details = new StepDetailsPage(page);
      for (const [label, id, text] of messages) {
        await insertStep(page, label);
        const field = details.field(id);
        await field
          .locator("input, textarea, [role=combobox], .cm-content")
          .first()
          .click();
        await page.keyboard.press("Tab");
        await expect(field.locator(".param-line"), label).toContainText(text);
        await details.close();
      }
    });

    test("a decision table step counts its table's required inputs", async ({
      page,
    }) => {
      const details = await openStepFixture(page, twoRequired, async (p) => {
        const result = await connected(p);
        await withLanguageFeatures(p);
        return result;
      });
      const table = paymentPolicy("1.0.0");
      (table.spec["inputSchema"] as { required: string[] }).required = [
        "amount",
        "tier",
      ];
      await routeTables(page, [table]);
      await details.open("score");
      await expect(details.field("input.tier")).toBeVisible();
      await expect(details.topFields()).toHaveCount(3);
    });
  });
