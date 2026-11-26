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
// The activation dialog's trigger rows at a phone, a tablet and a desktop:
// nothing runs out of the dialog, each row takes the list's width with its
// controls stacked inside it, and on a phone every control is a 44 px target.
// The same holds after the activation, when a row says what went wrong with
// Retry.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { activationsOf, platform, revision, versionId } from "./activation-po";
import { command } from "./support";
import { withNewEditor } from "./step-details-po";

const OLD_VERSION = "22222222-2222-4222-8222-222222222222";
const KAFKA = "55555555-5555-4555-8555-555555555555";
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
      id: "trigger-1b2c3d4e",
      kind: "webhook",
      name: "orders-in-from-the-storefront-checkout",
      secretRef: "orders-signing",
    },
    {
      id: "trigger-3d4e5f6a",
      kind: "broker",
      name: "orders",
      clusterId: "orders-cluster",
      topic: "orders",
      deadLetterPolicy: "receipt",
    },
  ],
};

/** An earlier version's webhook left behind, a Kafka connection, and a create that fails once. */
async function routes(page: Page) {
  await page.route("**/environments/development/triggers?*", (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: "w-old",
            name: "orders-old-storefront-webhook",
            kind: "run",
            activation_id: "activation-old",
            secret_ref: "orders-signing",
            payload_schema: { type: "object" },
            max_body_bytes: 1048576,
            tolerance_seconds: 300,
            principal_id: "p-1",
            disabled: false,
            revision: 3,
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/environments/development/triggers", (r) =>
    r.fulfill({
      status: 500,
      json: { code: "WV-INTERNAL", message: "The platform couldn't store it." },
    }),
  );
  for (const list of ["broker-triggers", "schedules", "provider-sources"])
    await page.route(`**/environments/development/${list}?*`, (r) =>
      r.fulfill({ json: { items: [], next_cursor: null } }),
    );
  await page.route("**/environments/development/broker-triggers", (r) =>
    r.fulfill({
      status: 500,
      json: { code: "WV-INTERNAL", message: "The platform couldn't store it." },
    }),
  );
  await page.route("**/environments/development/bindable-connections?*", (r) =>
    r.fulfill({
      json: {
        items: [
          {
            connection_id: "33333333-3333-4333-8333-333333333333",
            revision_id: revision,
            name: "pets",
            connector: "weave-http@2.0.0",
            enabled: true,
          },
          {
            connection_id: "66666666-6666-4666-8666-666666666666",
            revision_id: KAFKA,
            name: "orders-kafka",
            connector: "weave-kafka@1.0.0",
            enabled: true,
          },
        ],
        next_cursor: null,
      },
    }),
  );
  activationsOf(page).items.push({
    id: "activation-old",
    name: "pets",
    revision: 1,
    retired: false,
    request: { version_id: OLD_VERSION },
  });
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
          {
            id: OLD_VERSION,
            name: "pets",
            version: "0.9.0",
            digest: "f".repeat(64),
          },
        ],
        next_cursor: null,
      },
    }),
  );
}

async function openDialog(page: Page) {
  await platform(page);
  await routes(page);
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
  await command(page, "Publish…");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Publish version" })
    .click();
  await command(page, "Activate…");
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("list", { name: "Triggers" })).toBeVisible();
  return dialog;
}

/** Nothing in the dialog runs past its right edge, and each row takes the list's width. */
async function expectContained(dialog: Locator) {
  const overflow = await dialog.evaluate(
    (node) => node.scrollWidth - node.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
  const list = dialog.getByRole("list", { name: "Triggers" });
  const width = (await list.boundingBox())!.width;
  for (const row of await list.getByRole("listitem").all()) {
    const box = (await row.boundingBox())!;
    expect(Math.abs(box.width - width)).toBeLessThanOrEqual(1);
    expect(
      await row.evaluate((node) => node.scrollWidth - node.clientWidth),
    ).toBeLessThanOrEqual(1);
  }
}
async function expectTargets(controls: Locator[], min: number) {
  for (const control of controls) {
    const box = (await control.boundingBox())!;
    expect(
      box.height,
      await control.evaluate((node) => node.outerHTML.slice(0, 80)),
    ).toBeGreaterThanOrEqual(min);
  }
}

for (const size of [
  { width: 360, height: 780 },
  { width: 768, height: 1024 },
  { width: 1440, height: 900 },
])
  test.describe(`activation trigger rows at ${size.width}x${size.height}`, () => {
    test.use({ viewport: size });
    const touch = size.width < 768 ? 44 : 24;

    test("before activating: rows stay inside the dialog, and their controls are full targets", async ({
      page,
    }) => {
      const dialog = await openDialog(page);
      const rows = dialog.getByRole("list", { name: "Triggers" });
      await expect(rows.getByRole("listitem")).toHaveText([
        /^Create webhook orders-in-from-the-storefront-checkout/,
        /^Create Kafka trigger orders/,
        /^Webhook orders-old-storefront-webhook keeps running on 0\.9\.0/,
      ]);
      await expectContained(dialog);
      await expectTargets(
        [
          rows.locator(".activation-stop-it"),
          rows.getByRole("combobox", { name: /Connection$/ }),
        ],
        touch,
      );
    });

    test("after activating: a row that failed keeps its reason and Retry inside the dialog", async ({
      page,
    }) => {
      const dialog = await openDialog(page);
      await dialog
        .getByRole("button", { name: "Activate version", exact: true })
        .click();
      const rows = dialog.getByRole("list", { name: "Triggers" });
      await expect(rows.getByRole("button", { name: "Retry" })).toHaveCount(2);
      await expectContained(dialog);
      await expectTargets(
        [
          ...(await rows.getByRole("button", { name: "Retry" }).all()),
          dialog.getByRole("button", { name: "Done", exact: true }),
        ],
        touch,
      );
    });
  });
