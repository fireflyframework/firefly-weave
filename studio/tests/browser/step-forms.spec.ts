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
import { expect, test, type Page } from "@playwright/test";
import {
  openStepFixture,
  sizes,
  stepFixture,
  StepDetailsPage,
  withNewEditor,
  yamlFile,
} from "./step-details-po";
import { connected, lookupAction, offline, sourceText } from "./support";
import {
  hasPython,
  localAuthoring,
  stopLocalAuthoring,
} from "./local-authoring";
import {
  box,
  boxes,
  clippedText,
  contentEdges,
  density,
  expectAligned,
  formScroller,
  paintedOpacity,
  showParameters,
  textBox,
  visibleArea,
  type Box,
} from "./step-geometry";

async function openObjectItems(page: Page) {
  await withNewEditor(page);
  const action = structuredClone(lookupAction);
  action.spec.inputSchema = {
    type: "object",
    required: ["items"],
    properties: {
      items: {
        type: "array",
        title: "Items",
        items: {
          type: "object",
          title: "Address",
          required: ["city"],
          properties: { city: { type: "string", title: "City" } },
        },
      },
    },
  } as never;
  const recorded = await connected(page, {
    catalog: [{ id: "addresses", document: action }],
  });
  const opaque = {
    op: { name: "custom.opaque", args: [{ ref: "/input/address" }] },
    extension: true,
  };
  const workflow = parse(readFileSync(stepFixture, "utf8"));
  workflow.spec.steps = [
    {
      id: "addresses",
      kind: "action",
      uses: "sql.lookup@1.0.0",
      with: {
        object: {
          items: {
            array: [
              { ref: "/input/address" },
              opaque,
              { literal: "malformed" },
              { object: { city: { literal: "Before" } } },
            ],
          },
        },
      },
    },
  ];
  workflow.spec.inputSchema.properties.address = {
    type: "object",
    title: "Address",
    properties: { city: { type: "string" } },
  };
  await page
    .getByLabel("Choose a workflow file")
    .setInputFiles(yamlFile("addresses.yaml", stringify(workflow)));
  await expect.poll(() => recorded.exports).toContain("addresses");
  const details = new StepDetailsPage(page);
  const show = page.getByRole("button", { name: "Show canvas", exact: true });
  if (await show.isVisible()) await show.click();
  await details.openWithKeyboard("addresses");
  return { details, opaque };
}

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

    test("successive chip keyboard moves keep focus on the moved answer", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("notify-sales");
      const answers = details.field("answers");
      await answers
        .getByRole("textbox", { name: "Add answer" })
        .fill("Escalate");
      await answers.getByRole("textbox", { name: "Add answer" }).press("Enter");
      const third = answers.getByRole("textbox", { name: "Answer 3 of 3" });
      await third.focus();
      await page.keyboard.press("Alt+ArrowUp");
      const second = answers.getByRole("textbox", { name: "Answer 2 of 3" });
      await expect(second).toHaveValue("escalate");
      await expect(second).toBeFocused();
      await page.keyboard.press("Alt+ArrowUp");
      const first = answers.getByRole("textbox", { name: "Answer 1 of 3" });
      await expect(first).toHaveValue("escalate");
      await expect(first).toBeFocused();
      await page.keyboard.press("Alt+ArrowDown");
      await expect(second).toHaveValue("escalate");
      await expect(second).toBeFocused();
      await details.dialog.screenshot({
        path: resolve(`../build/editor-m3/task-13-fix1/chips-${size.tag}.png`),
      });
      await details.close();
      const saved = parse(await sourceText(page));
      expect(saved.spec.steps[2].cases[0].steps[0].decisions).toEqual([
        "approve",
        "escalate",
        "reject",
      ]);
    });

    test("structural Reset menus protect populated paths and branches while empty path removal remains undoable", async ({
      page,
    }) => {
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      const details = await openStepFixture(page);
      const before = await sourceText(page);
      await details.openWithKeyboard("route-by-value");
      const cases = details.field("cases");
      const rows = cases.locator(".param-row.is-card");
      await rows
        .first()
        .getByRole("button", { name: "Field menu for Path", exact: true })
        .click();
      let reset = page
        .getByRole("menu", { name: "Field menu for Path", exact: true })
        .getByRole("menuitem", { name: /Reset to default/ });
      await expect(reset).toHaveAttribute("aria-disabled", "true");
      await expect(reset).toContainText("Move or delete its steps first.");
      await page.screenshot({
        path: resolve(
          `../build/editor-m3/task-13-fix2/protected-path-${size.tag}.png`,
        ),
      });
      await reset.focus();
      await reset.press("Enter");
      await page.keyboard.press("Escape");
      await expect(rows).toHaveCount(2);
      await cases
        .getByRole("button", { name: "Field menu for Paths", exact: true })
        .click();
      reset = page
        .getByRole("menu", { name: "Field menu for Paths", exact: true })
        .getByRole("menuitem", { name: /Reset to default/ });
      await expect(reset).toHaveAttribute("aria-disabled", "true");
      await expect(reset).toContainText("Move or delete its steps first.");
      await reset.focus();
      await reset.press("Enter");
      await page.keyboard.press("Escape");
      await details.close();
      expect(await sourceText(page)).toBe(before);
      await details.openWithKeyboard("fan-out");
      await details
        .field("branches")
        .getByRole("button", { name: "Field menu for Branches", exact: true })
        .click();
      reset = page
        .getByRole("menu", { name: "Field menu for Branches", exact: true })
        .getByRole("menuitem", { name: /Reset to default/ });
      await expect(reset).toHaveAttribute("aria-disabled", "true");
      await expect(reset).toContainText("Move or delete its steps first.");
      await reset.focus();
      await reset.press("Enter");
      await page.keyboard.press("Escape");
      await details.close();
      expect(await sourceText(page)).toBe(before);
      await details.openWithKeyboard("route-by-value");
      await rows
        .nth(1)
        .getByRole("button", { name: "Field menu for Path", exact: true })
        .click();
      reset = page
        .getByRole("menu", { name: "Field menu for Path", exact: true })
        .getByRole("menuitem", { name: /Reset to default/ });
      await expect(reset).not.toHaveAttribute("aria-disabled", "true");
      await reset.click();
      await expect(rows).toHaveCount(1);
      await page.keyboard.press("ControlOrMeta+z");
      await expect(rows).toHaveCount(2);
      await details.close();
      expect(await sourceText(page)).toBe(before);
      expect(errors).toEqual([]);
    });

    test("a final empty required path's Reset menu explains the minimum and keeps it", async ({
      page,
    }) => {
      const workflow = parse(readFileSync(stepFixture, "utf8"));
      workflow.spec.steps[2].cases = [
        { when: { literal: true }, steps: [], output: { literal: {} } },
      ];
      const details = await openStepFixture(
        page,
        yamlFile("one-path.yaml", stringify(workflow)),
      );
      const before = await sourceText(page);
      await details.openWithKeyboard("route-by-value");
      const row = details.field("cases").locator(".param-row.is-card");
      await row
        .getByRole("button", { name: "Field menu for Path", exact: true })
        .click();
      const reset = page
        .getByRole("menu", { name: "Field menu for Path", exact: true })
        .getByRole("menuitem", { name: /Reset to default/ });
      await expect(reset).toHaveAttribute("aria-disabled", "true");
      await expect(reset).toContainText("Keep at least 1.");
      await page.screenshot({
        path: resolve(
          `../build/editor-m3/task-13-fix2/required-path-${size.tag}.png`,
        ),
      });
      await reset.focus();
      await reset.press("Enter");
      await page.keyboard.press("Escape");
      await expect(row).toHaveCount(1);
      await details.close();
      expect(await sourceText(page)).toBe(before);
    });

    test("object-list cards expose each item's mapping and malformed import before its children", async ({
      page,
    }) => {
      const { details, opaque } = await openObjectItems(page);
      const rows = details.field("input.items").locator(".param-row.is-card");
      await expect(rows).toHaveCount(4);
      await expect(
        rows.nth(0).getByRole("button", { name: /reference Input Address/ }),
      ).toBeVisible();
      await expect(
        rows.nth(0).getByRole("textbox", { name: "City", exact: true }),
      ).toHaveCount(0);
      await expect(rows.nth(1).locator(".param-formula")).toHaveText(
        JSON.stringify(opaque),
      );
      await expect(
        rows.nth(1).getByRole("textbox", { name: "City", exact: true }),
      ).toHaveCount(0);
      await expect(
        rows
          .nth(2)
          .getByText(
            "This imported value is edited with Edit as YAML for now.",
            { exact: true },
          ),
      ).toBeVisible();
      await expect(
        rows.nth(2).getByRole("textbox", { name: "City", exact: true }),
      ).toHaveCount(0);
      await details.dialog.screenshot({
        path: resolve(
          `../build/editor-m3/task-13-fix1/object-items-${size.tag}.png`,
        ),
      });
      await rows
        .nth(3)
        .getByRole("textbox", { name: "City", exact: true })
        .fill("Allowed");
      await rows
        .nth(0)
        .getByRole("button", { name: /reference Input Address/ })
        .click();
      const picker = rows
        .nth(0)
        .getByRole("combobox", { name: "Address", exact: true });
      await picker.fill("/input/customerId");
      await picker.press("Enter");
      await expect(
        rows
          .nth(0)
          .getByRole("button", { name: /reference Input Customer ID/ }),
      ).toBeVisible();
      await rows
        .nth(0)
        .getByRole("radio", { name: "Fixed", exact: true })
        .click();
      await page
        .getByRole("dialog", {
          name: "Replace the mapping with a fixed value?",
        })
        .getByRole("button", { name: "Keep the mapping" })
        .click();
      await expect(
        rows
          .nth(0)
          .getByRole("button", { name: /reference Input Customer ID/ }),
      ).toBeVisible();
      await details.close();
      const saved = parse(await sourceText(page));
      expect(saved.spec.steps[0].with.object.items.array).toEqual([
        { ref: "/input/customerId" },
        opaque,
        { literal: "malformed" },
        { literal: { city: "Allowed" } },
      ]);
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
      // Rows are 32 px on desktop with a fine pointer and 44 px for touch;
      // no target is under 24 px.
      const sizes = await density(page);
      for (const control of await rows.locator("input, button").all()) {
        if (!(await control.isVisible())) continue;
        await control.scrollIntoViewIfNeeded();
        const box = await control.boundingBox();
        const button = await control.evaluate((element) =>
          element.matches("button"),
        );
        expect(box!.height).toBeGreaterThanOrEqual(
          button ? sizes.target : sizes.row,
        );
        if (button) expect(box!.width).toBeGreaterThanOrEqual(sizes.target);
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

    test("object-list card controls keep their target size and fit narrow forms", async ({
      page,
    }) => {
      const { details } = await openObjectItems(page);
      const items = details.field("input.items");
      const sizes = await density(page);
      for (const control of await items.locator("input, button").all()) {
        if (!(await control.isVisible())) continue;
        await control.scrollIntoViewIfNeeded();
        const box = await control.boundingBox();
        expect(box!.height).toBeGreaterThanOrEqual(sizes.target);
        expect(box!.width).toBeGreaterThanOrEqual(sizes.target);
        expect(box!.x).toBeGreaterThanOrEqual(0);
        expect(box!.x + box!.width).toBeLessThanOrEqual(size.width);
        expect(
          await control.evaluate((element) => {
            const rect = element.getBoundingClientRect();
            const hit = document.elementFromPoint(
              rect.left + rect.width / 2,
              rect.top + rect.height / 2,
            );
            return !!hit && (hit === element || element.contains(hit));
          }),
        ).toBe(true);
      }
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
      ).toBeLessThanOrEqual(size.width);
      await items.locator(".param-row").first().scrollIntoViewIfNeeded();
      await details.dialog.screenshot({
        path: resolve(
          `../build/editor-m3/task-13-fix1/object-geometry-${size.tag}.png`,
        ),
      });
      await details.close();
    });

    test("collection rows and chips fit the scrolling form with their target sizes", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("check-customer");
      const values = details.field("value");
      const sizes = await density(page);
      for (const control of await values.locator("input, button").all()) {
        if (!(await control.isVisible())) continue;
        await control.scrollIntoViewIfNeeded();
        const box = await control.boundingBox();
        expect(box!.height).toBeGreaterThanOrEqual(sizes.target);
        expect(box!.width).toBeGreaterThanOrEqual(sizes.target);
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
        expect(box!.height).toBeGreaterThanOrEqual(sizes.target);
        expect(box!.width).toBeGreaterThanOrEqual(sizes.target);
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

/** Every top-level field of the visible tab: its label, control, menu and Add actions. */
async function formEdges(details: StepDetailsPage) {
  const panel = details.dialog.locator(".sd-tabpanel:not([hidden])");
  const edges = await contentEdges(panel);
  const labels = await panel
    .locator(
      ".param-form > weave-param-field > .param > .param-label-row .param-label:not(.sr-only)",
    )
    .evaluateAll((elements) =>
      elements
        .filter((element) => element.getClientRects().length)
        .map((element) => {
          const range = document.createRange();
          range.selectNodeContents(element);
          return range.getClientRects()[0].x;
        }),
    );
  const controls = await boxes(
    panel.locator(
      ".param-form > weave-param-field:not(.is-option) > .param > .param-control > :first-child",
    ),
  );
  const cards = await boxes(
    panel.locator(".param-form > weave-param-field.is-option > .param"),
  );
  const adds = await boxes(
    panel.locator(
      ".param-form > weave-param-field:not(.is-option) > .param > .param-control > weave-key-value-field .param-add-row > .param-add:first-child > weave-icon, .param-form > weave-add-option",
    ),
  );
  // The last tool of each label row: the value mode, or the field menu.
  const menus = await boxes(
    panel.locator(
      ".param-form > weave-param-field > .param > .param-label-row > .param-tools > :last-child",
    ),
  );
  return { edges, labels, controls, cards, adds, menus };
}

const formSizes = [
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "600x500", width: 600, height: 500 },
  { tag: "360x640", width: 360, height: 640 },
  { tag: "640x360", width: 640, height: 360 },
];
for (const size of formSizes)
  test.describe(`parameter form alignment at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("fields share the pane's left and right edges, and hints wrap instead of clipping", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      for (const id of ["lookup", "check-customer", "summarize", "score"]) {
        await details.open(id);
        await showParameters(page, details.dialog);
        const form = await formEdges(details);
        expect(form.labels.length).toBeGreaterThan(0);
        expectAligned(
          [
            form.edges.left,
            ...form.labels,
            ...form.controls.map((c) => c.x),
            ...form.cards.map((c) => c.x),
            ...form.adds.map((c) => c.x),
          ],
          `${id} left edge`,
        );
        expectAligned(
          [form.edges.right, ...form.menus.map((m) => m.right)],
          `${id} label-row tools right edge`,
        );
        // Labels and their required marks share a baseline.
        for (const row of await details.dialog
          .locator(
            ".sd-tabpanel:not([hidden]) .param-label-row:has(.param-required)",
          )
          .all()) {
          if (!(await row.isVisible())) continue;
          const label = await textBox(row.locator(".param-label"));
          const mark = await textBox(row.locator(".param-required"));
          expectAligned([label.bottom, mark.bottom], `${id} required mark`);
        }
        expect(await clippedText(details.dialog)).toEqual([]);
        await details.close();
      }
    });

    test("key and value rows keep one height and one center line", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("check-customer");
      await showParameters(page, details.dialog);
      const field = details.field("value");
      const rows = field.locator(".param-kv-row:not(.is-blank)");
      await expect(rows).toHaveCount(2);
      const label = await box(
        field.locator(":scope > .param > .param-label-row"),
      );
      const first = await box(rows.first());
      // No empty band between the field's label row and its first row.
      expect(first.y - label.bottom).toBeLessThanOrEqual(8.5);
      const right = (
        await contentEdges(details.dialog.locator(".sd-tabpanel:not([hidden])"))
      ).right;
      const trailing = [right];
      for (const row of await rows.all()) {
        // Hovering shows the field menu without moving anything.
        await row.scrollIntoViewIfNeeded();
        const resting = await box(row.locator(".param-row-value .param-mode"));
        await row.hover();
        expect(await box(row.locator(".param-row-value .param-mode"))).toEqual(
          resting,
        );
        const key = await box(row.locator(".param-kv-name"));
        const value = await box(row.locator(".param-row-value"));
        const remove = await box(row.locator(":scope > button"));
        const grip = await box(row.locator(":scope > .param-grip"));
        const mode = await box(row.locator(".param-row-value .param-mode"));
        trailing.push(remove.right);
        const sideBySide = value.y < key.bottom;
        if (sideBySide) {
          expectAligned(
            [key.height, value.height, remove.height],
            "row heights",
          );
          expectAligned(
            [key.middle, value.middle, remove.middle, grip.middle],
            "row centers",
          );
          expectAligned([key.y, value.y], "key and value tops");
        } else {
          // Too narrow for both: the value goes under its key, on the key's edge.
          expectAligned(
            [key.middle, remove.middle, grip.middle],
            "row centers",
          );
          expectAligned([key.x, value.x], "key and value edge");
          expectAligned([value.y, key.bottom + 8], "value under its key");
        }
        if (size.width >= 768) expect(sideBySide).toBe(true);
        // Fixed | Mapped sits inside the value's box, on its right inset; the
        // field menu that appears on hover sits inboard of it.
        expect(mode.x).toBeGreaterThanOrEqual(value.x);
        expect(mode.y).toBeGreaterThanOrEqual(value.y);
        expect(mode.bottom).toBeLessThanOrEqual(value.bottom);
        expectAligned(
          [mode.right, value.right - 12],
          "toggle on the value's inset",
          2,
        );
        const menu = await box(
          row.locator(".param-row-value .param-menu .row-menu-toggle"),
        );
        expect(menu.right).toBeLessThanOrEqual(mode.x + 0.5);
      }
      // Remove buttons end on the pane's right edge.
      expectAligned(trailing, "row trailing edge");
      // Add actions read as actions, not links: no underline at rest.
      for (const add of await field.locator(".param-add").all())
        await expect(add).toHaveCSS("text-decoration-line", "none");
    });

    test("a decision introduces its paths once, wraps the intro and starts each card's body under its header", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("route-by-value");
      await showParameters(page, details.dialog);
      const panel = details.dialog.locator(".sd-tabpanel:not([hidden])");
      const visiblePaths = await panel
        .locator(".param-label:not(.sr-only)")
        .filter({ hasText: /^Paths$/ })
        .count();
      expect(visiblePaths).toBeLessThanOrEqual(1);
      const intro = panel.locator(".param-intro");
      await expect(intro).toHaveText(
        "Paths are checked from top to bottom. Each path is an output on the canvas, labeled with its condition.",
      );
      await expect(intro).toBeVisible();
      // The intro is said once: not repeated as a hint under the paths.
      await expect(
        panel
          .getByText("Paths are checked from top to bottom.", { exact: false })
          .filter({ visible: true }),
      ).toHaveCount(1);
      const cards = panel.locator(".param-row.is-card");
      await expect(cards).toHaveCount(2);
      expect((await box(intro)).bottom).toBeLessThanOrEqual(
        (await box(cards.first())).y,
      );
      // One field menu for the paths: the structural group around them has none.
      await expect(
        panel.getByRole("button", { name: "Field menu for Paths" }),
      ).toHaveCount(1);
      // Step details opens on a control, never on a label row's tools.
      expect(
        await page.evaluate(
          () =>
            !!document.activeElement?.closest(".param-label-row, .param-tools"),
        ),
      ).toBe(false);
      // A card's header ends on its field menu, shown at rest on the card's inner edge.
      for (const card of await cards.all()) {
        const menu = card.locator(
          ":scope > weave-param-field > .param > .param-label-row .param-menu .row-menu-toggle",
        );
        expect(await paintedOpacity(menu)).toBeGreaterThan(0);
        const inner = await contentEdges(
          card.locator(
            ":scope > weave-param-field > .param > .param-label-row",
          ),
        );
        expectAligned(
          [(await box(menu)).right, inner.right],
          "card menu edge",
          2,
        );
        const actions = await box(card.locator(".param-card-actions"));
        expect(actions.right).toBeLessThanOrEqual((await box(menu)).x + 0.5);
        // The move and remove buttons stay on the header row, beside the title.
        const title = await box(card.locator(".param-card-head strong"));
        expect(Math.abs(actions.middle - title.middle)).toBeLessThanOrEqual(2);
      }
      for (const card of await cards.all()) {
        // The card's header band: its title, chip, next step and buttons.
        const head = await box(
          card.locator(
            ":scope > weave-param-field > .param > .param-label-row",
          ),
        );
        expect(
          await card
            .locator(":scope > weave-param-field > .param > .param-label-row")
            .getByRole("button", { name: /^Remove Path/ })
            .count(),
        ).toBe(1);
        const body = await box(
          card.locator(".param-control weave-param-field").first(),
        );
        expect(body.y - head.bottom).toBeLessThanOrEqual(16);
      }
      const add = panel.getByRole("button", { name: "Add path", exact: true });
      await expect(add).toHaveCSS("border-top-style", "dashed");
      await expect(add).toHaveCSS("text-decoration-line", "none");
      expect(await clippedText(details.dialog)).toEqual([]);
    });

    test("Add option lists its options start-aligned", async ({ page }) => {
      const details = await openStepFixture(page);
      await details.open("summarize");
      await showParameters(page, details.dialog);
      const add = details.dialog.getByRole("button", {
        name: "Add option",
        exact: true,
      });
      await add.scrollIntoViewIfNeeded();
      await add.click();
      const menu = details.dialog.getByRole("menu", { name: "Add option" });
      await expect(menu).toBeVisible();
      const menuBox = await box(menu);
      const starts: number[] = [];
      for (const item of await menu.getByRole("menuitem").all()) {
        const itemBox = await box(item);
        expect(itemBox.height).toBeGreaterThanOrEqual(
          size.width < 768 ? 44 : 24,
        );
        starts.push((await textBox(item.locator("b"))).x);
      }
      const menuEdge = (await contentEdges(menu)).left;
      expect(menuEdge).toBeGreaterThan(menuBox.x);
      expectAligned([menuEdge + 12, ...starts], "menu labels start");
      // The whole menu is in view inside the pane, with focus on its first item.
      const area = await visibleArea(formScroller(page, details.dialog));
      expect(menuBox.y).toBeGreaterThanOrEqual(area.y - 0.5);
      expect(menuBox.bottom).toBeLessThanOrEqual(area.bottom + 0.5);
      expect(menuBox.x).toBeGreaterThanOrEqual(area.x - 0.5);
      expect(menuBox.right).toBeLessThanOrEqual(area.right + 0.5);
      await expect(menu.getByRole("menuitem").first()).toBeFocused();
    });

    test("visible trailing controls end on the pane's right edge at rest", async ({
      page,
    }) => {
      const workflow = parse(readFileSync(stepFixture, "utf8"));
      workflow.spec.steps[3].prompt = { ref: "/input/customerId" };
      const details = await openStepFixture(
        page,
        yamlFile("mapped-prompt.yaml", stringify(workflow)),
      );
      await details.open("summarize");
      await showParameters(page, details.dialog);
      await page.mouse.move(1, 1);
      const panel = details.dialog.locator(".sd-tabpanel:not([hidden])");
      const right = (await contentEdges(panel)).right;
      const prompt = details.field("prompt");
      const mode = prompt.locator(".param-mode");
      await prompt.scrollIntoViewIfNeeded();
      expect(await paintedOpacity(mode)).toBe(1);
      const resting = await box(mode);
      expectAligned([resting.right, right], "Prompt toggle edge", 2);
      // The field menu appears inboard of the toggle, and nothing moves.
      await prompt.hover();
      const menu = prompt.getByRole("button", {
        name: "Field menu for Prompt",
      });
      expect(await paintedOpacity(menu)).toBe(1);
      expect((await box(menu)).right).toBeLessThanOrEqual(resting.x + 0.5);
      expect(await box(mode)).toEqual(resting);
      // Label-row targets stay clear of the control below them.
      for (const id of ["model", "prompt"]) {
        const field = details.field(id);
        const control = await box(
          field.locator(":scope > .param > .param-control"),
        );
        for (const target of await boxes(
          field.locator(":scope > .param > .param-label-row button"),
        ))
          expect(target.bottom).toBeLessThanOrEqual(control.y + 0.5);
      }
    });

    test("in a key and value row, Tab reaches the value before its tools", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("check-customer");
      await showParameters(page, details.dialog);
      const row = details.field("value").locator(".param-kv-row").first();
      await row.getByRole("textbox", { name: "Name of row 1" }).focus();
      await page.keyboard.press("Tab");
      await expect(
        row.getByRole("button", { name: "reference Input Customer ID" }),
      ).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(
        row.getByRole("button", { name: "Field menu for Value" }),
      ).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(
        row.getByRole("radio", { name: "Mapped", exact: true }),
      ).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(
        row.getByRole("button", { name: "Remove customer" }),
      ).toBeFocused();
    });

    test("in-place choices open downward and take their content's width", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("summarize");
      await showParameters(page, details.dialog);
      const model = details.field("model");
      await expect(
        model
          .getByRole("button", { name: "Show choices for Model" })
          .locator("weave-icon"),
      ).toHaveAttribute("data-icon", "chevronDown");
      const control = await box(model.locator(".choice-field"));
      const edges = await contentEdges(
        details.dialog.locator(".sd-tabpanel:not([hidden])"),
      );
      if (size.width >= 600)
        expect(control.width).toBeLessThan((edges.right - edges.left) * 0.75);
      const prompt = await box(details.field("prompt").locator(".param-input"));
      // One control height for the form: 36 px on desktop, 44 px for touch.
      const sizes = await density(page);
      expectAligned([control.height, sizes.control], "control height");
      expect(prompt.height).toBeGreaterThanOrEqual(sizes.control);
    });
  });

const httpWorkflow = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: order-intake, version: 1.3.0}
spec:
  inputSchema:
    type: object
    properties:
      customerId: {type: string}
      status: {type: string}
  outputSchema: {type: object}
  connections:
    crm: {connector: weave-http@2.0.0, required: true}
  steps:
    - id: get-customer-orders
      kind: action
      uses: order-intake.get-customer-orders@1.0.0
      connection: crm
      with:
        object:
          path:
            object:
              customerId: {ref: /input/customerId}
          query:
            object:
              limit: {literal: "25"}
              status: {ref: /input/status}
  output: {literal: {}}
`;
/** Opens a workflow kept on this computer whose step calls an API the workflow owns. */
async function openHttpStep(page: Page) {
  await withNewEditor(page);
  await page.addInitScript((source) => {
    const id = "2f6c1a40-0000-4000-8000-000000000001";
    const savedAt = "2026-10-11T10:04:00.000Z";
    localStorage.setItem(
      `weave.localDraft.${id}`,
      JSON.stringify({
        source,
        format: "yaml",
        layout: null,
        canvas: {
          schemaVersion: 2,
          kind: "weave.studio/canvas",
          ownedActions: ["order-intake.get-customer-orders"],
          actionRecipes: {
            "order-intake.get-customer-orders": {
              kind: "http",
              method: "GET",
              pathTemplate: "/customers/{customerId}/orders",
              statuses: [200],
              timeoutSeconds: 30,
            },
          },
        },
        savedAt,
      }),
    );
    localStorage.setItem(
      "weave.localDrafts.v1",
      JSON.stringify([{ id, name: "order-intake", version: "1.3.0", savedAt }]),
    );
  }, httpWorkflow);
  await localAuthoring(page);
  await offline(page);
  await page
    .locator(".preview-row", { hasText: "order-intake" })
    .first()
    .click();
  await expect(page.locator(".editor-bar")).toBeVisible();
  const show = page.getByRole("button", { name: "Show canvas", exact: true });
  const canvas = page.getByLabel("Workflow canvas", { exact: true });
  await expect(canvas.or(show).first()).toBeVisible();
  if (await show.isVisible()) await show.click();
  const details = new StepDetailsPage(page);
  await details.open("get-customer-orders");
  return details;
}

test.describe("a Call an API step", () => {
  test.skip(!hasPython, "Needs the repository's Python environment.");
  test.use({ viewport: { width: 1440, height: 900 } });
  test.afterAll(() => stopLocalAuthoring());

  test("Method, query parameters and Add option follow the field anatomy", async ({
    page,
  }) => {
    const details = await openHttpStep(page);
    const method = details.field("method");
    await expect(
      method
        .getByRole("button", { name: "Show choices for Method" })
        .locator("weave-icon"),
    ).toHaveAttribute("data-icon", "chevronDown");
    const edges = await contentEdges(
      details.dialog.locator(".sd-tabpanel:not([hidden])"),
    );
    const methodBox = await box(method.locator(".choice-field"));
    expect(methodBox.width).toBeLessThan((edges.right - edges.left) / 2);
    const connection = await box(
      details.field("connection").locator(".choice-field"),
    );
    expectAligned([methodBox.height, connection.height], "picker heights");
    expectAligned([connection.right, edges.right], "connection right edge");
    // Query parameters: one line per row, Fixed | Mapped inside the value.
    const query = details.field("query");
    const rows = query.locator(".param-kv-row");
    await expect(rows).toHaveCount(2);
    const status = rows.nth(1);
    const key = await box(status.locator(".param-kv-name"));
    const value = await box(status.locator(".param-row-value"));
    const mode = await box(status.locator(".param-mode"));
    expectAligned([key.y, value.y], "key and value tops");
    expectAligned([key.middle, value.middle, mode.middle], "row centers");
    expectAligned(
      [mode.right, value.right - 12],
      "toggle on the value's inset",
      2,
    );
    expect(mode.x).toBeGreaterThanOrEqual(value.x);
    const card = await box(query.locator(":scope > .param"));
    const label = await box(
      query.locator(":scope > .param > .param-label-row"),
    );
    expect((await box(rows.first())).y - label.bottom).toBeLessThanOrEqual(8.5);
    // Row remove buttons and the option's last tool end on the card's inner edge;
    // Remove option, shown on hover, sits inboard of the field menu.
    const trash = await boxes(
      query.locator(
        ".param-kv-row > button, :scope > .param > .param-label-row > .param-tools > :last-child",
      ),
    );
    expectAligned(
      trash.map((t: Box) => t.right),
      "remove buttons right edge",
    );
    expectAligned([card.right - 1 - 12, trash[0].right], "card inner edge");
    const removeOption = await box(
      query.getByRole("button", { name: "Remove option" }),
    );
    expect(removeOption.right).toBeLessThanOrEqual(trash.at(-1)!.x + 0.5);
    // The Add option menu: labels start-aligned, Body disabled with its reason.
    const add = details.dialog.getByRole("button", {
      name: "Add option",
      exact: true,
    });
    await add.scrollIntoViewIfNeeded();
    await add.click();
    const menu = details.dialog.getByRole("menu", { name: "Add option" });
    const body = menu.getByRole("menuitem", { name: /^Body/ });
    await expect(body).toHaveAttribute("aria-disabled", "true");
    await expect(body).toContainText("Only for POST, PUT and PATCH");
    await expect(menu.getByRole("menuitem").first()).toBeFocused();
    const menuBox = await box(menu);
    const area = await visibleArea(formScroller(page, details.dialog));
    expect(menuBox.y).toBeGreaterThanOrEqual(area.y - 0.5);
    expect(menuBox.bottom).toBeLessThanOrEqual(area.bottom + 0.5);
    // At this size every option shows without scrolling the menu.
    expect(
      await menu.evaluate(
        (element) => element.scrollHeight <= element.clientHeight + 1,
      ),
    ).toBe(true);
    const starts: number[] = [];
    for (const item of await menu.getByRole("menuitem").all())
      starts.push((await textBox(item.locator("b"))).x);
    expectAligned(
      [(await contentEdges(menu)).left + 12, ...starts],
      "menu labels",
    );
  });
});

for (const size of [
  { tag: "1440x900", width: 1440, height: 900, touch: false },
  { tag: "600x500", width: 600, height: 500, touch: false },
  { tag: "1440x900-touch", width: 1440, height: 900, touch: true },
])
  test.describe(`step details density at ${size.tag}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
    });
    // Desktop with a fine pointer follows the reference's density: fields
    // 36 px, rows 32 px, 24 px label rows, 12 px between fields and 4 px from
    // a label row to its control (the reference has 22, 14 and 6). Touch
    // keeps 44 px controls with 16 px and 8 px.
    test("fields, rows and gaps follow the density", async ({ page }) => {
      const details = await openStepFixture(page);
      const sizes = await density(page);
      expect(sizes.touch).toBe(size.width < 768 || size.touch);
      await details.open("summarize");
      await showParameters(page, details.dialog);
      const model = details.field("model");
      const prompt = details.field("prompt");
      expectAligned(
        [(await box(model.locator(".choice-field"))).height, sizes.control],
        "select height",
      );
      const modelBox = await box(model);
      const promptBox = await box(prompt);
      expectAligned(
        [promptBox.y - modelBox.bottom, sizes.fieldGap],
        "field gap",
      );
      const label = await box(
        prompt.locator(":scope > .param > .param-label-row"),
      );
      expectAligned([label.height, sizes.labelRow], "label row");
      const control = await box(
        prompt.locator(":scope > .param > .param-control"),
      );
      expectAligned(
        [control.y - label.bottom, sizes.fieldInner],
        "label to control",
      );
      await details.close();
      await details.openWithKeyboard("lookup");
      await showParameters(page, details.dialog);
      const action = details.field("action");
      expectAligned(
        [
          (await box(action.locator(".param-segments"))).height,
          (await box(action.locator(".param-input, .choice-field").first()))
            .height,
          (await box(details.field("connection").locator(".choice-field")))
            .height,
          sizes.control,
        ],
        "control heights",
      );
      await details.close();
      await details.openWithKeyboard("check-customer");
      await showParameters(page, details.dialog);
      const row = details.field("value").locator(".param-kv-row").first();
      expectAligned(
        [
          (await box(row.locator(".param-kv-name"))).height,
          (await box(row.locator(".param-row-value"))).height,
          (await box(row.locator(":scope > button"))).height,
          sizes.row,
        ],
        "key and value row",
      );
      if (size.width >= 768) {
        const rows = await boxes(details.dialog.locator(".sd-input .data-row"));
        expectAligned([...rows.map((r) => r.height), sizes.row], "data rows");
        expectAligned([rows[1].y - rows[0].y, sizes.row], "data row pitch");
      }
      // No target in step details is smaller than its density allows.
      const small = await details.dialog.evaluate(
        (dialog, minimum) =>
          [
            ...dialog.querySelectorAll<HTMLElement>(
              "button, input:not([type=hidden]), select, textarea, a[href], [role=radio], [role=tab]",
            ),
          ]
            .filter(
              (element) =>
                element.getClientRects().length &&
                !element.closest(".sr-only, [hidden]"),
            )
            .map((element) => {
              const r = element.getBoundingClientRect();
              return {
                name:
                  element.getAttribute("aria-label") ??
                  element.textContent?.trim(),
                width: r.width,
                height: r.height,
              };
            })
            .filter(
              (target) =>
                target.width < minimum - 0.5 || target.height < minimum - 0.5,
            ),
        sizes.target,
      );
      expect(small).toEqual([]);
    });
  });

for (const size of [
  { tag: "600x500", width: 600, height: 500 },
  { tag: "360x640", width: 360, height: 640 },
  { tag: "640x360", width: 640, height: 360 },
])
  test.describe(`a decision opening on a small screen at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test("opens at its start: the tabs and the intro are in view", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.openWithKeyboard("route-by-value");
      await expect(details.dialog).toHaveAttribute("data-layout", "sheet");
      await expect(details.tab("Parameters")).toBeInViewport();
      await expect(
        details.dialog.locator(".sd-tabpanel:not([hidden]) .param-intro"),
      ).toBeInViewport();
      // Focus is on a control, not on a field's tools.
      expect(
        await page.evaluate(
          () =>
            !!document.activeElement?.closest(".sd-tabpanel") &&
            !document.activeElement?.closest(".param-label-row, .param-tools"),
        ),
      ).toBe(true);
    });
  });

test.describe("Add option near the bottom of a short pane", () => {
  test.use({ viewport: { width: 1440, height: 560 } });
  test("opens its menu above the button when there is room there", async ({
    page,
  }) => {
    const details = await openStepFixture(page);
    await details.open("summarize");
    const add = details.dialog.getByRole("button", {
      name: "Add option",
      exact: true,
    });
    const scroller = formScroller(page, details.dialog);
    const before = await scroller.evaluate((element) => element.scrollTop);
    await add.click();
    const menu = details.dialog.getByRole("menu", { name: "Add option" });
    await expect(menu).toBeVisible();
    await expect(menu).toHaveAttribute("data-side", "above");
    await expect(menu.getByRole("menuitem").first()).toBeFocused();
    const menuBox = await box(menu);
    const button = await box(add);
    const area = await visibleArea(scroller);
    expect(menuBox.bottom).toBeLessThanOrEqual(button.y);
    expect(menuBox.y).toBeGreaterThanOrEqual(area.y - 0.5);
    // Opening above doesn't scroll the pane.
    expect(await scroller.evaluate((element) => element.scrollTop)).toBe(
      before,
    );
    await page.keyboard.press("Escape");
    await expect(add).toBeFocused();
  });
});
