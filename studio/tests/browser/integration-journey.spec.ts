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
// The "Call an action" journey: explicit catalog states, the action's
// requirements, connection slots, schema-driven input and activation bindings.
import { selectChoice } from "./support";
import { test, expect, Page } from "@playwright/test";
import {
  allCapabilities,
  command,
  connected,
  insertStep,
  newWorkflow,
  offline,
  sourceText,
} from "./support";
import {
  actionPicker,
  chooseAction,
  paletteIntegrations,
} from "./integrations-po";

const contract = (page: Page) => page.locator(".integration-contract");
const inspectorHeading = (page: Page) => page.locator(".inspector header h2");

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
});

test("offline: explains how to connect and declares a connection slot", async ({
  page,
}) => {
  await offline(page);
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await expect(contract(page)).toContainText(
    "Connect to a platform in Settings",
  );
  await expect(contract(page)).not.toContainText("profile");
  await expect(contract(page)).toContainText(
    "Enter the action version to call.",
  );
  await page
    .getByLabel("Action version", { exact: true })
    .fill("crm.lookup@2.0.0");
  await contract(page).getByText("Add connection slot").click();
  await page.getByLabel("New slot name", { exact: true }).fill("crm");
  await page
    .getByLabel("New slot connector", { exact: true })
    .fill("crm@1.0.0");
  await page.getByRole("button", { name: "Add slot", exact: true }).click();
  await expect(
    page.getByLabel("Connection slot", { exact: true }),
  ).toHaveAttribute("data-value", "crm");
  await inspectorHeading(page).click();
  const source = await sourceText(page);
  expect(source).toContain("uses: crm.lookup@2.0.0");
  expect(source).toContain("connection: crm");
  expect(source).toMatch(
    /connections:\s+crm:\s+connector: crm@1\.0\.0\s+required: true/,
  );
});

test("catalog states: loading, then the published actions", async ({
  page,
}) => {
  await connected(page, { catalogDelays: [800] });
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await expect(contract(page)).toContainText("Loading published actions");
  await expect(actionPicker(page)).toBeVisible();
  await expect(contract(page)).toContainText("Choose a published action.");
});

test("catalog states: empty catalog", async ({ page }) => {
  await connected(page, { catalog: [] });
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await expect(contract(page)).toContainText(
    "This project has no published actions yet.",
  );
  await expect(
    page.getByRole("button", { name: "Refresh actions" }),
  ).toBeVisible();
});

test("catalog states: missing catalog permission", async ({ page }) => {
  const recorder = await connected(page, {
    capabilities: allCapabilities.filter((c) => c !== "catalog.read"),
  });
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await expect(contract(page)).toContainText(
    "Your account cannot read the action catalog in this workspace.",
  );
  await expect(contract(page)).toContainText("catalog.read");
  expect(recorder.catalogPages).toBe(0);
});

test("catalog states: a failed query explains and retries", async ({
  page,
}) => {
  const recorder = await connected(page, {
    catalogStatus: 503,
    catalogFailures: 2,
  });
  await newWorkflow(page);
  // The palette's Integrations section asks first and explains the failure.
  await expect(paletteIntegrations(page)).toContainText(
    "Published actions couldn't be loaded.",
  );
  expect(recorder.catalogPages).toBe(1);
  // A new action step asks again; that answer fails too.
  await insertStep(page, "Call an action");
  await expect(contract(page)).toContainText(
    "Published actions could not be loaded.",
  );
  await expect(contract(page)).toContainText("Support code: WV-UNAVAILABLE");
  await expect(contract(page)).not.toContainText("{");
  await contract(page).getByRole("button", { name: "Try again" }).click();
  await expect(actionPicker(page)).toBeVisible();
  expect(recorder.catalogPages).toBe(3);
});

