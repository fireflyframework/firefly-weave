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
// The new editor is what a person gets: on a first visit, after a reload,
// and until they turn it off; ?editor=classic opens the classic designer for
// one page without changing their choice.
import { expect, test } from "@playwright/test";
import { newWorkflow, offline } from "./support";

test.use({ viewport: { width: 1440, height: 900 } });
const choice = (page: import("@playwright/test").Page) =>
  page.evaluate(() => localStorage.getItem("ui:weave.editorNext"));

test("a first visit opens the new editor", async ({ page }) => {
  await offline(page);
  expect(await choice(page)).toBeNull();
  await newWorkflow(page);
  await expect(page.locator(".canvas-v2")).toBeVisible();
  await expect(page.locator(".palette-step")).toHaveCount(0);
});

test("turning it off opens the classic designer, and the choice is kept", async ({
  page,
}) => {
  await offline(page);
  await page.goto("/settings/preferences");
  await page.getByRole("switch", { name: "Use the new editor" }).uncheck();
  expect(await choice(page)).toBe("false");
  await page.reload();
  await page.goto("/");
  await newWorkflow(page);
  await expect(page.locator(".palette-step").first()).toBeAttached();
  await expect(page.locator(".canvas-v2")).toHaveCount(0);
});

test("?editor=classic opens the classic designer without changing the choice", async ({
  page,
}) => {
  await offline(page);
  await page.goto("/?editor=classic");
  await newWorkflow(page);
  await expect(page.locator(".palette-step").first()).toBeAttached();
  await expect(page.locator(".canvas-v2")).toHaveCount(0);
  expect(await choice(page)).toBeNull();
});
