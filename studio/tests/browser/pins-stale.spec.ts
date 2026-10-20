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
// Pinned output that may be out of date: the tile badge and the banner after
// an upstream change, Keep pinned, P on the canvas with one Undo, and P's and
// menu's words for a step without output.
import { expect, test, type Page } from "@playwright/test";
import { applySource } from "./owned-actions";
import { fitCanvas, sizes, type StepDetailsPage } from "./step-details-po";
import { box, clippedText, expectAligned } from "./step-geometry";
import { command, sourceText } from "./support";
import { banner, openTestDataFixture, outputPane } from "./test-data-po";

const UPSTREAM = "Pinned test data, out of date: what this step reads changed";
/** The tooltip of a step's tile, which names its badge. */
const tileTitle = (page: Page, id: string) =>
  page
    .locator(`[data-step="${id}"]`)
    .first()
    .evaluate(
      (element) =>
        (element.matches("[title]")
          ? element
          : element.querySelector("button[title]")
        )?.getAttribute("title") ?? "",
    );
const badgeOf = (page: Page, id: string) =>
  page.locator(`[data-step="${id}"] .tile-badge`).first();

async function pin(details: StepDetailsPage, id: string) {
  await details.open(id);
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "Generate sample" }).click();
  await expect(banner(pane)).toBeVisible();
  await details.close();
}
/** score reads another field of lookup's output: notify, which reads score, changes upstream. */
async function changeScore(page: Page) {
  const source = await sourceText(page);
  const changed = source.replace(
    "/steps/lookup/output/tier",
    "/steps/lookup/output/customerId",
  );
  expect(changed).not.toBe(source);
  await applySource(page, changed);
}

