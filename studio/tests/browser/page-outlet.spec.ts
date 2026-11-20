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
// The shell's pages load the first time they open, outside the first page,
// through one outlet. A page whose code fails to load leaves its area empty
// and goes to Angular's error handler, as a deferred block without @error
// did (the logged text is the import's own error now, not NG0750), and the
// shell keeps opening other pages. Another page starts at the top of the
// area, and replaces the page shown.
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

test("another page opens at the top with its heading in view, wherever the last one was scrolled", async ({
  page,
}) => {
  // One area holds every page, so the scroll of the last one must not carry.
  await page.setViewportSize({ width: 1280, height: 420 });
  await connected(page);
  await page.goto("/operate/runs");
  const nav = page.getByRole("navigation", { name: "Main navigation" });
  const area = page.locator(".page-content");
  const open = async (name: string) => {
    await nav.getByRole("button", { name, exact: true }).click();
    await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
  };
  // Both pages loaded, then Runs scrolled to its end.
  await expect(
    page.getByRole("heading", { name: "Runs", level: 1 }),
  ).toBeVisible();
  await open("Logs");
  await open("Runs");
  await area.evaluate((element) => element.scrollTo(0, element.scrollHeight));
  expect(await area.evaluate((element) => element.scrollTop)).toBeGreaterThan(
    0,
  );
  await open("Logs");
  expect(await area.evaluate((element) => element.scrollTop)).toBe(0);
  await expect(
    page.getByRole("heading", { name: "Logs", level: 1 }),
  ).toBeInViewport();
});

test("opening another page replaces the one shown, and its reads stop", async ({
  page,
}) => {
  await page.clock.install();
  await connected(page);
  const reads: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.endsWith("/run-summaries"))
      reads.push(request.url());
  });
  await page.goto("/operate/runs");
  await expect(page.locator("weave-runs-page")).toHaveCount(1);
  const nav = page.getByRole("navigation", { name: "Main navigation" });
  const open = async (name: string, element: string) => {
    await nav.getByRole("button", { name, exact: true }).click();
    await expect(page.locator(element)).toHaveCount(1);
  };
  // Both pages' code loaded first, so the outlet swaps one shown page for
  // another rather than waiting for code in between.
  await open("Logs", "weave-logs-page");
  await open("Runs", "weave-runs-page");
  await open("Logs", "weave-logs-page");
  await expect(page.locator("weave-runs-page")).toHaveCount(0);
  // Runs reads its list every 10 s while it's shown: none after it's gone.
  const before = reads.length;
  await page.clock.runFor(60_000);
  expect(reads.length).toBe(before);
});