test("requirements, required inputs and a compatible slot complete the step", async ({
  page,
}) => {
  await connected(page);
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await chooseAction(page, "sql.lookup@1.0.0");
  const requirements = page.locator(".integration-requirements");
  await expect(requirements).toContainText("Connector");
  await expect(requirements).toContainText("weave-postgresql@1.0.0");
  await expect(requirements).toContainText("read");
  await expect(requirements).toContainText("Read only");
  await expect(requirements).toContainText("30 seconds");
  await expect(requirements).toContainText("Up to 3 attempts");
  await expect(requirements).toContainText(
    "Requires a weave-postgresql@1.0.0 connection",
  );
  const issues = page.locator(".integration-issues");
  await expect(issues).toContainText("Fill in the required inputs: Parameters");
  await expect(issues).toContainText(
    "Choose a connection slot for weave-postgresql@1.0.0.",
  );
  await page.getByLabel(/^Customer ID/).fill("customer-104");
  await selectChoice(page.getByLabel(/^Region(\s*\(optional\))?$/), "us");
  await page.getByRole("button", { name: "Add Tags item" }).click();
  await page.getByLabel("Tags item 1", { exact: true }).fill("priority");
  await page.getByRole("button", { name: "Add Tags item" }).click();
  await page.getByLabel("Tags item 2", { exact: true }).fill("emea");
  // An optional yes/no input is "Not set", "Yes" or "No" (F3).
  await selectChoice(page.getByLabel(/^Dry run(\s*\(optional\))?$/), "true");
  await expect(issues).not.toContainText("required inputs");
  await page
    .getByRole("button", {
      name: "Add a PostgreSQL connection slot",
      exact: true,
    })
    .click();
  await expect(page.getByLabel("Connection slot", { exact: true })).toHaveValue(
    "weave-postgresql",
  );
  await expect(issues).toHaveCount(0);
  await inspectorHeading(page).click();
  await expect(page.locator(".apply-state")).toHaveCount(0);
  const source = await sourceText(page);
  expect(source).toContain("connection: weave-postgresql");
  expect(source).toMatch(/parameters:\s+customerId: customer-104\s+region: us/);
  expect(source).toMatch(/tags:\s+- priority\s+- emea/);
  expect(source).toContain("dryRun: true");
  expect(source).toMatch(
    /connections:\s+weave-postgresql:\s+connector: weave-postgresql@1\.0\.0\s+required: true/,
  );
});

test("later steps discover the action output reference", async ({ page }) => {
  await connected(page);
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await expect(contract(page)).toContainText("/steps/call-action-1/output");
  await expect(
    page.getByRole("button", { name: "Copy output reference" }),
  ).toBeVisible();
  await insertStep(page, "Transform");
  await page
    .getByRole("radiogroup", { name: "Value expression mode", exact: true })
    .getByRole("radio", { name: "Data" })
    .click();
  const reference = page.getByRole("combobox", {
    name: "Value reference",
    exact: true,
  });
  await reference.fill("");
  await reference.press("ArrowDown");
  const options = page.getByRole("listbox").getByRole("option");
  await expect(options).toHaveCount(2);
  expect(
    await options.evaluateAll((items) =>
      items.map((o) => o.getAttribute("data-ref")),
    ),
  ).toEqual(["/steps/call-action-1/output", "/input"]);
});

test("activation binds each connection slot to an environment connection", async ({
  page,
}) => {
  await connected(page);
  const connections = {
    items: [
      {
        id: "0f8fad5b-d9cb-469f-a165-70867728950e",
        name: "orders-db",
        revision: 3,
        connector: "weave-postgresql@1.0.0",
        adapter: "postgresql",
      },
      {
        id: "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        name: "crm-prod",
        revision: 1,
        connector: "crm@1.0.0",
        adapter: "http",
      },
    ],
    next_cursor: null,
  };
  let connectionRequests = 0;
  await page.route("**/environments/development/connections?*", (r) => {
    connectionRequests++;
    return connectionRequests === 1
      ? r.fulfill({ status: 500, json: { code: "WV-INTERNAL" } })
      : r.fulfill({ json: connections });
  });
  await page.route("**/projects/project/workflows", (r) =>
    r.fulfill({
      status: 201,
      json: {
        id: "11111111-1111-4111-8111-111111111111",
        name: "untitled-workflow",
        version: "1.0.0",
        digest: "sha256:abc",
      },
    }),
  );
  let activation: any = null;
  await page.route("**/environments/development/activations", (r) => {
    activation = r.request().postDataJSON();
    return r.fulfill({
      status: 201,
      json: { id: "activation", name: "untitled-workflow", revision: 1 },
    });
  });
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await chooseAction(page, "sql.lookup@1.0.0");
  await page.getByLabel(/^Customer ID/).fill("customer-104");
  await page
    .getByRole("button", {
      name: "Add a PostgreSQL connection slot",
      exact: true,
    })
    .click();
  await inspectorHeading(page).click();
  await command(page, "Publish…");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Publish version" })
    .click();
  await command(page, "Activate…");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(/Activate \S+ \d+\.\d+\.\d+/);
  await expect(dialog).toContainText("could not complete this request");
  await expect(dialog).toContainText("Support code: WV-INTERNAL");
  await dialog.getByRole("button", { name: "Try again" }).click();
  await expect(dialog).toContainText("picked automatically");
  await dialog.getByRole("button", { name: "Review", exact: true }).click();
  await expect(dialog).toContainText("orders-db");
  await dialog.getByRole("button", { name: "Activate version" }).click();
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => activation).not.toBeNull();
  expect(activation).toMatchObject({
    connection_revision_ids: {
      "weave-postgresql": "0f8fad5b-d9cb-469f-a165-70867728950e",
    },
    worker_release_ids: {},
    assignment_binding_ids: {},
    version_id: "11111111-1111-4111-8111-111111111111",
    artifact_digest: "sha256:abc",
  });
});

