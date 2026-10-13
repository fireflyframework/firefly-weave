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
// The formula editor in step details: typing with suggestions, previews,
// errors that don't apply, the canonical form, pills and keys.
import { expect, test } from "@playwright/test";
import { parse } from "yaml";
import {
  fieldLine,
  formulaBox,
  formulaText,
  formulasFixture,
  replaceFormula,
  setTestInput,
  suggestions,
} from "./formula-po";
import { box } from "./step-geometry";
import {
  openStepFixture,
  sizes,
  withLanguageFeatures,
} from "./step-details-po";
import { sourceText } from "./support";

const stepValue = async (page: import("@playwright/test").Page, id: string) =>
  parse(await sourceText(page)).spec.steps.find(
    (step: { id: string }) => step.id === id,
  ).value;

for (const size of sizes)
  test.describe(`the formula editor at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test.beforeEach(async ({ page }) => {
      await withLanguageFeatures(page);
    });

    test("types a formula with suggestions, previews true and false, and shows the canonical form on blur", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await setTestInput(page, details, {
        "Customer ID": "c-1042",
        "Email (optional)": "ada@example.com",
        Amount: "1500",
      });
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.amount > 1000",
      );
      await expect(fieldLine(details, "value")).toHaveText("= true");
      await formulaText(field).click();
      await page.keyboard.press("ControlOrMeta+a");
      await page.keyboard.type("input.am", { delay: 20 });
      const list = suggestions(page);
      await expect(list.getByRole("option", { name: /amount/ })).toBeVisible();
      await expect(list.getByRole("option", { name: /amount/ })).toContainText(
        "Number",
      );
      await page.keyboard.press("Enter");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.amount",
      );
      await page.keyboard.type(" > 1000 && exists(input.email", { delay: 10 });
      await page.keyboard.press("Escape");
      await page.keyboard.type(")");
      await expect(fieldLine(details, "value")).toHaveText("= true");
      await page.keyboard.press("Tab");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.amount > 1000 and exists(input.email)",
      );
      await replaceFormula(page, field, "input.amount > 2000");
      await expect(fieldLine(details, "value")).toHaveText("= false");
    });

    test("a formula that doesn't parse shows Not applied, keeps the workflow, and Escape restores it", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      // The Source tab is read before step details open over it.
      const before = await stepValue(page, "flag");
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await replaceFormula(page, field, "input.amount >");
      await expect(field.locator(".param-line")).toContainText("Not applied");
      await expect(field.locator(".param-line")).toContainText(
        "The formula ends too early. Add a value after `>`.",
      );
      await expect(formulaText(field)).toHaveAttribute("aria-invalid", "true");
      await details.close();
      expect(await stepValue(page, "flag")).toEqual(before);
      await details.openWithKeyboard("flag");
      await expect(formulaBox(details.field("value"))).toHaveAttribute(
        "data-text",
        "input.amount >",
      );
      await formulaText(details.field("value")).click();
      await page.keyboard.press("Escape");
      await expect(formulaBox(details.field("value"))).toHaveAttribute(
        "data-text",
        "input.amount > 1000",
      );
      await expect(
        details.field("value").locator(".param-line"),
      ).not.toContainText("Not applied");
    });

    test("pasting {{ input.amount }} stores a reference", async ({ page }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      await replaceFormula(page, details.field("value"), "{{ input.amount }}");
      await page.keyboard.press("Tab");
      await details.close();
      expect(await stepValue(page, "flag")).toEqual({ ref: "/input/amount" });
    });

    test("focusing and leaving a formula never rewrites the workflow", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      const before = await sourceText(page);
      await details.openWithKeyboard("flag");
      await formulaText(details.field("value")).click();
      await page.keyboard.press("Tab");
      await details.close();
      expect(await sourceText(page)).toBe(before);
    });

    test("an extended formula stays as it is", async ({ page }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("opaque");
      // The transform has one field; which one depends on how the value reads.
      const field = details.topFields().first();
      await expect(field.locator(".param-formula")).toContainText(
        '"literal":"hidden"',
      );
      await expect(field.locator("weave-formula-editor")).toHaveCount(0);
    });

    test("Escape closes the list, then reverts, then leaves the field, then closes step details", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await replaceFormula(page, field, "input.");
      await expect(suggestions(page)).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(suggestions(page)).toHaveCount(0);
      await expect(details.dialog).toBeVisible();
      await expect(field.locator(".param-line")).toContainText("Not applied");
      await page.keyboard.press("Escape");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.amount > 1000",
      );
      await expect(formulaText(field)).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(formulaText(field)).not.toBeFocused();
      await expect(details.dialog).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(details.dialog).toHaveCount(0);
    });

    test("the completion list is a named listbox that stays on screen", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await replaceFormula(page, field, "input.");
      const list = suggestions(page);
      await expect(list).toBeVisible();
      const listBox = await box(list);
      expect(listBox.x).toBeGreaterThanOrEqual(0);
      expect(listBox.right).toBeLessThanOrEqual(size.width);
      expect(listBox.bottom).toBeLessThanOrEqual(size.height);
      const active = await formulaText(field).getAttribute(
        "aria-activedescendant",
      );
      await expect(list.locator(`[id="${active}"]`)).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await page.keyboard.press("ArrowDown");
      await expect(list.getByRole("option").nth(1)).toHaveAttribute(
        "aria-selected",
        "true",
      );
    });

    test("typing on a reference pill opens the editor with its path", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("review");
      const context = details.field("context");
      await context.getByRole("button", { name: /^reference check/ }).focus();
      await page.keyboard.press(".");
      await expect(formulaBox(context)).toHaveAttribute(
        "data-text",
        "steps.check.output.",
      );
      await expect(formulaText(context)).toBeFocused();
      await expect(suggestions(page)).toBeVisible();
    });

    test("= in an empty Fixed field puts the caret in an empty formula", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("note");
      const field = details.field("value-text");
      const text = field.getByRole("textbox", { name: "Text" });
      await text.fill("");
      await text.press("=");
      await expect(formulaText(field)).toBeFocused();
      await expect(formulaBox(field)).toHaveAttribute("data-text", "");
      await expect(
        field.getByRole("radio", { name: "Mapped", exact: true }),
      ).toBeChecked();
    });
  });

// Keys in the editor that step details also listen to: Undo, Enter, and the
// suggestions on a phone-sized window.
test.describe("the formula editor's keys", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test.beforeEach(async ({ page }) => {
    await withLanguageFeatures(page);
  });

  test("Ctrl or Cmd+Z in a formula runs exactly one workflow Undo", async ({
    page,
  }) => {
    const details = await openStepFixture(page, formulasFixture);
    await details.openWithKeyboard("flag");
    const field = details.field("value");
    await replaceFormula(page, field, "input.amount > 2000");
    await page.keyboard.press("Tab");
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "input.amount > 2000",
    );
    // Past step details' burst, so the next edit is its own Undo step.
    await page.waitForTimeout(800);
    await replaceFormula(page, field, "input.amount > 3000");
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "input.amount > 3000",
    );
    await page.keyboard.press("ControlOrMeta+z");
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "input.amount > 2000",
    );
    await expect(formulaText(field)).toBeFocused();
    await page.keyboard.press("Tab");
    await details.close();
    expect(await stepValue(page, "flag")).toEqual({
      op: { name: "gt", args: [{ ref: "/input/amount" }, { literal: 2000 }] },
    });
  });

  test("Enter accepts a suggestion and never breaks the line of a one-line formula", async ({
    page,
  }) => {
    const details = await openStepFixture(page, formulasFixture);
    await details.openWithKeyboard("flag");
    const field = details.field("value");
    await replaceFormula(page, field, "");
    await expect(formulaText(field)).toHaveAttribute("aria-multiline", "false");
    await page.keyboard.type("input.am", { delay: 20 });
    await expect(suggestions(page)).toBeVisible();
    await page.keyboard.press("Enter");
    await expect(suggestions(page)).toHaveCount(0);
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "input.amount",
    );
    await page.keyboard.press("Enter");
    await page.keyboard.press("Shift+Enter");
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "input.amount",
    );
    await expect(field.locator("weave-formula-editor .cm-line")).toHaveCount(1);
    await expect(details.dialog).toBeVisible();
  });
});

test.describe("the formula editor on a phone", () => {
  test.use({ viewport: { width: 360, height: 640 } });

  test("the completion list stays on screen", async ({ page }) => {
    await withLanguageFeatures(page);
    const details = await openStepFixture(page, formulasFixture);
    await details.openWithKeyboard("flag");
    const field = details.field("value");
    await replaceFormula(page, field, "input.");
    const list = suggestions(page);
    await expect(list).toBeVisible();
    const listBox = await box(list);
    expect(listBox.x).toBeGreaterThanOrEqual(0);
    expect(listBox.y).toBeGreaterThanOrEqual(0);
    expect(listBox.right).toBeLessThanOrEqual(360);
    expect(listBox.bottom).toBeLessThanOrEqual(640);
    await page.keyboard.press("Enter");
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      /^input\.[A-Za-z]+$/,
    );
  });
});
