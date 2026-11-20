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
// The rule editor: a rule started from a suggested one, narrowed to a
// workflow and previewed before it is saved, the percent sent as a ratio;
// Preview with a value says whether the condition holds; a field that can't
// be saved says why and takes focus; Preview comes between the fields and
// Save in reading and Tab order. The platform is mocked.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page } from "@playwright/test";
import * as fixture from "../alerts-fixture";
import { alertsPlatform, sentTo } from "./alerts-po";

const captures = resolve(
  process.env["STUDIO_ALERTS_CAPTURES"]?.trim() || "test-results/alerts",
);
mkdirSync(captures, { recursive: true });
const shot = (page: Page, name: string) =>
  page.screenshot({ path: `${captures}/${name}.png`, fullPage: true });

test.use({ timezoneId: "UTC" });

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe(`The rule editor at ${size.width} px`, () => {
    test.use({ viewport: size });

    test("a rule is created from a suggested one and previewed", async ({
      page,
    }) => {
      const platform = await alertsPlatform(page, {
        path: "/operate/alerts?tab=rules",
      });
      await page.getByRole("button", { name: "Suggested rules" }).click();
      await page
        .getByRole("button", { name: "Start from Run failure rate above 10 %" })
        .click();
      const title = page.getByRole("heading", { name: "Add a rule" });
      await expect(title).toBeFocused();
      const name = page.getByLabel("Name");
      await expect(name).toHaveValue("Run failure rate above 10 %");
      await expect(page.getByLabel("Signal")).toHaveValue("run.failure_rate");
      await expect(page.locator("#rule-signal-hint")).toHaveText(
        "Percent of ended runs that failed or timed out. Test runs never count.",
      );
      await expect(page.getByLabel("Threshold")).toHaveValue("10");
      await expect(page.getByLabel("Window")).toHaveValue("900");
      await expect(page.getByLabel("Only with at least")).toHaveValue("10");
      await name.fill("invoice-approval failure rate above 10 %");
      await page.getByLabel("Workflow").selectOption("invoice-approval");
      await page.getByLabel("On-call email").check();
      await page.getByLabel("Repeat while firing").selectOption("3600");
      await page.getByRole("button", { name: "Preview", exact: true }).click();
      const result = page.locator(".preview-result");
      await expect(result).toContainText(
        "No data: fewer than 10 runs ended in the last 15 minutes.",
      );
      await expect(result).toContainText(
        "With no data, the rule stays as it is: it doesn't fire and doesn't resolve.",
      );
      await expect(result).toContainText("Run failure rate, invoice-approval");
      await expect(result).toContainText("1 (test runs don't count)");
      const [preview] = sentTo(platform, "/alert-rules/preview", "POST");
      expect(preview.body).toMatchObject({
        signal: "run.failure_rate",
        selector: { workflow: "invoice-approval" },
        threshold: 0.1,
        window_seconds: 900,
        min_samples: 10,
      });
      await expect(page.locator(".says")).toHaveText(
        "Saving checks this rule every 30 seconds in local. When it fires, it shows here, rings the bell in the navigation if it's critical, and mails the channels you chose; when it clears for a minute, it resolves and mails once more.",
      );
      await shot(page, `alerts-editor-${size.width}`);
      await page.getByRole("button", { name: "Save rule" }).click();
      const [created] = sentTo(platform, "/alert-rules", "POST");
      expect(created.body).toEqual({
        name: "invoice-approval failure rate above 10 %",
        enabled: true,
        signal: "run.failure_rate",
        selector: { workflow: "invoice-approval" },
        comparator: "gt",
        threshold: 0.1,
        window_seconds: 900,
        for_seconds: 0,
        min_samples: 10,
        severity: "warning",
        channel_ids: [fixture.ids.oncall],
        renotify_seconds: 3600,
      });
      await expect(
        page.getByRole("button", {
          name: "Edit invoice-approval failure rate above 10 %",
        }),
      ).toBeFocused();
      await expect(page).toHaveURL(/\?tab=rules$/);
    });
  });
}

