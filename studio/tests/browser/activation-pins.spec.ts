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
import { test, expect } from "@playwright/test";
import {
  withClassicDesigner,
  command,
  newWorkflow,
  selectChoice,
} from "./support";
import { DesignerPage } from "./designer-po";
import {
  binding,
  connectorVersion,
  httpRelease,
  openActivation,
  platform,
  revision,
  source,
  versionId,
  workerRelease,
} from "./activation-po";

test.beforeEach(async ({ page }) => {
  await withClassicDesigner(page);
});

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
