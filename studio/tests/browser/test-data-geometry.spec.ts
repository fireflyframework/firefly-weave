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
// Test data's surfaces measured at five sizes: the Output sub-row's tag,
// count and buttons, the pinned banner, the output editor, the question
// before closing, and values in the Input pane. Desktop with a fine pointer:
// icon buttons and rows 32 px; touch and below 768 px: 44 px.
import { expect, test, type Page } from "@playwright/test";
import { setTestInput } from "./formula-po";
import { type StepDetailsPage } from "./step-details-po";
import {
  box,
  boxes,
  clippedText,
  density,
  expectAligned,
  intersects,
} from "./step-geometry";
import { banner, openTestDataFixture, outputPane } from "./test-data-po";

const sizes = [
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "1280x720", width: 1280, height: 720 },
  { tag: "768x1024", width: 768, height: 1024 },
  { tag: "600x500", width: 600, height: 500 },
  { tag: "360x640", width: 360, height: 640 },
];
/** Icon buttons and rows: 32 px on desktop, 44 px on touch. */
const targetOf = async (page: Page) => ((await density(page)).touch ? 44 : 32);
async function pinned(details: StepDetailsPage) {
  await details.open("lookup");
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "Generate sample" }).click();
  await expect(banner(pane)).toBeVisible();
  return pane;
}
async function noSidewaysScroll(page: Page) {
  const wide = await page.evaluate(
    () => document.scrollingElement!.scrollWidth - innerWidth,
  );
  expect(wide, "sideways scroll").toBeLessThanOrEqual(0);
}

