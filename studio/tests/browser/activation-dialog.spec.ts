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
// The activation dialog: it stops when the platform doesn't run a language
// feature the workflow uses; it fills each connection slot from the
// connections the person can bind; it pins the current activation of each
// called workflow; and Publish… in More is disabled with its reason when the
// account can't publish.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { DesignerPage } from "./designer-po";
import {
  openActivation,
  platform,
  revision,
  source,
  versionId,
} from "./activation-po";
import {
  allCapabilities,
  connected,
  newWorkflow,
  withClassicDesigner,
} from "./support";

const MANIFEST = JSON.parse(
  readFileSync(resolve("tests/fixtures/language/manifest.json"), "utf8"),
) as Record<string, unknown>;
const CALLEE_VERSION = "22222222-2222-4222-8222-222222222222";
const CONNECTION = "33333333-3333-4333-8333-333333333333";

async function bindable(page: Page, items: unknown[]) {
  await page.route("**/environments/development/bindable-connections?*", (r) =>
    r.fulfill({ json: { items, next_cursor: null } }),
  );
}
const PETS = {
  connection_id: CONNECTION,
  revision_id: revision,
  name: "pets",
  connector: "weave-http@2.0.0",
  label: "pets",
  enabled: true,
};
async function features(
  page: Page,
  platformFeatures: string[],
  used: string[] | null,
  calls: string[] = [],
) {
  // The editor reads its step kinds from the same manifest: only its features change.
  await page.route("**/projects/project/language", (r) =>
    r.fulfill({ json: { ...MANIFEST, features: platformFeatures } }),
  );
  if (used || calls.length)
    await page.route(`**/projects/project/workflows/${versionId}/export`, (r) =>
      r.fulfill({
        json: {
          id: versionId,
          document: {
            apiVersion: "weave/v1alpha1",
            kind: "Workflow",
            metadata: { name: "pets", version: "1.0.0" },
            spec: {
              connections: { pets: { connector: "weave-http@2.0.0" } },
              steps: calls.map((uses, index) => ({
                id: `call-${index}`,
                kind: "callWorkflow",
                uses,
                with: { literal: {} },
              })),
            },
          },
          artifact: {
            executable: {
              features: used ?? [],
              connections: {},
              dependencies: [],
            },
          },
        },
      }),
    );
}
async function callees(page: Page, activations: unknown[]) {
  await page.route("**/projects/project/workflows?*", (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: CALLEE_VERSION,
            name: "notify-customer",
            version: "1.0.0",
            digest: "c".repeat(64),
          },
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
  await page.route("**/environments/development/activations?*", (r) =>
    r.fulfill({ json: { items: activations, next_cursor: null } }),
  );
}
const activate = (page: Page) =>
  page
    .getByRole("dialog")
    .getByRole("button", { name: "Activate version", exact: true });

test("a workflow with a loop on a platform without loops stops the dialog, and Activate is off", async ({
  page,
}) => {
  await platform(page);
  await bindable(page, [PETS]);
  await features(page, ["ai.agent", "text.concat"], ["flow.forEach"]);
  const dialog = await openActivation(page, [], false);
  await expect(
    dialog.getByText(
      "This platform doesn't run loops yet (flow.forEach). Update the platform first.",
    ),
  ).toBeVisible();
  await expect(activate(page)).toBeDisabled();
  await expect(
    dialog.getByRole("heading", { name: "Connections" }),
  ).toHaveCount(0);
});

test("a slot gets the only connection the person can bind, and Activate sends it", async ({
  page,
}) => {
  const captured = await platform(page);
  await bindable(page, [
    PETS,
    {
      ...PETS,
      revision_id: "44444444-4444-4444-8444-444444444444",
      enabled: false,
    },
  ]);
  await features(page, ["flow.forEach"], null);
  const dialog = await openActivation(page, [], false);
  await expect(dialog.locator(".automatic-choices")).toContainText(
    /\d+ chosen for you ·/,
  );
  await dialog.getByRole("button", { name: "Review", exact: true }).click();
  await expect(dialog.getByRole("combobox", { name: /pets/ })).toHaveValue(
    revision,
  );
  await activate(page).click();
  await expect.poll(() => captured.body).not.toBeNull();
  expect(captured.body!["connection_revision_ids"]).toEqual({ pets: revision });
});

test("a slot without a connection to bind says where to create one", async ({
  page,
}) => {
  await platform(page);
  await bindable(page, []);
  await features(page, ["flow.forEach"], null);
  const dialog = await openActivation(page, [], false);
  // The hint's last words are a link, so read the hint as a whole.
  await expect(
    dialog.locator(".hint").filter({
      hasText:
        /^\s*No connection for weave-http 2\.0\.0 in .+\.\s*Create one in Connections\.\s*$/,
    }),
  ).toBeVisible();
  // A required slot without a connection keeps Activate off, and says why.
  await expect(activate(page)).toBeDisabled();
  await expect(activate(page)).toHaveAttribute(
    "aria-describedby",
    "activation-missing",
  );
  await expect(
    dialog.getByText("Choose a connection for pets first.", { exact: true }),
  ).toBeVisible();
  // The way out is a link: it closes the dialog and opens Connections.
  await dialog
    .getByRole("button", { name: "Create one in Connections.", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Connections", level: 1 }),
  ).toBeVisible();
});

test("a called workflow pins its current activation", async ({ page }) => {
  const captured = await platform(page);
  await bindable(page, [PETS]);
  await features(
    page,
    ["flow.forEach", "flow.callWorkflow"],
    ["flow.callWorkflow"],
    ["notify-customer@1.0.0"],
  );
  await callees(page, [
    {
      id: "act-2",
      name: "notify-customer",
      revision: 2,
      retired: false,
      request: { version_id: CALLEE_VERSION },
    },
    {
      id: "act-3",
      name: "notify-customer",
      revision: 3,
      retired: false,
      request: { version_id: CALLEE_VERSION },
    },
  ]);
  const dialog = await openActivation(page, [], false);
  const rows = dialog.getByRole("list", { name: "Called workflows" });
  await expect(rows.getByRole("listitem")).toHaveText([
    "notify-customer 1.0.0 · activation 3",
  ]);
  await activate(page).click();
  await expect.poll(() => captured.body).not.toBeNull();
  expect(captured.body!["workflow_activation_ids"]).toEqual({
    "notify-customer@1.0.0": "act-3",
  });
});

test("a called workflow without a current activation blocks Activate with what to do", async ({
  page,
}) => {
  await platform(page);
  await bindable(page, [PETS]);
  await features(
    page,
    ["flow.forEach", "flow.callWorkflow"],
    ["flow.callWorkflow"],
    ["notify-customer@1.0.0"],
  );
  await callees(page, [
    {
      id: "act-3",
      name: "notify-customer",
      revision: 3,
      retired: true,
      request: { version_id: CALLEE_VERSION },
    },
  ]);
  const dialog = await openActivation(page, [], false);
  await expect(
    dialog
      .getByRole("list", { name: "Called workflows" })
      .getByRole("listitem"),
  ).toHaveText(["Activate notify-customer first"]);
  await expect(activate(page)).toBeDisabled();
  await expect(
    dialog.getByText("Activate notify-customer first", { exact: true }),
  ).toHaveCount(1);
});

test("Publish… in More is disabled with its reason when the account can't publish", async ({
  page,
}) => {
  await withClassicDesigner(page);
  await connected(page, {
    capabilities: allCapabilities.filter(
      (capability) => capability !== "definition.publish",
    ),
  });
  await newWorkflow(page);
  await new DesignerPage(page).setSource(source);
  const toolbar = page.getByRole("toolbar", { name: "Workflow commands" });
  await toolbar.getByRole("button", { name: "More", exact: true }).click();
  const item = page.getByRole("menuitem", { name: /^Publish…/ });
  await expect(item).toHaveAttribute("aria-disabled", "true");
  await expect(item).toContainText(
    "Your account can't publish workflows in this workspace.",
  );
  await item.click({ force: true });
  await expect(page.locator(".command-note")).toHaveText(
    "Your account can't publish workflows in this workspace.",
  );
});
