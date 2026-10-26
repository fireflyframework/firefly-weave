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
// Execute in step details: the test data fixture answered by the repository's
// own host code (localAuthoring), and the controls Execute adds.
import { expect, type Locator, type Page } from "@playwright/test";
import { localAuthoring, type LocalCalls } from "./local-authoring";
import { openStepFixture, type WorkflowFile } from "./step-details-po";
import { outputPane } from "./test-data-po";
import { resolve } from "node:path";
import type { StepDetailsPage } from "./step-details-po";
import { connected, defaultCatalog } from "./support";
import { lookupContract, PROJECT, TENANT } from "./test-data-po";
import { withLanguageFeatures } from "./step-details-po";

/** The test data fixture's workflow, with an AI profile and connection the compiler accepts. */
export const executeFixture = resolve("tests/fixtures/execute.yaml");

/** The execute fixture, with Execute answered by the host's own simulation. */
export const openExecuteFixture = (
  page: Page,
  before?: (page: Page) => Promise<unknown>,
) => openExecuteWorkflow(page, executeFixture, before);

/** Another workflow, connected as the test data fixture is, with Execute answered the same way. */
export async function openExecuteWorkflow(
  page: Page,
  file: WorkflowFile,
  before: (page: Page) => Promise<unknown> = async () => undefined,
): Promise<{ details: StepDetailsPage; calls: LocalCalls[] }> {
  const details = await openStepFixture(page, file, async (p) => {
    const result = await connected(p, {
      catalog: [...defaultCatalog, { id: "a3", document: lookupContract }],
      tenantId: TENANT,
      projectId: PROJECT,
    });
    await withLanguageFeatures(p);
    await before(p);
    return result;
  });
  const calls = await localAuthoring(page);
  return { details, calls };
}

export const executeButton = (dialog: Locator): Locator =>
  dialog.locator(".sd-execute > button").first();
export const moreWays = (dialog: Locator): Locator =>
  dialog.getByRole("button", { name: "More ways to execute" });
export const stopButton = (dialog: Locator): Locator =>
  dialog
    .locator(".sd-execute")
    .getByRole("button", { name: "Stop", exact: true });
export const gateDialog = (page: Page): Locator =>
  page.locator('.execute-dialog[role="alertdialog"]');
export const executions = (calls: LocalCalls[]): LocalCalls[] =>
  calls.filter((call) => call.path === "/studio/local/debug/execute");

/** Pins a sample output on `id` from its Output pane. */
export async function pinSample(details: StepDetailsPage, id: string) {
  await details.open(id);
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "Generate sample" }).click();
  await expect(pane.locator(".pinned-banner")).toBeVisible();
  await details.close();
}

/** Shows the header where its pane hides it (a narrow sheet), then returns Execute. */
export async function shownExecute(details: StepDetailsPage): Promise<Locator> {
  const button = executeButton(details.dialog);
  if (!(await button.isVisible())) {
    const parameters = details.dialog
      .getByRole("tablist", { name: "Step details panes" })
      .getByRole("tab", { name: "Parameters" });
    if (await parameters.count()) await parameters.click();
  }
  await expect(button).toBeVisible();
  return button;
}

/** Answers the host's check with `diagnostics`, and validates now. */
export async function validateWith(page: Page, diagnostics: object[]) {
  await page.route("**/studio/local/validate", (route) =>
    route.fulfill({
      json: {
        validationOk: false,
        errorCount: diagnostics.length,
        diagnostics,
        partial: true,
      },
    }),
  );
  const validate = page.getByRole("button", { name: "Validate", exact: true });
  if (await validate.isVisible()) await validate.click();
  else {
    await page.getByRole("button", { name: "More workflow commands" }).click();
    await page.getByRole("menuitem", { name: "Validate", exact: true }).click();
  }
  const dismiss = page.getByRole("button", { name: "Dismiss notification" });
  await expect(dismiss.first()).toBeVisible();
  await dismiss.first().click();
}
