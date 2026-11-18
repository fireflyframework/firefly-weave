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
// After an activation succeeds, its triggers are set up: a new webhook is
// created and kept in its trigger choice for the environment, so the next
// activation moves that webhook (its URL stays); a trigger that can't be set
// up says why with Retry, and the activation stays.
import { readFile } from "node:fs/promises";
import { expect, test, type Download, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { platform, versionId } from "./activation-po";
import { command } from "./support";
import { StepDetailsPage, withNewEditor } from "./step-details-po";

const WEBHOOK = "trigger-1b2c3d4e";
const workflow = stringify({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "pets", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [{ id: "check", kind: "transform", value: { ref: "/input" } }],
    output: { literal: {} },
  },
});
const canvas = {
  schemaVersion: 2,
  kind: "weave.studio/canvas",
  triggers: [
    {
      id: WEBHOOK,
      kind: "webhook",
      name: "orders-in",
      secretRef: "orders-signing",
    },
  ],
};
const view = (revision: number) => ({
  id: "w-new",
  name: "orders-in",
  kind: "run",
  activation_id: "activation",
  secret_ref: "orders-signing",
  payload_schema: { type: "object" },
  max_body_bytes: 1048576,
  tolerance_seconds: 300,
  principal_id: "p-1",
  disabled: false,
  revision,
});

interface Recorded {
  created: Record<string, unknown>[];
  moved: { body: unknown; ifMatch: string | null }[];
}
/** The webhooks of the environment: none until one is created, then that one. */
async function webhooks(page: Page, failFirst = false): Promise<Recorded> {
  const recorded: Recorded = { created: [], moved: [] };
  let attempts = 0;
  await page.route("**/environments/development/triggers?*", (r) =>
    r.fulfill({
      json: {
        items: recorded.created.length ? [view(1)] : [],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/environments/development/triggers", (r) => {
    attempts++;
    if (failFirst && attempts === 1)
      return r.fulfill({
        status: 500,
        json: { code: "WV-INTERNAL", message: "Broken" },
      });
    recorded.created.push(r.request().postDataJSON());
    return r.fulfill({ status: 201, json: view(1) });
  });
  await page.route(
    "**/environments/development/triggers/w-new/retarget",
    (r) => {
      recorded.moved.push({
        body: r.request().postDataJSON(),
        ifMatch: r.request().headers()["if-match"] ?? null,
      });
      return r.fulfill({ json: view(2) });
    },
  );
  await page.route("**/environments/development/activations?*", (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: "activation",
            name: "pets",
            revision: 1,
            retired: false,
            request: { version_id: versionId },
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/projects/project/workflows?*", (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: versionId,
            name: "pets",
            version: "1.0.0",
            digest: "e".repeat(64),
          },
        ],
        next_cursor: null,
      },
    }),
  );
  return recorded;
}

/** Opens the workflow with its trigger choice in the new editor, connected, and publishes it. */
async function open(page: Page, sidecar: object = canvas) {
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
      buffer: Buffer.from(JSON.stringify(sidecar)),
    },
  ]);
  await expect(page.locator(".editor-bar")).toBeVisible();
  await command(page, "Publish…");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Publish version" })
    .click();
}
async function activateDialog(page: Page) {
  await command(page, "Activate…");
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("status")).toHaveCount(0);
  return dialog;
}
/** The trigger choice as the workflow's canvas keeps it, read back through Save to file. */
async function storedChoice(
  page: Page,
): Promise<Record<string, unknown> | null> {
  const downloads: Download[] = [];
  const listener = (download: Download) => downloads.push(download);
  page.on("download", listener);
  await command(page, "Save to file");
  await expect.poll(() => downloads.length).toBe(2);
  page.off("download", listener);
  const file = downloads.find((download) =>
    download.suggestedFilename().endsWith(".canvas.json"),
  )!;
  const saved = JSON.parse(await readFile((await file.path())!, "utf8")) as {
    triggers?: Record<string, unknown>[];
  };
  return saved.triggers?.find((intent) => intent["id"] === WEBHOOK) ?? null;
}

