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
  openConnectedFixture,
  type StepDetailsPage,
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

const withLimit = () =>
  yamlFile(
    "limit.yaml",
    readFileSync(stepFixture, "utf8").replace(
      "with: {object: {parameters: {object: {customerId: {ref: /input/customerId}}}}}",
      "with: {object: {parameters: {object: {customerId: {ref: /input/customerId}}}, limit: {literal: 25}}}",
    ),
  );

for (const size of sizes)
  test.describe(`drag-to-map at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    const sheet = size.width < 768;
    const showInput = (details: StepDetailsPage) =>
      sheet
        ? details.dialog
            .getByRole("tablist", { name: "Step details panes" })
            .getByRole("tab", { name: "Input" })
            .click()
        : Promise.resolve();
    /** Maps an Input row to a field: by drag where both panes show, else with Map to…. */
    const map = async (
      details: StepDetailsPage,
      row: string,
      fieldId: string,
      label: string,
      edge = false,
    ) => {
      await showInput(details);
      const source = details.dialog
        .locator(".sd-input")
        .getByRole("listitem", { name: new RegExp(`^${row}, `) });
      if (sheet) {
        await source.focus();
        await details.page.keyboard.press("Enter");
        await details.page
          .getByRole("menu", { name: `Map ${row} to` })
          .getByRole("menuitem", { name: label, exact: true })
          .click();
        return;
      }
      const target = details.field(fieldId);
      const control =
        fieldId === "value"
          ? target.locator(":scope > .param > .param-label-row")
          : target.locator(".param-control :is(input, textarea)").first();
      const box = (await control.boundingBox())!;
      await source.dragTo(control, {
        targetPosition: { x: edge ? box.width - 4 : 8, y: box.height / 2 },
      });
    };

    test("dragging onto a number replaces it with Undo, and onto text keeps the text", async ({
      page,
    }) => {
      await withLanguageFeatures(page);
      const details = await openConnectedFixture(page, withLimit());
      await details.open("lookup");
      await expect(
        details.field("input.limit").getByRole("textbox", { name: "Limit" }),
      ).toHaveValue("25");
      await map(details, "amount", "input.limit", "Limit");
      await expect(page.locator(".toast-region")).toContainText(
        "Replaced 25 with check-customer › amount.",
      );
      await expect(details.dialog.locator("[aria-live=polite]")).toHaveText(
        "Mapped check-customer › amount to Limit",
      );
      await page
        .locator(".toast-region")
        .getByRole("button", { name: "Undo" })
        .click();
      await expect(
        details.field("input.limit").getByRole("textbox", { name: "Limit" }),
      ).toHaveValue("25");
      await details.close();
      await details.open("summarize");
      await showInput(details);
      await details.dialog
        .locator(".sd-input")
        .getByRole("combobox", { name: "Input source" })
        .selectOption("input");
      await map(details, "customerId", "prompt", "Prompt", true);
      await details.close();
      const prompt = JSON.stringify(
        parse(await sourceText(page)).spec.steps.find(
          (s: { id: string }) => s.id === "summarize",
        ).prompt,
      );
      expect(prompt).toContain("Summarize the order");
      expect(prompt).toContain("/input/customerId");
    });

    test("a mapped Input field carries the check mark", async ({ page }) => {
      const details = await openStepFixture(page);
      await details.open("lookup");
      await showInput(details);
      const input = details.dialog.locator(".sd-input");
      await input
        .getByRole("combobox", { name: "Input source" })
        .selectOption("input");
      await expect(
        input.getByRole("listitem", {
          name: "customerId, Text, mapped in this step",
        }),
      ).toBeVisible();
      await expect(
        input.getByRole("listitem", { name: "email, Text", exact: true }),
      ).toBeVisible();
    });

    test("Map to… maps a field without a pointer", async ({ page }) => {
      const details = await openStepFixture(page);
      await details.open("check-customer");
      await showInput(details);
      const email = details.dialog
        .locator(".sd-input")
        .getByRole("listitem", { name: /^email, / });
      await email.focus();
      await page.keyboard.press("Enter");
      const menu = page.getByRole("menu", { name: "Map email to" });
      await expect(menu.getByRole("menuitem").first()).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(menu).toHaveCount(0);
      await expect(email).toBeFocused();
      await expect(details.dialog).toBeVisible();
      await page.keyboard.press("Enter");
      await menu.getByRole("menuitem", { name: "Fields", exact: true }).click();
      await details.close();
      const value = parse(await sourceText(page)).spec.steps[0].value;
      expect(JSON.stringify(value)).toContain('"email":{"ref":"/input/email"}');
    });

    test("key-value rows take drops and Add all fields", async ({ page }) => {
      const details = await openStepFixture(page);
      await details.open("check-customer");
      await map(details, "email", "value", "Fields");
      if (sheet)
        await details.dialog
          .getByRole("tablist", { name: "Step details panes" })
          .getByRole("tab", { name: "Parameters" })
          .click();
      await details
        .field("value")
        .getByRole("button", { name: "Add all fields" })
        .click();
      await expect(details.dialog.locator("[aria-live=polite]")).toHaveText(
        "Added 3 fields to Fields",
      );
      await details.close();
      const keys = Object.keys(
        parse(await sourceText(page)).spec.steps[0].value.object,
      );
      expect(keys).toEqual([
        "customer",
        "amount",
        "email",
        "customerId",
        "status",
        "tier",
      ]);
    });
  });

for (const size of [
  ...sizes,
  { tag: "360x640", width: 360, height: 640 },
  { tag: "640x360", width: 640, height: 360 },
]) {
  test.describe(`mapping safety at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    const messages = new WeakMap<object, string[]>();
    test.beforeEach(({ page }) => {
      const errors: string[] = [];
      messages.set(page, errors);
      page.on("pageerror", (e) => errors.push(e.message));
      page.on("console", (e) => {
        if (e.type() === "error") errors.push(e.text());
      });
    });
    test.afterEach(({ page }) => expect(messages.get(page)).toEqual([]));
    const inputPane = async (details: StepDetailsPage) => {
      if (size.width < 768)
        await details.dialog
          .getByRole("tablist", { name: "Step details panes" })
          .getByRole("tab", { name: "Input", exact: true })
          .click();
      return details.dialog.locator(".sd-input");
    };
    test("the keyboard menu has usable targets, focus, and one Undo for an escaped key", async ({
      page,
    }) => {
      const workflow = parse(readFileSync(stepFixture, "utf8"));
      workflow.spec.inputSchema.properties["a/b~c"] = {
        type: "string",
        title: "Special field",
      };
      workflow.spec.steps[0].value.object.payload = {
        literal: { ref: "/input/email" },
      };
      const details = await openStepFixture(
        page,
        yamlFile("escaped.yaml", stringify(workflow)),
      );
      await details.openWithKeyboard("check-customer");
      const input = await inputPane(details);
      await expect(
        input.getByRole("listitem", { name: "email, Text", exact: true }),
      ).toBeVisible();
      const source = input.getByRole("listitem", {
        name: "a/b~c, Text",
        exact: true,
      });
      await expect(source).toHaveAttribute("title", "Special field");
      await source.focus();
      await page.keyboard.press("Enter");
      const menu = page.getByRole("menu", { name: "Map a/b~c to" });
      await expect(menu.getByRole("menuitem").first()).toBeFocused();
      const geometry = await menu.evaluate((element) => {
        const box = element.getBoundingClientRect();
        const controls = [
          ...element.querySelectorAll<HTMLElement>("button"),
        ].map((button) => {
          const b = button.getBoundingClientRect();
          const inside = b.top >= box.top && b.bottom <= box.bottom;
          const hit =
            !inside ||
            button.contains(
              document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2),
            );
          return { width: b.width, height: b.height, hit };
        });
        const focus = getComputedStyle(document.activeElement!);
        return {
          left: box.left,
          right: box.right,
          top: box.top,
          bottom: box.bottom,
          controls,
          focus: focus.outlineStyle,
        };
      });
      expect(geometry.left).toBeGreaterThanOrEqual(0);
      expect(geometry.right).toBeLessThanOrEqual(size.width);
      expect(geometry.top).toBeGreaterThanOrEqual(0);
      expect(geometry.bottom).toBeLessThanOrEqual(size.height);
      expect(geometry.focus).not.toBe("none");
      for (const control of geometry.controls) {
        expect(control.width).toBeGreaterThanOrEqual(44);
        expect(control.height).toBeGreaterThanOrEqual(44);
        expect(control.hit).toBe(true);
      }
      await page.screenshot({
        path: test.info().outputPath(`map-menu-${size.tag}.png`),
      });
      await page.keyboard.press("End");
      await expect(menu.getByRole("menuitem").last()).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(menu).toHaveCount(0);
      await expect(source).toBeFocused();
      await page.keyboard.press("Enter");
      await menu.getByRole("menuitem", { name: "Fields", exact: true }).click();
      await expect(
        details
          .field("value")
          .getByRole("textbox", { name: "Name of row 4", exact: true }),
      ).toHaveValue("a/b~c");
      if (size.width < 768)
        await expect(
          details.dialog
            .getByRole("tablist", { name: "Step details panes" })
            .getByRole("tab", { name: "Parameters", exact: true }),
        ).toHaveAttribute("aria-selected", "true");
      const focused = details
        .field("value")
        .getByRole("textbox", { name: "Name of row 1", exact: true });
      await expect(focused).toBeFocused();
      const focusedGeometry = await focused.evaluate((element) => {
        const box = element.getBoundingClientRect();
        const points = [
          [box.left + 4, box.top + 4],
          [box.right - 4, box.bottom - 4],
          [box.left + box.width / 2, box.top + box.height / 2],
        ];
        return {
          top: box.top,
          bottom: box.bottom,
          hits: points.map(([x, y]) =>
            element.contains(document.elementFromPoint(x, y)),
          ),
        };
      });
      expect(focusedGeometry.top).toBeGreaterThanOrEqual(0);
      expect(focusedGeometry.bottom).toBeLessThanOrEqual(size.height);
      expect(focusedGeometry.hits).toEqual([true, true, true]);
      await page.screenshot({
        path: test.info().outputPath(`mapped-fields-${size.tag}.png`),
      });
      await page.keyboard.press("ControlOrMeta+z");
      await expect(
        details
          .field("value")
          .getByRole("textbox", { name: "Name of row 4", exact: true }),
      ).toHaveCount(0);
      await details.close();
      expect(parse(await sourceText(page)).spec.steps[0].value).toEqual(
        workflow.spec.steps[0].value,
      );
    });
    test("an opaque template refuses mapping without success or losing its value", async ({
      page,
    }) => {
      await withLanguageFeatures(page);
      const expression = {
        op: {
          name: "concat",
          args: [{ literal: "Preserve " }, { ref: "/input/a~1b" }],
        },
      };
      const details = await openStepFixture(page, mappedWorkflow(expression));
      await details.openWithKeyboard("summarize");
      const input = await inputPane(details);
      await input
        .getByRole("combobox", { name: "Input source" })
        .selectOption("input");
      const row = input.getByRole("listitem", {
        name: "email, Text",
        exact: true,
      });
      await row.focus();
      await page.keyboard.press("Enter");
      await page
        .getByRole("menu", { name: "Map email to" })
        .getByRole("menuitem", { name: "Prompt", exact: true })
        .click();
      await expect(row).toBeFocused();
      await expect(details.dialog.locator("[aria-live=polite]")).toHaveText(
        /cannot be edited as text/,
      );
      await details.close();
      expect(
        parse(await sourceText(page)).spec.steps.find(
          (s: { id: string }) => s.id === "summarize",
        ).prompt,
      ).toEqual(expression);
    });
  });
}