test("activation without connection permission accepts revision IDs", async ({
  page,
}) => {
  await connected(page, {
    capabilities: allCapabilities.filter(
      (capability) => capability !== "catalog.read",
    ),
  });
  await page.route("**/environments/development/connections?*", (r) =>
    r.fulfill({ status: 403, json: { code: "WV-DENIED" } }),
  );
  await page.route("**/projects/project/workflows", (r) =>
    r.fulfill({
      status: 201,
      json: {
        id: "11111111-1111-4111-8111-111111111111",
        digest: "sha256:abc",
      },
    }),
  );
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await page
    .getByLabel("Action version", { exact: true })
    .fill("custom.lookup@1.0.0");
  await contract(page).getByText("Add connection slot").click();
  await page
    .getByLabel("New slot connector", { exact: true })
    .fill("crm@1.0.0");
  await page.getByRole("button", { name: "Add slot", exact: true }).click();
  await inspectorHeading(page).click();
  await command(page, "Publish…");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Publish version" })
    .click();
  await command(page, "Activate…");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(
    "Your account cannot list connections in this environment.",
  );
  await dialog.getByLabel(/^crm/).fill("not-a-uuid");
  await dialog.getByRole("button", { name: "Activate version" }).click();
  await expect(dialog).toContainText("must be a revision ID");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

test("activation keeps its dialog open while it runs and shows failures inline", async ({
  page,
}) => {
  await connected(page, {
    capabilities: allCapabilities.filter(
      (capability) => capability !== "catalog.read",
    ),
  });
  await page.route("**/environments/development/connections?*", (r) =>
    r.fulfill({ status: 403, json: { code: "WV-DENIED" } }),
  );
  await page.route("**/projects/project/workflows", (r) =>
    r.fulfill({
      status: 201,
      json: {
        id: "11111111-1111-4111-8111-111111111111",
        digest: "sha256:abc",
      },
    }),
  );
  let calls = 0;
  let release = () => {};
  await page.route("**/environments/development/activations", async (r) => {
    const call = ++calls;
    await new Promise<void>((resolve) => (release = resolve));
    return call === 1
      ? r.fulfill({
          status: 409,
          json: { status: 409, code: "WV-CONNECTION", message: "x" },
        })
      : r.fulfill({ status: 201, json: { id: "activation", revision: 1 } });
  });
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await page
    .getByLabel("Action version", { exact: true })
    .fill("custom.lookup@1.0.0");
  await contract(page).getByText("Add connection slot").click();
  await page
    .getByLabel("New slot connector", { exact: true })
    .fill("crm@1.0.0");
  await page.getByRole("button", { name: "Add slot", exact: true }).click();
  await inspectorHeading(page).click();
  await command(page, "Publish…");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Publish version" })
    .click();
  await command(page, "Activate…");
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel(/^crm/).fill("7c9e6679-7425-40de-944b-e07fc1f90ae7");
  const submit = dialog.getByRole("button", { name: "Activate version" });
  await submit.click();
  // The request runs with the dialog open; nothing can close it meanwhile.
  await expect(dialog.getByRole("status")).toContainText("Activating…");
  await expect(submit).toBeDisabled();
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeDisabled();
  // Focus stays in the dialog instead of falling back to the page behind it.
  await expect(dialog).toBeFocused();
  await dialog.getByLabel(/^crm/).focus();
  await page.keyboard.press("Escape");
  await dialog.getByRole("button", { name: "Close dialog" }).click();
  await expect(dialog).toBeVisible();
  await expect.poll(() => calls).toBe(1);
  release();
  // A failure stays in the dialog, in plain language with its support code.
  const problem = dialog.locator(".activation-problem");
  await expect(problem).toContainText(
    "The connection bindings do not match what this workflow needs.",
  );
  await expect(problem).toContainText("Support code: WV-CONNECTION");
  await expect(page.locator(".error-banner")).toHaveCount(0);
  // The failure takes focus, so it is read out and Tab stays in the dialog.
  await expect(problem).toBeFocused();
  await page.keyboard.press("Tab");
  expect(
    await dialog.evaluate((panel) => panel.contains(document.activeElement)),
  ).toBe(true);
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(problem).toHaveCount(0);
  await expect.poll(() => calls).toBe(2);
  await expect(dialog).toBeVisible();
  release();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".error-banner")).toHaveCount(0);
});
