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
import { test, expect } from "@playwright/test";
import {
  offline,
  connected,
  newWorkflow,
  closeSheet,
  expectHitTarget,
} from "./support";
import { DesignerPage } from "./designer-po";
import { chooseAction } from "./integrations-po";

for (const width of [1440, 1280, 600, 390, 320]) {
  test.describe(`Designer organization at ${width}`, () => {
    test.use({ viewport: { width, height: 720 } });
    test("palette search stays reachable while browsing steps", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      if (width < 1025)
        await page
          .getByRole("button", { name: "Insert step", exact: true })
          .click();
      const palette = page.locator(".palette");
      const decisionLabel = palette
        .getByRole("button", { name: "Decision table", exact: true })
        .locator(".palette-label");
      expect((await decisionLabel.boundingBox())!.height).toBeLessThanOrEqual(
        24,
      );
      const search = palette.getByRole("textbox", { name: "Search steps" });
      await palette
        .getByRole("heading", { name: "Steps", exact: true })
        .hover();
      await page.mouse.wheel(0, 900);
      await expect
        .poll(() => palette.evaluate((e) => e.scrollTop))
        .toBeGreaterThan(0);
      const box = (await palette.boundingBox())!;
      await expect
        .poll(async () => (await search.boundingBox())!.y)
        .toBeGreaterThanOrEqual(box.y);
      await search.click();
      await search.fill("signal");
      await expect(
        palette.getByRole("button", { name: "Wait for signal", exact: true }),
      ).toBeVisible();
    });
    test("toolbar groups remain aligned and keep overflow commands reachable", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(width);
      const toolbar = page.getByRole("toolbar", { name: "Workflow commands" });
      expect(await toolbar.evaluate((e) => e.scrollWidth)).toBeLessThanOrEqual(
        await toolbar.evaluate((e) => e.clientWidth),
      );
      for (const name of ["Edit workflow", "Check workflow", "Save and run"])
        await expect(
          toolbar.getByRole("group", { name, exact: true }),
        ).toBeVisible();
      const validate = toolbar.getByRole("button", {
        name: "Validate",
        exact: true,
      });
      const save = toolbar.getByRole("button", {
        name: "Save to file",
        exact: true,
      });
      await expectHitTarget(validate);
      await expectHitTarget(save);
      expect(
        Math.abs(
          (await validate.boundingBox())!.y - (await save.boundingBox())!.y,
        ),
      ).toBeLessThanOrEqual(2);
      if (width < 768) {
        const more = toolbar.getByRole("button", { name: "More", exact: true });
        expect((await more.boundingBox())!.width).toBeGreaterThanOrEqual(60);
        await more.click();
        for (const name of [
          /^Undo$/,
          /^Redo$/,
          /^Simulate/,
          /^(Show|Hide) inspector$/,
          /^Back to workflows$/,
        ])
          await expect(page.getByRole("menuitem", { name })).toBeVisible();
        await page.keyboard.press("Escape");
      } else {
        await expect(
          toolbar.getByRole("button", { name: "Undo", exact: true }),
        ).toBeVisible();
        await expect(
          toolbar.getByRole("button", { name: "Simulate", exact: true }),
        ).toBeVisible();
      }
      const canvasTools = page.getByRole("toolbar", { name: "Canvas view" });
      await expect(canvasTools).toBeVisible();
      const canvasBox = (await page.locator(".canvas").boundingBox())!;
      const toolsBox = (await canvasTools.boundingBox())!;
      expect(toolsBox.x).toBeGreaterThanOrEqual(canvasBox.x);
      expect(toolsBox.x + toolsBox.width).toBeLessThanOrEqual(
        canvasBox.x + canvasBox.width,
      );
      await page.screenshot({
        path: test.info().outputPath(`toolbar-${width}.png`),
      });
    });
    test("action setup keeps published details and slot management out of the input flow", async ({
      page,
    }) => {
      await connected(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("action");
      await designer.selectStep("call-action-1");
      await chooseAction(page, "sql.lookup@1.0.0");
      const details = designer.inspector.locator("details.action-details");
      await expect(details).not.toHaveAttribute("open", "");
      await expect(
        designer.inspector.locator("details.slot-management"),
      ).not.toHaveAttribute("open", "");
      await expect(
        designer.inspector.locator("summary.properties-heading"),
      ).toHaveCount(0);
      const customer = designer.inspector.getByRole("textbox", {
        name: "Customer ID",
        exact: true,
      });
      await customer.fill("customer-1");
      await details.locator("summary").click();
      await expect(details).toContainText("30 seconds");
      await details.locator("summary").click();
      await expect(customer).toHaveValue("customer-1");
      await designer.inspector.locator(".inspector-body").hover();
      await page.mouse.wheel(0, -2000);
      await expect
        .poll(() =>
          designer.inspector
            .locator(".inspector-body")
            .evaluate((e) => e.scrollTop),
        )
        .toBe(0);
      await page.screenshot({
        path: test.info().outputPath(`action-inspector-${width}.png`),
      });
    });
    test("human deadlines are optional and retain their fields when collapsed", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("humanTask");
      await designer.selectStep("approval-1");
      const deadlines = designer.inspector.locator("details.human-deadlines");
      await expect(deadlines).not.toHaveAttribute("open", "");
      await deadlines.locator("summary").click();
      const due = deadlines.getByLabel("Due after", { exact: true });
      await due.fill("2");
      await deadlines.locator("summary").click();
      await deadlines.locator("summary").click();
      await expect(due).toHaveValue("2");
      await closeSheet(page);
    });
  });
}
