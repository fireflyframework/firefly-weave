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
// The fields a new step of every kind shows by default, counted as a person
// sees them on its Parameters tab.
import { expect, test } from "@playwright/test";
import {
  StepDetailsPage,
  addCommonStep,
  settingsGear,
  sizes,
  withNewEditor,
} from "./step-details-po";
import { insertStep, newWorkflow, offline } from "./support";

/** Palette steps and their default field counts. A new action step has no action yet, so it shows Action alone; its connection and required inputs follow the chosen action. */
const kinds: [string, number][] = [
  ["Call an action", 1],
  ["Decision table", 1],
  ["AI task", 2],
  ["Transform", 1],
  ["Decision", 1],
  ["Parallel", 1],
  ["Wait for time", 1],
  ["Wait for signal", 1],
  ["Human task", 3],
  ["Fail", 2],
];
const common: [string, number][] = [
  ["Call an API", 3],
  ["Send email", 4],
  ["Reply to an email", 4],
  ["List files", 2],
  ["Read a file", 2],
  ["Download a file", 2],
  ["Write a file", 3],
  ["Move a file", 3],
  ["Delete a file", 2],
];

for (const size of sizes)
  test.describe(`default fields at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a new step of each kind shows its default fields", async ({
      page,
    }) => {
      test.slow();
      await withNewEditor(page);
      await offline(page);
      await newWorkflow(page);
      const details = new StepDetailsPage(page);
      for (const [label, count] of kinds) {
        await insertStep(page, label);
        await expect(details.dialog, label).toBeVisible();
        await expect(details.topFields(), label).toHaveCount(count);
        await details.close();
      }
      for (const [label, count] of common) {
        await addCommonStep(page, label);
        await expect(details.dialog, label).toBeVisible();
        await expect(details.topFields(), label).toHaveCount(count);
        await details.close();
      }
      await details.trigger().dblclick();
      await expect(details.topFields(), "Manual form trigger").toHaveCount(1);
      await details.close();
      await settingsGear(page).click();
      await expect(
        page
          .getByRole("dialog", { name: "Workflow settings" })
          .locator(".param-form > [data-param]"),
      ).toHaveCount(2);
    });
  });
