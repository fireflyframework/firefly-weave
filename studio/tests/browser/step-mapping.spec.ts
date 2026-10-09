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
import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { parse, stringify } from "yaml";
import {
  openStepFixture,
  sizes,
  stepFixture,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";
import { sourceText } from "./support";

for (const size of sizes)
  test.describe(`mapping at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("the toggle shows on hover and stays while Mapped, and = switches an empty field", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("summarize");
      const prompt = details.field("prompt");
      const toggle = prompt.getByRole("radiogroup", {
        name: "Value mode for Prompt",
      });
      await details.dialog
        .getByRole("button", { name: "Execute step", exact: true })
        .focus();
      await details.dialog
        .getByRole("button", { name: "Close", exact: true })
        .hover();
      await expect(toggle).toHaveCSS("opacity", "0");
      await prompt.hover();
      await expect(toggle).toBeVisible();
      await expect(toggle).toHaveCSS("opacity", "1");
      await expect(toggle.getByRole("radio", { name: "Fixed" })).toBeChecked();
      const text = prompt.getByRole("textbox", { name: "Prompt" });
      await text.fill("");
      await text.press("=");
      await expect(toggle.getByRole("radio", { name: "Mapped" })).toBeChecked();
      const data = prompt.getByRole("combobox", { name: "Prompt" });
      await expect(data).toBeFocused();
      await data.fill("/input/email");
      await data.press("Enter");
      await details.field("model").hover();
      await expect(toggle).toBeVisible();
      await expect(toggle).toHaveCSS("opacity", "1");
      await prompt
        .getByRole("button", { name: "Field menu for Prompt" })
        .click();
      const menu = page.getByRole("menu", { name: "Field menu for Prompt" });
      await expect(menu.getByRole("menuitem")).toHaveText([
        /Reset to default/,
        /Use data…/,
        /Edit formula/,
        /Copy value/,
      ]);
      await expect(
        menu.getByRole("menuitem", { name: /Edit formula/ }),
      ).toHaveAttribute("aria-disabled", "true");
      await page.keyboard.press("Escape");
      await details.close();
      expect(await sourceText(page)).toContain("ref: /input/email");
    });

    test("Use data… picks a field", async ({ page }) => {
      await withLanguageFeatures(page);
      const details = await openStepFixture(page);
      await details.open("reject-order");
      await details.close();
      await details.openWithKeyboard("summarize");
      await details
        .field("prompt")
        .getByRole("button", { name: "Field menu for Prompt" })
        .click();
      await page.getByRole("menuitem", { name: /Use data…/ }).click();
      const data = details
        .field("prompt")
        .getByRole("combobox", { name: "Prompt" });
      await expect(data).toBeFocused();
      await data.press("ArrowDown");
      await page
        .getByRole("option", { name: /Amount/ })
        .first()
        .click();
      await expect(
        details
          .field("prompt")
          .getByRole("button", { name: /reference Input Amount/ }),
      ).toBeVisible();
      await details
        .field("prompt")
        .getByRole("button", { name: "Field menu for Prompt" })
        .click();
      await page.getByRole("menuitem", { name: /Use data…/ }).click();
      await expect(data).toBeFocused();
      await data.press("ArrowDown");
      await page.getByRole("option", { name: /Email/ }).first().click();
      await expect(
        details
          .field("prompt")
          .getByRole("button", { name: /reference Input Email/ }),
      ).toBeVisible();
    });
  });

function mappedWorkflow(expression: unknown) {
  const workflow = parse(readFileSync(stepFixture, "utf8"));
  workflow.spec.steps.find(
    (step: { id: string }) => step.id === "summarize",
  ).prompt = expression;
  return yamlFile("mapped-order.yaml", stringify(workflow));
}
function neighboringTemplates() {
  const workflow = parse(readFileSync(stepFixture, "utf8"));
  const index = workflow.spec.steps.findIndex(
    (step: { id: string }) => step.id === "summarize",
  );
  const template = (prefix: string) => ({
    op: {
      name: "concat",
      args: [{ literal: prefix }, { ref: "/input/email" }],
    },
  });
  workflow.spec.steps[index].prompt = template("First ");
  workflow.spec.steps.splice(index + 1, 0, {
    ...structuredClone(workflow.spec.steps[index]),
    id: "second-summary",
    prompt: template("Second "),
  });
  return yamlFile("neighboring-templates.yaml", stringify(workflow));
}
for (const size of sizes) {
  test.describe(`mapped text at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    const errors = new WeakMap<object, string[]>();
    test.beforeEach(({ page }) => {
      const messages: string[] = [];
      errors.set(page, messages);
      page.on("pageerror", (error) => messages.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error") messages.push(message.text());
      });
    });
    test.afterEach(({ page }) => {
      expect(errors.get(page)).toEqual([]);
    });
    test("a published Action field keeps focus across an ordinary model Undo", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("wait-for-payment");
      const name = details
        .field("name")
        .getByRole("textbox", { name: "Signal name" });
      await name.fill("settled");
      await name.blur();
      await details.close();
      await details.openWithKeyboard("lookup");
      const menu = details
        .field("action")
        .getByRole("button", { name: "Field menu for Action" });
      await menu.focus();
      await page.keyboard.press("ControlOrMeta+z");
      await expect(menu).toBeFocused();
      await expect(details.dialogFor("lookup")).toBeVisible();
      await details.close();
      const workflow = parse(await sourceText(page));
      expect(
        workflow.spec.steps.find(
          (step: { id: string }) => step.id === "wait-for-payment",
        ).name,
      ).toBe("payment-received");
    });
    test("Undo and Redo reconcile a focused template and continued typing", async ({
      page,
    }) => {
      await withLanguageFeatures(page);
      const details = await openStepFixture(page, neighboringTemplates());
      await details.openWithKeyboard("summarize");
      const text = details
        .field("prompt")
        .getByRole("textbox", { name: "Prompt" });
      await text.fill("Changed {{ input.customerId }}");
      await page.keyboard.press("ControlOrMeta+z");
      await expect(text).toBeFocused();
      await expect(text).toHaveValue("First {{ input.email }}");
      await page.keyboard.press("ControlOrMeta+Shift+z");
      await expect(text).toHaveValue("Changed {{ input.customerId }}");
      await page.keyboard.press("ControlOrMeta+z");
      await expect(text).toHaveValue("First {{ input.email }}");
      await text.fill("Next {{ input.customerId }}");
      await expect(text).toBeFocused();
      await text.blur();
      await details.close();
      const workflow = parse(await sourceText(page));
      expect(
        workflow.spec.steps.find(
          (step: { id: string }) => step.id === "summarize",
        ).prompt,
      ).toEqual({
        op: {
          name: "concat",
          args: [{ literal: "Next " }, { ref: "/input/customerId" }],
        },
      });
    });
    test("navigation between matching field ids isolates invalid text and pending pointers", async ({
      page,
    }) => {
      await withLanguageFeatures(page);
      const details = await openStepFixture(page, neighboringTemplates());
      await details.openWithKeyboard("summarize");
      const prompt = details.field("prompt");
      await prompt
        .getByRole("textbox", { name: "Prompt" })
        .fill("Invalid {{ input.email");
      await expect(prompt.getByRole("alert")).toBeVisible();
      await details.dialog
        .getByRole("button", { name: "Next step", exact: true })
        .click();
      await expect(details.dialogFor("second-summary")).toBeVisible();
      await expect(prompt.getByRole("textbox", { name: "Prompt" })).toHaveValue(
        "Second {{ input.email }}",
      );
      await expect(prompt.getByRole("alert")).toHaveCount(0);
      await details.dialog
        .getByRole("button", { name: "Previous step", exact: true })
        .click();
      await expect(details.dialogFor("summarize")).toBeVisible();
      await prompt
        .getByRole("button", { name: "Field menu for Prompt" })
        .click();
      await page.getByRole("menuitem", { name: /Use data…/ }).click();
      const picker = prompt.getByRole("combobox", { name: "Prompt" });
      await picker.fill("/input/previousDraft");
      await expect(picker).toBeFocused();
      await details.dialog
        .getByRole("button", { name: "Next step", exact: true })
        .click();
      await expect(details.dialogFor("second-summary")).toBeVisible();
      const second = prompt.getByRole("textbox", { name: "Prompt" });
      await expect(second).toHaveValue("Second {{ input.email }}");
      await second.focus();
      await second.blur();
      await details.dialog
        .getByRole("button", { name: "More actions for second-summary" })
        .click();
      await page.getByRole("menuitem", { name: "Edit as YAML" }).click();
      const editor = page.getByRole("dialog", {
        name: "Edit second-summary as YAML",
      });
      const yaml = await editor
        .getByRole("textbox", { name: "Step YAML" })
        .inputValue();
      expect(parse(yaml).prompt).toEqual({
        op: {
          name: "concat",
          args: [{ literal: "Second " }, { ref: "/input/email" }],
        },
      });
    });
    for (const expression of [
      {
        op: {
          name: "concat",
          args: [{ literal: "Hello " }, { ref: "/input/email" }],
        },
        literal: "hidden",
      },
      { ref: "/input/email", literal: "hidden" },
    ]) {
      test(`an extended ${"op" in expression ? "concat" : "ref"} stays opaque and editable as YAML`, async ({
        page,
      }) => {
        await withLanguageFeatures(page);
        const details = await openStepFixture(page, mappedWorkflow(expression));
        await details.openWithKeyboard("summarize");
        await expect(
          details.field("prompt").locator(".param-formula"),
        ).toHaveText(JSON.stringify(expression));
        await expect(details.field("prompt").getByRole("textbox")).toHaveCount(
          0,
        );
        await details.dialog
          .getByRole("button", { name: "More actions for summarize" })
          .click();
        await page.getByRole("menuitem", { name: "Edit as YAML" }).click();
        const editor = page.getByRole("dialog", {
          name: "Edit summarize as YAML",
        });
        const yaml = editor.getByRole("textbox", { name: "Step YAML" });
        await expect(yaml).toBeEditable();
        expect(parse(await yaml.inputValue()).prompt).toEqual(expression);
      });
    }
    test("a stored template stays as a formula when text templates are unavailable", async ({
      page,
    }) => {
      await withLanguageFeatures(page, []);
      const expression = {
        op: {
          name: "concat",
          args: [{ literal: "Hello " }, { ref: "/input/email" }],
        },
      };
      const details = await openStepFixture(page, mappedWorkflow(expression));
      await details.openWithKeyboard("summarize");
      await expect(
        details.field("prompt").locator(".param-formula"),
      ).toHaveText(JSON.stringify(expression));
      const yamlEditor = async () => {
        await details.dialog
          .getByRole("button", { name: "More actions for summarize" })
          .click();
        await page.getByRole("menuitem", { name: "Edit as YAML" }).click();
        const editor = page.getByRole("dialog", {
          name: "Edit summarize as YAML",
        });
        const text = editor.getByRole("textbox", { name: "Step YAML" });
        await expect(text).toBeEditable();
        const value = await text.inputValue();
        expect(parse(value).prompt).toEqual(expression);
        await editor
          .getByRole("button", { name: "Cancel", exact: true })
          .click();
        return value;
      };
      const before = await yamlEditor();
      for (let i = 0; i < 2; i++)
        await details.dialog
          .getByRole("button", { name: "Next step", exact: true })
          .click();
      const name = details
        .field("name")
        .getByRole("textbox", { name: "Signal name" });
      await name.fill("payment-settled");
      await name.blur();
      await expect(name).toHaveValue("payment-settled");
      for (let i = 0; i < 2; i++)
        await details.dialog
          .getByRole("button", { name: "Previous step", exact: true })
          .click();
      expect(await yamlEditor()).toBe(before);
      await details.close();
    });
    test("a focused template keeps its editor until literal text is committed on blur", async ({
      page,
    }) => {
      await withLanguageFeatures(page);
      const details = await openStepFixture(
        page,
        mappedWorkflow({
          op: {
            name: "concat",
            args: [{ literal: "Hello " }, { ref: "/input/email" }],
          },
        }),
      );
      await details.openWithKeyboard("summarize");
      const text = details
        .field("prompt")
        .getByRole("textbox", { name: "Prompt" });
      await text.focus();
      await text.press("ControlOrMeta+a");
      await page.keyboard.type("Dear {{ input.customerId }}", { delay: 10 });
      await expect(text).toBeFocused();
      await expect(text).toHaveValue("Dear {{ input.customerId }}");
      await expect(
        details
          .field("prompt")
          .getByRole("radio", { name: "Mapped", exact: true }),
      ).toBeChecked();
      await text.press("ControlOrMeta+a");
      await page.keyboard.type("Plain text", { delay: 10 });
      await expect(text).toBeFocused();
      await expect(
        details
          .field("prompt")
          .getByRole("radio", { name: "Mapped", exact: true }),
      ).toBeChecked();
      await text.blur();
      await expect(
        details
          .field("prompt")
          .getByRole("radio", { name: "Fixed", exact: true }),
      ).toBeChecked();
      await expect(text).toHaveValue("Plain text");
      await page.keyboard.press("ControlOrMeta+z");
      await expect(
        details
          .field("prompt")
          .getByRole("radio", { name: "Mapped", exact: true }),
      ).toBeChecked();
      await expect(text).toHaveValue(/\{\{ input\./);
    });
    test("a multiline template retains newlines through typing and blur", async ({
      page,
    }) => {
      await withLanguageFeatures(page);
      const details = await openStepFixture(
        page,
        mappedWorkflow({
          op: {
            name: "concat",
            args: [{ literal: "First line\n" }, { ref: "/input/email" }],
          },
        }),
      );
      await details.openWithKeyboard("summarize");
      const text = details.field("prompt").locator("textarea.param-template");
      await expect(text).toHaveValue("First line\n{{ input.email }}");
      await text.fill("First line\nFor {{ input.customerId }}");
      await text.blur();
      await expect(text).toHaveValue("First line\nFor {{ input.customerId }}");
      await details.close();
      const source = await sourceText(page);
      expect(source).toContain("First line");
      expect(source).toContain("ref: /input/customerId");
      await details.openWithKeyboard("summarize");
      await expect(
        details.field("prompt").locator("textarea.param-template"),
      ).toHaveValue("First line\nFor {{ input.customerId }}");
    });
    test("simple templates edit as text and formulas keep their original references", async ({
      page,
    }) => {
      await withLanguageFeatures(page);
      const details = await openStepFixture(
        page,
        mappedWorkflow({
          op: {
            name: "concat",
            args: [{ literal: "Hello " }, { ref: "/input/email" }],
          },
        }),
      );
      await details.openWithKeyboard("summarize");
      const text = details
        .field("prompt")
        .getByRole("textbox", { name: "Prompt" });
      await expect(text).toHaveValue("Hello {{ input.email }}");
      await text.fill("For {{ input.customerId }}");
      await details.close();
      expect(await sourceText(page)).toContain("ref: /input/customerId");
    });
    for (const ref of [
      "/steps/check.output/output/amount",
      "/input/with.dot",
      "/input/with~1slash",
    ]) {
      test(`a template preserves ${ref} in its formula presentation`, async ({
        page,
      }) => {
        await withLanguageFeatures(page);
        const expression = {
          op: { name: "concat", args: [{ literal: "Value: " }, { ref }] },
        };
        const details = await openStepFixture(page, mappedWorkflow(expression));
        const before = await sourceText(page);
        await details.openWithKeyboard("summarize");
        await expect(
          details.field("prompt").locator(".param-formula"),
        ).toHaveText(JSON.stringify(expression));
        await expect(details.field("prompt").getByRole("textbox")).toHaveCount(
          0,
        );
        await details.close();
        expect(await sourceText(page)).toBe(before);
      });
    }
    test("replacing a mapping gives focus back to the field and one Undo restores it", async ({
      page,
    }) => {
      const details = await openStepFixture(
        page,
        mappedWorkflow({ ref: "/input/email" }),
      );
      await details.openWithKeyboard("summarize");
      await details
        .field("prompt")
        .getByRole("radio", { name: "Fixed", exact: true })
        .click();
      const confirmation = page.getByRole("dialog", {
        name: "Replace the mapping with a fixed value?",
      });
      await expect(
        confirmation.getByRole("button", { name: "Replace", exact: true }),
      ).toBeFocused();
      await confirmation
        .getByRole("button", { name: "Keep the mapping" })
        .click();
      await expect(
        details
          .field("prompt")
          .getByRole("button", { name: /reference Input Email/ }),
      ).toBeFocused();
      await details
        .field("prompt")
        .getByRole("radio", { name: "Fixed", exact: true })
        .click();
      await confirmation
        .getByRole("button", { name: "Replace", exact: true })
        .click();
      await expect(
        details.field("prompt").getByRole("textbox", { name: "Prompt" }),
      ).toBeFocused();
      await page.keyboard.press("ControlOrMeta+z");
      await expect(
        details
          .field("prompt")
          .getByRole("button", { name: /reference Input Email/ }),
      ).toBeVisible();
    });
  });
}
