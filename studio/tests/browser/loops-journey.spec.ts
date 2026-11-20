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
// Building a loop from scratch on the local authoring host (the
// repository's Python): add a loop, choose its list, add a step inside it
// and map the current item's field by drag, pick item 2, pin items, then
// Execute workflow goes through both items.
import { expect, test } from "@playwright/test";
import { CanvasPage } from "./canvas-po";
import { openExecuteWorkflow } from "./execute-po";
import { replaceFormula } from "./formula-po";
import { hasPython } from "./local-authoring";
import { withLanguageFeatures, yamlFile } from "./step-details-po";
import { hostScope } from "./support";

/** Two orders by default (Execute workflow's sample uses them); fetch passes them on; the rest is built in the test. */
const START = yamlFile(
  "orders-journey.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: orders-journey, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      orders:
        type: array
        items:
          type: object
          properties: {name: {type: string}, email: {type: string}}
        default:
          - {name: Ada, email: ada@example.test}
          - {name: Bo, email: bo@example.test}
  outputSchema: {}
  steps:
    - id: fetch
      kind: transform
      value: {ref: /input/orders}
  output: {literal: null}
`,
);

test.describe("a loop built, pinned and executed at 1440x900", () => {
  test.skip(!hasPython, "needs the repository's Python");
  test.use({ viewport: { width: 1440, height: 900 } });

  test("add a loop, map its items, pin them and execute both", async ({
    page,
  }) => {
    const { details } = await openExecuteWorkflow(page, START, async (p) => {
      // A platform that runs loops, and the host's answer for item fields.
      await withLanguageFeatures(p, [
        "text.concat",
        "text.join",
        "flow.forEach",
      ]);
      await hostScope(p);
    });
    const canvas = new CanvasPage(page);
    // Add a loop after fetch, and choose the list it goes over.
    await canvas.insertTarget("Add a step after fetch").click();
    await canvas.pick("Loop over items");
    const loop = details.dialog;
    await expect(loop).toContainText("Loop over items");
    await replaceFormula(page, details.field("items"), "input.orders");
    await details.close();
    // A step inside the loop, its input mapped from the current item by drag.
    await canvas.insertTarget("Add a step to For each item of loop-1").click();
    // The current item is a source once the host's scope answers.
    const scoped = page.waitForResponse("**/studio/local/scope");
    await canvas.pick("crm.lookup");
    await scoped;
    const input = details.dialog.locator("weave-input-pane");
    await expect(input.getByLabel("Input source")).toHaveValue("item");
    const target = details.field("input.customer");
    const area = (await target.boundingBox())!;
    await input
      .getByRole("listitem", { name: /^name, / })
      .first()
      .dragTo(target, {
        targetPosition: { x: Math.max(4, area.width - 48), y: area.height / 2 },
      });
    // Item 2, picked in the Input pane: its value shows.
    const picker = input.locator("weave-iteration-picker");
    await picker.getByRole("button", { name: "Next item" }).click();
    await expect(
      picker.getByRole("button", { name: /Choose an item$/ }),
    ).toHaveAccessibleName("Item 2. Choose an item");
    // Pin for every item, then item 2's own.
    const output = details.dialog.locator("weave-output-pane");
    const pinAs = async (text: string) => {
      await output.getByRole("button", { name: "Edit output" }).click();
      await output.getByRole("textbox", { name: /^Output of / }).click();
      await page.keyboard.press("ControlOrMeta+a");
      await page.keyboard.insertText(text);
      await output.getByRole("button", { name: "Save" }).click();
    };
    await pinAs('{"tier": "every"}');
    const banner = output.locator(".pinned-banner");
    await banner.getByRole("button", { name: "Pin for every item" }).click();
    await expect(banner).toContainText("Pinned for every item.");
    await pinAs('{"tier": "gold"}');
    await expect(banner).toContainText("Pinned for item 2.");
    await details.close();
    // Execute workflow: both items, each with its own result.
    await page
      .locator(".editor-bar")
      .getByRole("button", { name: "Execute workflow" })
      .click();
    const run = page.locator('.canvas-v2 .frame-run[data-tone="done"]');
    await expect(run).toHaveText("· 2 of 2");
    const log = page.getByRole("region", { name: "Run log" });
    const items = log.getByRole("list", { name: /^Items of / });
    await expect(items.getByRole("button")).toHaveText([
      /Item 1\s*call-action-1/,
      /Item 2\s*call-action-1/,
    ]);
  });
});