test.describe("pointer mapping", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  test("a long text drop follows the visible glyphs after source focus resets horizontal scrolling", async ({
    page,
  }) => {
    const workflow = parse(readFileSync(stepFixture, "utf8"));
    const human = workflow.spec.steps.find(
      (step: { id: string }) => step.id === "route-by-value",
    ).cases[0].steps[0];
    const text = "Review this customer carefully. ".repeat(8);
    human.title = { literal: text };
    const details = await openConnectedFixture(
      page,
      yamlFile("scrolling-title.yaml", stringify(workflow)),
    );
    await details.openWithKeyboard("notify-sales");
    const input = details.dialog.locator(".sd-input");
    await input
      .getByRole("combobox", { name: "Input source" })
      .selectOption("input");
    const source = input.getByRole("listitem", {
      name: "email, Text",
      exact: true,
    });
    const control = details
      .field("title")
      .getByRole("textbox", { name: "Title", exact: true });
    await control.focus();
    await control.press("End");
    expect(
      await control.evaluate((element) => element.scrollLeft),
    ).toBeGreaterThan(0);
    const point = await control.evaluate((element) => {
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      const context = document.createElement("canvas").getContext("2d")!;
      context.font = style.font;
      return {
        x:
          parseFloat(style.paddingLeft) +
          parseFloat(style.borderLeftWidth) +
          context.measureText("Review ").width,
        y: box.height / 2,
      };
    });
    await source.hover();
    await page.mouse.down();
    // Native source focus blurs the input; Chromium returns its text to the start.
    expect(await control.evaluate((element) => element.scrollLeft)).toBe(0);
    const box = (await control.boundingBox())!;
    await page.mouse.move(box.x + point.x, box.y + point.y, { steps: 12 });
    await page.mouse.move(box.x + point.x, box.y + point.y);
    await page.mouse.up();
    await expect(control).toHaveValue(
      `Review {{ input.email }}${text.slice(7)}`,
    );
    expect(
      await control.evaluate(
        (element) => (element as HTMLInputElement).selectionStart,
      ),
    ).toBe(7 + 17);
    await details.close();
    expect(
      parse(await sourceText(page)).spec.steps.find(
        (step: { id: string }) => step.id === "route-by-value",
      ).cases[0].steps[0].title,
    ).toEqual({
      op: {
        name: "concat",
        args: [
          { literal: "Review " },
          { ref: "/input/email" },
          { literal: text.slice(7) },
        ],
      },
    });
  });
  test("canceling a text drop keeps its text and selection without leaving a mirror", async ({
    page,
  }) => {
    const text = "Hello 🙂\nSecond line";
    const details = await openConnectedFixture(
      page,
      mappedWorkflow({ literal: text }),
    );
    await details.openWithKeyboard("summarize");
    const input = details.dialog.locator(".sd-input");
    await input
      .getByRole("combobox", { name: "Input source" })
      .selectOption("input");
    const source = input.getByRole("listitem", {
      name: "email, Text",
      exact: true,
    });
    const control = details
      .field("prompt")
      .getByRole("textbox", { name: "Prompt" });
    await control.focus();
    await control.press("ControlOrMeta+Home");
    await control.press("ArrowRight");
    const selection = await control.evaluate(
      (element) => (element as HTMLTextAreaElement).selectionStart,
    );
    const bodyChildren = await page.locator("body > *").count();
    const box = (await control.boundingBox())!;
    await source.hover();
    await page.mouse.down();
    await page.mouse.move(box.x + 20, box.y + 20, { steps: 12 });
    await page.mouse.move(box.x + 20, box.y + 20);
    await expect(details.field("prompt")).toHaveAttribute(
      "data-drop-hover",
      "",
    );
    await page.keyboard.press("Escape");
    await page.mouse.up();
    await expect(control).toHaveValue(text);
    expect(
      await control.evaluate(
        (element) => (element as HTMLTextAreaElement).selectionStart,
      ),
    ).toBe(selection);
    await expect(page.locator("body > *")).toHaveCount(bodyChildren);
    await expect(
      details.dialog.locator("[data-drop-hover], [data-fit]"),
    ).toHaveCount(0);
    await details.close();
    expect(
      parse(await sourceText(page)).spec.steps.find(
        (step: { id: string }) => step.id === "summarize",
      ).prompt,
    ).toEqual({ literal: text });
  });
  test("a multiline drop inserts at the pointer while keeping both sides of the text", async ({
    page,
  }) => {
    await withLanguageFeatures(page);
    const details = await openStepFixture(
      page,
      mappedWorkflow({ literal: "Hello there\nSecond line" }),
    );
    await details.openWithKeyboard("summarize");
    await details.dialog
      .locator(".sd-input")
      .getByRole("combobox", { name: "Input source" })
      .selectOption("input");
    const source = details.dialog
      .locator(".sd-input")
      .getByRole("listitem", { name: "email, Text", exact: true });
    const control = details
      .field("prompt")
      .getByRole("textbox", { name: "Prompt" });
    const point = await control.evaluate((element) => {
      const style = getComputedStyle(element);
      const context = document.createElement("canvas").getContext("2d")!;
      context.font = style.font;
      return {
        x:
          parseFloat(style.paddingLeft) +
          parseFloat(style.borderLeftWidth) +
          context.measureText("Hello ").width,
        y:
          parseFloat(style.paddingTop) +
          parseFloat(style.borderTopWidth) +
          parseFloat(style.lineHeight) / 2,
      };
    });
    await source.dragTo(control, { targetPosition: point });
    await expect(control).toHaveValue(
      "Hello {{ input.email }}there\nSecond line",
    );
    await expect(control).toBeFocused();
    expect(
      await control.evaluate(
        (element) => (element as HTMLTextAreaElement).selectionStart,
      ),
    ).toBe(23);
    await details.close();
    expect(
      parse(await sourceText(page)).spec.steps.find(
        (s: { id: string }) => s.id === "summarize",
      ).prompt,
    ).toEqual({
      op: {
        name: "concat",
        args: [
          { literal: "Hello " },
          { ref: "/input/email" },
          { literal: "there\nSecond line" },
        ],
      },
    });
  });
  test("a wrapped multiline drop uses the visible line and removes its layout mirror", async ({
    page,
  }) => {
    const text = "W".repeat(150) + "\nTail🙂";
    const details = await openConnectedFixture(
      page,
      mappedWorkflow({ literal: text }),
    );
    await details.openWithKeyboard("summarize");
    const input = details.dialog.locator(".sd-input");
    await input
      .getByRole("combobox", { name: "Input source" })
      .selectOption("input");
    const source = input.getByRole("listitem", {
      name: "email, Text",
      exact: true,
    });
    const control = details
      .field("prompt")
      .getByRole("textbox", { name: "Prompt" });
    const position = await control.evaluate((element) => {
      const style = getComputedStyle(element);
      const context = document.createElement("canvas").getContext("2d")!;
      context.font = style.font;
      const width =
        element.clientWidth -
        parseFloat(style.paddingLeft) -
        parseFloat(style.paddingRight);
      let count = 0;
      while (context.measureText("W".repeat(count + 1)).width <= width) count++;
      return {
        at: count + 3,
        point: {
          x:
            parseFloat(style.paddingLeft) +
            parseFloat(style.borderLeftWidth) +
            context.measureText("WWW").width,
          y:
            parseFloat(style.paddingTop) +
            parseFloat(style.borderTopWidth) +
            parseFloat(style.lineHeight) * 1.5,
        },
      };
    });
    const bodyChildren = await page.locator("body > *").count();
    await source.dragTo(control, { targetPosition: position.point });
    const before = text.slice(0, position.at),
      after = text.slice(position.at);
    await expect(control).toHaveValue(`${before}{{ input.email }}${after}`);
    expect(
      await control.evaluate(
        (element) => (element as HTMLTextAreaElement).selectionStart,
      ),
    ).toBe(position.at + 17);
    await expect(page.locator("body > *")).toHaveCount(bodyChildren);
    await page.screenshot({
      path: test.info().outputPath("wrapped-text-desktop.png"),
    });
    await details.close();
    expect(
      parse(await sourceText(page)).spec.steps.find(
        (s: { id: string }) => s.id === "summarize",
      ).prompt,
    ).toEqual({
      op: {
        name: "concat",
        args: [
          { literal: before },
          { ref: "/input/email" },
          { literal: after },
        ],
      },
    });
  });
  test("a drop beside Unicode text preserves complete characters and the second line", async ({
    page,
  }) => {
    const before = "Hello 🙂e\u0301👩‍💻",
      after = " world\nNext line";
    const details = await openConnectedFixture(
      page,
      mappedWorkflow({ literal: before + after }),
    );
    await details.openWithKeyboard("summarize");
    const input = details.dialog.locator(".sd-input");
    await input
      .getByRole("combobox", { name: "Input source" })
      .selectOption("input");
    const source = input.getByRole("listitem", {
      name: "email, Text",
      exact: true,
    });
    const control = details
      .field("prompt")
      .getByRole("textbox", { name: "Prompt" });
    const point = await control.evaluate((element, prefix) => {
      const style = getComputedStyle(element);
      const context = document.createElement("canvas").getContext("2d")!;
      context.font = style.font;
      return {
        x:
          parseFloat(style.paddingLeft) +
          parseFloat(style.borderLeftWidth) +
          context.measureText(prefix).width,
        y:
          parseFloat(style.paddingTop) +
          parseFloat(style.borderTopWidth) +
          parseFloat(style.lineHeight) / 2,
      };
    }, before);
    await source.dragTo(control, { targetPosition: point });
    await expect(control).toHaveValue(`${before}{{ input.email }}${after}`);
    expect(
      await control.evaluate(
        (element) => (element as HTMLTextAreaElement).selectionStart,
      ),
    ).toBe(before.length + 17);
    await details.close();
    expect(
      parse(await sourceText(page)).spec.steps.find(
        (s: { id: string }) => s.id === "summarize",
      ).prompt,
    ).toEqual({
      op: {
        name: "concat",
        args: [
          { literal: before },
          { ref: "/input/email" },
          { literal: after },
        ],
      },
    });
  });
});

