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
import { platform, routeWebhooks, versionId } from "./activation-po";
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

/** A token's color, as the browser computes it. */
const inkOf = (page: Page, token: string) =>
  page.evaluate((name) => {
    const probe = document.body.appendChild(document.createElement("span"));
    probe.style.color = `var(${name})`;
    const { color } = getComputedStyle(probe);
    probe.remove();
    return color;
  }, token);

test("a new webhook is created for the activation, kept for the environment, and moved by the next one", async ({
  page,
}) => {
  await platform(page);
  const recorded = await routeWebhooks(page);
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
  // The created IDs go into the trigger choices at once (kept on this
  // computer with the canvas), and the draft's autosave clears Unsaved on its
  // own: at most ten seconds after the last save, the publish's.
  const chip = page.locator(".status-chip");
  await expect(chip).toHaveCount(1);
  await expect(chip).not.toHaveText(/Unsaved/, { timeout: 15_000 });
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
  const recorded = await routeWebhooks(page, true);
  await open(page);
  const dialog = await activateDialog(page);
  await dialog
    .getByRole("button", { name: "Activate version", exact: true })
    .click();
  const rows = dialog.getByRole("list", { name: "Triggers" });
  await expect(rows.getByRole("listitem")).toContainText([
    /^Couldn't create webhook orders-in: /,
  ]);
  await expect(
    page
      .locator(".toast")
      .filter({ hasText: "Activated pets 1.0.0. 1 trigger needs attention." }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Activate version" }),
  ).toHaveCount(0);
  // The dialog reads as done: the activation succeeded, one trigger didn't.
  const done = page.getByRole("dialog", { name: "Activated pets 1.0.0" });
  await expect(done).toBeVisible();
  await expect(done.locator("#activation-summary")).toHaveText(
    /^\s*Activated in .+\. 1 trigger needs attention\.\s*$/,
  );
  await expect(done.getByRole("button", { name: "Review" })).toHaveCount(0);
  await rows.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(rows.getByRole("listitem")).toContainText([
    "Created webhook orders-in",
  ]);
  expect(recorded.created).toHaveLength(1);
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(await storedChoice(page)).toMatchObject({
    resources: { development: "w-new" },
  });
});

test("Retry keeps focus on its row while a webhook can't be created, the first of two rows to fix", async ({
  page,
}) => {
  await platform(page);
  await routeWebhooks(page);
  const held = { asked: 0, release: () => undefined as void };
  await page.route("**/environments/development/triggers", async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    if (++held.asked > 2)
      await new Promise<void>((resolve) => (held.release = resolve));
    return r.fulfill({
      status: 500,
      json: { code: "WV-INTERNAL", message: "Broken" },
    });
  });
  await open(page, {
    ...canvas,
    triggers: [
      canvas.triggers[0]!,
      {
        id: "trigger-2c3d4e5f",
        kind: "webhook",
        name: "orders-out",
        secretRef: "orders-signing",
      },
    ],
  });
  const dialog = await activateDialog(page);
  await dialog
    .getByRole("button", { name: "Activate version", exact: true })
    .click();
  const retry = dialog.getByRole("button", { name: "Retry", exact: true });
  await expect(retry).toHaveCount(2);
  await retry.nth(1).focus();
  await page.keyboard.press("Enter");
  await expect.poll(() => held.asked).toBe(3);
  await expect(retry.nth(1)).toBeDisabled();
  held.release();
  await expect(retry.nth(1)).toBeEnabled();
  await expect(retry.nth(1)).toBeFocused();
});

