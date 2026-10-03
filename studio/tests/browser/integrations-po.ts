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
// Page helpers for the quick-integration UI: the inspector's searchable action
// picker (an ARIA combobox), the palette's Integrations section and the
// shared screenshot folder for the program's journey evidence.
import { expect, Page, TestInfo } from "@playwright/test";
import { copyFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

/** The inspector's "Published action" combobox. */
export const actionPicker = (page: Page) =>
  page.getByRole("combobox", { name: "Published action", exact: true });

/**
 * Chooses a published action in the inspector's picker with real clicks: by
 * name@version, or the first action listed when the catalog has only one.
 */
export async function chooseAction(page: Page, uses?: string) {
  const box = actionPicker(page);
  await box.click();
  if (uses) await box.fill(uses);
  const option = uses
    ? page.locator(`[role="option"][data-uses="${uses}"]`)
    : page.locator('[role="option"][data-uses]').first();
  await option.click();
  await expect(page.locator('[role="listbox"]:visible')).toHaveCount(0);
}

/** The palette's "Published actions" section. */
export const paletteIntegrations = (page: Page) =>
  page.locator("weave-palette-integrations");

/** Shows the palette (a popover on narrow layouts) and returns its Published actions section. */
export async function openPaletteIntegrations(page: Page) {
  // The designer renders a moment after it opens: wait for the palette's
  // section (wide layouts) or the toolbar's "Insert step" (narrow ones).
  const section = paletteIntegrations(page);
  const insert = page.getByRole("button", { name: "Insert step" });
  await expect(
    section.or(insert).filter({ visible: true }).first(),
  ).toBeVisible();
  if (!(await section.isVisible())) await insert.click();
  await expect(section).toBeVisible();
  return section;
}

/**
 * Saves a journey screenshot into this run's output folder (for example
 * test-results/<lane>/journey/) and, when STUDIO_PROGRAM_SHOTS names a
 * folder, a copy there for the program's evidence.
 */
export async function shot(page: Page, info: TestInfo, name: string) {
  const folder = join(info.project.outputDir, "journey");
  mkdirSync(folder, { recursive: true });
  const file = join(folder, `${name}.png`);
  await page.screenshot({ path: file });
  const copies = process.env["STUDIO_PROGRAM_SHOTS"]?.trim();
  if (copies) {
    mkdirSync(copies, { recursive: true });
    copyFileSync(file, join(copies, `${name}.png`));
  }
}

/** Opens the builder's "Show YAML" disclosure, where the action's YAML is. */
export async function openYaml(page: Page) {
  const details = page.locator(".hb-yaml-details");
  await expect(details).toBeVisible();
  if (!(await details.evaluate((d) => (d as HTMLDetailsElement).open)))
    await details.locator("summary").click();
}
