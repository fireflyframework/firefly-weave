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
// Who may execute a step for real, where, and what happens to an answer that
// arrives after the workspace changed. The platform decides; Studio offers
// Execute in local only to people who hold the step tester role there.
import { expect, test } from "@playwright/test";
import {
  PRODUCTION,
  STAGING,
  bindable,
  connectionSelect,
  crmWorkflow,
  keptOnThisComputer,
  openConnected,
} from "./connections-po";
import {
  accepted,
  executeIn,
  output,
  routeStepTests,
  sent,
  succeeded,
  view,
} from "./step-tests-po";
import { allCapabilities } from "./support";

test.use({ viewport: { width: 1440, height: 900 } });
const withInput = crmWorkflow(
  {},
  { with: { object: { customerId: { literal: "c-1042" } } } },
);

test("without the step tester role there's no Execute in local", async ({
  page,
}) => {
  const { details } = await openConnected(
    page,
    {
      bindable: { [PRODUCTION]: [bindable(1)] },
      capabilities: [...allCapabilities, "compile"],
    },
    withInput,
  );
  await details.open("crm-lookup");
  // Connected to Production: the field lists its connections, so only the role is missing.
  await connectionSelect(details).click();
  await expect(page.locator(".choice-group")).toHaveText(["In Production"]);
  await page.keyboard.press("Escape");
  await details.dialog
    .getByRole("button", { name: "More ways to execute" })
    .click();
  await expect(page.getByRole("menuitem")).toHaveCount(1);
  await expect(page.getByRole("menuitem")).toContainText(
    "Execute previous steps",
  );
});

test("an unreadable test policy says so", async ({ page }) => {
  const { details } = await openConnected(
    page,
    {
      bindable: { [PRODUCTION]: [bindable(1)] },
      policy: { [PRODUCTION]: 403 },
    },
    withInput,
  );
  await details.open("crm-lookup");
  const item = await executeIn(details, "Production");
  await expect(item).toBeDisabled();
  await expect(item).toContainText(
    "Your account can't see whether test calls are on in Production.",
  );
});

test("switching workspace while a step runs drops its answer", async ({
  page,
}) => {
  let release!: () => void;
  const holdReads = new Promise<void>((resolve) => (release = resolve));
  const { details } = await openConnected(
    page,
    {
      bindable: {
        [PRODUCTION]: [bindable(1)],
        [STAGING]: [bindable(2)],
      },
    },
    withInput,
  );
  const steps = await routeStepTests(page, {
    starts: [{ status: 202, json: accepted }],
    // The one read is the late, final answer: it lands after the switch.
    views: [succeeded({ name: "LATE-ANSWER-91c", open: 1 })],
    holdReads,
  });
  await details.open("crm-lookup");
  await connectionSelect(details).click();
  await page
    .getByRole("listbox", { name: "Connection options" })
    .getByRole("option")
    .filter({ hasText: /^acme-crm-1/ })
    .click();
  await (await executeIn(details, "Production")).click();
  await sent(steps, 1);
  await details.close();
  await page.locator(".platform-indicator").click();
  await page.getByRole("button", { name: "Switch workspace" }).click();
  await page.getByRole("radio", { name: "Acme / Payments / Staging" }).check();
  await page.getByRole("button", { name: "Start working" }).click();
  release();
  // Wait until the platform's late answer has reached the page.
  await expect.poll(() => steps.answered).toBeGreaterThan(0);
  await details.open("crm-lookup");
  await expect(output(details)).not.toContainText("LATE-ANSWER-91c");
  await expect(output(details)).not.toContainText("From Production");
  expect(await keptOnThisComputer(page)).not.toContain("LATE-ANSWER-91c");
});