test("a new webhook is created for the activation, kept for the environment, and moved by the next one", async ({
  page,
}) => {
  await platform(page);
  const recorded = await webhooks(page);
  await open(page);
  let dialog = await activateDialog(page);
  await expect(
    dialog.getByRole("list", { name: "Triggers" }).getByRole("listitem"),
  ).toHaveText(["Create webhook orders-in"]);
  await dialog
    .getByRole("button", { name: "Activate version", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.locator(".toast").filter({ hasText: "Activated pets 1.0.0" }),
  ).toBeVisible();
  expect(recorded.created).toEqual([
    expect.objectContaining({
      name: "orders-in",
      activation_id: "activation",
      secret_ref: "orders-signing",
      payload_schema: { type: "object" },
    }),
  ]);
  expect(await storedChoice(page)).toMatchObject({
    resources: { development: "w-new" },
  });

  // The webhook's step details show its production URL, with Copy.
  await page
    .locator(`.tile-node[data-tile="$trigger:${WEBHOOK}"] .tile-body`)
    .dblclick();
  const details = page.getByRole("dialog", {
    name: "Step details: Webhook trigger",
  });
  await expect(
    details.getByRole("textbox", { name: "Production URL" }),
  ).toHaveValue(/\/webhooks\/w-new$/);
  await new StepDetailsPage(page).close();
  await expect(details).toHaveCount(0);

  dialog = await activateDialog(page);
  await expect(
    dialog.getByRole("list", { name: "Triggers" }).getByRole("listitem"),
  ).toHaveText([
    "Move webhook orders-in from 1.0.0 to 1.0.0 (the URL stays the same)",
  ]);
  await dialog
    .getByRole("button", { name: "Activate version", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(recorded.moved).toEqual([
    { body: { activation_id: "activation" }, ifMatch: '"1"' },
  ]);
});

test("a trigger that can't be created says why with Retry, and the activation stays", async ({
  page,
}) => {
  await platform(page);
  const recorded = await webhooks(page, true);
  await open(page);
  const dialog = await activateDialog(page);
  await dialog
    .getByRole("button", { name: "Activate version", exact: true })
    .click();
  const rows = dialog.getByRole("list", { name: "Triggers" });
  await expect(rows.getByRole("listitem")).toContainText([
    /^Couldn't create orders-in: /,
  ]);
  await expect(
    page
      .locator(".toast")
      .filter({ hasText: "Activated pets 1.0.0. 1 trigger needs attention." }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Activate version" }),
  ).toHaveCount(0);
  await rows.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(rows.getByRole("listitem")).toContainText(["Created"]);
  expect(recorded.created).toHaveLength(1);
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(await storedChoice(page)).toMatchObject({
    resources: { development: "w-new" },
  });
});

test("a new Kafka trigger asks for its Kafka connection, of any version, and is created on it", async ({
  page,
}) => {
  await platform(page);
  await webhooks(page);
  const KAFKA = "55555555-5555-4555-8555-555555555555";
  const listed: string[] = [];
  await page.route(
    "**/environments/development/bindable-connections?*",
    (r) => {
      listed.push(new URL(r.request().url()).search);
      return r.fulfill({
        json: {
          items: [
            {
              connection_id: "66666666-6666-4666-8666-666666666666",
              revision_id: KAFKA,
              name: "orders-kafka",
              connector: "weave-kafka@1.0.0",
              enabled: true,
            },
            {
              connection_id: "77777777-7777-4777-8777-777777777777",
              revision_id: "88888888-8888-4888-8888-888888888888",
              name: "pets",
              connector: "weave-http@2.0.0",
              enabled: true,
            },
          ],
          next_cursor: null,
        },
      });
    },
  );
  await page.route("**/environments/development/broker-triggers?*", (r) =>
    r.fulfill({ json: { items: [], next_cursor: null } }),
  );
  const created: Record<string, unknown>[] = [];
  await page.route("**/environments/development/broker-triggers", (r) => {
    created.push(r.request().postDataJSON());
    return r.fulfill({
      status: 201,
      json: {
        id: "b-1",
        name: "orders",
        kind: "run",
        activation_id: "activation",
        connection_revision_id: KAFKA,
        cluster_id: "orders-cluster",
        topic: "orders",
        dead_letter_policy: "receipt",
        payload_schema: { type: "object" },
        revision: 1,
        disabled: false,
      },
    });
  });
  await open(page, {
    ...canvas,
    triggers: [
      {
        id: "trigger-3d4e5f6a",
        kind: "broker",
        name: "orders",
        clusterId: "orders-cluster",
        topic: "orders",
        deadLetterPolicy: "receipt",
      },
    ],
  });
  const dialog = await activateDialog(page);
  const connection = dialog.getByRole("combobox", {
    name: "Create Kafka trigger orders Connection",
  });
  await expect(connection).toHaveValue(KAFKA);
  await expect(connection.getByRole("option")).toHaveText([
    "Choose a weave-kafka connection",
    "orders-kafka",
  ]);
  expect(listed).toContain("?limit=100");
  await dialog
    .getByRole("button", { name: "Activate version", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(created).toEqual([
    expect.objectContaining({
      name: "orders",
      connection_revision_id: KAFKA,
      cluster_id: "orders-cluster",
      topic: "orders",
    }),
  ]);
});
