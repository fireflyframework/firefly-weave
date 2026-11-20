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
// A step inside a loop in step details: the Input pane starts at the loop's
// current item and the loops around it, their fields drag in as the loop's
// own references, and the picker chooses which item the step shows. The
// Studio host (the repository's Python) answers what each field can read.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { parse } from "yaml";
import { openWorkflow } from "./canvas-po";
import { formulaText } from "./formula-po";
import { StepDetailsPage, yamlFile } from "./step-details-po";
import { hostScope, sourceText } from "./support";

/** orders over input.orders holds check and lines; lines over item.lines holds send; after follows the loop. */
const NESTED_SOURCE = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: order-lines, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      orders:
        type: array
        items:
          type: object
          properties:
            id: {type: string}
            customerEmail: {type: string}
            lines:
              type: array
              items: {type: object, properties: {sku: {type: string}}}
  outputSchema: {}
  steps:
    - id: orders
      kind: forEach
      items: {ref: /input/orders}
      concurrency: 1
      maxItems: 1000
      collect: all
      body:
        steps:
          - id: check
            kind: transform
            value: {ref: /input/orders}
          - id: lines
            kind: forEach
            items: {ref: /item/lines}
            concurrency: 1
            maxItems: 50
            collect: all
            body:
              steps:
                - id: send
                  kind: transform
                  value: {ref: /input/orders}
              output: {ref: /item}
        output: {ref: /item}
    - id: after
      kind: transform
      value: {ref: /input/orders}
  output: {literal: null}
