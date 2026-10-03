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
// Activation pins (WP-09): connector releases, worker releases and human task
// assignments are chosen from the environment instead of typed, and the
// request carries connector_release_ids.
import { selectChoice } from "./support";
import { test, expect, Page } from "@playwright/test";
import {
  command,
  connected,
  newWorkflow,
  type CatalogEntry,
  workerAction,
} from "./support";
import { DesignerPage } from "./designer-po";

const digest =
  "eddfa829184f8505fd0e1bc7a84b490fc57b555a39495b9724b2728b277133d8";
const connectorVersion = "7a8cbeef-7d5c-483a-b926-4157ad4286d0";
const httpRelease = "3f2a9b1c-0000-4000-8000-000000000001";
const otherRelease = "3f2a9b1c-0000-4000-8000-000000000009";
const workerRelease = "3f2a9b1c-0000-4000-8000-000000000002";
const binding = "3f2a9b1c-0000-4000-8000-000000000003";
const revision = "0f8fad5b-d9cb-469f-a165-70867728950e";
const versionId = "11111111-1111-4111-8111-111111111111";

const getPet = {
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: "get-pet", version: "1.0.0" },
  spec: {
    implementation: {
      kind: "connector",
      uses: "weave-http@2.0.0",
      action: "read",
    },
    connection: { connector: "weave-http@2.0.0" },
    sideEffect: "read_only",
    timeoutSeconds: 30,
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
  },
};
const catalog = [
  { id: "h1", document: getPet },
  { id: "a2", document: workerAction },
] as unknown as CatalogEntry[];
const source = `apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: pets
  version: 1.0.0
spec:
  inputSchema:
    type: object
  outputSchema:
    type: object
  connections:
    pets:
      connector: weave-http@2.0.0
  steps:
    - id: fetch
      kind: action
      uses: get-pet@1.0.0
      connection: pets
      with:
        literal: {}
    - id: crm
      kind: action
      uses: crm.lookup@2.0.0
      with:
        literal:
          customer: c-1
    - id: review
      kind: humanTask
      assignment: reviewers
      title:
        literal: Review
      context:
        literal: {}
      formSchema:
        type: object
      decisions: [approve, reject]
  output:
    literal: {}
`;
const binding404 = { code: "WV-NOT-FOUND", message: "Not found" };

