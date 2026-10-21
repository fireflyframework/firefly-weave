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
// The trigger's Test event is the saved test input: it comes back after a
// reload, later steps' Input pane shows its values, formulas preview pinned
// output, and a field marked secret is never kept.
import { expect, test, type Page } from "@playwright/test";
import { fieldLine, formulasFixture, setTestInput } from "./formula-po";
import {
  openStepFixture,
  sizes,
  withLanguageFeatures,
  yamlFile,
  type StepDetailsPage,
} from "./step-details-po";

/** A change on the canvas, so the workflow itself is kept on this computer too. */
async function keepWorkflow(page: Page, step: string) {
  await page.locator(`[data-step="${step}"]`).first().click();
  await page.keyboard.press("ControlOrMeta+d");
  await expect(page.locator(".status-chip")).toContainText(/Saved/, {
    timeout: 10_000,
  });
}
const draftId = (page: Page) =>
  /\/workflows\/([^/]+)\/designer/.exec(page.url())![1];
async function openTrigger(page: Page, details: StepDetailsPage) {
  await details.trigger().focus();
  await page.keyboard.press("Enter");
  const dialog = details.dialogFor("Manual form trigger");
  await expect(dialog).toBeVisible();
  if ((page.viewportSize()?.width ?? 1440) < 768)
    await dialog
      .getByRole("tablist", { name: "Step details panes" })
      .getByRole("tab", { name: "Test event" })
      .click();
  return dialog;
}

const pinned = yamlFile(
  "tiered.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: tiered, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      customerId: {type: string, title: Customer ID}
  outputSchema: {type: object}
  steps:
    - id: lookup
      kind: action
      uses: sql.lookup@1.0.0
      with: {object: {customerId: {ref: /input/customerId}}}
    - id: review
      kind: humanTask
      assignment: reviewers
      title: {op: {name: concat, args: [{literal: "Tier "}, {ref: /steps/lookup/output/tier}]}}
      context: {literal: {}}
      formSchema: {type: object, properties: {}}
      decisions: [approve, reject]
  output: {literal: {}}
`,
);

for (const size of sizes)
  test.describe(`test input at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("the test input is saved with a local draft and comes back after a reload", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await setTestInput(page, details, {
        "Customer ID": "c-1042",
        Amount: "1500",
      });
      await keepWorkflow(page, "note");
      await page.reload();
      const dialog = await openTrigger(page, details);
      await expect(dialog).toContainText(
        "Test input is saved with this workflow.",
      );
      const box = (label: string) =>
        dialog.locator(".sd-input").getByLabel(label, { exact: true });
      await expect(box("Customer ID")).toHaveValue("c-1042");
      await expect(box("Amount")).toHaveValue("1500");
    });

    test("a later step's Input shows the test input's values", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await setTestInput(page, details, {
        "Customer ID": "c-1042",
        Amount: "1500",
      });
      await details.openWithKeyboard("check");
      const dialog = details.dialogFor("check");
      if (size.width < 768)
        await dialog
          .getByRole("tablist", { name: "Step details panes" })
          .getByRole("tab", { name: "Input" })
          .click();
      const pane = dialog.locator("weave-input-pane");
      await expect(pane.locator(".pane-source-tag")).toHaveText("Test input");
      const row = pane.locator(".data-row", {
        has: page.locator(".data-key", { hasText: /^customerId$/ }),
      });
      await expect(row.locator(".data-sample")).toHaveText('"c-1042"');
    });

    test("a formula that reads a pinned step previews it", async ({ page }) => {
      await withLanguageFeatures(page);
      const details = await openStepFixture(page, pinned);
      await keepWorkflow(page, "review");
      const key = `local:weave.localDraft.${draftId(page)}.testData`;
      await page.evaluate(
        (key) =>
          localStorage.setItem(
            key,
            JSON.stringify({
              schemaVersion: 1,
              kind: "weave.studio/testData",
              updatedAt: "2026-10-20T10:00:00Z",
              pins: {
                lookup: {
                  output: { tier: "gold" },
                  source: "manual",
                  pinnedAt: "2026-10-20T10:00:00Z",
                  configHash: "fnv1a64:0000000000000001",
                  contractHash: "fnv1a64:0000000000000002",
                  upstreamHash: "fnv1a64:0000000000000003",
                },
              },
            }),
          ),
        key,
      );
      await page.reload();
      await details.openWithKeyboard("review");
      await expect(fieldLine(details, "title")).toHaveText("= Tier gold");
    });

    test("a secret test input field is never saved", async ({ page }) => {
      const details = await openStepFixture(page, formulasFixture);
      const dialog = await openTrigger(page, details);
      await dialog.getByRole("button", { name: "Generate sample" }).click();
      await expect(
        dialog.locator(".sd-input").getByLabel("Customer ID", { exact: true }),
      ).not.toHaveValue("");
      await details.close();
      await keepWorkflow(page, "note");
      // The sample filled every field but the secret one, and only that was kept.
      const kept = await page.evaluate(() =>
        Object.entries(localStorage)
          .filter(([key]) => key.endsWith(".testData"))
          .map(([, value]) => value)
          .join(""),
      );
      expect(kept).toContain("customerId");
      expect(kept).not.toContain("password");
    });
  });
