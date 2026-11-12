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
// Step details list and read decision tables themselves: the Table field's
// list follows the account, a chosen version's inputs show without the
// classic designer, and a version read once is kept on this computer.
import { expect, test, type Page } from "@playwright/test";
import { connected } from "./support";
import {
  openStepFixture,
  sizes,
  stepFixture,
  withLanguageFeatures,
} from "./step-details-po";
import { routeTables } from "./decision-grid-po";

const TENANT = "00000000-0000-4000-8000-000000000001";
const PROJECT = "00000000-0000-4000-8000-000000000002";

const openWith = (page: Page, options: Parameters<typeof connected>[1] = {}) =>
  openStepFixture(page, stepFixture, async (p) => {
    const result = await connected(p, options);
    await withLanguageFeatures(p);
    return result;
  });

for (const size of sizes)
  test.describe(`decision tables in step details at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("the table's inputs show without the classic designer", async ({
      page,
    }) => {
      const details = await openWith(page);
      const routes = await routeTables(page);
      await details.open("score");
      await expect(details.field("input")).toContainText("Table input");
      expect(routes.lists).toBe(1);
      expect(routes.exports).toEqual(["payment-policy@1.0.0"]);
      // Reopening reads nothing again.
      await details.close();
      await details.open("score");
      await expect(details.field("input")).toContainText("Table input");
      expect(routes.exports).toEqual(["payment-policy@1.0.0"]);
    });

    test("the list follows the account", async ({ page }) => {
      const details = await openWith(page, { identityDelay: 4_000 });
      const routes = await routeTables(page);
      await details.open("score");
      const table = details.field("table");
      await table.getByRole("radio", { name: "From list" }).click();
      await expect(table).toContainText("Sign in to list published tables.");
      // Signing in lists the published versions without a Retry.
      await expect(
        page.getByRole("button", { name: /^Platform: .* · Signed in\b/ }),
      ).toBeVisible({ timeout: 10_000 });
      await expect(table).not.toContainText("Sign in to list");
      await table.getByRole("combobox", { name: "Table" }).click();
      await expect(
        page
          .getByRole("listbox", { name: "Table options" })
          .getByRole("option", { name: "payment-policy@1.0.0" }),
      ).toBeVisible();
      expect(routes.lists).toBeGreaterThan(0);
    });

    test("a version read once is kept on this computer", async ({ page }) => {
      const details = await openWith(page, {
        tenantId: TENANT,
        projectId: PROJECT,
      });
      const routes = await routeTables(page, undefined, PROJECT);
      await details.open("score");
      await expect(details.field("input")).toContainText("Table input");
      expect(routes.exports).toEqual(["payment-policy@1.0.0"]);
      await details.close();
      // The platform can't list or export tables anymore.
      routes.fail(503);
      await page.goto("/");
      await page
        .getByLabel("Choose a workflow file")
        .setInputFiles(stepFixture);
      const bar = page.locator(".editor-bar");
      await expect(bar).toBeVisible();
      const show = page.getByRole("button", { name: "Show canvas" });
      if (await show.isVisible()) await show.click();
      await details.open("score");
      await expect(details.field("input")).toContainText("Table input");
      const table = details.field("table");
      await table.getByRole("radio", { name: "From list" }).click();
      const combobox = table.getByRole("combobox", { name: "Table" });
      await expect(combobox).toHaveAttribute(
        "data-value",
        "payment-policy@1.0.0",
      );
      await combobox.click();
      await expect(
        page
          .getByRole("listbox", { name: "Table options" })
          .getByRole("option", {
            name: "payment-policy@1.0.0 · on this computer",
          }),
      ).toBeVisible();
      await page.keyboard.press("Escape");
      expect(routes.exports).toEqual(["payment-policy@1.0.0"]);
    });
  });
