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
// A change sent while the platform's sign-in belongs to someone else than
// the person Studio shows: the platform refuses it, and Studio says so and
// sends nothing more.
import { expect, test } from "@playwright/test";
import {
  PRODUCTION,
  bindable,
  connectionSelect,
  crmWorkflow,
  openConnected,
} from "./connections-po";
import { executeIn, output, refusal, routeStepTests } from "./step-tests-po";

test.use({ viewport: { width: 1440, height: 900 } });

test("a step test refused for another person says so and sends nothing more", async ({
  page,
}) => {
  const { details } = await openConnected(
    page,
    { bindable: { [PRODUCTION]: [bindable(1)] } },
    crmWorkflow(
      {},
      { with: { object: { customerId: { literal: "c-1042" } } } },
    ),
  );
  const steps = await routeStepTests(page, {
    starts: [refusal(409, "WV-PRINCIPAL-CHANGED")],
    views: [],
  });
  await details.open("crm-lookup");
  await connectionSelect(details).click();
  await page
    .getByRole("listbox", { name: "Connection options" })
    .getByRole("option")
    .filter({ hasText: /^acme-crm-1/ })
    .click();
  await (await executeIn(details, "Production")).click();
  await expect(output(details)).toContainText(
    "You're now signed in as someone else, so Studio sent nothing. Check the account in Settings.",
  );
  await page.waitForTimeout(500);
  expect(steps.posts).toHaveLength(1);
});
