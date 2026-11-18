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
// The activation journey end to end on a mocked platform: a webhook chosen
// as the trigger is set up in step details, the workflow is activated with
// its connection chosen for the person, and the webhook is created with its
// production URL. Studio then listens for its test event on a test URL with
// a signed cURL, and the event's run opens in the Runs tab. A new version,
// activated, moves the same webhook: its URL stays.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { platform, routeWebhooks } from "./activation-po";
import { RUN_IDS, routeRuns, runFixture } from "./runs-po";
import { allCapabilities, command, showView } from "./support";
import { StepDetailsPage, withNewEditor } from "./step-details-po";

const WEBHOOK = "trigger-1b2c3d4e";
const TEST_URL = "https://platform.test/hooks/test/7c1d";
const workflow = stringify({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "pets", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    connections: { pets: { connector: "weave-http@2.0.0" } },
    steps: [
      {
        id: "fetch",
        kind: "action",
        uses: "get-pet@1.0.0",
        connection: "pets",
        with: { literal: {} },
      },
    ],
    output: { literal: {} },
  },
});
// "What triggers this workflow?" chose a webhook: its settings are still empty.
const canvas = {
  schemaVersion: 2,
  kind: "weave.studio/canvas",
  triggers: [{ id: WEBHOOK, kind: "webhook" }],
};

/** The test activation, its listen route, and the run a signed request starts. */
async function routeListening(page: Page) {
  let received = false;
  const run = runFixture("test");
  await routeRuns(page, [
    {
      ...run,
      summary: {
        ...run.summary,
        workflow: { ...run.summary.workflow, name: "pets" },
      },
    },
  ]);
  await page.route("**/environments/*/test-policy", (route) =>
    route.fulfill({ json: { test_calls: "enabled", revision: 1 } }),
  );
  await page.route("**/environments/*/run-summaries**", (route) => {
    const url = new URL(route.request().url());
    if (!url.searchParams.get("activation_id")) return route.fallback();
    return route.fulfill({
      json: {
        items: received ? [{ id: RUN_IDS.test, status: "running" }] : [],
        next_cursor: null,
      },
    });
  });
  await page.route("**/projects/*/drafts/*", (route) => {
    const request = route.request();
    if (request.method() !== "PUT") return route.fallback();
    const id = request.url().split("/drafts/")[1].split(/[?/]/)[0];
    return route.fulfill({
      json: { id, revision: 1, document: request.postDataJSON()["document"] },
    });
  });
  await page.route("**/environments/*/test-activations", (route) =>
    route.fulfill({
      status: 201,
      json: {
        id: "ta-1",
        activation_id: "a7a7a7a7-1111-4222-8333-444455556666",
        artifact_digest: "a".repeat(64),
        side_effect: "read_only",
        expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
      },
    }),
  );
  await page.route("**/environments/*/test-activations/*/listen", (route) =>
    route.fulfill({
      json: {
        test_url: TEST_URL,
        expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
      },
    }),
  );
  return { receive: () => (received = true) };
}

const field = (details: Locator, id: string) =>
  details.locator(`.sd-tabpanel:not([hidden]) [data-param="${id}"]`).first();
const webhookDetails = async (page: Page) => {
  await page
    .locator(`.tile-node[data-tile="$trigger:${WEBHOOK}"] .tile-body`)
    .dblclick();
  const details = page.getByRole("dialog", {
    name: "Step details: Webhook trigger",
    exact: true,
  });
  await expect(details).toBeVisible();
  return details;
};
async function activate(page: Page, rows: (string | RegExp)[]) {
  await command(page, "Publish…");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Publish version" })
    .click();
  await command(page, "Activate…");
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("status")).toHaveCount(0);
  // Everything is chosen for the person: one click on Activate.
  await expect(dialog.locator(".automatic-choices")).toContainText(
    /\d+ chosen for you ·/,
  );
  await expect(
    dialog.getByRole("list", { name: "Triggers" }).getByRole("listitem"),
  ).toHaveText(rows);
  await dialog
    .getByRole("button", { name: "Activate version", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
}

test("a webhook set up in step details is created at activation, listens for its test event, and keeps its URL for the next version", async ({
  page,
}) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 1440, height: 900 });
  await platform(page, {
    capabilities: [...allCapabilities, "step.test", "trigger.manage"],
  });
  const webhooks = await routeWebhooks(page);
  const listening = await routeListening(page);
  await withNewEditor(page);
  await page.reload();
  await page.getByLabel("Choose a workflow file").setInputFiles([
    {
      name: "pets.yaml",
      mimeType: "application/yaml",
      buffer: Buffer.from(workflow),
    },
    {
      name: "pets.canvas.json",
      mimeType: "application/json",
      buffer: Buffer.from(JSON.stringify(canvas)),
    },
  ]);
  await expect(page.locator(".editor-bar")).toBeVisible();

  // Set up in step details.
  let details = await webhookDetails(page);
  const parameters = details.getByRole("tab", { name: "Parameters" });
  if (await parameters.isVisible()) await parameters.click();
  await field(details, "name").getByRole("textbox").fill("orders-in");
  await field(details, "secretRef").getByRole("textbox").fill("orders-signing");
  await field(details, "testSecretRef")
    .getByRole("textbox")
    .fill("orders-test");
  await field(details, "testSecretRef").getByRole("textbox").blur();
  await new StepDetailsPage(page).close();

  // Activated: the webhook is created, and its step details show its URL.
  await activate(page, ["Create webhook orders-in"]);
  expect(webhooks.created).toEqual([
    expect.objectContaining({
      name: "orders-in",
      secret_ref: "orders-signing",
      activation_id: "activation",
    }),
  ]);
  details = await webhookDetails(page);
  const production = details.getByRole("textbox", { name: "Production URL" });
  await expect(production).toHaveValue(/\/webhooks\/w-new$/);
  const url = await production.inputValue();

  // Listening: a test URL with a signed cURL; the event's run opens in Runs.
  await details
    .locator(".sd-actions")
    .getByRole("button", { name: "Listen for test event", exact: true })
    .click();
  await expect(
    details.getByRole("textbox", { name: "Make a POST request to:" }),
  ).toHaveValue(TEST_URL);
  await expect(details.getByLabel("Signed cURL")).toContainText(
    "X-Weave-Signature: $SIGNATURE",
  );
  listening.receive();
  await expect(page.locator("#editor-view")).toHaveAttribute(
    "aria-labelledby",
    "view-tab-runs",
    { timeout: 10_000 },
  );
  await expect(details).toContainText(
    "Received a test event. Its run is open in Runs.",
  );
  await new StepDetailsPage(page).close();
  await showView(page, "Designer");

  // A new version, activated, moves the same webhook.
  await showView(page, "Source");
  const source = page.getByRole("textbox", { name: "Workflow source" });
  await source.fill(
    (await source.inputValue()).replace("version: 1.0.0", "version: 1.1.0"),
  );
  await showView(page, "Designer");
  await page
    .getByRole("dialog", { name: "Apply the changes typed in Source?" })
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await activate(page, [
    "Move webhook orders-in from 1.0.0 to 1.1.0 (the URL stays the same)",
  ]);
  expect(webhooks.moved).toEqual([
    { body: { activation_id: "activation" }, ifMatch: '"1"' },
  ]);
  details = await webhookDetails(page);
  await expect(
    details.getByRole("textbox", { name: "Production URL" }),
  ).toHaveValue(url);
});