for (const size of sizes) {
  test(`an existing number and row reference can be remapped and undone at ${size.tag}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: size.width, height: size.height });
    const workflow = parse(readFileSync(stepFixture, "utf8"));
    workflow.spec.steps.find(
      (step: { id: string }) => step.id === "lookup",
    ).with.object.limit = { ref: "/input/amount" };
    const details = await openConnectedFixture(
      page,
      yamlFile("remap.yaml", stringify(workflow)),
    );
    const showInput = async () => {
      if (size.width < 768)
        await details.dialog
          .getByRole("tablist", { name: "Step details panes" })
          .getByRole("tab", { name: "Input", exact: true })
          .click();
      return details.dialog.locator(".sd-input");
    };
    await details.openWithKeyboard("lookup");
    const input = await showInput();
    const amount = input.getByRole("listitem", {
      name: "amount, Number",
      exact: true,
    });
    if (size.width < 768) {
      await amount.focus();
      await page.keyboard.press("Enter");
      await page
        .getByRole("menu", { name: "Map amount to" })
        .getByRole("menuitem", { name: "Limit", exact: true })
        .click();
    } else
      await amount.dragTo(
        details
          .field("input.limit")
          .getByRole("button", { name: "reference Input Amount", exact: true }),
      );
    await expect(
      details.field("input.limit").getByRole("button", {
        name: "reference check-customer Amount",
        exact: true,
      }),
    ).toBeVisible();
    await expect(page.locator(".toast-region")).toContainText(
      "Replaced the previous mapping with check-customer › amount.",
    );
    await page
      .locator(".toast-region")
      .getByRole("button", { name: "Undo", exact: true })
      .click();
    await expect(
      details
        .field("input.limit")
        .getByRole("button", { name: "reference Input Amount", exact: true }),
    ).toBeVisible();
    await details.close();
    await details.openWithKeyboard("check-customer");
    const email = (await showInput()).getByRole("listitem", {
      name: "email, Text",
      exact: true,
    });
    const row = details.field("value").locator(".param-row-value").first();
    if (size.width < 768) {
      await email.focus();
      await page.keyboard.press("Enter");
      await page
        .getByRole("menu", { name: "Map email to" })
        .getByRole("menuitem", { name: "Value · customer", exact: true })
        .click();
    } else
      await email.dragTo(
        row.getByRole("button", {
          name: "reference Input Customer ID",
          exact: true,
        }),
      );
    await expect(
      row.getByRole("button", { name: "reference Input Email", exact: true }),
    ).toBeVisible();
    await expect(page.locator(".toast-region")).toContainText(
      "Replaced the previous mapping with Input › email.",
    );
    await page
      .locator(".toast-region")
      .getByRole("button", { name: "Undo", exact: true })
      .click();
    await expect(
      row.getByRole("button", {
        name: "reference Input Customer ID",
        exact: true,
      }),
    ).toBeVisible();
    await details.close();
    const stored = parse(await sourceText(page));
    expect(stored.spec.steps[0].value).toEqual(workflow.spec.steps[0].value);
    expect(
      stored.spec.steps.find((step: { id: string }) => step.id === "lookup")
        .with.object.limit,
    ).toEqual({ ref: "/input/amount" });
  });
  test(`a whole-object mapping has an accessible no-target menu at ${size.tag}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: size.width, height: size.height });
    const workflow = parse(readFileSync(stepFixture, "utf8"));
    workflow.spec.steps[0].value = { ref: "/input" };
    const details = await openStepFixture(
      page,
      yamlFile("whole.yaml", stringify(workflow)),
    );
    await details.openWithKeyboard("check-customer");
    if (size.width < 768)
      await details.dialog
        .getByRole("tablist", { name: "Step details panes" })
        .getByRole("tab", { name: "Input", exact: true })
        .click();
    const row = details.dialog
      .locator(".sd-input")
      .getByRole("listitem", { name: /^email, / });
    await row.focus();
    await page.keyboard.press("Enter");
    const menu = page.getByRole("menu", { name: "Map email to" });
    await expect(menu).toBeFocused();
    await expect(menu).toHaveText(/This step has no field that takes data/);
    await expect(menu.getByRole("menuitem")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await expect(row).toBeFocused();
    await expect(details.dialog).toBeVisible();
    await details.close();
    expect(parse(await sourceText(page)).spec.steps[0].value).toEqual({
      ref: "/input",
    });
  });
}

