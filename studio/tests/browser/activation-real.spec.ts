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
// Listening for a webhook's test event on a REAL running local platform (no
// route mocks): Studio gets a test URL on a test activation of the saved
// draft, a request signed with the test secret starts a test run, Studio
// follows it in the Runs tab marked Test, and Operate › Runs shows it only
// with Include test runs. Skipped unless WEAVE_E2E_PLATFORM_DIR and
// WEAVE_E2E_PERSON_FILE are set, as real-platform.spec.ts describes, and the
// acceptance run's private canaries.json (WEAVE_ACC_RUN_DIR) holds the value
// of the order-review-webhook handle; build the app first (`npm run build`).
import { createHmac, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";
import {
  connectAndSignIn,
  expectNoCredentialsLeft,
  openSidebar,
  pair,
  removePlatform,
  session,
  setup,
  shot,
  uniqueName,
  useRealPlatformSuite,
} from "./real-platform-support";
import { useNewEditor } from "./canvas-po";

useRealPlatformSuite("activation-real");

const runDir = process.env["WEAVE_ACC_RUN_DIR"]?.trim() ?? "";
/** The test secret's handle and its value, from the acceptance run's canaries. */
function testSecret(): { handle: string; value: string } {
  const handle = "order-review-webhook";
  const canaries = JSON.parse(
    readFileSync(join(runDir, "private", "canaries.json"), "utf8"),
  ) as Record<string, string>;
  return { handle, value: canaries[handle] };
}

test("a webhook listens on a real test URL; the signed request's run is followed as a Test run", async ({
  page,
}) => {
  test.skip(!runDir, "Set WEAVE_ACC_RUN_DIR to the acceptance run's folder.");
  test.setTimeout(600_000);
  const { person } = setup();
  const secret = testSecret();
  const s = await session(page);
  const name = uniqueName("e2e-listen");
  await useNewEditor(page);
  await pair(page, s.host!, "al-01-pairing");
  await connectAndSignIn(page, name, person);

  const workflow = uniqueName("listen");
  const webhook = uniqueName("orders-in");
  await page.getByRole("button", { name: "Home", exact: true }).click();
  await page.getByLabel("Choose a workflow file").setInputFiles([
    {
      name: `${workflow}.yaml`,
      mimeType: "application/yaml",
      buffer: Buffer.from(`apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: ${workflow}, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties: {orderId: {type: string}}
  outputSchema: {}
  steps:
    - id: echo
      kind: transform
      value: {ref: /input}
  output: {ref: /steps/echo/output}
`),
    },
    {
      name: `${workflow}.canvas.json`,
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({
          schemaVersion: 2,
          kind: "weave.studio/canvas",
          triggers: [
            {
              id: "trigger-1b2c3d4e",
              kind: "webhook",
              name: webhook,
              secretRef: "acme-api-key",
              testSecretRef: secret.handle,
            },
          ],
        }),
      ),
    },
  ]);
  await expect(page.locator(".editor-bar")).toBeVisible({ timeout: 30_000 });
  await page
    .locator('.tile-node[data-tile="$trigger:trigger-1b2c3d4e"] .tile-body')
    .dblclick();
  const details = page.getByRole("dialog", {
    name: "Step details: Webhook trigger",
    exact: true,
  });
  await expect(details).toBeVisible();

  // Listening saves the draft and gets a test URL on a test activation.
  const listen = details
    .locator(".sd-actions")
    .getByRole("button", { name: "Listen for test event", exact: true });
  await expect(listen).toBeVisible({ timeout: 60_000 });
  await listen.click();
  const url = details.getByRole("textbox", { name: "Make a POST request to:" });
  await expect(url).toHaveValue(/\/webhooks\//, { timeout: 120_000 });
  await expect(details.getByLabel("Signed cURL")).toContainText(
    "X-Weave-Signature: $SIGNATURE",
  );
  await shot(page, "al-02-listening");

  // A request signed with the test secret, as the cURL sends it.
  const eventId = `e2e-${randomUUID()}`;
  const body = JSON.stringify({ eventId, payload: { orderId: "o-77" } });
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = createHmac("sha256", secret.value)
    .update(`${timestamp}.${body}`)
    .digest("hex");
  const sent = await fetch(await url.inputValue(), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Weave-Timestamp": timestamp,
      "X-Weave-Event-Id": eventId,
      "X-Weave-Signature": signature,
    },
    body,
  });
  expect(sent.status, await sent.text()).toBe(202);

  // Studio follows the run it started in the Runs tab, marked Test.
  await expect(page.locator("#editor-view")).toHaveAttribute(
    "aria-labelledby",
    "view-tab-runs",
    { timeout: 60_000 },
  );
  await shot(page, "al-03-test-event-received");
  await details.getByRole("button", { name: "Close", exact: true }).click();
  await expect(details).toHaveCount(0);
  const list = page.locator(".runs-list");
  await expect(list.getByRole("option").first()).toContainText("Test", {
    timeout: 60_000,
  });
  // The run the signed request started is the one the Runs tab shows.
  await expect(page.locator(".runs-canvas")).toContainText(/Run [0-9a-f]{8}/, {
    timeout: 60_000,
  });
  await shot(page, "al-04-test-run");

  // Operate › Runs leaves test runs out unless asked.
  await openSidebar(page, "Runs");
  const operate = page.locator("weave-runs-page");
  await expect(operate).toBeVisible();
  // The run's row names its workflow with a button; the filters list it too.
  const row = operate
    .getByRole("table", { name: "Runs" })
    .getByRole("row")
    .filter({
      has: page.getByRole("button", { name: workflow, exact: true }),
    });
  await expect(row).toHaveCount(0);
  await operate.getByLabel("Include test runs").check();
  await expect(row).toHaveCount(1, { timeout: 30_000 });
  await expect(row).toContainText("Test");
  await shot(page, "al-05-operate-include-test");

  await removePlatform(page, name);
  expectNoCredentialsLeft();
});
