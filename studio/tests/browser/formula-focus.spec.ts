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
// Focus and the shape of the form while a formula is typed: the field stays
// where it is, nothing is cleared under the caret, the Result panel's own
// controls are usable, and Escape takes back what wasn't applied.
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { parse, stringify } from "yaml";
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
  openConnectedFixture,
  openStepFixture,
  sizes,
  stepFixture,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";
import { sourceText } from "./support";

const stepOf = async (page: Page, id: string) =>
  parse(await sourceText(page)).spec.steps.find(
    (step: { id: string }) => step.id === id,
  );
/** Closes the suggestions if they are open, so Tab leaves the field. */
async function closeSuggestions(page: Page) {
  if (await suggestions(page).count()) await page.keyboard.press("Escape");
  await expect(suggestions(page)).toHaveCount(0);
}
const activeTag = (page: Page) =>
  page.evaluate(() => document.activeElement?.tagName ?? "");
const RECORD = {
  parameters: { object: { customerId: { ref: "/input/customerId" } } },
};
/**
 * Types text into the focused formula and, in the same task, presses Undo or
 * Escape or leaves the field: a key that arrives before the page draws the
 * typed text, as from a fast typist on a busy machine.
 */
async function typeThen(
  page: Page,
  text: string,
  then: "undo" | "escape" | "leave",
) {
  await page.evaluate(
    async ({ text, then }) => {
      const editor = document.activeElement as HTMLElement;
      document.execCommand("insertText", false, text);
      // The editor reads the typed text when the DOM change is delivered.
      await Promise.resolve();
      if (then === "leave") return editor.blur();
      const mac = /Mac/.test(navigator.platform);
      editor.dispatchEvent(
        new KeyboardEvent(
          "keydown",
          then === "undo"
            ? {
                key: "z",
                code: "KeyZ",
                metaKey: mac,
                ctrlKey: !mac,
                bubbles: true,
                cancelable: true,
              }
            : {
                key: "Escape",
                code: "Escape",
                bubbles: true,
                cancelable: true,
              },
        ),
      );
    },
    { text, then },
  );
}