test.describe("drag cancellation and scrolling", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  const longFields = () => {
    const workflow = parse(readFileSync(stepFixture, "utf8"));
    workflow.spec.steps[0].value = {
      object: Object.fromEntries(
        Array.from({ length: 35 }, (_, i) => [`field${i}`, { literal: "" }]),
      ),
    };
    return workflow;
  };
  test("a sticky nested target wins and Escape cancels without changing data", async ({
    page,
  }) => {
    const workflow = longFields();
    const details = await openStepFixture(
      page,
      yamlFile("sticky.yaml", stringify(workflow)),
    );
    await details.openWithKeyboard("check-customer");
    const source = details.dialog
      .locator(".sd-input")
      .getByRole("listitem", { name: "email, Text", exact: true });
    const nested = details.field("value").locator(".param-row-value").first();
    const targetBox = (await nested.boundingBox())!;
    const control = nested.getByRole("textbox", { name: "Value", exact: true });
    const controlBox = (await control.boundingBox())!;
    await source.hover();
    await page.mouse.down();
    await page.mouse.move(
      targetBox.x + targetBox.width + 5,
      controlBox.y + controlBox.height / 2,
      { steps: 12 },
    );
    // Chromium emits dragover on a movement after entering the final element.
    await page.mouse.move(
      targetBox.x + targetBox.width + 5,
      controlBox.y + controlBox.height / 2,
    );
    await expect(nested).toHaveAttribute("data-drop-hover", "");
    await expect(nested).toHaveAttribute("data-fit", "fits");
    await expect(details.field("value")).not.toHaveAttribute(
      "data-drop-hover",
      "",
    );
    await page.screenshot({
      path: test.info().outputPath("sticky-drag-desktop.png"),
    });
    await page.keyboard.press("Escape");
    await page.mouse.up();
    await expect(
      details.dialog.locator("[data-drop-hover], [data-fit]"),
    ).toHaveCount(0);
    await expect(control).toHaveValue("");
    await source.dragTo(control);
    await expect(
      nested.getByRole("button", { name: /reference Input Email/ }),
    ).toBeVisible();
    await page.keyboard.press("ControlOrMeta+z");
    await expect(
      nested.getByRole("textbox", { name: "Value", exact: true }),
    ).toHaveValue("");
    await details.close();
    expect(parse(await sourceText(page)).spec.steps[0].value).toEqual(
      workflow.spec.steps[0].value,
    );
  });
  test("holding a dragged field near the bottom scrolls the Parameters pane", async ({
    page,
  }) => {
    const details = await openStepFixture(
      page,
      yamlFile("scroll.yaml", stringify(longFields())),
    );
    await details.openWithKeyboard("check-customer");
    const pane = details.dialog.locator("#sd-panel-parameters");
    const box = (await pane.boundingBox())!;
    const source = details.dialog
      .locator(".sd-input")
      .getByRole("listitem", { name: "email, Text", exact: true });
    await source.hover();
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height - 12, {
      steps: 12,
    });
    await expect
      .poll(() => pane.evaluate((element) => element.scrollTop))
      .toBeGreaterThan(48);
    await page.screenshot({
      path: test.info().outputPath("autoscroll-desktop.png"),
    });
    await page.keyboard.press("Escape");
    await page.mouse.up();
    await expect(
      details.dialog.locator("[data-drop-hover], [data-fit]"),
    ).toHaveCount(0);
    await details.close();
  });
});

