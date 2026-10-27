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
// The first page stays small: the test data service, its reader, its
// operations and its check load with the test data tools once a workflow
// opens, and the designer waits for them.
import { expect, test, type Page } from "@playwright/test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { repository } from "../python-path";
import {
  fitCanvas,
  openStepFixture,
  withLanguageFeatures,
} from "./step-details-po";
import { connected, defaultCatalog } from "./support";
import {
  banner,
  lookupContract,
  outputPane,
  PROJECT,
  routeDrafts,
  TENANT,
  testDataFixture,
} from "./test-data-po";

/**
 * Text only the test data service and its operations hold. The production
 * build is what a person loads; the dev server bundles differently.
 */
const MARKERS = [
  "couldn't read the test data kept for this workflow",
  "Test data is limited to 1 MB.",
];
const BUILD = join(repository, "studio/dist/studio/browser");

/** Starts connected, as the test data fixture does. */
async function startConnected(page: Page) {
  const result = await connected(page, {
    catalog: [...defaultCatalog, { id: "a3", document: lookupContract }],
    tenantId: TENANT,
    projectId: PROJECT,
  });
  await withLanguageFeatures(page);
  return result;
}

test("the first page of the production build holds no test data code", () => {
  // CI builds the production app before the browser tests (the real-host test serves it too).
  const index = readFileSync(join(BUILD, "index.html"), "utf8");
  const first = [...index.matchAll(/(?:src|href)="([^"]+\.js)"/g)].map(
    (match) => match[1],
  );
  expect(first.length).toBeGreaterThan(0);
  const holding = first.filter((file) => {
    const text = readFileSync(join(BUILD, file), "utf8");
    return MARKERS.some((marker) => text.includes(marker));
  });
  expect(holding).toEqual([]);
  // The service is in the build, in a chunk loaded later.
  const later = readdirSync(BUILD).filter(
    (file) =>
      file.endsWith(".js") &&
      !first.includes(file) &&
      readFileSync(join(BUILD, file), "utf8").includes(MARKERS[0]),
  );
  expect(later.length).toBe(1);
});

test("the designer opens once the test data service loads, and step details read it", async ({
  page,
}) => {
  const details = await openStepFixture(page, testDataFixture, startConnected);
  await details.open("lookup");
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "Generate sample" }).click();
  await expect(banner(pane)).toBeVisible();
});

test("a pin set right after opening is kept and follows a rename", async ({
  page,
}) => {
  // On a cold load the designer waits for the test data chunk; pins behave as before.
  const details = await openStepFixture(page, testDataFixture, startConnected);
  const drafts = await routeDrafts(page);
  await details.open("lookup");
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "Generate sample" }).click();
  await expect(banner(pane)).toBeVisible();
  await details.close();
  // A change to the workflow saves the draft (and the pin with it) on the platform.
  await fitCanvas(page);
  await page.locator('[data-step="notify"]').first().click();
  await page.keyboard.press("ControlOrMeta+d");
  await expect.poll(() => drafts.size, { timeout: 15000 }).toBe(1);
  await page.reload();
  await details.open("lookup");
  await expect(banner(await outputPane(details))).toBeVisible();
  await details.dialog.getByRole("button", { name: "Rename lookup" }).click();
  await details.dialog.getByRole("textbox", { name: "Step name" }).fill("find");
  await page.keyboard.press("Enter");
  await expect(details.dialogFor("find")).toBeVisible();
  await expect(banner(await outputPane(details))).toBeVisible();
});
