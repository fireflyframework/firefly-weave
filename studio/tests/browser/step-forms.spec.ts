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
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { openStepFixture, sizes } from "./step-details-po";
import { sourceText } from "./support";

for (const size of sizes)
  test.describe(`step forms at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("required fields, defaults and help follow the field anatomy", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("reject-order");
      const code = details.field("code");
      await expect(code.locator(".param-required")).toHaveText("*");
      await expect(
        code.getByRole("textbox", { name: "Error code" }),
      ).toHaveAttribute("aria-required", "true");
      await expect(details.topFields()).toHaveCount(2);
      await details.close();
      await details.openWithKeyboard("summarize");
      const help = details.dialog.getByRole("button", { name: "Model: help" });
      await help.focus();
      await page.keyboard.press("Enter");
      await expect(
        details.dialog.getByText(
          "The AI profile this step uses: its provider, model and options.",
        ),
      ).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(
        details.dialog.getByText(
          "The AI profile this step uses: its provider, model and options.",
        ),
      ).toBeHidden();
      await expect(details.dialog).toBeVisible();
      await details.close();
      await details.openWithKeyboard("wait-a-minute");
      await expect(
        details.field("duration").getByRole("textbox", { name: "Wait for" }),
      ).toHaveValue("1");
      await expect(
        details
          .field("duration")
          .getByRole("combobox", { name: "Wait for unit" }),
      ).toHaveValue("minutes");
    });

    test("the field menu offers resetting, mapping and copying a Fixed value", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("summarize");
      await details
        .field("prompt")
        .getByRole("button", { name: "Field menu for Prompt" })
        .click();
      const menu = page.getByRole("menu", { name: "Field menu for Prompt" });
      await expect(menu.getByRole("menuitem")).toHaveText([
        /Reset to default/,
        /Use data…/,
        /Copy value/,
      ]);
      await expect(
        menu.getByRole("menuitem", { name: /Edit formula/ }),
      ).toHaveCount(0);
      await page.keyboard.press("Escape");
      await expect(details.dialog).toBeVisible();
    });

    test("one Undo reverts a burst of typing", async ({ page }) => {
      const details = await openStepFixture(page);
      await details.open("reject-order");
      const message = details
        .field("message")
        .getByRole("textbox", { name: "Message" });
      await message.click();
      await message.press("End");
      await page.keyboard.type(" Call us.", { delay: 20 });
      await expect(message).toHaveValue("The order was rejected. Call us.");
      await page.keyboard.press("ControlOrMeta+z");
      await expect(message).toHaveValue("The order was rejected.");
    });
  });

test("fields edit the workflow as typed, and the YAML is unchanged when nothing is edited", async ({
  page,
}) => {
  const details = await openStepFixture(page);
  const before = await sourceText(page);
  await details.open("wait-for-payment");
  await details.close();
  expect(await sourceText(page)).toBe(before);
  await details.open("wait-for-payment");
  await details
    .field("name")
    .getByRole("textbox", { name: "Signal name" })
    .fill("Payment Settled!");
  await details
    .field("name")
    .getByRole("textbox", { name: "Signal name" })
    .blur();
  await expect(
    details.field("name").getByRole("textbox", { name: "Signal name" }),
  ).toHaveValue("payment-settled");
  await details.close();
  expect(await sourceText(page)).toContain("name: payment-settled");
});

for (const size of [
  { tag: "desktop", width: 1440, height: 900, scale: 1 },
  { tag: "narrow", width: 360, height: 740, scale: 1 },
  { tag: "small", width: 600, height: 500, scale: 1 },
  { tag: "zoom200", width: 640, height: 360, scale: 2 },
]) {
  test.describe(`parameter controls at ${size.tag}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      deviceScaleFactor: size.scale,
      hasTouch: size.width < 768,
    });
    test("fields remain readable and their controls fit at narrow and zoomed sizes", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("summarize");
      const prompt = details.field("prompt");
      await prompt.hover();
      await prompt.getByRole("textbox", { name: "Prompt" }).focus();
      const height = await prompt
        .getByRole("textbox", { name: "Prompt" })
        .evaluate((element) => ({
          height: element.getBoundingClientRect().height,
          line: parseFloat(getComputedStyle(element).lineHeight),
        }));
      expect(height.height).toBeGreaterThanOrEqual(height.line * 3);
      const controls = prompt.locator(
        "input, textarea, button:not([disabled])",
      );
      const dimensions = await controls.evaluateAll((elements) =>
        elements.map((element) => {
          const box = element.getBoundingClientRect();
          return {
            width: box.width,
            height: box.height,
            left: box.left,
            right: box.right,
          };
        }),
      );
      for (const box of dimensions) {
        expect(box.width).toBeGreaterThanOrEqual(size.width < 768 ? 44 : 24);
        expect(box.height).toBeGreaterThanOrEqual(size.width < 768 ? 44 : 24);
        expect(box.left).toBeGreaterThanOrEqual(0);
        expect(box.right).toBeLessThanOrEqual(size.width);
      }
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(size.width);
      await page.screenshot({
        path: resolve(`../build/editor-m3/task-12/parameters-${size.tag}.png`),
      });
      await details.close();
    });
  });
}