interface Platform {
  releases?: number;
  assignments?: number;
  /** Fail the version export, so the pins come from the editor. */
  exportStatus?: number;
}
/** The environment, project and publish mocks; returns the captured activation body. */
async function platform(page: Page, options: Platform = {}) {
  const captured: {
    body: Record<string, unknown> | null;
    exports: number;
    contracts: string[];
  } = { body: null, exports: 0, contracts: [] };
  captured.contracts = (await connected(page, { catalog })).exports;
  await page.route("**/environments/development/connections?*", (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: revision,
            name: "pets",
            revision: 1,
            connector: "weave-http@2.0.0",
            adapter: "weave-http-v2",
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/projects/project/connectors?*", (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: connectorVersion,
            kind: "Connector",
            name: "weave-http",
            version: "2.0.0",
            digest: "a".repeat(64),
            definition_digest: digest,
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/projects/project/connector-descriptors/*", (r) =>
    r.fulfill({ status: 404, json: binding404 }),
  );
  await page.route("**/environments/development/worker-releases?*", (r) =>
    options.releases
      ? r.fulfill({ status: options.releases, json: { code: "WV-DENIED" } })
      : r.fulfill({
          json: {
            items: [
              {
                id: httpRelease,
                image_digest: "sha256:" + "b".repeat(64),
                capabilities: [
                  {
                    taskType: "weave-connector-http-read",
                    taskVersion: "2.0.0",
                  },
                  {
                    taskType: "weave-connector-http-write",
                    taskVersion: "2.0.0",
                  },
                ],
                connector_bindings: [
                  {
                    connector_digest: digest,
                    action: "read",
                    adapter: "weave-http-v2",
                    implementation_version: "2.0.0",
                    task_reference: "weave-connector-http-read@2.0.0",
                  },
                  {
                    connector_digest: digest,
                    action: "write",
                    adapter: "weave-http-v2",
                    implementation_version: "2.0.0",
                    task_reference: "weave-connector-http-write@2.0.0",
                  },
                ],
              },
              {
                // Bound to another connector digest: never offered.
                id: otherRelease,
                image_digest: "sha256:" + "c".repeat(64),
                capabilities: [
                  {
                    taskType: "weave-connector-http-read",
                    taskVersion: "2.0.0",
                  },
                ],
                connector_bindings: [
                  {
                    connector_digest: "1".repeat(64),
                    action: "read",
                    adapter: "weave-http-v2",
                    task_reference: "weave-connector-http-read@2.0.0",
                  },
                ],
              },
              {
                id: workerRelease,
                image_digest: "sha256:" + "d".repeat(64),
                capabilities: [
                  { taskType: "crm-lookup", taskVersion: "1.0.0" },
                ],
              },
            ],
            next_cursor: null,
          },
        }),
  );
  await page.route("**/environments/development/human-assignments", (r) =>
    options.assignments
      ? r.fulfill({ status: options.assignments, json: { code: "WV-DENIED" } })
      : r.fulfill({
          json: {
            items: [
              {
                binding_id: binding,
                name: "reviewers",
                enabled: true,
                revision: 2,
                principal_ids: ["8c1f1c55-0000-4000-8000-000000000001"],
                group_ids: [],
              },
            ],
          },
        }),
  );
  await page.route("**/projects/project/workflows", (r) =>
    r.fulfill({
      status: 201,
      json: {
        id: versionId,
        name: "pets",
        version: "1.0.0",
        digest: "e".repeat(64),
      },
    }),
  );
  await page.route(`**/projects/project/workflows/${versionId}/export`, (r) => {
    captured.exports++;
    if (options.exportStatus)
      return r.fulfill({ status: options.exportStatus, json: binding404 });
    return r.fulfill({
      json: {
        id: versionId,
        document: {
          spec: { connections: { pets: { connector: "weave-http@2.0.0" } } },
        },
        artifact: {
          executable: {
            connections: {
              pets: { connector: "weave-http@2.0.0", required: true },
            },
            dependencies: [
              {
                kind: "Action",
                reference: "get-pet@1.0.0",
                digest: "2".repeat(64),
                document: getPet,
              },
              {
                kind: "Action",
                reference: "crm.lookup@2.0.0",
                digest: "3".repeat(64),
                document: workerAction,
              },
              {
                kind: "Connector",
                reference: "weave-http@2.0.0",
                digest,
                document: { spec: { adapter: "weave-http-v2" } },
              },
            ],
            graph: {
              nodes: [
                { kind: "humanTask", id: "review", assignment: "reviewers" },
              ],
            },
          },
        },
      },
    });
  });
  await page.route("**/environments/development/activations", (r) => {
    captured.body = r.request().postDataJSON();
    return r.fulfill({
      status: 201,
      json: { id: "activation", name: "pets", revision: 1 },
    });
  });
  return captured;
}

/** Opens the published workflow's activation dialog with the crm contract loaded. */
async function openActivation(page: Page, contracts: string[], review = true) {
  const designer = new DesignerPage(page);
  await newWorkflow(page);
  await designer.setSource(source);
  // Selecting the worker action loads its contract, so its task type is known.
  await designer.selectStep("crm");
  await expect.poll(() => contracts).toContain("a2");
  // On narrow layouts the inspector covers the toolbar; close it first.
  const close = page.getByRole("button", { name: "Close inspector" });
  if (await close.isVisible()) await close.click();
  await command(page, "Publish…");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Publish version" })
    .click();
  await command(page, "Activate…");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(/Activate \S+ \d+\.\d+\.\d+/);
  await expect(dialog.getByRole("status")).toHaveCount(0);
  const reviewButton = dialog.getByRole("button", {
    name: "Review",
    exact: true,
  });
  if (review && (await reviewButton.isVisible())) await reviewButton.click();
  return dialog;
}

test("one compatible choice is collapsed and activation fits at 1280x720", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  const captured = await platform(page);
  const dialog = await openActivation(page, captured.contracts, false);
  await expect(dialog).toContainText("4 items picked automatically");
  await expect(dialog.getByRole("combobox")).toHaveCount(0);
  expect(
    await dialog.evaluate(
      (element) => element.scrollHeight <= element.clientHeight,
    ),
  ).toBe(true);
  await dialog.getByRole("button", { name: "Review", exact: true }).click();
  await expect(dialog.getByLabel(/^pets/)).toHaveValue(revision);
  await expect(dialog.getByLabel(/^crm-lookup/)).toHaveValue(workerRelease);
});

