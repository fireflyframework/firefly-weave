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
// WP-22a: a run starts from a form built from the activated version's input
// schema; problems stay in the dialog in plain words; the last input is
// remembered; a toast offers to open the new run.
import { selectChoice } from "./support";
import { test, expect, Page, Request } from "@playwright/test";
import { allCapabilities, connected } from "./support";

const versionId = "44444444-4444-4444-8444-444444444444";
const activation = {
  id: "act-1",
  name: "onboarding",
  revision: 1,
  request: { version_id: versionId },
};
const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";

async function runsWithActivation(page: Page, status = 201) {
  const starts: Request[] = [];
  await page.route(`${environment}/activations?*`, (r) =>
    r.fulfill({ json: { items: [activation], next_cursor: null } }),
  );
  await page.route(`**/projects/project/workflows/${versionId}/export`, (r) =>
    r.fulfill({
      json: {
        document: {
          apiVersion: "weave/v1alpha1",
          kind: "Workflow",
          metadata: { name: "onboarding", version: "1.0.0" },
          spec: {
            inputSchema: {
              type: "object",
              required: ["customerId"],
              properties: {
                customerId: { type: "string", title: "Customer ID" },
                priority: { type: "integer", title: "Priority" },
                apiKey: { type: "string", title: "API key", writeOnly: true },
              },
            },
            outputSchema: { type: "object" },
            steps: [],
            output: { literal: {} },
          },
        },
      },
    }),
  );
  await page.route(`${environment}/runs`, (r) => {
    starts.push(r.request());
    return status === 201
      ? r.fulfill({
          status: 201,
          json: { id: "run-1", state: { status: "queued" } },
        })
      : r.fulfill({
          status,
          json: {
            code: "WV-READINESS",
            message: "This activation is not ready to run.",
          },
        });
  });
  await page.route(`${environment}/runs/run-1`, (r) =>
    r.fulfill({
      json: {
        id: "run-1",
        business_key: "onboarding-7",
        state: { status: "queued" },
      },
    }),
  );
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  return starts;
}
const dialog = (page: Page) =>
  page.getByRole("dialog", { name: "Start a run" });

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("the run input is a form from the workflow's input schema", async ({
      page,
    }) => {
      await connected(page);
      const starts = await runsWithActivation(page);
      await page
        .getByRole("button", { name: "Start run", exact: true })
        .click();
      await selectChoice(
        dialog(page).getByLabel("Version to run", { exact: true }),
        "act-1",
      );
      const customer = dialog(page).getByLabel("Customer ID");
      await expect(customer).toBeVisible();
      await dialog(page).getByText("Add a business key (optional)").click();
      await dialog(page).getByLabel("Business key").fill("onboarding-7");
      await dialog(page).getByRole("button", { name: "Start run" }).click();
      await expect(dialog(page).getByRole("alert")).toHaveText(
        "Fill in Customer ID.",
      );
      expect(starts).toHaveLength(0);
      await customer.fill("C-104");
      await dialog(page).getByLabel("Priority").fill("2");
      // A write-only field is locked: the platform rejects secret values in
      // run input, and Studio never enters one (F5).
      await expect(dialog(page)).toContainText(
        "Secret: Studio never enters, sends or stores this value.",
      );
      await expect(
        dialog(page).getByRole("textbox", { name: "API key" }),
      ).toHaveCount(0);
      await dialog(page).getByRole("button", { name: "Start run" }).click();
      await expect(dialog(page)).toHaveCount(0);
      expect(starts).toHaveLength(1);
      expect(starts[0].postDataJSON()).toEqual({
        activation_id: "act-1",
        input: { customerId: "C-104", priority: 2 },
        business_key: "onboarding-7",
      });
      // The toast opens the new run.
      const toast = page.locator(".toast");
      await expect(toast).toContainText("Started run run-1.");
      await toast.getByRole("button", { name: "View" }).click();
      await expect(page.locator(".record-detail")).toContainText(
        "onboarding-7",
      );
      // The input is remembered for the next run, without write-only fields.
      await page.getByRole("button", { name: "Close detail" }).click();
      await page
        .getByRole("button", { name: "Start run", exact: true })
        .click();
      await selectChoice(
        dialog(page).getByLabel("Version to run", { exact: true }),
        "act-1",
      );
      await expect(dialog(page).getByLabel("Customer ID")).toHaveValue("C-104");
      await expect(
        dialog(page).getByRole("textbox", { name: "API key" }),
      ).toHaveCount(0);
    });

    test("a field error stays until it is fixed and blocks the start", async ({
      page,
    }) => {
      await connected(page);
      const starts = await runsWithActivation(page);
      await page
        .getByRole("button", { name: "Start run", exact: true })
        .click();
      await selectChoice(
        dialog(page).getByLabel("Version to run", { exact: true }),
        "act-1",
      );
      const priority = dialog(page).getByLabel("Priority");
      await priority.pressSequentially("1.5", { delay: 50 });
      await priority.press("Tab");
      const error = dialog(page).getByText("Priority must be a whole number.");
      await expect(error).toBeVisible();
      // Typing elsewhere keeps the error and the typed text.
      await dialog(page)
        .getByLabel("Customer ID")
        .pressSequentially("C-1", { delay: 50 });
      await expect(error).toBeVisible();
      await expect(priority).toHaveValue("1.5");
      await dialog(page).getByRole("button", { name: "Start run" }).click();
      await expect(dialog(page)).toContainText(
        "Correct the fields marked with an error.",
      );
      await expect(priority).toBeFocused();
      expect(starts).toHaveLength(0);
      await priority.fill("2");
      await dialog(page).getByRole("button", { name: "Start run" }).click();
      await expect(dialog(page)).toHaveCount(0);
      expect(starts[0].postDataJSON().input).toEqual({
        customerId: "C-1",
        priority: 2,
      });
    });

    test("secret run input is never entered, sent or remembered, at any depth", async ({
      page,
    }) => {
      await connected(page);
      const starts = await runsWithActivation(page);
      await page.route(
        `**/projects/project/workflows/${versionId}/export`,
        (r) =>
          r.fulfill({
            json: {
              document: {
                apiVersion: "weave/v1alpha1",
                kind: "Workflow",
                metadata: { name: "onboarding", version: "1.0.0" },
                spec: {
                  inputSchema: {
                    type: "object",
                    properties: {
                      customerId: { type: "string", title: "Customer ID" },
                      auth: {
                        type: "object",
                        title: "Sign-in",
                        properties: {
                          user: { type: "string", title: "User" },
                          token: {
                            type: "string",
                            title: "Token",
                            "x-secret": true,
                          },
                        },
                      },
                    },
                  },
                  outputSchema: { type: "object" },
                  steps: [],
                  output: { literal: {} },
                },
              },
            },
          }),
      );
      await page
        .getByRole("button", { name: "Start run", exact: true })
        .click();
      await selectChoice(
        dialog(page).getByLabel("Version to run", { exact: true }),
        "act-1",
      );
      await dialog(page).getByLabel("Customer ID").fill("C-7");
      await dialog(page).getByLabel("User").fill("ana");
      await expect(
        dialog(page).getByRole("textbox", { name: "Token" }),
      ).toHaveCount(0);
      await dialog(page).getByRole("button", { name: "Start run" }).click();
      await expect(dialog(page)).toHaveCount(0);
      expect(starts[0].postDataJSON().input).toEqual({
        customerId: "C-7",
        auth: { user: "ana" },
      });
      const stored = await page.evaluate(() =>
        Object.keys(localStorage)
          .filter((key) => key.startsWith("weave-studio-run-input:"))
          .map((key) => localStorage.getItem(key) ?? ""),
      );
      expect(stored).toEqual([
        JSON.stringify({ customerId: "C-7", auth: { user: "ana" } }),
      ]);
    });

    test("JSON input and platform failures are explained in the dialog", async ({
      page,
    }) => {
      await connected(page);
      const starts = await runsWithActivation(page, 422);
      await page
        .getByRole("button", { name: "Start run", exact: true })
        .click();
      await selectChoice(
        dialog(page).getByLabel("Version to run", { exact: true }),
        "act-1",
      );
      await dialog(page).getByLabel("Edit as JSON").check();
      const text = dialog(page).getByLabel("Run input (JSON)");
      await text.fill("{bad");
      await dialog(page).getByRole("button", { name: "Start run" }).click();
      await expect(dialog(page).getByRole("alert")).toContainText(
        "Enter the run input as a JSON object",
      );
      await expect(page.getByText(/SyntaxError|JSON\.parse/)).toHaveCount(0);
      expect(starts).toHaveLength(0);
      await text.fill('{"customerId": "C-9"}');
      await dialog(page).getByRole("button", { name: "Start run" }).click();
      await expect(dialog(page).getByRole("alert")).toContainText(
        "The run didn't start. This activation is not ready to run.",
      );
      await expect(dialog(page)).toContainText("Support code: WV-READINESS");
      expect(starts[0].postDataJSON().input).toEqual({ customerId: "C-9" });
    });
  });

