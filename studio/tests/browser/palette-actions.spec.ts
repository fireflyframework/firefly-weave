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
import { connected, offline, newWorkflow } from "./support";
import { openPaletteIntegrations } from "./integrations-po";

for (const width of [1440, 390]) {
  test.describe(`Action palette at ${width}`, () => {
    test.use({ viewport: { width, height: 800 } });
    test("local authoring separates reuse from creating an API action", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const palette = await openPaletteIntegrations(page);
      await expect(
        palette.getByRole("heading", { name: "Use an existing action" }),
      ).toBeVisible();
      await expect(
        palette.getByRole("heading", { name: "Create an API action" }),
      ).toBeVisible();
      const help = palette.locator(".palette-create details");
      const summary = help.locator("summary");
      await summary.focus();
      await summary.press("Enter");
      await expect(help).toContainText(
        "configure and save an action file on this computer",
      );
      await summary.press("Escape");
      await expect(help).not.toHaveAttribute("open", "");
      await expect(summary).toBeFocused();
      await page.screenshot({
        path: test.info().outputPath(`palette-local-${width}.png`),
      });
      await palette
        .getByRole("button", { name: "New API action", exact: true })
        .click();
      await expect(
        page.getByRole("dialog", { name: "New API action" }),
      ).toBeVisible();
    });
    test("reuse offers the existing platform connection flow", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const palette = await openPaletteIntegrations(page);
      await palette
        .getByRole("button", { name: "Connect to a platform", exact: true })
        .click();
      await expect(page.locator("#wizard-heading")).toHaveText(
        "Connect to a platform",
      );
      await expect(page.getByLabel("Server address")).toBeVisible();
    });
    test("published actions remain directly insertable", async ({ page }) => {
      await connected(page);
      await newWorkflow(page);
      const palette = await openPaletteIntegrations(page);
      await expect(
        palette.getByRole("button", {
          name: "Connect to a platform",
          exact: true,
        }),
      ).toHaveCount(0);
      await palette.locator('[data-uses="sql.lookup@1.0.0"]').click();
      await expect(page.locator('[data-step="call-action-1"]')).toContainText(
        "sql.lookup@1.0.0",
      );
    });
  });
}
