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
// Edit output: the step's pinned output as JSON in the Output pane, Save and
// Cancel, the words for text that isn't JSON or holds a secret, its own
// Undo, and the question before an unsaved edit closes.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { sizes, type StepDetailsPage } from "./step-details-po";
import { box, clippedText, expectAligned, intersects } from "./step-geometry";
import { sourceText } from "./support";
import { banner, openTestDataFixture, outputPane } from "./test-data-po";

const editorOf = (pane: Locator) =>
  pane.getByRole("textbox", { name: "Output of lookup" });
/** Replaces the editor's text as typing would. */
async function replaceText(page: Page, editor: Locator, text: string) {
  await editor.click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.press("Backspace");
  await page.keyboard.insertText(text);
}
async function openEditor(details: StepDetailsPage) {
  await details.open("lookup");
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "Edit output" }).click();
  const editor = editorOf(pane);
  await expect(editor).toBeFocused();
  return { pane, editor };
}
const guard = (page: Page) =>
  page.getByRole("alertdialog", {
    name: "Save the output changes before closing?",
  });

for (const size of sizes)
  test.describe(`output editor at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("Edit output opens a JSON editor in the pane with Cancel, Save and the note", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      const { pane, editor } = await openEditor(details);
      await expect(editor).toHaveText("{}");
      await expect(pane).toContainText(
        "Paste data from an earlier run to reuse it.",
      );
      await expect(pane.getByRole("button", { name: "Cancel" })).toBeVisible();
      await expect(pane.getByRole("button", { name: "Save" })).toBeVisible();
      await pane.getByRole("button", { name: "Cancel" }).click();
      await expect(editor).toHaveCount(0);
    });

    test("Save checks the value and pins it", async ({ page }) => {
      const details = await openTestDataFixture(page);
      const { pane, editor } = await openEditor(details);
      await replaceText(
        page,
        editor,
        '{"customerId": "c-9", "found": true, "tier": "gold"}',
      );
      await pane.getByRole("button", { name: "Save" }).click();
      await expect(editor).toHaveCount(0);
      await expect(banner(pane)).toBeVisible();
      await pane.getByRole("radio", { name: "JSON" }).click();
      await expect(pane.locator(".pane-json")).toContainText('"c-9"');
      // Edit output opens with the pinned value.
      await pane.getByRole("button", { name: "Edit output" }).click();
      await expect(editorOf(pane)).toContainText('"tier": "gold"');
    });

    test("text that isn't JSON says where", async ({ page }) => {
      const details = await openTestDataFixture(page);
      const { pane, editor } = await openEditor(details);
      await replaceText(page, editor, '{"a": }');
      await pane.getByRole("button", { name: "Save" }).click();
      const error = pane.getByRole("alert");
      await expect(error).toHaveText(
        "This is not valid JSON near line 1, column 7. Check for missing quotes, commas or brackets.",
      );
      const described = (await editor.getAttribute("aria-describedby")) ?? "";
      expect(described.split(" ")).toContain(await error.getAttribute("id"));
      await expect(banner(pane)).toHaveCount(0);
    });

    test("a secret in Edit output is refused and nothing is kept", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      const { pane, editor } = await openEditor(details);
      await replaceText(page, editor, '{"token": "abc"}');
      await pane.getByRole("button", { name: "Save" }).click();
      await expect(pane.getByRole("alert")).toContainText("secret");
      await expect(banner(pane)).toHaveCount(0);
      await page.waitForTimeout(1000);
      await page.reload();
      expect(
        await page.evaluate(() => JSON.stringify(localStorage)),
      ).not.toContain("abc");
    });

    test("closing with an unsaved change asks first, and Discard leaves the pin as it was", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      await details.open("lookup");
      let pane = await outputPane(details);
      await pane.getByRole("button", { name: "Generate sample" }).click();
      await expect(banner(pane)).toBeVisible();
      await pane.getByRole("button", { name: "Edit output" }).click();
      await replaceText(page, editorOf(pane), '{"customerId": "changed"}');
      // The first Escape leaves the editor; the second closes step details, which asks.
      await page.keyboard.press("Escape");
      await page.keyboard.press("Escape");
      await expect(guard(page)).toBeVisible();
      await expect(
        guard(page).getByRole("button", { name: "Save" }),
      ).toBeFocused();
      await guard(page).getByRole("button", { name: "Discard" }).click();
      await expect(details.dialog).toHaveCount(0);
      await details.open("lookup");
      pane = await outputPane(details);
      await pane.getByRole("radio", { name: "JSON" }).click();
      await expect(pane.locator(".pane-json")).not.toContainText("changed");
      // Keep editing: the editor stays with the changed text.
      await pane.getByRole("button", { name: "Edit output" }).click();
      await replaceText(page, editorOf(pane), '{"customerId": "kept"}');
      await page.keyboard.press("Escape");
      await page.keyboard.press("Escape");
      await guard(page).getByRole("button", { name: "Keep editing" }).click();
      await expect(guard(page)).toHaveCount(0);
      await expect(editorOf(pane)).toContainText("kept");
      // Focus is back in the editor. Save: pinned, and step details closes.
      await expect(editorOf(pane)).toBeFocused();
      await page.keyboard.press("Escape");
      await page.keyboard.press("Escape");
      await expect(guard(page)).toBeVisible();
      await guard(page).getByRole("button", { name: "Save" }).click();
      await expect(details.dialog).toHaveCount(0);
      await details.open("lookup");
      pane = await outputPane(details);
      await pane.getByRole("radio", { name: "JSON" }).click();
      await expect(pane.locator(".pane-json")).toContainText('"kept"');
    });

    test("Escape and Undo stay inside the output editor", async ({ page }) => {
      const details = await openTestDataFixture(page);
      const before = await sourceText(page);
      const { pane, editor } = await openEditor(details);
      await page.keyboard.insertText('{"customerId": "x"}');
      await page.waitForTimeout(700);
      await page.keyboard.press("ControlOrMeta+z");
      await expect(editor).toHaveText("{}");
      await page.keyboard.press("ControlOrMeta+Shift+z");
      await expect(editor).toContainText('"x"');
      await page.keyboard.press("ControlOrMeta+z");
      await expect(editor).toHaveText("{}");
      // Nothing changed: Escape closes the editor without asking.
      await page.keyboard.press("Escape");
      await expect(editor).toHaveCount(0);
      await expect(guard(page)).toHaveCount(0);
      await expect(details.dialog).toBeVisible();
      await expect(pane).toBeVisible();
      // The workflow's own Undo never ran.
      await details.close();
      expect(await sourceText(page)).toBe(before);
    });
  });

for (const size of [
  { tag: "1440x900", width: 1440, height: 900, row: 32 },
  { tag: "600x500", width: 600, height: 500, row: 44 },
  { tag: "360x640", width: 360, height: 640, row: 44 },
])
  test.describe(`output editor geometry at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test("the editor fills the pane and its actions sit on one line", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      const { pane, editor } = await openEditor(details);
      const field = await box(pane.locator(".output-editor-field"));
      const content = await box(pane.locator(".pane-content"));
      expectAligned([field.x, content.x], "editor left edge");
      expectAligned([field.right, content.right], "editor right edge");
      expect(field.height).toBeGreaterThanOrEqual(120);
      const cancel = await box(pane.getByRole("button", { name: "Cancel" }));
      const save = await box(pane.getByRole("button", { name: "Save" }));
      for (const button of [cancel, save])
        expect(Math.round(button.height)).toBe(size.row);
      expectAligned([cancel.middle, save.middle], "Cancel and Save", 1);
      expectAligned([save.right, content.right], "Save right edge");
      expect(cancel.right).toBeLessThanOrEqual(save.x - 4);
      expect(intersects(field, cancel)).toBe(false);
      const note = await box(pane.locator(".output-editor-note"));
      expect(note.x).toBeGreaterThanOrEqual(content.x - 1);
      expect(intersects(note, cancel)).toBe(false);
      await expect(editor).toBeVisible();
      expect(await clippedText(details.dialog)).toEqual([]);
    });
    test("the question before closing fits the window", async ({ page }) => {
      const details = await openTestDataFixture(page);
      const { editor } = await openEditor(details);
      await page.keyboard.insertText('{"a": 1}');
      await editor.press("Escape");
      await page.keyboard.press("Escape");
      const panel = await box(guard(page));
      expect(panel.x).toBeGreaterThanOrEqual(0);
      expect(panel.right).toBeLessThanOrEqual(size.width);
      expect(panel.bottom).toBeLessThanOrEqual(size.height);
      const buttons = guard(page).getByRole("button");
      await expect(buttons).toHaveText(["Keep editing", "Discard", "Save"]);
      for (const button of await buttons.all())
        expect(Math.round((await box(button)).height)).toBe(
          size.width < 768 ? 44 : 36,
        );
      expect(await clippedText(guard(page))).toEqual([]);
    });
  });
