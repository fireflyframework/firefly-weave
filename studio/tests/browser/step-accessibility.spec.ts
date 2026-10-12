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
// Step details for keyboard and assistive technology: every control has a
// name, a visible focus ring and a 24 px target; toggles, Add option and
// rows are named; drops are announced.
import { expect, test } from "@playwright/test";
import { openStepFixture, sizes } from "./step-details-po";
import { walkFocus } from "./step-focus";

for (const size of sizes)
  test.describe(`step details accessibility at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    const sheet = size.width < 768;

    test("every control in step details has a name, a visible focus ring and a 24 px target", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("summarize");
      expect(await walkFocus(page, details.dialog)).toBeGreaterThan(10);
    });

    test("toggles, Add option and rows have their names, and drops are announced", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("summarize");
      const mode = details
        .field("prompt")
        .getByRole("radiogroup", { name: "Value mode for Prompt" });
      await expect(mode.getByRole("radio", { name: "Fixed" })).toHaveAttribute(
        "aria-checked",
        "true",
      );
      await expect(mode.getByRole("radio", { name: "Mapped" })).toHaveAttribute(
        "aria-checked",
        "false",
      );
      const add = details.dialog.getByRole("button", { name: "Add option" });
      await expect(add).toHaveAttribute("aria-haspopup", "menu");
      await expect(add).toHaveAttribute("aria-expanded", "false");
      await add.click();
      await expect(add).toHaveAttribute("aria-expanded", "true");
      await expect(
        page.getByRole("menu", { name: "Add option" }),
      ).toBeVisible();
      await page.keyboard.press("Escape");
      await details.close();
      await details.openWithKeyboard("check-customer");
      await expect(
        details.field("value").getByRole("group", { name: "Row 1 of 2" }),
      ).toBeVisible();
      if (sheet)
        await details.dialog
          .getByRole("tablist", { name: "Step details panes" })
          .getByRole("tab", { name: "Input" })
          .click();
      await details.dialog
        .locator(".sd-input")
        .getByRole("listitem", { name: /^email, / })
        .focus();
      await page.keyboard.press("Enter");
      await page
        .getByRole("menu", { name: "Map email to" })
        .getByRole("menuitem", { name: "Fields", exact: true })
        .click();
      await expect(details.dialog.locator("[aria-live=polite]")).toHaveText(
        "Mapped Input › email to Fields",
      );
    });
  });
