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
import { test, expect } from "@playwright/test";
import { offline, newWorkflow, insertStep } from "./support";
import { DesignerPage } from "./designer-po";

test.use({ viewport: { width: 1440, height: 900 } });

for (const direction of ["back", "forward"] as const) {
  test(`browser ${direction} keeps invalid edits and preserves the destination for retry`, async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    await insertStep(page, "Fail");
    const designer = new DesignerPage(page);
    const workflowUrl = page.url();
    if (direction === "forward") {
      await page.getByRole("button", { name: "Home", exact: true }).click();
      await page.getByRole("button", { name: "Leave", exact: true }).click();
      await expect(page).toHaveURL(/\/home$/);
      await page.goBack();
      await expect(page).toHaveURL(workflowUrl);
    }
    await designer.selectStep("fail-1");
    const field = designer.inspectorField("Error code");
    await field.fill("not valid!");
    // The reload/close guard must also see raw edits that cannot be committed.
    expect(
      await page.evaluate(() => {
        const event = new Event("beforeunload", { cancelable: true });
        window.dispatchEvent(event);
        return event.defaultPrevented;
      }),
    ).toBe(true);
    await page.evaluate((direction) => history[direction](), direction);
    await expect(
      page.getByText(
        "Fix the invalid fields before continuing. Your edits are still here.",
        { exact: true },
      ),
    ).toBeVisible();
    await expect(page).toHaveURL(workflowUrl);
    await expect(field).toHaveValue("not valid!");
    await field.fill("corrected-error");
    await page.evaluate((direction) => history[direction](), direction);
    await expect(page).toHaveURL(/\/home$/);
    await expect(page.locator(".editor-bar")).not.toBeVisible();
    await page.evaluate(
      (direction) => history[direction === "back" ? "forward" : "back"](),
      direction,
    );
    await expect(page).toHaveURL(workflowUrl);
    await designer.selectStep("fail-1");
    await expect(designer.inspectorField("Error code")).toHaveValue(
      "corrected-error",
    );
  });
}
