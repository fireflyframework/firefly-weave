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
// A simulated failure in step details: a step pasted with its hand-set
// failure brings it into the test data, and the Output pane says the step
// fails when executed in Studio, with its code, and removes it with Undo.
import { expect, test } from "@playwright/test";
import { openNewWorkflow } from "./canvas-po";
import { StepDetailsPage } from "./step-details-po";
import { outputPane } from "./test-data-po";

test.use({ permissions: ["clipboard-read", "clipboard-write"] });

const PASTED = JSON.stringify({
  weaveClipboard: 1,
  steps: [
    {
      id: "submit",
      kind: "transform",
      value: { object: { sent: { literal: true } } },
      onError: "continue",
    },
  ],
  connections: {},
  llmProfiles: {},
  pins: {},
  errors: {
    submit: { code: "REQUEST_REFUSED", outcome: "unknown", source: "manual" },
  },
});

test("a pasted step keeps its simulated failure, shown over its output and removed with Undo", async ({
  page,
}) => {
  const canvas = await openNewWorkflow(page);
  await page.evaluate((text) => navigator.clipboard.writeText(text), PASTED);
  await canvas.root.focus();
  await page.keyboard.press("ControlOrMeta+v");
  await expect(canvas.tile("submit")).toBeVisible();
  const details = new StepDetailsPage(page);
  await details.open("submit");
  const pane = await outputPane(details);
  const banner = pane.locator("weave-failure-banner");
  await expect(banner.getByRole("status")).toHaveText(
    "This step fails when you execute it in Studio. Real runs ignore it.",
  );
  await expect(banner.locator(".failure-banner-code")).toHaveText(
    "REQUEST_REFUSED · Unknown",
  );
  await expect(
    pane.locator(".pill", { hasText: "Fails when executed" }),
  ).toBeVisible();
  await banner.getByRole("button", { name: "Remove" }).click();
  await expect(banner).toHaveCount(0);
  const toast = page
    .locator(".toast")
    .filter({ hasText: "Removed the simulated failure of submit." });
  await toast.getByRole("button", { name: "Undo" }).click();
  await expect(banner.locator(".failure-banner-code")).toHaveText(
    "REQUEST_REFUSED · Unknown",
  );
});
