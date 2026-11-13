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
// The Runs tab by keyboard, as on every platform the visual tour runs on:
// the list's keys, Escape back from the canvas and from step details, the
// keyboard sheet's Runs section, and editing keys that change nothing.
import { expect, test } from "@playwright/test";
import { listedKinds, openRunsTab, RUN_IDS } from "./runs-po";
import { StepDetailsPage } from "./step-details-po";

test.use({ viewport: { width: 1440, height: 900 } });

test("the list, the canvas and step details work by keyboard", async ({
  page,
}) => {
  const { runs } = await openRunsTab(page);
  await expect(runs.rows).toHaveCount(listedKinds.length + 1);
  // Up, Down, Home and End choose; Enter shows the chosen run.
  await runs.rows.first().focus();
  await page.keyboard.press("ArrowDown");
  await expect(runs.rows.nth(1)).toBeFocused();
  await page.keyboard.press("End");
  await expect(runs.rows.last()).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Home");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await expect(runs.canvas.locator(".run-header h2")).toHaveText(
    "Run 1c0d7f8a",
  );
  const chosen = runs.row(RUN_IDS.failed);
  await expect(chosen).toHaveAttribute("aria-selected", "true");

  // Enter on a tile opens step details at Output; Escape returns to the tile.
  const details = new StepDetailsPage(page);
  await details.openWithKeyboard("send");
  await page.keyboard.press("Escape");
  await expect(details.dialog).toHaveCount(0);
  await expect(details.node("send")).toBeFocused();
  // Escape on the canvas first clears the selection, then returns to the
  // chosen row.
  await page.keyboard.press("Escape");
  await expect(details.node("send")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(chosen).toBeFocused();

  // Editing keys change nothing on the Runs tab, and ask nothing.
  await details.node("send").focus();
  for (const key of ["Delete", "ControlOrMeta+z", "d", "v"])
    await page.keyboard.press(key);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(details.node("send")).toBeVisible();

  // The keyboard sheet lists the Runs keys.
  await chosen.focus();
  await page.keyboard.press("?");
  const sheet = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  await expect(sheet.getByRole("heading", { name: "Runs" })).toBeVisible();
  await expect(sheet).toContainText("Show the run on the canvas");
});
