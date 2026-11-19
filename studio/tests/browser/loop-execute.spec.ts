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
// One item of a loop in step details: Execute runs the item picked, the
// Output pane shows and pins that item's data (or every item's), and the
// loop step's own output holds one result per item. The Studio host is the
// repository's Python.
import { expect, test, type Locator } from "@playwright/test";
import {
  executeButton,
  executions,
  moreWays,
  openExecuteWorkflow,
} from "./execute-po";
import { hasPython } from "./local-authoring";
import { yamlFile, type StepDetailsPage } from "./step-details-po";

/** orders goes through three people (Bo has no note) and keeps only the notes there are. */
const LOOP_SOURCE = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: loop-execute, version: 1.0.0}
spec:
  inputSchema: {type: object, properties: {}}
  outputSchema: {}
  steps:
    - id: orders
      kind: forEach
      items: {literal: [{name: Ada, note: a}, {name: Bo, note: null}, {name: Cy, note: c}]}
      concurrency: 1
      maxItems: 1000
      collect: nonNull
      body:
        steps:
          - {id: send, kind: transform, value: {object: {to: {ref: /item/name}}}}
        output: {ref: /item/note}
  output: {literal: null}
`;
const LOOP = yamlFile("loop-execute.yaml", LOOP_SOURCE);
/** The same, with an action inside the loop, whose output can be pinned. */
const PINS = yamlFile(
  "loop-pins.yaml",
  LOOP_SOURCE.replace(
    "          - {id: send, kind: transform, value: {object: {to: {ref: /item/name}}}}\n",
    "          - {id: send, kind: transform, value: {object: {to: {ref: /item/name}}}}\n" +
      "          - {id: lookup, kind: action, uses: crm.lookup@1.0.0, with: {object: {customerId: {ref: /item/name}}}}\n",
  ),
);

const pane = (details: StepDetailsPage, name: "input" | "output"): Locator =>
  details.dialog.locator(`weave-${name}-pane`);
async function pickSecond(details: StepDetailsPage) {
  const row = pane(details, "input").getByRole("group", {
    name: "Items of orders",
  });
  await expect(
    row.getByRole("button", { name: /Choose an item$/ }),
  ).toHaveAccessibleName("Item 1 of 3. Choose an item");
  await row.getByRole("button", { name: "Next item" }).click();
}

test.describe("one item of a loop at 1440x900", () => {
  test.skip(!hasPython, "needs the repository's Python");
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Execute step runs the item picked and shows its output", async ({
    page,
  }) => {
    const { details, calls } = await openExecuteWorkflow(page, LOOP);
    await details.open("send");
    await pickSecond(details);
    const answered = page.waitForResponse("**/studio/local/debug/execute");
    await executeButton(details.dialog).click();
    await answered;
    expect(executions(calls).at(-1)?.body).toMatchObject({
      target: { mode: "step", stepId: "send", instanceKey: "send[1]" },
    });
    const output = pane(details, "output");
    await expect(output.locator(".pane-source-tag")).toHaveText(
      "Simulated · item 2",
    );
    await expect(output).toContainText("Bo");
  });

  test("the Output pane picks the same item, and Execute names it", async ({
    page,
  }) => {
    const { details } = await openExecuteWorkflow(page, LOOP);
    await details.open("send");
    const out = pane(details, "output").getByRole("group", {
      name: "Items of orders",
    });
    await out.getByRole("button", { name: "Next item" }).click();
    await expect(
      pane(details, "input")
        .getByRole("group", { name: "Items of orders" })
        .getByRole("button", { name: /Choose an item$/ }),
    ).toHaveAccessibleName("Item 2 of 3. Choose an item");
    await expect(executeButton(details.dialog)).toHaveAttribute(
      "title",
      "Execute step on item 2 (Ctrl/Cmd+Enter)",
    );
    await moreWays(details.dialog).click();
    await expect(
      page.getByRole("menuitem", {
        name: /^Execute previous steps for item 2/,
      }),
    ).toBeVisible();
  });

  test("Execute previous steps stops before the item picked", async ({
    page,
  }) => {
    const { details, calls } = await openExecuteWorkflow(page, LOOP);
    await details.open("send");
    await pickSecond(details);
    await moreWays(details.dialog).click();
    const answered = page.waitForResponse("**/studio/local/debug/execute");
    await page
      .getByRole("menuitem", { name: /^Execute previous steps/ })
      .click();
    await answered;
    expect(executions(calls).at(-1)?.body).toMatchObject({
      target: { mode: "before", stepId: "send", instanceKey: "send[1]" },
    });
    // The run ended inside the loop: one item of three finished, not done,
    // and the frame doesn't say how many items it had.
    await details.close();
    const tile = page.locator('.canvas-v2 .tile-node[data-tile="orders"]');
    await expect(tile.locator(".frame-run")).toHaveText("· 1 of 3");
    await expect(tile.locator(".frame-run")).toHaveAttribute(
      "data-tone",
      "stopped",
    );
    await expect(tile).not.toContainText("3 items");
    const log = page.getByRole("region", { name: "Run log" });
    await expect(log).toContainText("1 of 3 items finished");
  });

  test("an item's pin says so, and Pin for every item shares it", async ({
    page,
  }) => {
    const { details } = await openExecuteWorkflow(page, PINS);
    await details.open("lookup");
    await pickSecond(details);
    const output = pane(details, "output");
    await output.getByRole("button", { name: "Edit output" }).click();
    const editor = output.getByRole("textbox", { name: "Output of lookup" });
    await editor.click();
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.insertText('{"to": "pinned"}');
    await output.getByRole("button", { name: "Save" }).click();
    const banner = output.locator(".pinned-banner");
    await expect(banner).toContainText(
      "Pinned for item 2. Other items use their own data, and production runs ignore pins.",
    );
    await banner.getByRole("button", { name: "Pin for every item" }).click();
    await expect(banner).toContainText(
      "Pinned for every item. Production runs ignore pins.",
    );
    // One toast for the one change, naming the item as people count it.
    const toasts = page.locator(".toast");
    await expect(toasts).toHaveCount(1);
    await expect(toasts).toContainText(
      "Pinned lookup for every item. Item 2's own pin was removed.",
    );
    // Its Undo puts item 2's own pin back, and no pin for every item.
    await toasts.getByRole("button", { name: "Undo" }).click();
    await expect(banner).toContainText("Pinned for item 2.");
    await pane(details, "input")
      .getByRole("group", { name: "Items of orders" })
      .getByRole("button", { name: "Previous item" })
      .click();
    await expect(banner).toBeHidden();
    await pane(details, "input")
      .getByRole("group", { name: "Items of orders" })
      .getByRole("button", { name: "Next item" })
      .click();
    await banner.getByRole("button", { name: "Pin for every item" }).click();
    // Item 1 has no pin of its own: it uses the one for every item.
    await pane(details, "input")
      .getByRole("group", { name: "Items of orders" })
      .getByRole("button", { name: "Previous item" })
      .click();
    await expect(output.locator(".pill").first()).toHaveText(
      "Pinned for every item",
    );
  });

  test("the loop's own output has one result per item, without the empty ones", async ({
    page,
  }) => {
    const { details } = await openExecuteWorkflow(page, LOOP);
    await details.open("orders");
    const answered = page.waitForResponse("**/studio/local/debug/execute");
    await executeButton(details.dialog).click();
    await answered;
    const output = pane(details, "output");
    await output.getByRole("radio", { name: "JSON" }).click();
    await expect(output.locator("pre").first()).toHaveText(
      /\[\s*"a",\s*"c"\s*\]/,
    );
  });
});
