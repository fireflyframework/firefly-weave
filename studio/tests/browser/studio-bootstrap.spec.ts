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
import { offline } from "./support";

test("a hard reload recovers its paired session after local admission rejects two reads", async ({
  page,
}) => {
  await offline(page);
  await expect(page.locator(".shell")).toBeVisible();
  let attempts = 0;
  await page.route("**/studio/session", async (route) => {
    attempts++;
    if (attempts <= 2) {
      await route.fulfill({
        status: 429,
        headers: { "Retry-After": "0.25" },
        json: {
          code: "WV-STUDIO-BUSY",
          message: "Studio is busy; wait for the current requests",
        },
      });
    } else await route.fallback();
  });
  await page.reload();
  await expect(page.locator(".shell")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "New workflow", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Pairing code", { exact: true })).toHaveCount(0);
  expect(attempts).toBe(3);
});