for (const size of sizes)
  test.describe(`a formula being typed at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test.beforeEach(async ({ page }) => {
      await withLanguageFeatures(page);
    });

    test("a record typed over a whole Input keeps the field, its focus and the value, and Undo and Redo move between them", async ({
      page,
    }) => {
      const workflow = parse(readFileSync(stepFixture, "utf8"));
      workflow.spec.steps.find(
        (step: { id: string }) => step.id === "lookup",
      ).with = {
        ref: "/input",
      };
      const details = await openConnectedFixture(
        page,
        yamlFile("whole-input.yaml", stringify(workflow)),
      );
      await details.openWithKeyboard("lookup");
      const field = details.field("input");
      await field.getByRole("button", { name: /^reference/ }).focus();
      await page.keyboard.press("x");
      await expect(formulaText(field)).toBeFocused();
      await page.keyboard.press("ControlOrMeta+a");
      await page.keyboard.type("{parameters: {customerId: input.customerId}}", {
        delay: 15,
      });
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "{parameters: {customerId: input.customerId}}",
      );
      await expect(formulaText(field)).toBeFocused();
      await expect(page.getByText(/^Removed /)).toHaveCount(0);
      await closeSuggestions(page);
      await page.keyboard.press("Tab");
      await expect(page.getByText(/^Removed /)).toHaveCount(0);
      await details.close();
      expect((await stepOf(page, "lookup")).with).toEqual({ object: RECORD });
      await details.openWithKeyboard("lookup");
      await details.dialog.focus();
      await page.keyboard.press("ControlOrMeta+z");
      await details.close();
      expect((await stepOf(page, "lookup")).with).toEqual({ ref: "/input" });
      await details.openWithKeyboard("lookup");
      await details.dialog.focus();
      await page.keyboard.press("ControlOrMeta+Shift+z");
      await details.close();
      expect((await stepOf(page, "lookup")).with).toEqual({ object: RECORD });
    });

    test("typing a text formula key by key in a transform's Fields never drops a key", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await replaceFormula(page, field, "");
      await page.keyboard.type('concat("a", input.email)', { delay: 15 });
      await closeSuggestions(page);
      await page.keyboard.type("zz", { delay: 15 });
      await page.keyboard.press("Backspace");
      await page.keyboard.press("Backspace");
      await expect(formulaText(field)).toBeFocused();
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        'concat("a", input.email)',
      );
      await page.keyboard.press("Tab");
      await details.close();
      expect((await stepOf(page, "flag")).value).toEqual({
        op: {
          name: "concat",
          args: [{ literal: "a" }, { ref: "/input/email" }],
        },
      });
    });

    test("Show all in the Result panel can be clicked and reached with Tab", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await setTestInput(page, details, {
        "Customer ID": "c".repeat(300),
        Amount: "1500",
      });
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await replaceFormula(page, field, "input.customerId");
      await closeSuggestions(page);
      const result = field.getByRole("region", { name: "Result of Fields" });
      await result.getByRole("button", { name: "Show all" }).click();
      await expect(result).toBeVisible();
      await expect(
        result.getByRole("button", { name: "Show less" }),
      ).toBeFocused();
      await expect(result).toContainText("c".repeat(300));
      // By keyboard: from the formula, Tab reaches the panel's button.
      await formulaText(field).click();
      for (let i = 0; i < 4; i++) {
        if (
          await result
            .getByRole("button", { name: "Show less" })
            .evaluate((b) => b === document.activeElement)
        )
          break;
        await page.keyboard.press("Tab");
        await expect(result).toBeVisible();
      }
      await expect(
        result.getByRole("button", { name: "Show less" }),
      ).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(
        result.getByRole("button", { name: "Show all" }),
      ).toBeFocused();
      expect(await activeTag(page)).toBe("BUTTON");
      // Leaving the field closes it.
      await page.keyboard.press("Tab");
      await expect(result).toHaveCount(0);
    });

    test("Escape takes back a literal that waits for the field to be left", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      const before = await sourceText(page);
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await replaceFormula(page, field, '"hello"');
      await expect(formulaBox(field)).toHaveAttribute("data-text", '"hello"');
      await page.keyboard.press("Escape");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.amount > 1000",
      );
      await expect(formulaText(field)).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(formulaText(field)).not.toBeFocused();
      await details.close();
      expect(await sourceText(page)).toBe(before);
    });

    test("a key pressed before the typed text is drawn still leaves the field showing what the workflow holds", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      const before = await sourceText(page);
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      const select = async () => {
        await formulaText(field).click();
        await page.keyboard.press("ControlOrMeta+a");
      };
      // Undo takes back the formula just written, and the field shows it.
      await select();
      await typeThen(page, "input.amount > 5", "undo");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.amount > 1000",
      );
      await expect(formulaText(field)).toBeFocused();
      // Escape takes back text that doesn't apply, and a literal waiting for
      // the field to be left.
      await typeThen(page, " )", "escape");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.amount > 1000",
      );
      await select();
      await typeThen(page, '"hello"', "escape");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.amount > 1000",
      );
      // Leaving shows the formula in its standard form.
      await select();
      await typeThen(page, "input.amount>1000", "leave");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.amount > 1000",
      );
      await details.close();
      expect(await sourceText(page)).toBe(before);
    });

    test("a keystroke that leaves the value as it was writes nothing", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      const before = await sourceText(page);
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await formulaText(field).click();
      await page.keyboard.press("ControlOrMeta+a");
      await page.keyboard.press("ArrowRight");
      await page.keyboard.type(" ");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.amount > 1000 ",
      );
      await page.keyboard.press("Tab");
      await details.close();
      expect(await sourceText(page)).toBe(before);
    });

    test("a reference pill and a template with pills are as tall as the fields around them", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("review");
      const control = await details.dialog.evaluate((element) =>
        parseFloat(
          getComputedStyle(
            element.querySelector(".param") ?? element,
          ).getPropertyValue("--sd-control"),
        ),
      );
      expect(control).toBe(size.width < 768 ? 44 : 36);
      const pill = await box(
        details
          .field("context")
          .getByRole("button", { name: /^reference check/ }),
      );
      expect(pill.height).toBe(control);
      const title = await box(formulaBox(details.field("title")));
      expect(title.height).toBe(control);
    });

    test("text typed on a reference pill that doesn't apply comes back after reopening", async ({
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
      await details.close();
      await details.openWithKeyboard("review");
      await expect(formulaBox(details.field("context"))).toHaveAttribute(
        "data-text",
        "steps.check.output.",
      );
      await expect(fieldLine(details, "context")).toContainText("Not applied");
    });
  });

test.describe("keys of a one-line formula", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Shift+Enter in a one-line formula changes nothing", async ({
    page,
  }) => {
    await withLanguageFeatures(page);
    const details = await openStepFixture(page, formulasFixture);
    await details.openWithKeyboard("flag");
    const field = details.field("value");
    await formulaText(field).click();
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.press("ArrowRight");
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("Shift+Enter");
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "input.amount > 1000",
    );
  });

  test("backslashes before a typed {{ stay text", async ({ page }) => {
    await withLanguageFeatures(page);
    const details = await openStepFixture(page);
    await details.openWithKeyboard("summarize");
    const field = details.field("prompt");
    const text = field.getByRole("textbox", { name: "Prompt" });
    await text.fill("C:\\");
    await text.press("End");
    await page.keyboard.type("{{");
    await expect(formulaText(field)).toBeFocused();
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "C:\\\\{{  }}",
    );
    await page.keyboard.type("input.email", { delay: 15 });
    await closeSuggestions(page);
    await page.keyboard.press("Tab");
    await details.close();
    expect((await stepOf(page, "summarize")).prompt).toEqual({
      op: {
        name: "concat",
        args: [{ literal: "C:\\" }, { ref: "/input/email" }],
      },
    });
  });
});
