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
// Action choice, offline authoring and result replacement use the same draft.
import { test, expect } from "@playwright/test";
import { parse } from "yaml";
import {
  connected,
  offline,
  lookupAction,
  newWorkflow,
  insertStep,
  sourceText,
  expectHitTarget,
} from "./support";
import { actionPicker, chooseAction } from "./integrations-po";

for (const viewport of [
  { width: 1280, height: 720 },
  { width: 600, height: 500 },
]) {
  test.describe(`Action configuration at ${viewport.width}`, () => {
    test.use({ viewport });
    test("offline action starts empty and is editable in the first section", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      await insertStep(page, "Call an action");
      if (viewport.width < 768)
        await page.locator('[data-step="call-action-1"] .node-body').click();
      const version = page.getByLabel("Action version", { exact: true });
      await expect(version).toHaveValue("");
      await expectHitTarget(version);
      await version.fill("erp.lookup@3.1.0");
      await version.press("Tab");
      expect(parse(await sourceText(page)).spec.steps[0].uses).toBe(
        "erp.lookup@3.1.0",
      );
    });
    test("search chooses latest version and the version menu can choose older", async ({
      page,
    }) => {
      const catalog = ["2.0.0", "3.0.0", "3.1.0"].map((version, index) => ({
        id: `erp-${index}`,
        document: {
          ...lookupAction,
          metadata: { name: "erp.lookup", version },
        },
      }));
      await connected(page, { catalog });
      await newWorkflow(page);
      await insertStep(page, "Call an action");
      if (viewport.width < 768)
        await page.locator('[data-step="call-action-1"] .node-body').click();
      const picker = actionPicker(page);
      await picker.click();
      await expect(picker).not.toHaveAttribute("aria-activedescendant", /.+/);
      await picker.fill("erp");
      await expect(
        page.locator('[role=option][data-uses^="erp.lookup"]'),
      ).toHaveCount(1);
      await picker.press("Enter");
      await expect(picker).toHaveValue("erp.lookup@3.1.0");
      await page
        .getByRole("button", { name: "Choose version of erp.lookup" })
        .click();
      await page.getByRole("menuitem", { name: /^2.0.0/ }).click();
      await expect(picker).toHaveValue("erp.lookup@2.0.0");
      expect(parse(await sourceText(page)).spec.steps[0].uses).toBe(
        "erp.lookup@2.0.0",
      );
    });
    test("switching input editing shows typed fields and keeps invalid drafts in view", async ({
      page,
    }) => {
      await connected(page);
      await newWorkflow(page);
      await insertStep(page, "Call an action");
      if (viewport.width < 768)
        await page.locator('[data-step="call-action-1"] .node-body').click();
      await chooseAction(page, "sql.lookup@1.0.0");
      const customer = page.getByLabel("Customer ID", { exact: true });
      await customer.fill("customer-42");
      await page
        .getByRole("button", {
          name: "Write one expression for the whole input",
        })
        .click();
      // The whole-input expression starts from the fields typed so far.
      const input = page.locator(
        'weave-step-property-grid [data-field="with"]',
      );
      await input
        .getByRole("button", { name: "Input options", exact: true })
        .click();
      await page
        .getByRole("menuitem", { name: "Advanced: JSON value", exact: true })
        .click();
      const json = input.getByRole("textbox", {
        name: "Advanced JSON value",
        exact: true,
      });
      const typed = await json.inputValue();
      expect(JSON.parse(typed)).toEqual({
        parameters: { customerId: "customer-42" },
      });
      // An invalid draft blocks the switch instead of disappearing with it.
      await json.fill('{"parameters":');
      const fields = page.getByRole("button", {
        name: "Edit the input field by field",
      });
      await fields.click();
      await expect(
        page.getByText(
          "Fix the invalid fields before continuing. Your edits are still here.",
          { exact: true },
        ),
      ).toBeVisible();
      await expect(json).toHaveValue('{"parameters":');
      await json.fill(typed);
      await fields.click();
      await expect(customer).toHaveValue("customer-42");
      await page.locator(".inspector header h2").click();
      expect(parse(await sourceText(page)).spec.steps[0].with).toEqual({
        literal: { parameters: { customerId: "customer-42" } },
      });
    });
    test("using action result requires review and can be undone", async ({
      page,
    }) => {
      await connected(page);
      await newWorkflow(page);
      await insertStep(page, "Call an action");
      if (viewport.width < 768)
        await page.locator('[data-step="call-action-1"] .node-body').click();
      await chooseAction(page, "sql.lookup@1.0.0");
      await page.locator(".inspector header h2").click();
      await page.getByLabel("Customer ID", { exact: true }).fill("customer-1");
      await page.getByLabel("Customer ID", { exact: true }).press("Tab");
      const output = page
        .locator("weave-action-inspector details")
        .filter({
          has: page
            .locator(":scope > summary")
            .filter({ hasText: /^\s*4\s*Output\s*$/ }),
        })
        .first();
      if ((await output.getAttribute("open")) === null)
        await output.locator(":scope > summary").click();
      await page
        .getByRole("button", {
          name: "Use action output as workflow result",
          exact: true,
        })
        .click();
      const dialog = page.getByRole("dialog", {
        name: "Replace the workflow result?",
      });
      await expect(dialog).toBeVisible();
      await dialog
        .getByRole("button", { name: "Replace result", exact: true })
        .click();
      await expect(
        page.getByText("Workflow result updated.", { exact: true }),
      ).toBeVisible();
      await page
        .getByRole("button", { name: "Undo", exact: true })
        .last()
        .click();
      expect(parse(await sourceText(page)).spec.output).toEqual({
        literal: {},
      });
    });
  });
}