test("what happened to each trigger has an icon beside its words, and what wasn't set up yet has none", async ({
  page,
}) => {
  await platform(page);
  await routeWebhooks(page);
  const webhook = (id: string, name: string, activation: string) => ({
    id,
    name,
    kind: "run",
    activation_id: activation,
    secret_ref: "orders-signing",
    payload_schema: { type: "object" },
    max_body_bytes: 1048576,
    tolerance_seconds: 300,
    principal_id: "p-1",
    disabled: false,
    revision: 3,
  });
  const moving = webhook("w-1", "orders-in", "activation");
  const left = webhook("w-2", "orders-old", "activation");
  const idle = webhook("w-3", "orders-older", "activation");
  // The list, a move, a stop, and a create: one name is refused once.
  await page.route(/\/environments\/development\/triggers(?:[/?].*)?$/, (r) => {
    const { pathname } = new URL(r.request().url());
    if (r.request().method() === "GET")
      return r.fulfill({
        json: { items: [moving, left, idle], next_cursor: null },
      });
    if (pathname.endsWith("/w-1/retarget"))
      return r.fulfill({ json: { ...moving, revision: 4 } });
    if (pathname.endsWith("/w-2/disable"))
      return r.fulfill({ json: { ...left, disabled: true, revision: 4 } });
    const { name } = r.request().postDataJSON() as { name: string };
    return name === "orders-out"
      ? r.fulfill({ status: 201, json: webhook("w-4", name, "activation") })
      : r.fulfill({
          status: 500,
          json: { code: "WV-INTERNAL", message: "Broken" },
        });
  });
  await open(page, {
    ...canvas,
    triggers: [
      {
        ...canvas.triggers[0],
        resources: { development: "w-1" },
      },
      {
        id: "trigger-2c3d4e5f",
        kind: "webhook",
        name: "orders-out",
        secretRef: "orders-signing",
      },
      {
        id: "trigger-3d4e5f6a",
        kind: "webhook",
        name: "orders-back",
        secretRef: "orders-signing",
      },
    ],
  });
  const dialog = await activateDialog(page);
  const rows = dialog
    .getByRole("list", { name: "Triggers" })
    .getByRole("listitem");
  await expect(rows).toHaveCount(5);
  await expect(rows.locator("weave-icon")).toHaveCount(0);
  await rows
    .filter({ hasText: "orders-old keeps running" })
    .getByRole("checkbox", { name: "Stop it" })
    .check();
  await dialog
    .getByRole("button", { name: "Activate version", exact: true })
    .click();
  const row = (text: RegExp) => rows.filter({ hasText: text });
  for (const [text, icon, ink] of [
    [/^\s*Moved webhook orders-in/, "ok", "--success-ink"],
    [/^\s*Created webhook orders-out/, "ok", "--success-ink"],
    [/^\s*Couldn't create webhook orders-back/, "failCircle", "--danger-ink"],
    [/^\s*Stopped webhook orders-old/, "ok", "--success-ink"],
  ] as const) {
    const drawn = row(text).locator("weave-icon");
    await expect(drawn).toHaveAttribute("data-icon", icon);
    // The icon is in the row's own ink, left of its words.
    await expect(drawn).toHaveCSS("color", await inkOf(page, ink));
    const [at, words] = [
      (await drawn.boundingBox())!,
      (await row(text).locator("[id^='trigger-row-']").boundingBox())!,
    ];
    expect([at.width, at.height]).toEqual([16, 16]);
    expect(words.x - (at.x + at.width)).toBe(12);
  }
  await expect(
    row(/orders-older keeps running/).locator("weave-icon"),
  ).toHaveCount(0);
});

test("a new Kafka trigger asks for its Kafka connection, of any version, and is created on it", async ({
  page,
}) => {
  await platform(page);
  await routeWebhooks(page);
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

test("a new webhook takes the activated version's input, not the draft's edited since", async ({
  page,
}) => {
  await platform(page);
  const recorded = await routeWebhooks(page);
  const published = {
    type: "object",
    properties: { orderId: { type: "string" } },
    required: ["orderId"],
  };
  // The version being activated, as published: its own input.
  await page.route(`**/projects/project/workflows/${versionId}/export`, (r) =>
    r.fulfill({
      json: {
        id: versionId,
        document: {
          apiVersion: "weave/v1alpha1",
          kind: "Workflow",
          metadata: { name: "pets", version: "1.0.0" },
          spec: {
            inputSchema: published,
            outputSchema: { type: "object" },
            steps: [
              { id: "check", kind: "transform", value: { ref: "/input" } },
            ],
            output: { literal: {} },
          },
        },
        artifact: {
          executable: { features: [], connections: {}, dependencies: [] },
        },
      },
    }),
  );
  await open(page);
  const dialog = await activateDialog(page);
  await dialog
    .getByRole("button", { name: "Activate version", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(recorded.created).toEqual([
    expect.objectContaining({ name: "orders-in", payload_schema: published }),
  ]);
});

test("an activation answer without an ID sets up no triggers, and says so", async ({
  page,
}) => {
  await platform(page);
  const recorded = await routeWebhooks(page);
  await page.route("**/environments/development/activations", (r) =>
    r.request().method() === "POST"
      ? r.fulfill({ status: 201, json: { name: "pets", revision: 1 } })
      : r.fallback(),
  );
  await open(page);
  const dialog = await activateDialog(page);
  await dialog
    .getByRole("button", { name: "Activate version", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.locator(".toast").filter({
      hasText:
        "Activated pets 1.0.0. Studio didn't set up its triggers: the platform's answer didn't name the activation.",
    }),
  ).toBeVisible();
  expect(recorded.created).toEqual([]);
});

test("a schedule whose run input no longer fits offers Open schedule, which opens its step details", async ({
  page,
}) => {
  await platform(page);
  await routeWebhooks(page);
  await page.route(`**/projects/project/workflows/${versionId}/export`, (r) =>
    r.fulfill({
      json: {
        id: versionId,
        document: {
          apiVersion: "weave/v1alpha1",
          kind: "Workflow",
          metadata: { name: "pets", version: "1.0.0" },
          spec: {
            inputSchema: {
              type: "object",
              properties: { orderId: { type: "string" } },
              required: ["orderId"],
            },
          },
        },
        artifact: {
          executable: { features: [], connections: {}, dependencies: [] },
        },
      },
    }),
  );
  await open(page, {
    ...canvas,
    triggers: [
      {
        id: "trigger-4e5f6a7b",
        kind: "schedule",
        cron: "0 9 * * 1",
        input: {},
      },
    ],
  });
  const dialog = await activateDialog(page);
  const row = dialog
    .getByRole("list", { name: "Triggers" })
    .getByRole("listitem")
    .filter({ hasText: "no longer fits" });
  await row.getByRole("button", { name: "Open schedule", exact: true }).click();
  await expect(dialog.filter({ hasText: "Activate pets" })).toHaveCount(0);
  await expect(
    page.getByRole("dialog", {
      name: "Step details: Schedule trigger",
      exact: true,
    }),
  ).toBeVisible();
});

test("while the activation runs, Open schedule and the Connections link are off", async ({
  page,
}) => {
  await platform(page);
  await routeWebhooks(page);
  // The published version: an optional slot with no connection to bind, and an input the schedule's doesn't fit.
  await page.route(`**/projects/project/workflows/${versionId}/export`, (r) =>
    r.fulfill({
      json: {
        id: versionId,
        document: {
          apiVersion: "weave/v1alpha1",
          kind: "Workflow",
          metadata: { name: "pets", version: "1.0.0" },
          spec: {
            inputSchema: {
              type: "object",
              properties: { orderId: { type: "string" } },
              required: ["orderId"],
            },
          },
        },
        artifact: {
          executable: {
            features: [],
            connections: {
              audit: { connector: "weave-http@2.0.0", required: false },
            },
            dependencies: [],
          },
        },
      },
    }),
  );
  await page.route("**/environments/development/bindable-connections?*", (r) =>
    r.fulfill({ json: { items: [], next_cursor: null } }),
  );
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/environments/development/activations", async (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    await held;
    await r.fulfill({
      status: 201,
      json: { id: "activation", name: "pets", revision: 1 },
    });
  });
  await open(page, {
    ...canvas,
    triggers: [
      {
        id: "trigger-4e5f6a7b",
        kind: "schedule",
        cron: "0 9 * * 1",
        input: {},
      },
    ],
  });
  const dialog = await activateDialog(page);
  const openSchedule = dialog.getByRole("button", { name: "Open schedule" });
  const connections = dialog.getByRole("button", {
    name: "Create one in Connections.",
  });
  await expect(openSchedule).toBeEnabled();
  await expect(connections).toBeEnabled();
  await dialog
    .getByRole("button", { name: "Activate version", exact: true })
    .click();
  // The dialog stays until the activation answers: nothing may open behind it.
  await expect(openSchedule).toBeDisabled();
  await expect(connections).toBeDisabled();
  release();
});
