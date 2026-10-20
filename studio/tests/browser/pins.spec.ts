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
// Pin output in the Output pane: the toggle in its header, the banner that
// says what a pin does, Unpin with Undo, P outside fields, kinds that compute
// their output, and a pin that survives a reload, follows a rename and goes
// with its step.
import { expect, test, type Page } from "@playwright/test";
import { sizes } from "./step-details-po";
import {
  banner,
  openTestDataFixture,
  outputPane,
  pinButton,
  routeDrafts,
} from "./test-data-po";

const toast = (page: Page, text: string) =>
  page.locator(".toast").filter({ hasText: text });

for (const size of sizes)
  test.describe(`pins at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("Pin output sits in the Output pane header and the banner says what a pin does", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      await details.open("lookup");
      const pane = await outputPane(details);
      await expect(pinButton(pane)).toHaveAttribute("aria-pressed", "false");
      await expect(pinButton(pane)).toHaveAttribute(
        "title",
        "Set test output or generate a sample to pin it.",
      );
      await pane.getByRole("button", { name: "Generate sample" }).click();
      await expect(
        pane.getByRole("button", { name: "Unpin output" }),
      ).toHaveAttribute("aria-pressed", "true");
      await expect(pane.locator(".pane-sub .pill")).toHaveText("Pinned");
      await expect(banner(pane)).toHaveText(
        "This data is pinned for test runs. Production runs ignore it. Unpin",
      );
      // The sample never holds the secret field.
      await expect(
        pane.locator(".data-key", { hasText: /^tier$/ }),
      ).toBeVisible();
    });

    test("Unpin removes the pin, and Undo brings it back", async ({ page }) => {
      const details = await openTestDataFixture(page);
      await details.open("lookup");
      const pane = await outputPane(details);
      await pane.getByRole("button", { name: "Generate sample" }).click();
      await banner(pane).getByRole("button", { name: "Unpin" }).click();
      await expect(banner(pane)).toHaveCount(0);
      await expect(pane.getByText("No output yet")).toBeVisible();
      const undone = toast(page, "Unpinned lookup.");
      await undone.getByRole("button", { name: "Undo" }).click();
      await expect(banner(pane)).toBeVisible();
    });

    test("P pins only outside fields", async ({ page }) => {
      const details = await openTestDataFixture(page);
      await details.open("lookup");
      const pane = await outputPane(details);
      await pane.getByRole("button", { name: "Generate sample" }).click();
      await expect(banner(pane)).toBeVisible();
      // In a field, P types.
      await pane.getByRole("button", { name: "Search output" }).click();
      const field = pane.getByRole("textbox", { name: "Search output" });
      await expect(field).toBeFocused();
      await page.keyboard.press("p");
      await expect(field).toHaveValue("p");
      await expect(banner(pane)).toBeVisible();
      // Escape closes the search and leaves focus on its button: P pins or unpins.
      await page.keyboard.press("Escape");
      await page.keyboard.press("p");
      const shown = await outputPane(details);
      await expect(banner(shown)).toHaveCount(0);
      await expect(toast(page, "Unpinned lookup.")).toBeVisible();
      await page.keyboard.press("p");
      await expect(
        toast(page, "Set test output or generate a sample to pin it."),
      ).toBeVisible();
    });

    test("a transform says to pin the steps it reads instead", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      await details.open("score");
      const pane = await outputPane(details);
      await expect(pane).toContainText(
        "Output is computed from the input; pin the steps it reads instead.",
      );
      await expect(pinButton(pane)).toHaveCount(0);
    });
  });
