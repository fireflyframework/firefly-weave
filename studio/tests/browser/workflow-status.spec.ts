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
// Contract gaps share one status across cards, the inspector and diagnostics.
import { test, expect } from "@playwright/test";
import {
  connected,
  newWorkflow,
  closeSheet,
  offline,
  allCapabilities,
} from "./support";
import { DesignerPage } from "./designer-po";
import { chooseAction } from "./integrations-po";
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe("Workflow status at " + viewport.width, () => {
    test.use({ viewport });
    test("required inputs show on the node and diagnostics and focus the field", async ({
      page,
    }) => {
      await connected(page, { capabilities: [...allCapabilities, "compile"] });
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("action");
      await designer.selectStep("call-action-1");
      await chooseAction(page, "crm.lookup@2.0.0");
      const customer = designer.inspector.getByRole("textbox", {
        name: "Customer",
        exact: true,
      });
      await expect(customer).toBeVisible();
      await closeSheet(page);
      const chip = page
        .locator('[data-step="call-action-1"]')
        .getByRole("button", { name: "Needs: Customer", exact: true });
      await chip.focus();
      await expect(chip).toBeVisible();
      const strip = page.getByRole("region", { name: "Compiler diagnostics" });
      await expect(strip).toContainText("1 item needs attention");
      await chip.click();
      await expect(customer).toBeFocused();
      await customer.fill("C-42");
      await customer.press("Tab");
      await expect(chip).toHaveCount(0);
      await expect(strip).toContainText(
        "Checked locally — Validate to check against the project",
      );
      await closeSheet(page);
      await expect(
        page.getByRole("list", { name: "Workflow progress" }),
      ).toContainText(/Draft\s+Published\s+Active/);
      await expect(
        page.getByRole("button", { name: "More", exact: true }),
      ).toContainText("More");
    });
  });
}
for (const viewport of [
  { width: 1280, height: 720 },
  { width: 360, height: 780 },
]) {
  test(`the local status stays readable at ${viewport.width}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await offline(page);
    await newWorkflow(page);
    const designer = new DesignerPage(page);
    await designer.append("wait");
    const status = page.locator(".status-chip");
    await expect(status).toContainText("Draft saved");
    const close = page.getByRole("button", {
      name: "Close inspector",
      exact: true,
    });
    if (await close.isVisible()) await close.click();
    expect(
      await status.evaluate((node) => node.scrollWidth <= node.clientWidth),
    ).toBe(true);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await expect(
      page
        .getByRole("list", { name: "Workflow progress" })
        .locator('[aria-current="step"]'),
    ).toHaveText("Draft");
  });
}
