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
// Settings › Preferences › My alert address: a person sets and removes only
// their own address, at the tenant's my-alert-address operation that never
// names anyone; without one, the card says channels skip them. The platform
// is mocked.
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
const card = (page: Page) =>
  page.getByRole("region", { name: "My alert address" });

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe(`My alert address at ${size.width} px`, () => {
    test.use({ viewport: size });

    test("a person sets and removes only their own alert address", async ({
      page,
    }) => {
      const platform = await alertsPlatform(page, {
        capabilities: ["catalog.read"],
        path: "/settings/preferences",
      });
      const address = card(page).getByLabel("Address");
      await expect(address).toHaveValue("carla@acme.example");
      await expect(card(page)).toContainText(
        "Alerts sent to you as a project member go to this address.",
      );
      await expect(card(page)).toContainText(
        "Only you can set it. It's used only for alerts of projects you belong to, and no one else sees it.",
      );
      await shot(page, `alert-address-${size.width}`);
      await address.fill("not an address");
      await card(page).getByRole("button", { name: "Save" }).click();
      await expect(address).toHaveAttribute("aria-invalid", "true");
      expect(sentTo(platform, "/my-alert-address", "PUT")).toHaveLength(0);
      await address.fill("carla.mendez@acme.example");
      await card(page).getByRole("button", { name: "Save" }).click();
      await expect(card(page).getByRole("status")).toHaveText(
        "Saved. Alerts sent to you go to carla.mendez@acme.example.",
      );
      await card(page).getByRole("button", { name: "Remove" }).click();
      await expect(card(page).getByRole("status")).toHaveText(
        "Removed. Channels that list you skip you until you set one.",
      );
      await expect(address).toHaveValue("");
      await expect(card(page)).toContainText(
        "Until you set one, channels that list you skip you.",
      );
      const saves = sentTo(platform, "/my-alert-address", "PUT");
      expect(saves.map((request) => [request.path, request.body])).toEqual([
        [
          "/studio/api/api/v1/tenants/tenant/my-alert-address",
          { address: "carla.mendez@acme.example" },
        ],
        [
          "/studio/api/api/v1/tenants/tenant/my-alert-address",
          { address: null },
        ],
      ]);
      // The operation is about the caller: no person is named in the address.
      for (const request of saves)
        expect(request.path).not.toContain(fixture.ME);
    });
  });
}

test("without an address the card says channels skip you", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await alertsPlatform(page, {
    capabilities: ["catalog.read"],
    path: "/settings/preferences",
    answers: { address: {} },
  });
  await expect(card(page).getByLabel("Address")).toHaveValue("");
  await expect(card(page)).toContainText(
    "Until you set one, channels that list you skip you.",
  );
  await expect(card(page).getByRole("button", { name: "Remove" })).toHaveCount(
    0,
  );
  await shot(page, "alert-address-empty-1440");
});
