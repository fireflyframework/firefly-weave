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
// Executed data in the Input and Output panes: what each pane offers before
// anything ran, while it runs, once it ran (Simulated), when the step
// returned null and when earlier steps changed since.
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  executions,
  moreWays,
  openExecuteFixture,
  openExecuteWorkflow,
  pinSample,
  shownExecute,
} from "./execute-po";
import { fieldLine } from "./formula-po";
import { hasPython } from "./local-authoring";
import { applySource } from "./owned-actions";
import { sizes, yamlFile, type StepDetailsPage } from "./step-details-po";
import { sourceText } from "./support";
import { banner, outputPane } from "./test-data-po";

test.skip(!hasPython, "Create the Python development environment first.");

async function inputPane(details: StepDetailsPage): Promise<Locator> {
  const tabs = details.dialog
    .getByRole("tablist", { name: /^(Step details panes|Data)$/ })
    .getByRole("tab", { name: "Input" });
  if (await tabs.count()) await tabs.first().click();
  const pane = details.dialog.locator("weave-input-pane");
  await expect(pane).toBeVisible();
  return pane;
}
async function chooseSource(pane: Locator, label: RegExp) {
  const select = pane.getByLabel("Input source");
  const value = await select.evaluate(
    (element, pattern) =>
      [...(element as HTMLSelectElement).options].find((option) =>
        new RegExp(pattern).test((option.textContent ?? "").trim()),
      )?.value ?? "",
    label.source,
  );
  await select.selectOption(value);
}
async function executePrevious(page: Page, details: StepDetailsPage) {
  await shownExecute(details);
  await moreWays(details.dialog).click();
  await page.getByRole("menuitem", { name: /Execute previous steps/ }).click();
}

for (const size of sizes)
  test.describe(`executed data in the panes at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("with no data the Input offers to execute the earlier steps or add test input", async ({
      page,
    }) => {
      const { details } = await openExecuteFixture(page);
      await details.open("score");
      const pane = await inputPane(details);
      await chooseSource(pane, /Workflow input/);
      await expect(pane).toContainText("No input data yet");
      await expect(pane).toContainText(
        "Execute the previous steps to see what this step receives, or add test input.",
      );
      const execute = pane.getByRole("button", {
        name: "Execute previous steps",
      });
      await expect(execute).toHaveAttribute(
        "title",
        "Runs from the trigger up to this step, reusing pinned and current results",
      );
      await expect(
        pane.getByRole("button", { name: "Add test input" }),
      ).toBeVisible();
      await expect(
        pane.getByRole("list", { name: "Fields this step can read" }),
      ).toBeVisible();
    });

    test("an earlier step that hasn't run is named", async ({ page }) => {
      const { details } = await openExecuteFixture(page);
      await details.open("score");
      const pane = await inputPane(details);
      await chooseSource(pane, /^lookup/);
      await expect(pane).toContainText(
        "lookup hasn't run yet. Execute previous steps, or map from the fields below.",
      );
      await expect(
        pane.getByRole("button", { name: "Execute previous steps" }),
      ).toBeVisible();
    });

    test("with no output the Output offers Execute step, Set test output and Generate sample", async ({
      page,
    }) => {
      const { details, calls } = await openExecuteFixture(page);
      await details.open("lookup");
      const pane = await outputPane(details);
      await expect(pane).toContainText("No output yet");
      await expect(pane).toContainText(
        "Execute this step to see its output. You can also set test output, or generate a sample.",
      );
      for (const name of ["Execute step", "Set test output", "Generate sample"])
        await expect(pane.getByRole("button", { name })).toBeVisible();
      await pane.getByRole("button", { name: "Execute step" }).click();
      await expect.poll(() => executions(calls).length).toBe(1);
      await expect(pane.locator(".pane-notice")).toContainText(
        "Step lookup needs test data.",
      );
    });

    test("an out-of-date result shows the Input banner, and Execute previous steps clears it", async ({
      page,
    }) => {
      const { details, calls } = await openExecuteFixture(page);
      await pinSample(details, "lookup");
      await details.open("notify");
      await executePrevious(page, details);
      await expect.poll(() => executions(calls).length).toBe(1);
      let pane = await inputPane(details);
      await chooseSource(pane, /^score/);
      await expect(pane.locator(".pane-source-tag")).toHaveText("Simulated");
      await expect(pane.locator(".pane-stale")).toHaveCount(0);
      await details.close();
      // score now reads another field of lookup: its result is out of date.
      const source = await sourceText(page);
      await applySource(
        page,
        source.replace(
          "/steps/lookup/output/tier",
          "/steps/lookup/output/customerId",
        ),
      );
      await details.open("notify");
      pane = await inputPane(details);
      await chooseSource(pane, /^score/);
      const stale = pane.locator(".pane-stale");
      await expect(stale).toContainText(
        "Earlier steps changed since this data was produced.",
      );
      await stale
        .getByRole("button", { name: "Execute previous steps" })
        .click();
      await expect.poll(() => executions(calls).length).toBe(2);
      await expect(stale).toHaveCount(0);
    });

    test("the stale pin banner offers Execute step", async ({ page }) => {
      const { details, calls } = await openExecuteFixture(page);
      await pinSample(details, "notify");
      const source = await sourceText(page);
      await applySource(
        page,
        source.replace(
          "/steps/lookup/output/tier",
          "/steps/lookup/output/customerId",
        ),
      );
      await details.open("notify");
      const pane = await outputPane(details);
      const pinned = banner(pane);
      await expect(pinned).toContainText("Pinned output may be out of date");
      for (const name of ["Execute step", "Keep pinned", "Unpin"])
        await expect(pinned.getByRole("button", { name })).toBeVisible();
      await pinned.getByRole("button", { name: "Execute step" }).click();
      // Executing a pinned step asks first.
      await expect(
        page.locator('.execute-dialog[role="alertdialog"]'),
      ).toContainText("Unpin and execute?");
      await page.keyboard.press("Escape");
      expect(executions(calls)).toHaveLength(0);
      await pinned.getByRole("button", { name: "Keep pinned" }).click();
      await expect(pinned).toContainText("This data is pinned for test runs.");
    });
  });

const NULLED = yamlFile(
  "nulled.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: nulled, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - id: nothing
      kind: transform
      value: {literal: null}
  output: {literal: null}
`,
);

test("a step that returns null says so", async ({ page }) => {
  const { details } = await openExecuteWorkflow(page, NULLED);
  await details.open("nothing");
  await (await shownExecute(details)).click();
  const pane = await outputPane(details);
  await expect(pane).toContainText("This step returned no data (null).");
});

test("a formula preview reads the step's last result once executed", async ({
  page,
}) => {
  const { details, calls } = await openExecuteFixture(page);
  await pinSample(details, "lookup");
  await details.open("notify");
  await expect(fieldLine(details, "input.customerId")).not.toContainText("=");
  await executePrevious(page, details);
  await expect.poll(() => executions(calls).length).toBe(1);
  // score returned lookup's tier: notify's Customer ID reads it.
  await expect(fieldLine(details, "input.customerId")).toContainText("=");
});
