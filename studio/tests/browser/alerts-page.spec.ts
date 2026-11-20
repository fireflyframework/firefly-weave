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
// Operate › Alerts: what fires now with each channel's status and what
// resolved in the last 7 days; Acknowledge; the rules and where each stands;
// the channels and who they reach; the channel sheet with members first and
// outside addresses refused under their own field; and the access, loading,
// error, empty and evaluator states. The platform is mocked.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page } from "@playwright/test";
import * as fixture from "../alerts-fixture";
import { alertsPlatform, manager, sentTo } from "./alerts-po";

const captures = resolve(
  process.env["STUDIO_ALERTS_CAPTURES"]?.trim() || "test-results/alerts",
);
mkdirSync(captures, { recursive: true });
const shot = (page: Page, name: string) =>
  page.screenshot({ path: `${captures}/${name}.png`, fullPage: true });
const heading = (page: Page) =>
  expect(page.locator("#main h1")).toHaveText("Alerts");
const tab = (page: Page, name: RegExp) => page.getByRole("tab", { name });
const firingRows = (page: Page) =>
  page.getByRole("table", { name: "Firing alerts" }).locator(".rec:not(.head)");

test.use({ timezoneId: "UTC" });

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe(`Operate › Alerts at ${size.width} px`, () => {
    test.use({ viewport: size });

    test("shows firing and recent alerts with each channel's status", async ({
      page,
    }) => {
      const platform = await alertsPlatform(page);
      await heading(page);
      await expect(tab(page, /^Firing and recent/)).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await expect(tab(page, /^Firing and recent/)).toContainText("2");
      await expect(tab(page, /^Rules/)).toContainText("8");
      await expect(tab(page, /^Channels/)).toContainText("2");
      await expect(page.locator(".section-head").first()).toContainText(
        "Firing2 alerts · rules are checked every 30 seconds",
      );
      const rows = firingRows(page);
      await expect(rows).toHaveCount(2);
      await expect(rows.nth(0)).toContainText("Critical");
      await expect(rows.nth(0)).toContainText("Run failure rate above 10 %");
      await expect(rows.nth(0)).toContainText("Firing · 14.6 % > 10 %");
      await expect(rows.nth(0)).toContainText("Today 10:42");
      await expect(rows.nth(0)).toContainText("firing for 12 min");
      await expect(rows.nth(0)).toContainText("On-call email: Sent");
      await expect(rows.nth(0)).toContainText(
        "Platform team: Failed: the mail server refused the message",
      );
      await expect(rows.nth(1)).toContainText(
        "Acknowledged by you · repeat notifications stopped until it resolves.",
      );
      await expect(rows.nth(1)).toContainText(
        "Platform team: Skipped: hourly limit reached",
      );
      await expect(
        rows.nth(1).getByRole("button", { name: "Acknowledge" }),
      ).toHaveCount(0);
      const recent = page
        .getByRole("table", { name: "Recently resolved alerts" })
        .locator(".rec:not(.head)");
      await expect(recent).toHaveCount(3);
      await expect(recent.nth(0)).toContainText("Resolved after 9 min");
      await expect(recent.nth(0)).toContainText("Back within the threshold");
      await expect(recent.nth(1)).toContainText("Yesterday 22:40");
      await expect(recent.nth(2)).toContainText("Resolved after 2 h 10 min");
      await expect(recent.nth(2)).toContainText("Rule disabled");
      // Firing alerts first, then the last 7 days of resolved ones.
      const firing = sentTo(platform, "/alerts")[0].query;
      expect(firing.get("state")).toBe("firing");
      const resolved = platform.sent.find(
        (request) => request.query.get("state") === "resolved",
      )!.query;
      // The last 7 days, in whole seconds; the page's clock runs on.
      expect(resolved.get("from")).toMatch(/^2026-11-11T10:5\d:\d\dZ$/);
      expect(
        await page.evaluate(
          () => document.scrollingElement!.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await shot(page, `alerts-firing-${size.width}`);
    });

    test("Acknowledge stops repeats and says so", async ({ page }) => {
      const platform = await alertsPlatform(page);
      const row = firingRows(page).first();
      await row.getByRole("button", { name: "Acknowledge" }).click();
      await expect(row.getByRole("status")).toHaveText(
        /Acknowledged by you · repeat notifications stopped until it resolves\./,
      );
      await expect(
        row.getByRole("button", { name: "Acknowledge" }),
      ).toHaveCount(0);
      // Focus stays on the row, which now says so.
      await expect(row).toBeFocused();
      expect(
        sentTo(platform, "/acknowledge", "POST").map((r) => r.path),
      ).toEqual([expect.stringContaining(fixture.ids.firingRate)]);
    });

    test("the Rules tab says where each rule stands, in the address", async ({
      page,
    }) => {
      await alertsPlatform(page);
      await tab(page, /^Rules/).click();
      await expect(page).toHaveURL(/\/operate\/alerts\?tab=rules$/);
      const rows = page
        .getByRole("table", { name: "Alert rules" })
        .locator(".rec:not(.head)");
      await expect(rows).toHaveCount(8);
      await expect(page.locator(".toolbar")).toContainText(
        "8 of 100 rules in local",
      );
      await expect(rows.nth(0)).toContainText("Suggested by Weave");
      await expect(rows.nth(0)).toContainText("In-app only");
      await expect(rows.nth(1)).toContainText(
        "Above 10 % over 15 min · at least 10 runs",
      );
      await expect(rows.nth(1)).toContainText("On-call email, Platform team");
      await expect(rows.nth(1)).toContainText("Firing for 12 min · 14.6 %");
      await expect(rows.nth(3)).toContainText(
        "Runners offline, k3d-weave-test.team-a",
      );
      await expect(rows.nth(3)).toContainText(
        "Firing for 38 min · 1 · acknowledged",
      );
      await expect(rows.nth(5)).toContainText("Last check failed 2 min ago");
      await expect(rows.nth(6)).toContainText(
        "Not checked: your access to kind-weave-test was removed",
      );
      // A name inside a sentence never breaks at its hyphen.
      await expect(
        rows.nth(6).locator(".nb", { hasText: "kind-weave-test" }),
      ).toHaveCount(2);
      await expect(rows.nth(7)).toContainText(
        "Not checked: this server doesn't know the signal",
      );
      await expect(
        page.getByRole("switch", { name: "Enabled: No workers online" }),
      ).toBeChecked();
      await expect(
        page.getByRole("button", { name: "Edit No workers online" }),
      ).toBeVisible();
      await shot(page, `alerts-rules-${size.width}`);
      await page.goBack();
      await expect(tab(page, /^Firing and recent/)).toHaveAttribute(
        "aria-selected",
        "true",
      );
    });

    test("switching a rule off waits for the platform and keeps its revision", async ({
      page,
    }) => {
      const platform = await alertsPlatform(page, {
        path: "/operate/alerts?tab=rules",
      });
      const toggle = page.getByRole("switch", {
        name: "Enabled: No workers online",
      });
      await toggle.click();
      await expect(toggle).not.toBeChecked();
      const [update] = sentTo(
        platform,
        `/alert-rules/${fixture.ids.noWorkers}`,
        "PUT",
      );
      expect(update.body).toMatchObject({ enabled: false, threshold: 1 });
      await expect(
        page
          .getByRole("table", { name: "Alert rules" })
          .locator(".rec:not(.head)")
          .nth(2),
      ).toContainText("Switched off");
    });

    test("Channels name each recipient, the owner and the webhooks", async ({
      page,
    }) => {
      await alertsPlatform(page, { path: "/operate/alerts?tab=channels" });
      const rows = page
        .getByRole("table", { name: "Email channels" })
        .locator(".rec:not(.head)");
      await expect(rows).toHaveCount(2);
      await expect(rows.nth(0)).toContainText(
        "Email · sent as you, from acme-mail",
      );
      await expect(rows.nth(0)).toContainText("4 rules");
      await expect(rows.nth(1)).toContainText(
        "4 rules · approval notifications",
      );
      await expect(rows.nth(1)).toContainText(
        "hasn't set an alert address. Ask them to set one in Settings › Preferences.",
      );
      await expect(rows.nth(1)).toContainText(
        "oncall@vendor.example · outside address",
      );
      await expect(page.locator(".webhooks")).toHaveText(
        "2 webhook subscriptions receive alert changes. Manage them with weave subscriptions.",
      );
      await shot(page, `alerts-channels-${size.width}`);
    });

    test("the channel sheet lists members first and refuses an outside address under its field", async ({
      page,
    }) => {
      const platform = await alertsPlatform(page, {
        path: "/operate/alerts?tab=channels",
        answers: {
          updateChannel: {
            status: 422,
            json: {
              code: "WV-ALERT-RECIPIENT",
              message: "Some recipients can't receive alert mail.",
              result: {
                recipients: [{ index: 4, reason: "domain_not_allowed" }],
              },
            },
          },
        },
      });
      const edit = page.getByRole("button", { name: "Edit Platform team" });
      await edit.click();
      const sheet = page.getByRole("dialog", { name: "Edit Platform team" });
      await expect(sheet).toBeVisible();
      await expect(sheet.getByLabel("Name")).toHaveValue("Platform team");
      await expect(sheet).toContainText(
        "owns this channel now; it sends from their email connection.",
      );
      await expect(sheet).toContainText(
        "hasn't set an alert address. Ask them to set one in Settings › Preferences. The rest of the channel still sends.",
      );
      await expect(sheet.getByLabel("Send from")).toHaveValue(
        fixture.ids.acmeMail,
      );
      await expect(sheet).toContainText(
        "Saving makes you this channel's owner: its mail goes out as you, from acme-mail",
      );
      await expect(sheet.getByLabel("Outside address 1")).toHaveValue(
        "oncall@vendor.example",
      );
      await sheet
        .getByRole("button", { name: "Add an outside address" })
        .click();
      const added = sheet.getByLabel("Outside address 2");
      await expect(added).toBeFocused();
      await added.fill("alerts@example.org");
      await sheet.getByRole("button", { name: "Save channel" }).click();
      await expect(added).toHaveAttribute("aria-invalid", "true");
      await expect(sheet).toContainText(
        "example.org isn't an allowed email domain in local.",
      );
      const [update] = sentTo(
        platform,
        `/alert-channels/${fixture.ids.team}`,
        "PUT",
      );
      expect(update.body).toMatchObject({
        kind: "email",
        name: "Platform team",
        connection_revision_id: fixture.ids.acmeMail,
        recipients: [
          { member: fixture.BEN },
          { member: fixture.ME },
          { member: fixture.ANA },
          { address: "oncall@vendor.example" },
          { address: "alerts@example.org" },
        ],
      });
      await shot(page, `alerts-channel-sheet-${size.width}`);
      await page.keyboard.press("Escape");
      await expect(sheet).toHaveCount(0);
      await expect(edit).toBeFocused();
    });
  });
}

test.describe("Operate › Alerts states", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("without alert.read there is no entry, and a deep link says what's needed", async ({
    page,
  }) => {
    await alertsPlatform(page, {
      capabilities: ["run.read", "status.read", "catalog.read"],
    });
    await expect(page.locator("#main")).toContainText(
      "You need alert.read in local.",
    );
    await expect(page.locator("#main")).toContainText(
      "Ask an administrator in Settings › People and access.",
    );
    await expect(
      page.locator("nav").getByRole("button", { name: /^Alerts/ }),
    ).toHaveCount(0);
    await shot(page, "alerts-no-access-1440");
  });

  test("one tab's error leaves the others usable, and Try again reads it", async ({
    page,
  }) => {
    const platform = await alertsPlatform(page, {
      answers: { rules: 503 },
      path: "/operate/alerts?tab=rules",
    });
    const problem = page.getByRole("alert");
    await expect(problem).toContainText(
      "WV-UNAVAILABLE: The platform didn't answer in time.",
    );
    await shot(page, "alerts-error-1440");
    await tab(page, /^Channels/).click();
    await expect(
      page.getByRole("table", { name: "Email channels" }),
    ).toBeVisible();
    await tab(page, /^Rules/).click();
    platform.answers["rules"] = { items: fixture.rules(), next_cursor: null };
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(
      page.getByRole("table", { name: "Alert rules" }),
    ).toBeVisible();
  });

  test("shows skeleton rows while the first read is out", async ({ page }) => {
    let release = () => {};
    const held = new Promise<void>((done) => (release = done));
    await alertsPlatform(page, {
      answers: {
        firing: () => fixture.alertPage(fixture.firing()),
      },
    });
    await page.route("**/alerts?state=firing*", async (route) => {
      await held;
      await route.fallback();
    });
    await page.reload();
    await expect(page.locator(".skeleton").first()).toBeVisible();
    await shot(page, "alerts-loading-1440");
    release();
    await expect(firingRows(page)).toHaveCount(2);
  });

  test("says when no rules exist and nothing fires", async ({ page }) => {
    await alertsPlatform(page, {
      answers: {
        firing: fixture.alertPage([], 0, 0),
        resolved: fixture.alertPage([], 0, 0),
        rules: { items: [], next_cursor: null },
      },
    });
    await expect(page.locator("#main")).toContainText("No alerts are firing.");
    await expect(page.locator("#main")).toContainText(
      "Rules are checked every 30 seconds.",
    );
    await shot(page, "alerts-nothing-firing-1440");
    await tab(page, /^Rules/).click();
    await expect(page.locator("#main")).toContainText("No alert rules yet.");
    await expect(page.locator("#main")).toContainText(
      "Add a rule, or start from a suggested one.",
    );
    await expect(page.getByRole("button", { name: "Add rule" })).toBeVisible();
    await page.getByRole("button", { name: "Suggested rules" }).click();
    await expect(
      page.getByRole("button", {
        name: "Start from Run failure rate above 10 %",
      }),
    ).toBeVisible();
    await shot(page, "alerts-no-rules-1440");
  });

  test("warns when the alert checker isn't running", async ({ page }) => {
    await alertsPlatform(page, {
      answers: {
        summary: {
          observed_at: fixture.NOW,
          health: [{ component: "alert_evaluator", state: "down" }],
        },
      },
    });
    await expect(page.getByRole("status").first()).toContainText(
      "Alert rules aren't being checked right now. Check Operate › Overview.",
    );
    await shot(page, "alerts-evaluator-down-1440");
  });

  test("a read-only person sees alerts without the actions", async ({
    page,
  }) => {
    await alertsPlatform(page, {
      capabilities: manager.filter(
        (capability) => capability !== "alert.manage",
      ),
    });
    await expect(firingRows(page)).toHaveCount(2);
    await expect(page.getByRole("button", { name: "Acknowledge" })).toHaveCount(
      0,
    );
    await tab(page, /^Rules/).click();
    await expect(page.getByRole("button", { name: "Add rule" })).toHaveCount(0);
    await expect(page.getByRole("switch")).toHaveCount(0);
  });

  test("says outside addresses can't be used before a domain is allowed", async ({
    page,
  }) => {
    await alertsPlatform(page, {
      path: "/operate/alerts?tab=channels",
      answers: {
        settings: {
          recipient_domains: [],
          approval_channel_ids: [],
          revision: 0,
        },
      },
    });
    await page.getByRole("button", { name: "Add channel" }).click();
    const sheet = page.getByRole("dialog", { name: "Add a channel" });
    await expect(sheet).toContainText(
      "Outside addresses can't be used in local yet. An administrator can allow a domain in Settings › Administration.",
    );
    await shot(page, "alerts-no-domain-1440");
  });

  test("recipients never reach the address bar, storage or console", async ({
    page,
  }) => {
    const messages: string[] = [];
    page.on("console", (message) => messages.push(message.text()));
    await alertsPlatform(page, { path: "/operate/alerts?tab=channels" });
    await page.getByRole("button", { name: "Edit Platform team" }).click();
    const sheet = page.getByRole("dialog", { name: "Edit Platform team" });
    await sheet.getByRole("button", { name: "Add an outside address" }).click();
    await sheet.getByLabel("Outside address 2").fill("pager@vendor.example");
    await sheet.getByRole("button", { name: "Save channel" }).click();
    await expect(sheet).toHaveCount(0);
    const stored = await page.evaluate(() =>
      JSON.stringify([
        location.href,
        history.state,
        Object.entries(localStorage),
        Object.entries(sessionStorage),
      ]),
    );
    for (const secret of [
      "pager@vendor.example",
      "oncall@vendor.example",
      fixture.ANA,
      fixture.BEN,
    ]) {
      expect(stored).not.toContain(secret);
      expect(messages.join("\n")).not.toContain(secret);
    }
  });
});
