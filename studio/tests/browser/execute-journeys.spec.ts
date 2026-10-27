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
// Executing end to end, as a person does it: a new workflow built from its
// trigger, executed with no test input; then every way to execute on one
// workflow, with test answers, waits, the views, playback and breakpoints.
import { expect, test, type Page } from "@playwright/test";
import { CanvasPage, openNewWorkflow } from "./canvas-po";
import {
  executions,
  moreWays,
  openExecuteWorkflow,
  pinSample,
  shownExecute,
} from "./execute-po";
import { hasPython, localAuthoring } from "./local-authoring";
import { sizes, StepDetailsPage, yamlFile } from "./step-details-po";
import { outputPane } from "./test-data-po";

test.skip(!hasPython, "Create the Python development environment first.");

const runLog = (page: Page) => page.getByRole("region", { name: "Run log" });
const executeInBar = (page: Page) =>
  page.locator(".editor-bar").getByRole("button", { name: "Execute workflow" });

for (const size of sizes)
  test.describe(`executing end to end at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a new workflow built from its trigger executes and shows each step's output", async ({
      page,
    }) => {
      const canvas = await openNewWorkflow(page);
      const calls = await localAuthoring(page);
      await canvas.addFirstStep("Manual form");
      const trigger = page.getByRole("dialog", {
        name: "Step details: Manual form trigger",
      });
      await trigger.getByRole("button", { name: "Add field" }).click();
      await trigger.getByRole("textbox", { name: "Field name" }).fill("amount");
      await page.keyboard.press("Enter");
      await canvas.closeInspector();
      await canvas.insertTarget("Add a step at the start").click();
      await canvas.pick("Transform");
      await canvas.closeInspector();
      await executeInBar(page).click();
      await expect.poll(() => executions(calls).length).toBe(1);
      const log = runLog(page);
      await expect(log.locator(".run-log-status")).toHaveText(
        "Completed · 1 step",
      );
      await expect(log.locator(".run-log-note")).toHaveText(
        "Executed with a sample input. Add test input to choose your own.",
      );
      const tile = page.locator(".tile-node[data-step]").first();
      await expect(tile.locator(".tile-badge")).toHaveAttribute(
        "data-badge",
        "done",
      );
      const id = (await tile.getAttribute("data-step"))!;
      const details = new StepDetailsPage(page);
      await details.open(id);
      const pane = await outputPane(details);
      await expect(pane.locator(".pane-source-tag")).toHaveText("Simulated");
    });
  });

const JOURNEY = yamlFile(
  "journey.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: journey, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      customerId: {type: string, title: Customer ID}
      amount: {type: number, title: Amount}
  outputSchema: {}
  steps:
    - id: lookup
      kind: action
      uses: crm.lookup@1.0.0
      with: {object: {customerId: {ref: /input/customerId}}}
    - id: shape
      kind: transform
      value: {object: {tier: {ref: /steps/lookup/output/tier}}}
    - {id: pause, kind: wait, durationSeconds: 300}
    - id: paid
      kind: signal
      name: order-paid
      timeoutSeconds: 600
      payloadSchema: {type: object}
    - id: route
      kind: switch
      cases:
        - when: {op: {name: gte, args: [{ref: /input/amount}, {literal: 1}]}}
          steps: []
          output: {literal: {path: big}}
      default: {steps: [], output: {literal: null}}
    - id: rows
      kind: transform
      value: {literal: [{a: 1}, {a: 2, b: x}]}
  output: {literal: null}
`,
);

test.describe("every way to execute on one workflow", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("every way to execute, with scripts, waits, views, playback and breakpoints", async ({
    page,
  }) => {
    const { details, calls } = await openExecuteWorkflow(page, JOURNEY);
    const log = runLog(page);
    // 1. Execute step on shape: lookup needs test data.
    await details.open("shape");
    await (await shownExecute(details)).click();
    let pane = await outputPane(details);
    await expect(pane.locator(".pane-notice")).toContainText(
      "Step lookup needs test data.",
    );
    await details.close();
    // 2. Pin lookup; Execute step fills shape's Output.
    await pinSample(details, "lookup");
    await details.open("shape");
    await (await shownExecute(details)).click();
    pane = await outputPane(details);
    await expect(pane.locator(".pane-source-tag")).toHaveText("Simulated");
    await details.close();
    // 3. Execute workflow: the wait advances, the signal asks in the run log.
    await executeInBar(page).click();
    await expect(log.locator(".run-log-status")).toHaveText(
      "Waiting · Send signal order-paid",
    );
    await expect(log.locator('[data-entry="pause"]')).toContainText(
      "Waited 5 min",
    );
    await log.getByLabel("Payload (JSON)").fill('{"paid": true}');
    await log.getByRole("button", { name: "Send" }).click();
    await expect(log.locator(".run-log-status")).toHaveText(
      "Completed · 6 steps",
    );
    // 4. The decision's paths, and a list of objects on Table.
    await details.open("route");
    pane = await outputPane(details);
    await expect(pane.locator(".pane-count")).toHaveText("Took path 1");
    await details.close();
    await details.open("rows");
    pane = await outputPane(details);
    await expect(
      pane.getByRole("table", { name: "Output data" }),
    ).toBeVisible();
    await details.close();
    // 5. Execute previous steps pauses before a step.
    await details.open("route");
    await shownExecute(details);
    await moreWays(details.dialog).click();
    await page
      .getByRole("menuitem", { name: /Execute previous steps/ })
      .click();
    await expect(log.locator(".run-log-status")).toHaveText(
      "Paused before route",
    );
    await details.close();
    // 6. A breakpoint: Execute workflow pauses before rows; Continue finishes.
    await new CanvasPage(page).tileBody("rows").click();
    await page.keyboard.press("F9");
    await executeInBar(page).click();
    await expect(log.locator(".run-log-status")).toHaveText(
      "Paused before rows",
    );
    await log.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(log.locator(".run-log-status")).toHaveText(/^Completed/);
    expect(executions(calls).length).toBeGreaterThanOrEqual(5);
  });
});
