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
  // By keyboard: on a small window the canvas may have panned it out of view.
  await details.trigger().focus();
  await page.keyboard.press("Enter");
  const dialog = details.dialogFor("Manual form trigger");
  await expect(dialog).toBeVisible();
  if ((page.viewportSize()?.width ?? 1440) < 768)
    await dialog
      .getByRole("tablist", { name: "Step details panes" })
      .getByRole("tab", { name: "Test event" })
      .click();
  await dialog.getByRole("button", { name: "Generate sample" }).click();
  const box = (label: string) =>
    dialog.locator(".sd-input").getByLabel(label, { exact: true });
  // The sample replaces the form: wait for it before typing over it.
  await expect(box(Object.keys(values)[0])).not.toHaveValue("");
  for (const [label, value] of Object.entries(values))
    await box(label).fill(value);
  for (const [label, value] of Object.entries(values))
    await expect(box(label)).toHaveValue(value);
  await details.close();
}

/** The one line under a field: its message, its "= value", or its hint. */
export const fieldLine = (details: StepDetailsPage, id: string): Locator =>
  details.field(id).locator(".param-line").first();

/** A field's formula editor: its host carries the text in `data-text`. */
export const formulaBox = (field: Locator): Locator =>
  field.locator("weave-formula-editor");
/** The formula editor's textbox. */
export const formulaText = (field: Locator): Locator =>
  field.locator("weave-formula-editor .cm-content");
/** The open completion list. */
export const suggestions = (page: Page): Locator =>
  page.getByRole("listbox", { name: "Suggestions" });
/**
 * Replaces a formula's text: clears it, then inserts the new text in one
 * input event, as a paste does. (Chrome reports a replacement as only the
 * part that differs, so "input." over "input.amount" would arrive as a
 * deletion.)
 */
export async function replaceFormula(page: Page, field: Locator, text: string) {
  await formulaText(field).click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.press("Backspace");
  if (text) await page.keyboard.insertText(text);
}