test("pause and resume are offered only while a run is still in progress", async ({
  page,
}) => {
  await connected(page, {
    capabilities: [...allCapabilities, "run.pause", "run.resume"],
  });
  const runs = {
    "run-done": { id: "run-done", state: { status: "succeeded" } },
    "run-live": { id: "run-live", state: { status: "running" } },
  };
  await page.route(`${environment}/runs?*`, (r) =>
    r.fulfill({ json: { items: Object.values(runs), next_cursor: null } }),
  );
  for (const [id, run] of Object.entries(runs))
    await page.route(`${environment}/runs/${id}`, (r) =>
      r.fulfill({ json: run }),
    );
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  const detail = page.locator(".record-detail");
  await page.locator(".resource-row").filter({ hasText: "run-live" }).click();
  await expect(detail.getByRole("heading", { level: 2 })).toHaveText(
    /run-live/,
  );
  // A running run offers Pause only; Resume belongs to a paused one.
  await expect(detail.getByRole("button", { name: "Pause run" })).toBeVisible();
  await expect(detail.getByRole("button", { name: "Resume run" })).toHaveCount(
    0,
  );
  // A finished run can't be paused or resumed: Studio doesn't offer it.
  await page.locator(".resource-row").filter({ hasText: "run-done" }).click();
  await expect(detail.getByRole("heading", { level: 2 })).toHaveText(
    /run-done/,
  );
  await expect(detail.locator(".status-pill").first()).toHaveText("Succeeded");
  await expect(detail.getByRole("button", { name: "Pause run" })).toHaveCount(
    0,
  );
  await expect(detail.getByRole("button", { name: "Resume run" })).toHaveCount(
    0,
  );
});