test.describe("The rule editor's states", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Preview with a value says whether the condition holds", async ({
    page,
  }) => {
    await alertsPlatform(page, {
      path: "/operate/alerts?tab=rules",
      answers: { preview: fixture.previewValue() },
    });
    await page
      .getByRole("button", { name: "Edit Tasks waiting over 5 minutes" })
      .click();
    await expect(
      page.getByRole("heading", { name: "Edit Tasks waiting over 5 minutes" }),
    ).toBeFocused();
    await expect(page.getByLabel("Threshold")).toHaveValue("300");
    await expect(page.getByLabel("Window")).toHaveCount(0);
    await page.getByRole("button", { name: "Preview", exact: true }).click();
    const result = page.locator(".preview-result");
    await expect(result.locator(".value")).toHaveText("42 s");
    await expect(result).toContainText("Current value 42 s (evaluated now)");
    await expect(result).toContainText(
      "The condition isn't met: 42 s isn't above 300 s. Saving doesn't fire anything now.",
    );
    await shot(page, "alerts-preview-value-1440");
    // A change to the draft clears a preview that no longer describes it.
    await page.getByLabel("Threshold").fill("30");
    await expect(result).not.toContainText("42 s");
  });

  test("names each field it can't save and moves focus to the first", async ({
    page,
  }) => {
    const platform = await alertsPlatform(page, {
      path: "/operate/alerts?tab=rules",
    });
    await page.getByRole("button", { name: "Add rule" }).click();
    await page.getByRole("button", { name: "Save rule" }).click();
    const name = page.getByLabel("Name");
    await expect(name).toBeFocused();
    await expect(name).toHaveAttribute("aria-invalid", "true");
    await expect(page.locator("#rule-name-problem")).toHaveText(
      "Name the rule.",
    );
    await expect(page.locator("#rule-threshold-problem")).toHaveText(
      "Enter a percent from 0 to 100.",
    );
    expect(sentTo(platform, "/alert-rules", "POST")).toHaveLength(0);
  });

  test("a save someone else overtook says so and keeps the form", async ({
    page,
  }) => {
    await alertsPlatform(page, {
      path: "/operate/alerts?tab=rules",
      answers: {
        updateRule: {
          status: 409,
          json: { code: "WV-REVISION", message: "Revision mismatch" },
        },
      },
    });
    await page.getByRole("button", { name: "Edit No workers online" }).click();
    await page.getByRole("button", { name: "Save rule" }).click();
    await expect(page.getByRole("alert")).toContainText(
      "WV-REVISION: Someone else changed this item. Reload it to see the latest version, then try again.",
    );
    await expect(page.getByLabel("Name")).toHaveValue("No workers online");
  });

  test("Preview comes between the fields and Save in Tab order", async ({
    page,
  }) => {
    await alertsPlatform(page, { path: "/operate/alerts?tab=rules" });
    await page.getByRole("button", { name: "Add rule" }).click();
    await page.getByLabel("Repeat while firing").focus();
    await page.keyboard.press("Tab");
    await expect(
      page.getByRole("button", { name: "Preview", exact: true }),
    ).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "Save rule" })).toBeFocused();
    // Severity is one stop; arrows move between its choices.
    await page.getByRole("radio", { name: "Warning" }).focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("radio", { name: "Critical" })).toBeChecked();
  });

  test("without status.read, Preview says what it needs", async ({ page }) => {
    await alertsPlatform(page, {
      path: "/operate/alerts?tab=rules",
      capabilities: ["alert.read", "alert.manage", "catalog.read"],
    });
    await page.getByRole("button", { name: "Add rule" }).click();
    await expect(page.locator("#rule-preview-needs")).toHaveText(
      "Preview also needs status.read in local.",
    );
    await expect(
      page.getByRole("button", { name: "Preview", exact: true }),
    ).toHaveAttribute("aria-disabled", "true");
  });
});
