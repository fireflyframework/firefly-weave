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
// Settings › Administration: each section in its own state or access state,
// Run check, a retention plan's counts before Apply and Apply asking first,
// allowed domains, approval notifications, recording an opt-in with how the
// recipient agreed, and Revoke asking first. The platform is mocked.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page } from "@playwright/test";
import * as fixture from "../alerts-fixture";
import { ENV, administrator, alertsPlatform, sentTo } from "./alerts-po";

const captures = resolve(
  process.env["STUDIO_ALERTS_CAPTURES"]?.trim() || "test-results/alerts",
);
mkdirSync(captures, { recursive: true });
const shot = (page: Page, name: string) =>
  page.screenshot({ path: `${captures}/${name}.png`, fullPage: true });
const card = (page: Page, name: string) =>
  page.getByRole("region", { name, exact: true });
const open = (page: Page, capabilities = administrator, answers = {}) =>
  alertsPlatform(page, {
    capabilities,
    answers,
    path: "/settings/administration",
  });

test.use({ timezoneId: "UTC" });

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe(`Settings › Administration at ${size.width} px`, () => {
    test.use({ viewport: size });

    test("shows the platform, its compatibility, alert email and retention", async ({
      page,
    }) => {
      await open(page);
      await expect(
        page.getByRole("tab", { name: "Administration" }),
      ).toHaveAttribute("aria-selected", "true");
      await expect(page.locator(".intro")).toHaveText(
        "The platform at weave.invalid, environment local. Each section asks for its own access.",
      );
      const platform = card(page, "Platform");
      await expect(platform).toContainText("Version0.1.0a17");
      await expect(platform).toContainText(/Build\s*26ab04e1/);
      await expect(platform).toContainText("API uptime3 days 4 h");
      const compatibility = card(page, "Compatibility");
      await expect(compatibility).toContainText("Ready · 0 findings");
      await expect(compatibility).toContainText(
        "CheckedToday 08:02, 1,204 records",
      );
      const email = card(page, "Alert email");
      await expect(email).toContainText("vendor.example");
      await expect(
        email.getByRole("row", { name: /oncall@vendor\.example/ }),
      ).toContainText("Agreed by email to the on-call rota, 3 Nov");
      await expect(card(page, "Approval notifications")).toContainText(
        "Change requests awaiting approvalPlatform team",
      );
      await expect(card(page, "Retention")).toContainText(
        "A plan lists expired debug sessions that can be deleted. Nothing is deleted until you apply it.",
      );
      await expect(card(page, "Telemetry export")).toContainText(
        "Off · configured with WEAVE_TELEMETRY. Studio only shows this setting; the platform's environment sets it.",
      );
      const periods = card(page, "Operations data retention").locator(
        '[role="row"]:not(.h)',
      );
      await expect(periods).toHaveCount(9);
      await expect(periods.nth(0)).toContainText(
        "Run statistics, 5-minute buckets2 days",
      );
      await expect(periods.nth(3)).toContainText("Samples, per minute24 hours");
      expect(
        await page.evaluate(
          () => document.scrollingElement!.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await shot(page, `administration-${size.width}`);
    });
  });
}

test.describe("Settings › Administration actions", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("each section asks for its own capability", async ({ page }) => {
    await open(page, ["status.read", "alert.read", "catalog.read"]);
    await expect(card(page, "Retention")).toContainText(
      "You need retention.plan in local. Ask an administrator in Settings › People and access.",
    );
    await expect(card(page, "Compatibility")).toContainText(
      "Running a check needs compatibility.check in local.",
    );
    const email = card(page, "Alert email");
    await expect(email).toContainText(
      "Changing domains and opt-ins needs alert.manage and environment.manage in local.",
    );
    await expect(email.getByRole("button")).toHaveCount(0);
    await expect(card(page, "Platform")).toContainText("0.1.0a17");
    await shot(page, "administration-access-1440");
  });

  test("without alert.read the alert sections say what they need", async ({
    page,
  }) => {
    const platform = await open(page, ["status.read", "catalog.read"]);
    await expect(card(page, "Alert email")).toContainText(
      "You need alert.read in local.",
    );
    await expect(card(page, "Approval notifications")).toContainText(
      "You need alert.read in local.",
    );
    expect(sentTo(platform, "/alert-settings")).toHaveLength(0);
  });

  test("Run check shows the new findings", async ({ page }) => {
    const platform = await open(page);
    const compatibility = card(page, "Compatibility");
    await compatibility.getByRole("button", { name: "Run check" }).click();
    await expect(compatibility).toContainText("Restricted · 1 finding");
    await expect(compatibility).toContainText(
      "A worker speaks a worker protocol this version doesn't support.",
    );
    await expect(compatibility.getByRole("status")).toHaveText(
      "Checked just now: 1 finding.",
    );
    expect(
      sentTo(platform, "/operations/compatibility/check", "POST"),
    ).toHaveLength(1);
  });

  test("a retention plan shows its counts before Apply, and Apply asks first", async ({
    page,
  }) => {
    const platform = await open(page);
    const retention = card(page, "Retention");
    await retention.getByRole("button", { name: "Make a plan" }).click();
    await expect(retention).toContainText(
      "Can be deleted12 expired debug sessions",
    );
    await expect(retention).toContainText(
      "Kept2 debug sessions something still uses",
    );
    await expect(retention).toContainText("Plan expires at 14:32");
    expect(sentTo(platform, "/apply", "POST")).toHaveLength(0);
    const apply = retention.getByRole("button", { name: "Apply" });
    await apply.click();
    const confirm = page.getByRole("alertdialog");
    await expect(confirm).toHaveText(
      /Delete 12 expired debug sessions\? This can't be undone\. Run histories and the other records stay\./,
    );
    await expect(confirm.getByRole("button", { name: "Cancel" })).toBeFocused();
    await shot(page, "administration-apply-1440");
    await page.keyboard.press("Escape");
    await expect(confirm).toHaveCount(0);
    await expect(apply).toBeFocused();
    expect(sentTo(platform, "/apply", "POST")).toHaveLength(0);
    await apply.click();
    await page.getByRole("button", { name: "Delete 12" }).click();
    await expect(retention.getByRole("status")).toHaveText(
      "Deleted 12 debug sessions.",
    );
    expect(sentTo(platform, "/apply", "POST")).toHaveLength(1);
    const [made] = sentTo(platform, "/operations/retention/plans", "POST");
    expect(made.body).toEqual({ target: "expired_debug", limit: 100 });
  });

  test("changing allowed domains needs environment.manage and keeps the revision", async ({
    page,
  }) => {
    const platform = await open(page);
    const email = card(page, "Alert email");
    await email.getByRole("button", { name: "Add domain" }).click();
    const field = email.getByLabel("Domain to allow");
    await expect(field).toBeFocused();
    await field.fill("Vendor.Example");
    await email.getByRole("button", { name: "Allow" }).click();
    await expect(email).toContainText("vendor.example is already allowed.");
    await field.fill("acme.example");
    await field.press("Enter");
    await expect(email.getByRole("status")).toHaveText(
      "acme.example is allowed.",
    );
    const [saved] = sentTo(platform, "/alert-settings", "PUT");
    expect(saved.body).toEqual({
      recipient_domains: ["vendor.example", "acme.example"],
      approval_channel_ids: [fixture.ids.team],
    });
    await expect(
      email.getByRole("button", { name: "Remove acme.example" }),
    ).toBeVisible();
  });

  test("approval notifications go to the channels chosen", async ({ page }) => {
    const platform = await open(page);
    const approvals = card(page, "Approval notifications");
    await approvals
      .getByRole("button", { name: "Edit approval notifications" })
      .click();
    await approvals.getByLabel("On-call email").check();
    await approvals.getByRole("button", { name: "Save" }).click();
    await expect(approvals).toContainText("Platform team, On-call email");
    const [saved] = sentTo(platform, "/alert-settings", "PUT");
    expect(saved.body).toMatchObject({
      approval_channel_ids: [fixture.ids.team, fixture.ids.oncall],
    });
  });

  test("recording an opt-in asks how the recipient agreed, and Revoke asks first", async ({
    page,
  }) => {
    const platform = await open(page);
    const email = card(page, "Alert email");
    await email.getByRole("button", { name: "Record an opt-in" }).click();
    const dialog = page.getByRole("dialog", { name: "Record an opt-in" });
    await expect(dialog.getByLabel("Address")).toBeFocused();
    await expect(dialog).toContainText(
      "Up to 200 characters. It's kept with the opt-in and shown to administrators.",
    );
    await dialog.getByRole("button", { name: "Record opt-in" }).click();
    await expect(dialog).toContainText(
      "Say how they agreed to receive alert mail.",
    );
    await dialog.getByLabel("Address").fill("pager@vendor.example");
    await dialog
      .getByLabel("How did they agree?")
      .fill("Asked by phone on 12 Nov; they agreed to get on-call alerts.");
    await shot(page, "administration-record-1440");
    await dialog.getByRole("button", { name: "Record opt-in" }).click();
    await expect(dialog).toHaveCount(0);
    const [recorded] = sentTo(platform, "/alert-recipient-consents", "POST");
    expect(recorded.body).toEqual({
      address: "pager@vendor.example",
      statement: "Asked by phone on 12 Nov; they agreed to get on-call alerts.",
    });
    await expect(
      email.getByRole("row", { name: /pager@vendor\.example/ }),
    ).toBeVisible();

    await email
      .getByRole("button", { name: "Revoke oncall@vendor.example" })
      .click();
    const confirm = page.getByRole("alertdialog");
    await expect(confirm).toHaveText(
      /Revoke the opt-in for oncall@vendor\.example\? Mail to this address stops at the next send; Platform team keeps its other recipients\./,
    );
    await expect(confirm.getByRole("button", { name: "Cancel" })).toBeFocused();
    await shot(page, "administration-revoke-1440");
    await confirm.getByRole("button", { name: "Revoke" }).click();
    await expect(
      email.getByRole("row", { name: /oncall@vendor\.example/ }),
    ).toHaveCount(0);
    expect(
      sentTo(
        platform,
        `/alert-recipient-consents/${fixture.ids.consent}/revoke`,
        "POST",
      ),
    ).toHaveLength(1);
    expect(
      platform.sent.every((request) => request.path.startsWith("/studio/api/")),
    ).toBe(true);
    void ENV;
  });

  test("a refused opt-in says why under the address", async ({ page }) => {
    await open(page, administrator, {
      recordConsent: {
        status: 422,
        json: {
          code: "WV-ALERT-RECIPIENT",
          message: "Not allowed",
          result: { reason: "domain_not_allowed" },
        },
      },
    });
    await card(page, "Alert email")
      .getByRole("button", { name: "Record an opt-in" })
      .click();
    const dialog = page.getByRole("dialog", { name: "Record an opt-in" });
    await dialog.getByLabel("Address").fill("alerts@example.org");
    await dialog.getByLabel("How did they agree?").fill("By email.");
    await dialog.getByRole("button", { name: "Record opt-in" }).click();
    await expect(dialog.getByLabel("Address")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    await expect(dialog).toContainText(
      "example.org isn't an allowed email domain in local.",
    );
  });

  test("Administration shows only to people who read the status", async ({
    page,
  }) => {
    await alertsPlatform(page, {
      capabilities: ["alert.read", "catalog.read"],
      path: "/settings/preferences",
    });
    await expect(page.getByRole("tab", { name: "Preferences" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "Administration" })).toHaveCount(
      0,
    );
  });
});