for (const size of sizes) {
  test(`mapping leaves an unapplied number intact at ${size.tag}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: size.width, height: size.height });
    const details = await openConnectedFixture(page, withLimit());
    await details.openWithKeyboard("lookup");
    const limit = details
      .field("input.limit")
      .getByRole("textbox", { name: "Limit" });
    await limit.fill("unfinished");
    if (size.width < 768)
      await details.dialog
        .getByRole("tablist", { name: "Step details panes" })
        .getByRole("tab", { name: "Input", exact: true })
        .click();
    const row = details.dialog
      .locator(".sd-input")
      .getByRole("listitem", { name: /^amount, / });
    await row.focus();
    await page.keyboard.press("Enter");
    await page
      .getByRole("menu", { name: "Map amount to" })
      .getByRole("menuitem", { name: "Limit", exact: true })
      .click();
    await expect(details.dialog.locator("[aria-live=polite]")).toHaveText(
      /unapplied value/,
    );
    if (size.width < 768)
      await details.dialog
        .getByRole("tablist", { name: "Step details panes" })
        .getByRole("tab", { name: "Parameters", exact: true })
        .click();
    await expect(limit).toHaveValue("unfinished");
    if (size.width >= 768) {
      await row.dragTo(limit);
      await expect(limit).toHaveValue("unfinished");
    }
    await details.close();
    expect(
      parse(await sourceText(page)).spec.steps.find(
        (s: { id: string }) => s.id === "lookup",
      ).with.object.limit,
    ).toEqual({ literal: 25 });
  });
}

test("Add all fields keeps an unapplied child draft and all existing rows", async ({
  page,
}) => {
  const workflow = parse(readFileSync(stepFixture, "utf8"));
  workflow.spec.steps[0].value.object.payload = { literal: { keep: "value" } };
  const details = await openStepFixture(
    page,
    yamlFile("draft.yaml", stringify(workflow)),
  );
  await details.openWithKeyboard("check-customer");
  const invalid = details.field("value").locator("textarea");
  await invalid.fill("{");
  await details
    .field("value")
    .getByRole("button", { name: "Add all fields", exact: true })
    .click();
  await expect(invalid).toHaveValue("{");
  await expect(details.dialog.locator("[aria-live=polite]")).toHaveText(
    /unapplied value/,
  );
  await expect(
    details
      .field("value")
      .getByRole("textbox", { name: "Name of row 4", exact: true }),
  ).toHaveCount(0);
  await details.close();
  expect(parse(await sourceText(page)).spec.steps[0].value).toEqual(
    workflow.spec.steps[0].value,
  );
});