for (const size of sizes)
  test.describe(`test data geometry at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("the Output sub-row keeps its tag, count and buttons on one line, on the pane's right edge", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      const pane = await pinned(details);
      const target = await targetOf(page);
      const tag = await box(pane.locator(".pane-sub .pill"));
      const count = await box(pane.locator(".pane-count"));
      const edit = await box(pane.locator(".pane-edit"));
      const pin = await box(pane.locator(".pane-pin"));
      for (const button of [edit, pin])
        expect(Math.round(button.height)).toBe(target);
      expectAligned(
        [tag.middle, count.middle, edit.middle, pin.middle],
        "sub-row",
        2,
      );
      // One right edge per pane: the pin ends where the header's search ends.
      const search = await box(pane.locator(".pane-search-toggle"));
      expectAligned([pin.right, search.right], "pane right edge");
      const content = await box(pane.locator(".pane-content"));
      expect(pin.right).toBeLessThanOrEqual(content.right + 1);
      for (const [a, b] of [
        [tag, count],
        [count, edit],
        [edit, pin],
      ])
        expect(intersects(a, b)).toBe(false);
      expect(await clippedText(details.dialog)).toEqual([]);
      if (size.width < 768) await noSidewaysScroll(page);
    });

    test("the banner sits under the sub-row, inside the pane's padding, its links on the text", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      const pane = await pinned(details);
      const shown = await box(banner(pane));
      const sub = await box(pane.locator(".pane-sub"));
      const search = await box(pane.locator(".pane-search-toggle"));
      const tag = await box(pane.locator(".pane-sub .pill"));
      expectAligned([shown.x, tag.x], "banner left edge");
      expectAligned([shown.right, search.right], "banner right edge");
      expect(shown.y).toBeGreaterThanOrEqual(sub.bottom + 7);
      expect(shown.y).toBeLessThanOrEqual(sub.bottom + 17);
      const text = await box(banner(pane).locator("p").first());
      const link = await box(
        banner(pane).getByRole("button", { name: "Unpin" }),
      );
      expect(link.middle).toBeGreaterThan(text.y);
      expect(link.middle).toBeLessThan(text.bottom);
      expect(link.right).toBeLessThanOrEqual(shown.right);
      expect(await clippedText(details.dialog)).toEqual([]);
    });

    test("the output editor fills the pane above its action row", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      await details.open("lookup");
      const pane = await outputPane(details);
      await pane.getByRole("button", { name: "Edit output" }).click();
      const editor = pane.getByRole("textbox", { name: "Output of lookup" });
      await editor.click();
      await page.keyboard.insertText("x");
      await pane.getByRole("button", { name: "Save" }).click();
      const error = await box(pane.getByRole("alert"));
      const target = await targetOf(page);
      const field = await box(pane.locator(".output-editor-field"));
      const cancel = await box(pane.getByRole("button", { name: "Cancel" }));
      const save = await box(pane.getByRole("button", { name: "Save" }));
      const search = await box(pane.locator(".pane-search-toggle"));
      const sub = await box(pane.locator(".pane-sub"));
      for (const button of [cancel, save])
        expect(Math.round(button.height)).toBe(target);
      expectAligned([cancel.middle, save.middle], "Cancel and Save", 1);
      expectAligned([save.right, field.right, search.right], "right edge");
      expect(field.y).toBeGreaterThanOrEqual(sub.bottom + 7);
      expect(field.y).toBeLessThanOrEqual(sub.bottom + 17);
      // The error between the editor and the row, overlapping neither, 8 px apart.
      expect(error.y).toBeGreaterThanOrEqual(field.bottom + 7);
      expect(Math.min(cancel.y, save.y)).toBeGreaterThanOrEqual(error.bottom);
      const content = await box(pane.locator(".sd-pane-body"));
      expect(save.bottom).toBeLessThanOrEqual(content.bottom);
      expect(await clippedText(details.dialog)).toEqual([]);
      if (size.width < 768) await noSidewaysScroll(page);
    });

    test("the question before closing fits the window, its buttons in one row or one column", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      await details.open("lookup");
      const pane = await outputPane(details);
      await pane.getByRole("button", { name: "Edit output" }).click();
      await pane.getByRole("textbox", { name: "Output of lookup" }).click();
      await page.keyboard.insertText(" ");
      await page.keyboard.press("Escape");
      await page.keyboard.press("Escape");
      const guard = page.getByRole("alertdialog", {
        name: "Save the output changes before closing?",
      });
      const panel = await box(guard);
      const margin = size.width < 768 ? 16 : 0;
      expect(panel.x).toBeGreaterThanOrEqual(margin - 1);
      expect(panel.right).toBeLessThanOrEqual(size.width - margin + 1);
      expect(panel.bottom).toBeLessThanOrEqual(size.height);
      const buttons = await boxes(guard.getByRole("button"));
      expect(buttons).toHaveLength(3);
      const oneRow =
        Math.max(...buttons.map((b) => b.middle)) -
          Math.min(...buttons.map((b) => b.middle)) <=
        1;
      if (oneRow) expect(size.width).toBeGreaterThanOrEqual(600);
      else {
        // Stacked: one column of equal widths.
        expectAligned(
          buttons.map((b) => b.width),
          "stacked widths",
        );
        expectAligned(
          buttons.map((b) => b.x),
          "stacked left edge",
        );
      }
      if (size.width < 400) expect(oneRow).toBe(false);
      expect(await clippedText(guard)).toEqual([]);
    });

    test("Input values stay on their row, right-aligned and cut", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      await setTestInput(page, details, {
        "Customer ID": `c-${"1042".repeat(20)}`,
        Amount: "1500",
      });
      await details.open("lookup");
      if (size.width < 1280) {
        const tab = details.dialog
          .getByRole("tablist", { name: /^(Step details panes|Data)$/ })
          .getByRole("tab", { name: "Input" });
        if (await tab.count()) await tab.first().click();
      }
      const input = details.dialog.locator("weave-input-pane");
      const rows = input.locator(".data-row").filter({
        has: page.locator(".data-sample"),
      });
      await expect(rows).toHaveCount(2);
      // The Input tab has just been chosen: measure once its rows show.
      for (const row of await rows.all()) await expect(row).toBeVisible();
      const target = await targetOf(page);
      for (const row of await rows.all()) {
        const outer = await box(row);
        const sample = await box(row.locator(".data-sample"));
        expect(Math.round(outer.height)).toBe(target);
        expect(sample.right).toBeLessThanOrEqual(outer.right);
        expectAligned([sample.middle, outer.middle], "value on its row", 1);
      }
      // The long value is cut with an ellipsis, inside its row.
      await expect(rows.first().locator(".data-sample")).toHaveText(/…"?$/);
      const amount = rows.filter({ hasText: "amount" });
      const amountRow = await box(amount);
      const amountValue = await box(amount.locator(".data-sample"));
      expectAligned(
        [amountValue.right, amountRow.right - 8],
        "value right edge",
      );
      if (size.width < 768) await noSidewaysScroll(page);
    });
  });
