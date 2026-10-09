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
import { parse, stringify } from "yaml";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import {
  openStepFixture,
  sizes,
  stepFixture,
  yamlFile,
} from "./step-details-po";
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
    test("Add option adds a described option in order, focuses it, and Remove option restores the default", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("fan-out");
      await details.dialog.getByRole("button", { name: "Add option" }).click();
      const menu = details.dialog.getByRole("menu", { name: "Add option" });
      await expect(menu.getByRole("menuitem")).toHaveText([
        /Run at most\s*How many branches run at the same time\./,
        /Branch results/,
      ]);
      await menu.getByRole("menuitem", { name: /Run at most/ }).click();
      const field = details
        .field("concurrency")
        .getByRole("textbox", { name: "Run at most" });
      await expect(field).toBeFocused();
      await field.fill("3");
      await expect(details.topFields()).toHaveCount(2);
      await details.field("concurrency").hover();
      await details
        .field("concurrency")
        .getByRole("button", { name: "Remove option" })
        .click();
      await expect(details.topFields()).toHaveCount(1);
      await details.close();
      expect(await sourceText(page)).not.toContain("concurrency: 3");
    });

    test("list rows reorder with Alt+arrows and key-value rows add and move", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("check-customer");
      const fields = details.field("value");
      await expect(
        fields.getByRole("textbox", { name: /^Name of row/ }),
      ).toHaveCount(2);
      await fields.getByRole("textbox", { name: "Name of row 1" }).focus();
      await page.keyboard.press("Alt+ArrowDown");
      await expect(
        fields.getByRole("textbox", { name: "Name of row 1" }),
      ).toHaveValue("amount");
      await fields.getByRole("button", { name: "Add field" }).click();
      await expect(
        fields.getByRole("textbox", { name: "Name of row 3" }),
      ).toBeFocused();
      await page.keyboard.type("note");
      await page.keyboard.press("Tab");
      await details.close();
      await details.openWithKeyboard("notify-sales");
      const answers = details.field("answers");
      await answers
        .getByRole("textbox", { name: "Add answer" })
        .fill("Escalate");
      await answers.getByRole("textbox", { name: "Add answer" }).press("Enter");
      await expect(
        answers.getByRole("textbox", { name: /^Answer 3 of 3/ }),
      ).toHaveValue("escalate");
      await details.close();
      const workflow = parse(await sourceText(page)) as {
        spec: { steps: Record<string, unknown>[] };
      };
      const value = workflow.spec.steps[0]["value"] as {
        object: Record<string, unknown>;
      };
      expect(Object.keys(value.object)).toEqual(["amount", "customer", "note"]);
      const route = workflow.spec.steps[2] as {
        cases: { steps: Record<string, unknown>[] }[];
      };
      expect(route.cases[0].steps[0]["decisions"]).toEqual([
        "approve",
        "reject",
        "escalate",
      ]);
    });
    test("the final added option keeps focus when its Add option menu disappears", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("fan-out");
      await details.dialog
        .getByRole("button", { name: "Add option", exact: true })
        .click();
      await details.dialog
        .getByRole("menuitem", { name: /Run at most/ })
        .click();
      await expect(
        details
          .field("concurrency")
          .getByRole("textbox", { name: "Run at most" }),
      ).toBeFocused();
      await details.dialog
        .getByRole("button", { name: "Add option", exact: true })
        .click();
      await details.dialog
        .getByRole("menuitem", { name: /Branch results/ })
        .click();
      await expect(
        details.dialog.getByRole("button", { name: "Add option", exact: true }),
      ).toHaveCount(0);
      await expect(
        details
          .field("branch-results")
          .getByRole("button", { name: "ledger result", exact: true }),
      ).toBeFocused();
    });

    test("nested row modes retain names and whole mappings return safely to rows", async ({
      page,
    }) => {
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      const details = await openStepFixture(page);
      await details.open("check-customer");
      const value = details.field("value");
      let first = value.locator(".param-kv-row").first();
      await first.getByRole("radio", { name: "Fixed", exact: true }).click();
      const confirm = page.getByRole("dialog", {
        name: "Replace the mapping with a fixed value?",
      });
      await confirm
        .getByRole("button", { name: "Replace", exact: true })
        .click();
      await expect(
        value.getByRole("textbox", { name: "Name of row 1" }),
      ).toHaveValue("customer");
      await expect(
        value
          .locator(".param-kv-row")
          .first()
          .getByRole("textbox", { name: "Value", exact: true }),
      ).toHaveValue("");
      await page.keyboard.press("ControlOrMeta+z");
      first = value.locator(".param-kv-row").first();
      await expect(
        first.getByRole("button", {
          name: "reference Input Customer ID",
          exact: true,
        }),
      ).toBeVisible();
      await value
        .getByRole("button", { name: "Field menu for Fields", exact: true })
        .click();
      await page
        .getByRole("menuitem", { name: "Map the whole input", exact: true })
        .click();
      await expect(
        value
          .getByRole("radiogroup", {
            name: "Value mode for Fields",
            exact: true,
          })
          .getByRole("radio", { name: "Mapped", exact: true }),
      ).toBeChecked();
      // Choosing the current input is an explicit replacement of the rows.
      await value
        .getByRole("combobox", { name: "Fields", exact: true })
        .fill("/input");
      await value
        .getByRole("combobox", { name: "Fields", exact: true })
        .press("Enter");
      await value
        .getByRole("button", { name: "Field menu for Fields", exact: true })
        .click();
      await page
        .getByRole("menuitem", { name: "Use rows", exact: true })
        .click();
      await page
        .getByRole("dialog", {
          name: "Replace the mapping with a fixed value?",
        })
        .getByRole("button", { name: "Keep the mapping" })
        .click();
      await expect(
        value.getByRole("button", { name: "reference Input", exact: true }),
      ).toBeVisible();
      await value
        .getByRole("button", { name: "Field menu for Fields", exact: true })
        .click();
      await page
        .getByRole("menuitem", { name: "Use rows", exact: true })
        .click();
      await page
        .getByRole("dialog", {
          name: "Replace the mapping with a fixed value?",
        })
        .getByRole("button", { name: "Replace", exact: true })
        .click();
      await expect(value.locator(".param-kv-row.is-blank")).toBeVisible();
      await details.close();
      expect(errors).toEqual([]);
    });

    test("collection cards and names keep usable targets and preserve imported values", async ({
      page,
    }) => {
      const workflow = parse(readFileSync(stepFixture, "utf8"));
      workflow.spec.steps[0].value.object.customer = {
        literal: { nested: [1, true, null] },
      };
      const details = await openStepFixture(
        page,
        yamlFile("collections.yaml", stringify(workflow)),
      );
      const before = await sourceText(page);
      await details.open("check-customer");
      const rows = details.field("value");
      await expect(
        rows
          .locator(".param-kv-row")
          .first()
          .getByRole("textbox", { name: "Value", exact: true }),
      ).toHaveValue(JSON.stringify({ nested: [1, true, null] }, null, 2));
      await details.dialog.screenshot({
        path: resolve(`../build/editor-m3/task-13/keyed-${size.tag}.png`),
      });
      for (const control of await rows.locator("input, button").all()) {
        if (!(await control.isVisible())) continue;
        await control.scrollIntoViewIfNeeded();
        const box = await control.boundingBox();
        expect(box!.height).toBeGreaterThanOrEqual(44);
        if (await control.evaluate((element) => element.matches("button")))
          expect(box!.width).toBeGreaterThanOrEqual(44);
      }
      await details.close();
      expect(await sourceText(page)).toBe(before);
      await details.openWithKeyboard("route-by-value");
      const cases = details.field("cases");
      await expect(
        cases.getByRole("button", { name: "Remove Path 1 of 2", exact: true }),
      ).toBeDisabled();
      await cases
        .getByRole("button", { name: "Add path", exact: true })
        .click();
      await expect(cases.locator(".param-row.is-card")).toHaveCount(3);
      await cases
        .getByRole("button", { name: "Move Path 3 of 3 up", exact: true })
        .click();
      await cases
        .getByRole("button", { name: "Remove Path 2 of 3", exact: true })
        .click();
      await expect(cases.locator(".param-row.is-card")).toHaveCount(2);
      await details.dialog.screenshot({
        path: resolve(`../build/editor-m3/task-13/paths-${size.tag}.png`),
      });
      await details.close();
      await details.openWithKeyboard("notify-sales");
      await details
        .field("answers")
        .getByRole("textbox", { name: "Answer 1 of 2" })
        .fill("Accept");
      await details
        .field("answers")
        .getByRole("textbox", { name: "Answer 1 of 2" })
        .press("Tab");
      await details.dialog.screenshot({
        path: resolve(`../build/editor-m3/task-13/answers-${size.tag}.png`),
      });
      await details.close();
      const saved = parse(await sourceText(page));
      expect(saved.spec.steps[2].cases[0].steps[0].decisions).toEqual([
        "accept",
        "reject",
      ]);
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

    test("collection rows and chips fit the scrolling form with 44px targets", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("check-customer");
      const values = details.field("value");
      for (const control of await values.locator("input, button").all()) {
        if (!(await control.isVisible())) continue;
        await control.scrollIntoViewIfNeeded();
        const box = await control.boundingBox();
        expect(box!.height).toBeGreaterThanOrEqual(44);
        expect(box!.width).toBeGreaterThanOrEqual(44);
        expect(box!.x).toBeGreaterThanOrEqual(0);
        expect(box!.x + box!.width).toBeLessThanOrEqual(size.width);
      }
      await values
        .getByRole("textbox", { name: "Name of row 1" })
        .scrollIntoViewIfNeeded();
      await details.dialog.screenshot({
        path: resolve(`../build/editor-m3/task-13/collections-${size.tag}.png`),
      });
      await details.close();
      await details.openWithKeyboard("notify-sales");
      const answers = details.field("answers");
      for (const control of await answers.locator("input, button").all()) {
        if (!(await control.isVisible())) continue;
        await control.scrollIntoViewIfNeeded();
        const box = await control.boundingBox();
        expect(box!.height).toBeGreaterThanOrEqual(44);
        expect(box!.width).toBeGreaterThanOrEqual(44);
        expect(box!.x).toBeGreaterThanOrEqual(0);
        expect(box!.x + box!.width).toBeLessThanOrEqual(size.width);
      }
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(size.width);
      await details.dialog.screenshot({
        path: resolve(`../build/editor-m3/task-13/chips-${size.tag}.png`),
      });
      await details.close();
    });
  });
}
