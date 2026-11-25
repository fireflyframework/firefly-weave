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
        "With no data, the rule doesn't fire, and a firing alert resolves.",
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

  test("a rule with a signal Studio doesn't know asks for another before it saves", async ({
    page,
  }) => {
    const platform = await alertsPlatform(page, {
      path: "/operate/alerts?tab=rules",
    });
    await page
      .getByRole("button", { name: "Edit Transactions in doubt" })
      .click();
    await page.getByRole("button", { name: "Save rule" }).click();
    const signal = page.getByLabel("Signal");
    await expect(signal).toBeFocused();
    await expect(signal).toHaveAttribute("aria-invalid", "true");
    await expect(page.locator("#rule-signal-problem")).toHaveText(
      "Choose a signal from the list to save the rule.",
    );
    expect(
      sentTo(platform, `/alert-rules/${fixture.ids.inDoubt}`, "PUT"),
    ).toHaveLength(0);
  });

  for (const [signal, name] of [
    ["transactions.in_doubt", "Transactions in doubt"],
    ["transactions.undo_age_seconds", "Oldest transaction undoing"],
  ])
    test(`${name} counts the whole environment: no field narrows it`, async ({
      page,
    }) => {
      const platform = await alertsPlatform(page, {
        path: "/operate/alerts?tab=rules",
      });
      await page.getByRole("button", { name: "Add rule" }).click();
      await page.getByLabel("Workflow").selectOption("invoice-approval");
      const choice = page.getByLabel("Signal");
      await choice.selectOption({ label: name });
      await expect(choice).toHaveValue(signal);
      const whole = page.getByText(
        "Every workflow: this signal covers the whole environment.",
        { exact: true },
      );
      await expect(whole).toBeVisible();
      await expect(page.locator("#rule-selector")).toHaveCount(0);
      await expect(page.getByLabel("Workflow")).toHaveCount(0);
      // A screen reader hears it with the signal.
      await expect(choice).toHaveAccessibleDescription(
        /Test runs never count\. Every workflow: this signal covers the whole environment\.$/,
      );
      await shot(page, `alerts-editor-${signal}`);
      // A signal nobody would narrow says nothing.
      await choice.selectOption("outbox.pending");
      await expect(whole).toHaveCount(0);
      await choice.selectOption(signal);
      await page.getByLabel("Name").fill(name);
      await page.getByLabel("Threshold").fill("0");
      await page.getByRole("button", { name: "Save rule" }).click();
      await expect
        .poll(() => sentTo(platform, "/alert-rules", "POST").length)
        .toBe(1);
      expect(sentTo(platform, "/alert-rules", "POST")[0].body).toMatchObject({
        signal,
        selector: {},
        window_seconds: null,
      });
    });

  test("Preview of a transaction undoing with nothing to read says what a rule with no data does", async ({
    page,
  }) => {
    await alertsPlatform(page, {
      path: "/operate/alerts?tab=rules",
      answers: {
        preview: {
          value: null,
          unit: "seconds",
          condition_met: false,
          evaluated_at: fixture.NOW,
          no_data_reason: "no_data",
        },
      },
    });
    await page.getByRole("button", { name: "Add rule" }).click();
    await page
      .getByLabel("Signal")
      .selectOption("transactions.undo_age_seconds");
    await page.getByLabel("Name").fill("Undoing over 1 hour");
    await page.getByLabel("Threshold").fill("3600");
    await page.getByRole("button", { name: "Preview", exact: true }).click();
    const result = page.locator(".preview-result");
    await expect(result).toContainText(
      "With nothing undoing, the signal has no data: the rule doesn't fire, and a firing alert resolves.",
    );
    await expect(result).not.toContainText("it doesn't fire");
    await shot(page, "alerts-preview-nothing-undoing-1440");
  });

  for (const [what, capabilities, sentence, named] of [
    [
      "without run.read names it",
      ["alert.read", "alert.manage", "status.read"],
      "You need run.read in this environment for a rule on this signal. Ask an administrator in Settings › People and access. Support code: WV-FORBIDDEN.",
      ["run.read"],
    ],
    [
      "for a reason of the platform's own claims nothing missing",
      ["alert.read", "alert.manage", "status.read", "run.read"],
      "You can't SAVE this rule. Ask an administrator in Settings › People and access. Support code: WV-FORBIDDEN.",
      [],
    ],
  ] as const)
    test(`a rule on transactions refused ${what}, and keeps the form`, async ({
      page,
    }) => {
      const refused = {
        status: 403,
        json: { code: "WV-FORBIDDEN", message: "Access denied" },
      };
      await alertsPlatform(page, {
        path: "/operate/alerts?tab=rules",
        capabilities: [...capabilities],
        answers: { createRule: refused },
      });
      await page.route("**/alert-rules/preview", (route) =>
        route.fulfill(refused),
      );
      await page.getByRole("button", { name: "Add rule" }).click();
      await page.getByLabel("Signal").selectOption("transactions.in_doubt");
      await page.getByLabel("Name").fill("Transactions in doubt here");
      await page.getByLabel("Threshold").fill("0");
      await page.getByRole("button", { name: "Preview", exact: true }).click();
      const preview = page.locator(".preview-result .problem");
      await expect(preview).toHaveText(sentence.replace("SAVE", "preview"));
      await expect(preview.locator("code")).toHaveText([...named]);
      await page.getByRole("button", { name: "Save rule" }).click();
      const saving = page.getByRole("alert");
      await expect(saving).toHaveText(sentence.replace("SAVE", "save"));
      await expect(saving.locator("code")).toHaveText([...named]);
      await shot(page, `alerts-rule-refused-${named.length}`);
      await expect(page.getByLabel("Name")).toHaveValue(
        "Transactions in doubt here",
      );
      await expect(
        page.getByRole("heading", { name: "Add a rule" }),
      ).toBeVisible();
    });

  test("the rule Weave suggests for transactions in doubt is critical and fires at once", async ({
    page,
  }) => {
    const [degraded, ...rest] = fixture.rules();
    await alertsPlatform(page, {
      path: "/operate/alerts?tab=rules",
      answers: {
        rules: {
          items: [
            degraded,
            // As the platform installs it, with 4 in doubt for 21 minutes.
            {
              ...degraded,
              id: "d1000000-0000-4000-8000-000000000021",
              name: "Transactions in doubt",
              signal: "transactions.in_doubt",
              for_seconds: 0,
              severity: "critical",
              preset_id: "transactions-in-doubt",
              state: {
                ...degraded.state,
                state: "firing",
                since: "2026-11-18T10:33:00Z",
                value: 4,
                unit: "count",
              },
            },
            ...rest.filter((item) => item.id !== fixture.ids.inDoubt),
          ],
          next_cursor: null,
        },
      },
    });
    const rule = page
      .getByRole("table", { name: "Alert rules" })
      .locator(".rec:not(.head)")
      .nth(1);
    for (const said of [
      "Transactions in doubt",
      "Suggested by Weave",
      "Above 0",
      "Critical",
      "In-app only",
      "Firing for 21 min · 4",
    ])
      await expect(rule).toContainText(said);
    await expect(rule).not.toContainText("· for");
    await shot(page, "alerts-rules-in-doubt-1440");
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
