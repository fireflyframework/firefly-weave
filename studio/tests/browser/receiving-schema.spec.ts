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
import { connected, newWorkflow, sourceText, workerAction } from "./support";
import { DesignerPage } from "./designer-po";

for (const width of [1440, 600]) {
  test(`nullable action formulas retain their field type through lazy loading at ${width}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await connected(page);
    await page.route("**/actions/a2/export", (route) =>
      route.fulfill({
        json: {
          id: "a2",
          document: {
            ...workerAction,
            spec: {
              ...workerAction.spec,
              inputSchema: {
                type: "object",
                $defs: { Customer: { type: ["string", "null"] } },
                properties: {
                  customer: { $ref: "#/$defs/Customer", title: "Customer" },
                },
              },
            },
          },
        },
      }),
    );
    await newWorkflow(page);
    const designer = new DesignerPage(page);
    await designer.setSource(`apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: nullable-input, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - id: lookup
      kind: action
      uses: crm.lookup@2.0.0
      with:
        object:
          customer: {op: {name: coalesce, args: [{literal: null}, {literal: fallback}]}}
  output: {literal: {}}
`);
    await designer.selectStep("lookup");
    const form = designer.inspector.locator("weave-task-form");
    const operator = form.getByRole("combobox", {
      name: "Customer operator",
      exact: true,
    });
    await operator.click();
    await expect(page.getByRole("listbox").getByRole("option")).toHaveCount(1);
    await page.keyboard.press("Escape");
    const firstType = form
      .getByRole("combobox", { name: "Value type", exact: true })
      .first();
    await firstType.click();
    const options = page.getByRole("listbox").getByRole("option");
    await expect(options).toHaveCount(2);
    await expect(options).toContainText(["Text", "Empty"]);
    await page.keyboard.press("Escape");
    await expect(form.locator('.property-error[role="status"]')).toHaveCount(0);
    expect(
      parse(await sourceText(page)).spec.steps[0].with.object.customer.op.args,
    ).toEqual([{ literal: null }, { literal: "fallback" }]);
  });
}
