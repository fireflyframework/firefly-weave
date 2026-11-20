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
// The shell's pages load the first time they open, outside the first page.
// A page whose code fails to load leaves its area empty and reports the
// error, as a deferred block did, and the shell keeps opening other pages.
import { expect, test } from "@playwright/test";
import { connected } from "./support";

test("a page whose code fails to load leaves its area empty, and other pages still open", async ({
  page,
}) => {
  await connected(page);
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  let blocked = 0;
  // The Logs page's own code, wherever the build put it.
  await page.route(
    (url) => url.pathname.endsWith(".js") && !url.pathname.endsWith("/main.js"),
    async (route) => {
      const response = await route.fetch();
      const body = await response.text();
      if (/selectors:\s*\[\[\s*["'`]weave-logs-page["'`]/.test(body)) {
        blocked++;
        return route.abort();
      }
      return route.fulfill({ response, body });
    },
  );
  await page.goto("/home");
  const nav = page.getByRole("navigation", { name: "Main navigation" });
  await nav.getByRole("button", { name: "Logs", exact: true }).click();
  await expect(page).toHaveURL(/\/operate\/logs$/);
  await expect.poll(() => blocked).toBeGreaterThan(0);
  await expect
    .poll(() => errors.join("\n"))
    .toContain("Failed to fetch dynamically imported module");
  await expect(page.locator(".page-content")).toBeEmpty();
  await nav.getByRole("button", { name: "Runs", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Runs", level: 1 }),
  ).toBeVisible();
});
