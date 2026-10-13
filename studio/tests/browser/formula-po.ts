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
// Helpers for formulas in step details: the fixture, test input typed in
// the trigger's Test event, and the line under a field.
import { expect, type Locator, type Page } from "@playwright/test";
import { resolve } from "node:path";
import type { StepDetailsPage } from "./step-details-po";

export const formulasFixture = resolve("tests/fixtures/formulas.yaml");

/** Types test input in the trigger's Test event, then closes it. */
export async function setTestInput(
  page: Page,
  details: StepDetailsPage,
  values: Record<string, string>,
) {
  await details.trigger().dblclick();
  const dialog = details.dialogFor("Manual form trigger");
  await expect(dialog).toBeVisible();
  if ((page.viewportSize()?.width ?? 1440) < 768)
    await dialog
      .getByRole("tablist", { name: "Step details panes" })
      .getByRole("tab", { name: "Test event" })
      .click();
  await dialog.getByRole("button", { name: "Generate sample" }).click();
  for (const [label, value] of Object.entries(values))
    await dialog
      .locator(".sd-input")
      .getByLabel(label, { exact: true })
      .fill(value);
  await details.close();
}

/** The one line under a field: its message, its "= value", or its hint. */
export const fieldLine = (details: StepDetailsPage, id: string): Locator =>
  details.field(id).locator(".param-line").first();