for (const [width, height] of [
  [1440, 900],
  [600, 500],
] as const)
  test(`pins come from the environment, not typed IDs (${width}x${height})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height });
    const captured = await platform(page);
    const dialog = await openActivation(page, captured.contracts);
    // The only compatible connection, worker release and binding are preselected.
    await expect(dialog.getByLabel(/^pets/)).toHaveValue(revision);
    const worker = dialog.getByLabel(/^crm-lookup/);
    await expect(worker).toHaveValue(workerRelease);
    await expect(dialog).toContainText(
      "Selected the only release that offers crm-lookup.",
    );
    await expect(dialog.getByLabel(/^reviewers/)).toHaveValue(binding);
    // Only the release bound to this connector digest is offered.
    const connector = dialog.getByLabel(/^weave-http@2\.0\.0/);
    const labels = await connector
      .locator("option")
      .evaluateAll((items) => items.map((o) => o.textContent?.trim()));
    expect(labels).toEqual(["Choose a release", "Version 2.0.0"]);
    await connector.scrollIntoViewIfNeeded();
    await selectChoice(connector, httpRelease);
    const submit = dialog.getByRole("button", { name: "Activate version" });
    await submit.scrollIntoViewIfNeeded();
    await submit.click();
    await expect(dialog).toHaveCount(0);
    expect(captured.body).toMatchObject({
      connection_revision_ids: { pets: revision },
      connector_release_ids: { [connectorVersion]: httpRelease },
      worker_release_ids: { "crm-lookup": workerRelease },
      assignment_binding_ids: { reviewers: binding },
      version_id: versionId,
    });
  });

test("without release access, IDs can still be entered and are checked", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const captured = await platform(page, { releases: 403, assignments: 403 });
  const dialog = await openActivation(page, captured.contracts);
  await expect(dialog).toContainText("Your account can't list releases here.");
  await expect(dialog).toContainText(
    "Your account can't list assignment bindings.",
  );
  // Lists are refused, so each pin is a text field.
  await dialog.getByLabel(/^weave-http@2\.0\.0/).fill(httpRelease);
  const worker = dialog.getByLabel(/^crm-lookup/);
  await worker.fill("not-an-id");
  await dialog.getByLabel(/^reviewers/).fill(binding);
  await dialog.getByRole("button", { name: "Activate version" }).click();
  await expect(dialog.getByRole("alert")).toContainText(
    "Choose a worker release for crm-lookup",
  );
  // Fixing the value clears the problem and the request carries the typed IDs.
  await worker.fill(workerRelease);
  await expect(dialog.getByRole("alert")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Activate version" }).click();
  await expect(dialog).toHaveCount(0);
  expect(captured.body).toMatchObject({
    connector_release_ids: { [connectorVersion]: httpRelease },
    worker_release_ids: { "crm-lookup": workerRelease },
    assignment_binding_ids: { reviewers: binding },
  });
});

test("an added worker row needs a task type and an ID; keyboard works", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  // Without a readable export the pins come from the editor, which may not
  // know every worker, so rows can be added by hand.
  const captured = await platform(page, { exportStatus: 404 });
  const dialog = await openActivation(page, captured.contracts);
  if (captured.exports)
    await expect(dialog).toContainText(
      "Studio couldn't read this version's requirements",
    );
  const add = dialog.getByRole("button", { name: "Add worker release" });
  await add.click();
  // The new row takes focus, so typing goes straight into it.
  const taskType = dialog.getByLabel("Worker task type", { exact: true });
  await expect(taskType).toBeFocused();
  await page.keyboard.type("billing");
  await page.keyboard.press("Tab");
  await page.keyboard.type("x");
  await page.keyboard.press("Enter");
  await expect(dialog.getByRole("alert")).toContainText(
    "Each worker release needs a name and an ID",
  );
  await dialog.getByRole("button", { name: "Remove", exact: true }).click();
  await expect(dialog.getByRole("alert")).toHaveCount(0);
  // Removing the focused row keeps focus in the dialog, on the Add button.
  await expect(add).toBeFocused();
  await selectChoice(dialog.getByLabel(/^weave-http@2\.0\.0/), httpRelease);
  await dialog.getByRole("button", { name: "Activate version" }).click();
  await expect(dialog).toHaveCount(0);
  expect(Object.keys(captured.body!["worker_release_ids"] as object)).toEqual([
    "crm-lookup",
  ]);
});

// The dialog reads the version's export: the shell passes the published
// version ID (`[versionId]` on <weave-activation-dialog>).
test("with the published version, the connector release is preselected", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const captured = await platform(page);
  const dialog = await openActivation(page, captured.contracts);
  expect(captured.exports).toBe(1);
  await expect(dialog.getByLabel(/^weave-http@2\.0\.0/)).toHaveValue(
    httpRelease,
  );
  await expect(dialog).toContainText("Used by get-pet@1.0.0");
  await expect(dialog).toContainText(
    "Selected the only release that runs weave-http@2.0.0.",
  );
  // Exact requirements: no hand-added rows.
  await expect(
    dialog.getByRole("button", { name: "Add worker release" }),
  ).toHaveCount(0);
  await dialog.getByRole("button", { name: "Activate version" }).click();
  await expect(dialog).toHaveCount(0);
  expect(captured.body).toMatchObject({
    connection_revision_ids: { pets: revision },
    connector_release_ids: { [connectorVersion]: httpRelease },
    worker_release_ids: { "crm-lookup": workerRelease },
    assignment_binding_ids: { reviewers: binding },
  });
});

test("after editing a published workflow, the published version's slots are bound", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const captured = await platform(page);
  const designer = new DesignerPage(page);
  await newWorkflow(page);
  await designer.setSource(source);
  await designer.selectStep("crm");
  await expect.poll(() => captured.contracts).toContain("a2");
  await command(page, "Publish…");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Publish version" })
    .click();
  // The editor renames the slot after publishing: "audit" exists only in the
  // editor, "pets" only in the published version.
  await designer.setSource(
    source
      .replace("    pets:\n", "    audit:\n")
      .replace("connection: pets", "connection: audit"),
  );
  await command(page, "Activate…");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(/Activate \S+ \d+\.\d+\.\d+/);
  await expect(dialog.getByRole("status")).toHaveCount(0);
  // Only the published version's slot is offered and bound.
  await dialog.getByRole("button", { name: "Review", exact: true }).click();
  await expect(dialog.getByLabel(/^audit/)).toHaveCount(0);
  await expect(dialog.getByLabel(/^pets/)).toHaveValue(revision);
  await dialog.getByRole("button", { name: "Activate version" }).click();
  await expect(dialog).toHaveCount(0);
  expect(captured.body).toMatchObject({
    connection_revision_ids: { pets: revision },
    connector_release_ids: { [connectorVersion]: httpRelease },
  });
  expect(
    Object.keys(captured.body!["connection_revision_ids"] as object),
  ).toEqual(["pets"]);
});