for (const size of sizes)
  test.describe(`stale pins at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("changing an upstream mapping marks the pin out of date, and restoring it clears the mark", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      await pin(details, "notify");
      await expect(badgeOf(page, "notify")).toHaveAttribute(
        "data-badge",
        "pinned",
      );
      expect(await tileTitle(page, "notify")).toContain("Pinned test data");
      await changeScore(page);
      await expect(badgeOf(page, "notify")).toHaveAttribute(
        "data-badge",
        "pinned-stale",
      );
      expect(await tileTitle(page, "notify")).toContain(UPSTREAM);
      await details.open("notify");
      let pane = await outputPane(details);
      await expect(banner(pane)).toContainText(
        "Pinned output may be out of date: earlier steps changed.",
      );
      await expect(
        banner(pane).getByRole("button", { name: "Keep pinned" }),
      ).toBeVisible();
      await expect(
        banner(pane).getByRole("button", { name: "Unpin" }),
      ).toBeVisible();
      await details.close();
      await command(page, "Undo");
      await expect(badgeOf(page, "notify")).toHaveAttribute(
        "data-badge",
        "pinned",
      );
      await details.open("notify");
      pane = await outputPane(details);
      await expect(banner(pane)).toContainText(
        "This data is pinned for test runs.",
      );
    });

    test("Keep pinned makes it current again", async ({ page }) => {
      const details = await openTestDataFixture(page);
      await pin(details, "notify");
      await changeScore(page);
      await details.open("notify");
      const pane = await outputPane(details);
      await banner(pane).getByRole("button", { name: "Keep pinned" }).click();
      await expect(banner(pane)).toContainText(
        "This data is pinned for test runs.",
      );
      await details.close();
      await expect(badgeOf(page, "notify")).toHaveAttribute(
        "data-badge",
        "pinned",
      );
    });

    test("P on the canvas unpins the selected steps with one Undo", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      await pin(details, "lookup");
      await pin(details, "notify");
      await fitCanvas(page);
      await page.locator('[data-step="lookup"]').first().click();
      await page
        .locator('[data-step="notify"]')
        .first()
        .click({ modifiers: ["Shift"] });
      await page.keyboard.press("p");
      await expect(badgeOf(page, "lookup")).toHaveCount(0);
      await expect(badgeOf(page, "notify")).toHaveCount(0);
      const toast = page
        .locator(".toast")
        .filter({ hasText: "Unpinned 2 steps." });
      await expect(toast).toHaveCount(1);
      await toast.getByRole("button", { name: "Undo" }).click();
      await expect(badgeOf(page, "lookup")).toHaveAttribute(
        "data-badge",
        "pinned",
      );
      await expect(badgeOf(page, "notify")).toHaveAttribute(
        "data-badge",
        "pinned",
      );
    });

    test("P on a step without output says how to pin it, and the menu agrees", async ({
      page,
    }) => {
      await openTestDataFixture(page);
      await fitCanvas(page);
      await page.locator('[data-step="lookup"]').first().click();
      await page.keyboard.press("p");
      await expect(
        page.locator(".toast").filter({
          hasText: "Set test output or generate a sample to pin it.",
        }),
      ).toHaveCount(1);
      await expect(badgeOf(page, "lookup")).toHaveCount(0);
      // The step's menu says the same, and offers nothing to run.
      await page.keyboard.press("Escape");
      await page
        .locator('[data-step="lookup"]')
        .first()
        .click({ button: "right" });
      const item = page.getByRole("menuitem", { name: /^Pin output/ });
      await expect(item).toHaveAttribute("aria-disabled", "true");
      await expect(item).toContainText(
        "Set test output or generate a sample to pin it.",
      );
      await expect(page.getByRole("menuitem", { name: /^Unpin/ })).toHaveCount(
        0,
      );
    });
  });

for (const size of [
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "600x500", width: 600, height: 500 },
  { tag: "360x640", width: 360, height: 640 },
])
  test.describe(`pinned banner geometry at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test("the stale banner fits the pane, its icon on the first line, its links in the text", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      await pin(details, "notify");
      await changeScore(page);
      await details.open("notify");
      const pane = await outputPane(details);
      const shown = banner(pane);
      await expect(shown).toHaveAttribute("data-tone", "warning");
      const outer = await box(shown);
      const content = await box(pane.locator(".pane-content"));
      expect(outer.x).toBeGreaterThanOrEqual(content.x - 1);
      expect(outer.right).toBeLessThanOrEqual(content.right + 1);
      const icon = await box(shown.locator("weave-icon").first());
      const text = await box(shown.locator("p").first());
      // The icon sits on the first line of text, never below it.
      expect(icon.y).toBeGreaterThanOrEqual(text.y - 1);
      expect(icon.y).toBeLessThan(text.y + 12);
      expect(icon.right).toBeLessThan(text.x);
      for (const name of ["Keep pinned", "Unpin"]) {
        const link = await box(shown.getByRole("button", { name }));
        // Its target may reach into the padding; never out of the banner.
        expect(link.x).toBeGreaterThanOrEqual(outer.x);
        expect(link.right).toBeLessThanOrEqual(outer.right);
      }
      // The Pinned tag and the count share the sub-row with Edit and Pin.
      const tag = await box(pane.locator(".pane-sub .pill"));
      const edit = await box(pane.locator(".pane-edit"));
      const pinButton = await box(pane.locator(".pane-pin"));
      expectAligned([tag.middle, edit.middle, pinButton.middle], "sub-row", 2);
      expect(pinButton.right).toBeLessThanOrEqual(edit.x);
      expect(await clippedText(details.dialog)).toEqual([]);
    });
  });

for (const size of [
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "600x500", width: 600, height: 500 },
  { tag: "360x640", width: 360, height: 640 },
])
  test.describe(`pin focus at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test("Generate sample, Unpin and Keep pinned leave focus on the pin toggle", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      await details.open("notify");
      const pane = await outputPane(details);
      const toggle = pane.locator(".pane-pin");
      await pane.getByRole("button", { name: "Generate sample" }).focus();
      await page.keyboard.press("Enter");
      await expect(banner(pane)).toBeVisible();
      await expect(toggle).toBeFocused();
      await banner(pane).getByRole("button", { name: "Unpin" }).focus();
      await page.keyboard.press("Enter");
      await expect(banner(pane)).toHaveCount(0);
      await expect(toggle).toBeFocused();
      // Out of date, then Keep pinned from the banner.
      await pane.getByRole("button", { name: "Generate sample" }).focus();
      await page.keyboard.press("Enter");
      await expect(banner(pane)).toBeVisible();
      await details.close();
      await changeScore(page);
      await details.open("notify");
      const again = await outputPane(details);
      await banner(again).getByRole("button", { name: "Keep pinned" }).focus();
      await page.keyboard.press("Enter");
      await expect(banner(again)).toHaveAttribute("data-tone", "info");
      await expect(again.locator(".pane-pin")).toBeFocused();
    });
  });
