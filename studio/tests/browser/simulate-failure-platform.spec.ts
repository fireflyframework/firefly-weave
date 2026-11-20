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
// A platform that predates simulated failures refuses test data holding
// one: Studio saves the rest, keeps the failure for this session, says so
// over the step's output, and keeps no copy of it on this computer.
import { expect, test } from "@playwright/test";
import { StepDetailsPage } from "./step-details-po";
import { outputPane } from "./test-data-po";
import {
  edit,
  fresh,
  kept,
  openDraft,
  testData,
} from "./connected-test-data-po";

test("a simulated failure the platform can't save lasts for the session, and the rest saves", async ({
  page,
}) => {
  const state = fresh();
  state.refuse.push({
    status: 422,
    json: {
      status: 422,
      code: "WV-STUDIO-DOCUMENT-SCHEMA",
      message: "The document doesn't match its schema.",
    },
  });
  await openDraft(
    page,
    state,
    testData(
      { customerId: "c-1" },
      {
        errors: {
          "check-customer": { code: "REQUEST_REFUSED", source: "manual" },
        },
      },
    ),
  );
  await edit(page);
  await expect
    .poll(() => state.saves.filter((save) => save.kind === "test-data").length)
    .toBe(2);
  const [refused, saved] = state.saves.filter(
    (save) => save.kind === "test-data",
  );
  expect(JSON.stringify(refused.body)).toContain("REQUEST_REFUSED");
  expect(JSON.stringify(saved.body)).not.toContain("REQUEST_REFUSED");
  expect(JSON.stringify(saved.body)).toContain("c-1");
  // No copy on this computer keeps it.
  await expect.poll(() => kept(page)).not.toContain("REQUEST_REFUSED");
  const details = new StepDetailsPage(page);
  await details.open("check-customer");
  const banner = (await outputPane(details)).locator("weave-failure-banner");
  await expect(banner).toContainText(
    "This platform can't save simulated failures yet. They last until you close this workflow.",
  );
});