`;
const NESTED = yamlFile("order-lines.yaml", NESTED_SOURCE);
/** The same, with lines' Items a formula, which step details edit in the formula editor. */
const FORMULA = yamlFile(
  "order-lines-formula.yaml",
  NESTED_SOURCE.replace(
    "items: {ref: /item/lines}",
    "items: {op: {name: coalesce, args: [{ref: /item/lines}, {literal: []}]}}",
  ),
);

/** Where a drop maps the field: its right side, clear of the pill's own controls. */
async function rightSide(target: Locator) {
  const area = (await target.boundingBox())!;
  return { x: Math.max(4, area.width - 48), y: area.height / 2 };
}
async function open(page: Page, file = NESTED) {
  await hostScope(page);
  await openWorkflow(page, file);
  return new StepDetailsPage(page);
}

async function inputPane(details: StepDetailsPage): Promise<Locator> {
  const tabs = details.dialog
    .getByRole("tablist", { name: /^(Step details panes|Data)$/ })
    .getByRole("tab", { name: "Input" });
  if (await tabs.count()) await tabs.first().click();
  const pane = details.dialog.locator("weave-input-pane");
  await expect(pane).toBeVisible();
  return pane;
}
const sourceOptions = (pane: Locator) =>
  pane
    .getByLabel("Input source")
    .locator("option")
    .evaluateAll((options) =>
      options.map((option) => (option.textContent ?? "").trim()),
    );
async function chooseSource(pane: Locator, label: string) {
  const select = pane.getByLabel("Input source");
  await expect(select.locator("option", { hasText: label })).toHaveCount(1);
  await select.selectOption({ label });
}
/** What the step's value reads in the source, once step details closed. */
async function valueOf(page: Page, details: StepDetailsPage, id: string) {
  await details.close();
  const steps = parse(await sourceText(page)).spec.steps;
  const all = [
    ...steps,
    ...steps[0].body.steps,
    ...steps[0].body.steps[1].body.steps,
  ];
  return all.find((step: { id: string }) => step.id === id).value;
}

test.describe("data inside a loop at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the Input pane starts at the current item, then the loops around it", async ({
    page,
  }) => {
    const details = await open(page);
    await details.open("send");
    const pane = await inputPane(details);
    await expect
      .poll(() => sourceOptions(pane))
      .toEqual([
        "Current item",
        "Item position",
        "Loop orders › Current item",
        "Loop orders › Item position",
        "Workflow input",
        "check · 1 step back",
      ]);
    await expect(pane.getByLabel("Input source")).toHaveValue("item");
    await expect(
      pane.getByRole("listitem", { name: /^sku, / }).first(),
    ).toBeVisible();
  });

  test("a field of the current item, or of the loop around, maps as that loop's own", async ({
    page,
  }) => {
    const details = await open(page);
    await details.open("check");
    let pane = await inputPane(details);
    await expect(pane.getByLabel("Input source")).toHaveValue("item");
    let target = details.field("value");
    await pane
      .getByRole("listitem", { name: /^customerEmail, / })
      .first()
      .dragTo(target, { targetPosition: await rightSide(target) });
    expect(await valueOf(page, details, "check")).toEqual({
      ref: "/item/customerEmail",
    });

    await details.open("send");
    pane = await inputPane(details);
    await chooseSource(pane, "Loop orders › Current item");
    target = details.field("value");
    await pane
      .getByRole("listitem", { name: /^customerEmail, / })
      .first()
      .dragTo(target, { targetPosition: await rightSide(target) });
    expect(await valueOf(page, details, "send")).toEqual({
      ref: "/loops/orders/item/customerEmail",
    });
  });

  test("an item dropped on a step the loop isn't around is refused, and nothing changes", async ({
    page,
  }) => {
    const details = await open(page);
    await details.open("after");
    const target = details.field("value");
    await expect(target).toBeVisible();
    const transfer = await page.evaluateHandle(() => {
      const data = new DataTransfer();
      data.setData(
        "application/x-weave-ref",
        JSON.stringify({
          ref: "/item/customerEmail",
          breadcrumb: "Current item › customerEmail",
          schema: { type: "string" },
        }),
      );
      return data;
    });
    // Where the drop lands: the field's middle.
    const box = (await target.boundingBox())!;
    const at = {
      clientX: box.x + box.width / 2,
      clientY: box.y + box.height / 2,
    };
    for (const type of ["dragenter", "dragover", "drop"])
      await target.dispatchEvent(type, { dataTransfer: transfer, ...at });
    await expect(
      page.getByText("Only steps inside the loop can read its items").first(),
    ).toBeVisible();
    expect(await valueOf(page, details, "after")).toEqual({
      ref: "/input/orders",
    });
  });

  test("the picker shows one row per loop and chooses the item", async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
    page.on("pageerror", (error) => errors.push(String(error)));
    const details = await open(page);
    await details.open("send");
    const pane = await inputPane(details);
    const picker = pane.locator("weave-iteration-picker");
    const rows = picker.locator(".picker-row");
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0)).toContainText("ordersOuter loop");
    await expect(rows.nth(1)).toContainText("linesLoop");
    const lines = picker.getByRole("group", { name: "Items of lines" });
    const item = lines.getByRole("button", { name: /Choose an item$/ });
    // Before the steps before have run, the count isn't known.
    await expect(item).toHaveAccessibleName("Item 1. Choose an item");
    await expect(picker).toContainText(
      "Execute previous steps to see the items.",
    );
    // On the current item, the row says it: the empty state keeps only its buttons.
    await expect(pane.getByLabel("Input source")).toHaveValue("item");
    await expect(pane.getByText("No input data yet")).toHaveCount(0);
    await expect(
      pane.getByRole("button", { name: "Execute previous steps" }),
    ).toBeVisible();
    const previous = lines.getByRole("button", { name: "Previous item" });
    await expect(previous).toHaveAttribute("aria-disabled", "true");
    await lines.getByRole("button", { name: "Next item" }).click();
    await expect(item).toHaveAccessibleName("Item 2. Choose an item");
    await expect(picker.getByRole("status")).toHaveText("Item 2");
    await expect(picker).toContainText(
      "The number of items shows once the previous steps have run.",
    );
    // Go to item, from the keyboard; Escape returns to the item button.
    await item.focus();
    await page.keyboard.press("Enter");
    const list = page.getByRole("dialog", { name: "Items of lines" });
    await expect(list).toBeVisible();
    await expect(list.getByLabel("Go to item")).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(list).toBeHidden();
    await expect(item).toBeFocused();
    await expect(details.dialog).toBeVisible();
    await page.keyboard.press("Enter");
    await list.getByLabel("Go to item").fill("7");
    await page.keyboard.press("Enter");
    await expect(item).toHaveAccessibleName("Item 7. Choose an item");
    await expect(item).toBeFocused();
    // The loop step itself, and a step outside loops, show no picker.
    await details.close();
    await details.open("orders");
    await expect(details.dialog.locator("weave-iteration-picker")).toHaveCount(
      0,
    );
    // Counting the items never loops change detection.
    expect(errors).toEqual([]);
  });
});

test.describe("the Result panel inside a loop at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("names the picked item and holds a compact picker that keeps it open", async ({
    page,
  }) => {
    const details = await open(page, FORMULA);
    // lines' Items read the current item of orders, the loop around it.
    await details.open("lines");
    // The formula editor shows the Result panel while it has focus.
    await formulaText(details.field("items")).click();
    const result = details.dialog.getByRole("region", {
      name: "Result of Items",
    });
    await expect(result).toBeVisible();
    const picker = result.getByRole("group", { name: "Items of orders" });
    const item = picker.getByRole("button", { name: /Choose an item$/ });
    await expect(item).toHaveAccessibleName("Item 1. Choose an item");
    // Compact: the buttons only, no loop name, at the head's right end.
    await expect(result.locator(".picker-name")).toHaveCount(0);
    const head = (await result.locator(".formula-result-head").boundingBox())!;
    const placed = (await picker.boundingBox())!;
    expect(
      Math.abs(placed.x + placed.width - (head.x + head.width)),
    ).toBeLessThan(2);
    await picker.getByRole("button", { name: "Next item" }).click();
    await expect(item).toHaveAccessibleName("Item 2. Choose an item");
    await expect(result).toBeVisible();
  });
});

test.describe("the picker on a phone", () => {
  test.use({ viewport: { width: 360, height: 740 } });

  test("keeps touch sizes below 768 px wide, whatever the pointer", async ({
    page,
  }) => {
    const details = await open(page);
    // The phone's canvas opens on the trigger: the keyboard reaches send.
    await details.openWithKeyboard("send");
    const pane = await inputPane(details);
    const picker = pane.locator("weave-iteration-picker");
    const sizes = await picker.evaluate((element) => {
      const size = (selector: string) =>
        [...element.querySelectorAll(selector)].map((each) => {
          const box = each.getBoundingClientRect();
          return [Math.round(box.width), Math.round(box.height)];
        });
      return {
        steps: size(".picker-step"),
        item: size(".picker-item"),
        row: size(".picker-row"),
      };
    });
    for (const [width, height] of sizes.steps) {
      expect(width).toBeGreaterThanOrEqual(44);
      expect(height).toBeGreaterThanOrEqual(44);
    }
    expect(sizes.item[0][0]).toBeGreaterThanOrEqual(128);
    expect(sizes.item[0][1]).toBeGreaterThanOrEqual(44);
    expect(sizes.row[0][1]).toBeGreaterThanOrEqual(56);
  });
});

test.describe("the picker on touch", () => {
  test.use({ viewport: { width: 600, height: 800 }, hasTouch: true });

  test("its buttons are 44 px", async ({ page }) => {
    const details = await open(page);
    await details.open("send");
    const pane = await inputPane(details);
    const buttons = pane.locator("weave-iteration-picker .picker-group button");
    await expect(buttons).toHaveCount(6);
    for (const box of await buttons.evaluateAll((elements) =>
      elements.map((element) => element.getBoundingClientRect()),
    )) {
      expect(box.width).toBeGreaterThanOrEqual(44);
      expect(box.height).toBeGreaterThanOrEqual(44);
    }
  });
});
