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
// From choosing a connection to pinning a real result, with nothing kept on
// this computer at the start: choose one the platform lists, create another
// in place, test it, and find the choice again after a reload; then execute
// the step in the environment (the platform asks for a confirmation first),
// pin what it returned, and find the pin and where it came from after a reload.
import { expect, test } from "@playwright/test";
import {
  PRODUCTION,
  bindable,
  connectionSelect,
  crmWorkflow,
  keptOnThisComputer,
  openConnected,
  routeConnectionForm,
  type ConnectionsState,
} from "./connections-po";
import {
  RUN_ID,
  accepted,
  executeIn,
  refusal,
  routeStepTests,
  sent,
  showOutput,
  succeeded,
  view,
} from "./step-tests-po";
import { sizes } from "./step-details-po";

for (const size of sizes)
  test.describe(`connections and real step runs at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("choose, create and test a connection, execute the step for real and pin its output", async ({
      page,
    }) => {
      test.setTimeout(90_000);
      const state: ConnectionsState = {
        bindable: { [PRODUCTION]: [bindable(1)] },
      };
      const { details, recorder } = await openConnected(
        page,
        state,
        crmWorkflow(
          {},
          { with: { object: { customerId: { literal: "c-1042" } } } },
        ),
      );
      expect(await keptOnThisComputer(page)).not.toContain("devBindings");
      const created: { id: string; body: Record<string, unknown> }[] = [];
      await routeConnectionForm(page, state, created);
      const steps = await routeStepTests(page, {
        starts: [
          refusal(409, "WV-STEPTEST-CONFIRM", {
            side_effect: "idempotency_key",
            action: "acme.crm-lookup@1.0.0",
            environment: "Production",
          }),
          { status: 202, json: accepted },
        ],
        views: [view(), succeeded({ name: "Ana", open: 2 })],
      });
      const options = page
        .getByRole("listbox", { name: "Connection options" })
        .getByRole("option");

      // Choose from the platform's list.
      await details.open("crm-lookup");
      await connectionSelect(details).click();
      await options.filter({ hasText: /^acme-crm-1/ }).click();
      await expect(details.field("connection")).toContainText(
        "Tested just now",
      );

      // Create another in place; it becomes the step's connection, tested.
      await connectionSelect(details).click();
      await options.filter({ hasText: "Create connection" }).click();
      const form = page.getByRole("dialog", {
        name: "New API connection",
        exact: true,
      });
      await form.getByLabel("Connection name", { exact: true }).fill("acme-eu");
      await form
        .getByLabel("API address", { exact: true })
        .fill("https://eu.acme.test");
      const create = form.getByRole("button", { name: "Create connection" });
      await create.scrollIntoViewIfNeeded();
      await create.click();
      await expect(form).toHaveCount(0);
      await expect(connectionSelect(details)).toHaveValue("acme-eu");
      await expect(details.field("connection")).toContainText(
        "Tested just now",
      );
      expect(recorder.tests).toHaveLength(2);
      await expect
        .poll(() => keptOnThisComputer(page))
        .toContain(created[0].id);

      // Execute in Production: the platform asks first, Run it sends a new key.
      await (await executeIn(details, "Production")).click();
      await page
        .getByRole("alertdialog", { name: "Execute crm-lookup in Production?" })
        .getByRole("button", { name: "Run it" })
        .click();
      await sent(steps, 2);
      expect(steps.posts[1].headers()["idempotency-key"]).not.toBe(
        steps.posts[0].headers()["idempotency-key"],
      );
      const output = await showOutput(details);
      await expect(output).toContainText(
        `From Production · run ${RUN_ID.slice(0, 8)}`,
      );
      await output.getByRole("button", { name: "Pin output" }).click();
      await expect(output).toContainText("Pinned");
      await expect
        .poll(
          () =>
            steps.documents.some((request) =>
              (request.postData() ?? "").includes('"test-call"'),
            ),
          { timeout: 15_000 },
        )
        .toBe(true);
      await details.close();

      // After a reload: the choice and the pin are back; nothing else was kept.
      await page.reload();
      await details.open("crm-lookup");
      await expect(connectionSelect(details)).toHaveValue("acme-eu");
      const again = await showOutput(details);
      await expect(again).toContainText("Pinned");
      await expect(again).toContainText("Ana");
      // Kept on this computer: the pin, as test data from that run, and never
      // the real result among the last results.
      const kept = JSON.parse(await keptOnThisComputer(page)) as {
        local: Record<string, string>;
      };
      const pins = Object.entries(kept.local).find(([key]) =>
        key.includes(":testData."),
      );
      expect(JSON.parse(pins![1]).pins["crm-lookup"]).toMatchObject({
        source: "test-call",
        runId: RUN_ID,
        runEnvironmentId: PRODUCTION,
      });
      for (const [key, text] of Object.entries(kept.local))
        if (key.includes("weave.lastExecution."))
          expect(text).not.toContain("Ana");
    });
  });
