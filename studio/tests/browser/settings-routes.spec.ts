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
import { expect, test } from "@playwright/test";
import { offline } from "./support";

test("Settings tabs have their own addresses, and Preferences turns on the new editor", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await offline(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page).toHaveURL(/\/settings\/platforms$/);
  await expect(page.getByRole("tab", { name: "Platforms" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(
    page.getByRole("tab", { name: "People and access" }),
  ).toHaveCount(0);
  await page.getByRole("tab", { name: "Preferences" }).click();
  await expect(page).toHaveURL(/\/settings\/preferences$/);
  const toggle = page.getByRole("switch", { name: "Try the new editor" });
  await expect(toggle).not.toBeChecked();
  await toggle.check();
  expect(
    await page.evaluate(() => localStorage.getItem("ui:weave.editorNext")),
  ).toBe("true");
  await page.goBack();
  await expect(page).toHaveURL(/\/settings\/platforms$/);
  await expect(page.getByRole("tab", { name: "Platforms" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await page.goto("/settings/preferences");
  await expect(
    page.getByRole("switch", { name: "Try the new editor" }),
  ).toBeChecked();
});

test("/settings opens Platforms, and the tabs follow the arrow keys", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await offline(page);
  await page.goto("/settings");
  await expect(page).toHaveURL(/\/settings\/platforms$/);
  await page.getByRole("tab", { name: "Platforms" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Preferences" })).toBeFocused();
  await expect(page).toHaveURL(/\/settings\/preferences$/);
  await page.keyboard.press("Home");
  await expect(page.getByRole("tab", { name: "Platforms" })).toBeFocused();
  await expect(page).toHaveURL(/\/settings\/platforms$/);
});
