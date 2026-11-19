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
// Calling a workflow on a REAL running local platform (no route mocks): a
// callable workflow is published and activated from the editor; a draft that
// calls it runs in the environment as a test run on a test activation, which
// pins the callee's activation; on the Runs tab the call step's Output names
// the run it started, and Open run opens that run in the callee's Runs tab.
// Skipped unless WEAVE_E2E_PLATFORM_DIR and WEAVE_E2E_PERSON_FILE are set, as
// real-platform.spec.ts describes, on a platform whose environment allows test
// calls (the acceptance run's does); build the app first (`npm run build`).
import { expect, test, type Page } from "@playwright/test";
import { stringify } from "yaml";
import {
  connectAndSignIn,
  expectNoCredentialsLeft,
  pair,
  removePlatform,
  session,
  setup,
  shot,
  uniqueName,
  useRealPlatformSuite,
} from "./real-platform-support";
import { StepDetailsPage } from "./step-details-po";
import { command } from "./support";

useRealPlatformSuite("call-real");

const document = (
  name: string,
  steps: Record<string, unknown>[],
  output: string,
  callable: boolean,
) =>
  stringify({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name, version: "1.0.0" },
    spec: {
      inputSchema: {
        type: "object",
        properties: { customerId: { type: "string" } },
      },
      outputSchema: { type: "object" },
      ...(callable ? { callable: {} } : {}),
      steps,
      output: { ref: `/steps/${output}/output` },
    },
  });

/** Opens a workflow file from Home in the editor. */
async function openFile(page: Page, name: string, text: string) {
  await page.getByRole("button", { name: "Home", exact: true }).click();
  const leave = page.getByRole("dialog", { name: /^Leave / });
  if (await leave.isVisible().catch(() => false))
    await leave.getByRole("button", { name: "Leave", exact: true }).click();
  await page.getByLabel("Choose a workflow file").setInputFiles({
    name: `${name}.yaml`,
    mimeType: "application/yaml",
    buffer: Buffer.from(text),
  });
  await expect(page.locator(".editor-bar")).toContainText(name, {
    timeout: 30_000,
  });
}

test("a draft that calls a published workflow runs in the environment, and its call opens the run it started", async ({
  page,
}) => {
  test.setTimeout(600_000);
  await page.setViewportSize({ width: 1440, height: 900 });
  const { person } = setup();
  const s = await session(page);
  const platform = uniqueName("e2e-call");
  await pair(page, s.host!, "call-01-pairing");
  await connectAndSignIn(page, platform, person);

  // The callee: callable by any workflow of the project, published and activated.
  const callee = uniqueName("notify");
  await openFile(
    page,
    callee,
    document(
      callee,
      [
        {
          id: "render",
          kind: "transform",
          value: { object: { to: { ref: "/input/customerId" } } },
        },
      ],
      "render",
      true,
    ),
  );
  await command(page, "Publish…");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Publish version" })
    .click();
  await command(page, "Activate…");
  const activation = page.getByRole("dialog");
  await expect(activation.getByRole("status")).toHaveCount(0, {
    timeout: 60_000,
  });
  await activation
    .getByRole("button", { name: "Activate version", exact: true })
    .click();
  await expect(activation).toHaveCount(0, { timeout: 60_000 });
  await shot(page, "call-02-callee-active");

  // The caller: a draft that calls it with its input mapped.
  const caller = uniqueName("intake");
  await openFile(
    page,
    caller,
    document(
      caller,
      [
        {
          id: "notify",
          kind: "callWorkflow",
          uses: `${callee}@1.0.0`,
          mode: "wait",
          with: { object: { customerId: { literal: "c-42" } } },
        },
      ],
      "notify",
      false,
    ),
  );
  await page
    .locator(".editor-toolbar")
    .getByRole("button", { name: "More ways to execute" })
    .click();
  await page.getByRole("menuitem", { name: /^Execute workflow in / }).click();
  // The test run opens in the Runs tab once it ends.
  await expect(page.locator("#editor-view")).toHaveAttribute(
    "aria-labelledby",
    "view-tab-runs",
    { timeout: 120_000 },
  );
  await expect(page.locator(".runs-canvas")).toContainText(/Run [0-9a-f]{8}/, {
    timeout: 60_000,
  });
  await shot(page, "call-03-caller-test-run");

  // Its call step's Output names the run it started, and how it went.
  const details = new StepDetailsPage(page);
  await details.openWithKeyboard("notify");
  const card = details.dialog
    .locator('[data-region="output"]')
    .getByRole("group", { name: "Called run" });
  await expect(card.locator(".pane-child-title")).toHaveText(
    new RegExp(`^Ran as run [0-9a-f]{8} of ${callee}$`),
    { timeout: 60_000 },
  );
  await expect(card.locator(".pane-child-line")).toHaveText(
    new RegExp(`^${callee} 1\\.0\\.0 · (?:completed|succeeded|finished)`, "i"),
    { timeout: 60_000 },
  );
  const child = (await card.locator(".pane-child-title").textContent())!
    .split(" ")[3]
    .trim();
  await shot(page, "call-04-child-run-line");

  // Open run: the callee's Runs tab, on that run.
  await card.getByRole("button", { name: "Open run" }).click();
  // No draft of the callee here yet: Studio offers one from the version the run used.
  const create = page.getByRole("dialog", {
    name: "Create a draft from 1.0.0?",
  });
  const calleeBar = page.locator(".editor-bar").filter({ hasText: callee });
  await expect(create.or(calleeBar).first()).toBeVisible({ timeout: 60_000 });
  if (await create.isVisible())
    await create.getByRole("button", { name: "Create draft" }).click();
  await expect(page.locator(".editor-bar")).toContainText(callee, {
    timeout: 60_000,
  });
  await expect(page.locator("#editor-view")).toHaveAttribute(
    "aria-labelledby",
    "view-tab-runs",
  );
  await expect(page.locator(".runs-canvas")).toContainText(`Run ${child}`, {
    timeout: 60_000,
  });
  await shot(page, "call-05-callee-runs-tab");

  await removePlatform(page, platform);
  expectNoCredentialsLeft();
});
