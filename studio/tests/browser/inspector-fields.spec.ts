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
import { test, expect } from "@playwright/test";
import { parse } from "yaml";
import { offline, newWorkflow, sourceText } from "./support";
import { DesignerPage } from "./designer-po";

const workflow = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: fields, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      amount: {type: number, title: Amount}
  outputSchema: {type: object}
  steps:
    - id: map
      kind: transform
      value: {literal: {}}
    - id: route
      kind: switch
      cases:
        - when: {literal: true}
          steps: []
          output: {literal: {}}
      default: {steps: [], output: {literal: {}}}
  output: {literal: {}}
`;
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    test("Fields maps a named row to data and focuses each added name", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(workflow);
      await designer.selectStep("map");
      const field = designer.inspector.locator('[data-field="value"]');
      const names = field.getByRole("textbox", {
        name: "Field name",
        exact: true,
      });
      await expect(names).toHaveCount(1);
      await expect(names.first()).toBeVisible();
      await names.first().fill("total");
      await names.first().press("Tab");
      await field
        .getByRole("button", { name: "Use data", exact: true })
        .click();
      const data = field.getByRole("combobox", { name: "total reference" });
      await data.fill("/input/amount");
      await data.press("Tab");
      await page.locator(".inspector-header h2").click();
      const document = parse(await sourceText(page));
      expect(document.spec.steps[0].value).toEqual({
        object: { total: { ref: "/input/amount" } },
      });
      await designer.open();
      await designer.selectStep("map");
      await field
        .getByRole("button", { name: "+ Add field", exact: true })
        .click();
      await expect(names).toHaveCount(2);
      await expect(names.nth(1)).toBeFocused();
      await field.getByLabel("Value type", { exact: true }).click();
      await expect(page.getByRole("listbox").getByRole("option")).toHaveText([
        "Text",
        "Number",
        "Yes/No",
        "Empty",
      ]);
      await page.keyboard.press("Escape");
    });
    test("Fields reorders and removes rows, with literal JSON only in Advanced", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(
        workflow.replace(
          "value: {literal: {}}",
          'value: {literal: {first: 7, second: "Keep"}}',
        ),
      );
      await designer.selectStep("map");
      const field = designer.inspector.locator('[data-field="value"]');
      const rows = field.locator(".fields-row");
      await rows
        .nth(1)
        .getByRole("button", { name: "Move field up", exact: true })
        .click();
      await expect(
        rows.first().getByLabel("Field name", { exact: true }),
      ).toHaveValue("second");
      const moved = parse(await sourceText(page));
      expect(Object.keys(moved.spec.steps[0].value.literal)).toEqual([
        "second",
        "first",
      ]);
      await designer.selectStep("map");
      await rows
        .nth(1)
        .getByRole("button", { name: "Remove field", exact: true })
        .click();
      await field
        .getByRole("button", { name: "Value options", exact: true })
        .click();
      await page
        .getByRole("menuitem", { name: "Advanced: JSON value", exact: true })
        .click();
      const advanced = field.getByRole("textbox", {
        name: "Advanced JSON value",
        exact: true,
      });
      await expect(advanced).toHaveValue('{\n  "second": "Keep"\n}');
      await advanced.fill('{"second":"Edited","enabled":true}');
      await page.locator(".inspector-header h2").click();
      const document = parse(await sourceText(page));
      expect(document.spec.steps[0].value).toEqual({
        literal: { second: "Edited", enabled: true },
      });
    });
    test("objects inside literal lists use the same Fields builder", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(
        workflow.replace(
          "value: {literal: {}}",
          "value: {literal: {lines: [{amount: 7}]}}",
        ),
      );
      await designer.selectStep("map");
      const field = designer.inspector.locator('[data-field="value"]');
      await field.getByLabel("Value type", { exact: true }).click();
      await expect(page.getByRole("listbox").getByRole("option")).toHaveText([
        "Text",
        "Number",
        "Yes/No",
        "Empty",
      ]);
      await page.keyboard.press("Escape");
      const names = field.getByRole("textbox", {
        name: "Field name",
        exact: true,
      });
      await expect(names).toHaveCount(2);
      await expect(names.nth(1)).toHaveValue("amount");
      await field.getByLabel("Property value", { exact: true }).fill("9");
      await page.locator(".inspector-header h2").click();
      expect(parse(await sourceText(page)).spec.steps[0].value).toEqual({
        literal: { lines: [{ amount: 9 }] },
      });
    });

    test("path results start collapsed and preserve their required empty literal", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(workflow);
      await designer.selectStep("route");
      const result = designer.inspector.locator(
        '[data-field="cases/0/output"]',
      );
      await expect(result.locator(":scope > summary")).toBeVisible();
      await expect(result.locator("weave-expression-editor")).toBeHidden();
      await result.locator(":scope > summary").click();
      await expect(
        result.getByRole("button", { name: "+ Add field", exact: true }),
      ).toBeVisible();
      const document = parse(await sourceText(page));
      expect(document.spec.steps[1].cases[0].output).toEqual({ literal: {} });
    });
  });
}
