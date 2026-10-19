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
// Templates in step details: `{{` in a Fixed text field, any formula
// between the braces, and pills between faint braces.
import { expect, test } from "@playwright/test";
import { parse } from "yaml";
import {
  expectActiveRowOnTop,
  fieldLine,
  formulaBox,
  formulaText,
  replaceFormula,
  setTestInput,
  suggestions,
} from "./formula-po";
import {
  openStepFixture,
  sizes,
  withLanguageFeatures,
} from "./step-details-po";
import { sourceText } from "./support";

const prompt = async (page: import("@playwright/test").Page) =>
  parse(await sourceText(page)).spec.steps.find(
    (step: { id: string }) => step.id === "summarize",
  ).prompt;

for (const size of sizes)
  test.describe(`templates at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test.beforeEach(async ({ page }) => {
      await withLanguageFeatures(page);
    });

    test("{{ in a Fixed text field switches it to Mapped and opens suggestions between the braces", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("summarize");
      const field = details.field("prompt");
      const text = field.getByRole("textbox", { name: "Prompt" });
      await text.focus();
      await page.keyboard.press("End");
      await page.keyboard.type(" for {{");
      await expect(
        field.getByRole("radio", { name: "Mapped", exact: true }),
      ).toBeChecked();
      await expect(formulaText(field)).toBeFocused();
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "Summarize the order for {{  }}",
      );
      await expect(suggestions(page)).toBeVisible();
      await expectActiveRowOnTop(page);
      await page.keyboard.type("input.em", { delay: 20 });
      await page.keyboard.press("Enter");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "Summarize the order for {{ input.email }}",
      );
      await page.keyboard.press("Tab");
      await details.close();
      expect(await prompt(page)).toEqual({
        op: {
          name: "concat",
          args: [
            { literal: "Summarize the order for " },
            { ref: "/input/email" },
          ],
        },
      });
    });

    test("a placeholder takes any formula and previews it", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await setTestInput(page, details, {
        "Customer ID": "c-1042",
        Email: "ada@example.com",
        Amount: "1500",
      });
      await details.openWithKeyboard("summarize");
      const field = details.field("prompt");
      await field.getByRole("radio", { name: "Mapped", exact: true }).click();
      await replaceFormula(
        page,
        field,
        'Order of {{ coalesce(input.nickname, "standard") }} for {{ input.customerId }}',
      );
      await page.keyboard.press("Tab");
      await expect(fieldLine(details, "prompt")).toHaveText(
        "= Order of standard for c-1042",
      );
      await expect(
        field.locator(
          '.cm-formula-pill[aria-label="reference Input Customer ID"]',
        ),
      ).toHaveCount(1);
      await expect(
        field.locator(
          '.cm-formula-pill[aria-label="reference Input nickname"]',
        ),
      ).toHaveCount(1);
    });
  });
