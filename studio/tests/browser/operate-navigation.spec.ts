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
// Operate navigation: the Work and Operate groups, entries shown by
// capability, the paths from before Operate, and run addresses that open and
// close the run.
import { test, expect, Page } from "@playwright/test";
import { connected, offline } from "./support";

const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";
const runId = "5e6f7a8b-0000-4000-8000-000000000001";
const targetId = "11111111-1111-4111-8111-111111111111";
const run = {
  id: runId,
  business_key: "order-7",
  state: { status: "waiting", active: [] },
};

async function navLabels(page: Page) {
  return page
    .getByRole("navigation", { name: "Main navigation" })
    .locator("button span, .nav-group")
    .evaluateAll((items) =>
      items
        .filter((item) => getComputedStyle(item).display !== "none")
        .map((item) => item.textContent?.trim()),
    );
}

async function runRoutes(page: Page) {
  await page.route(`${environment}/runs?*`, (r) =>
    r.fulfill({ json: { items: [run], next_cursor: null } }),
  );
  await page.route(`${environment}/runs/${runId}`, (r) =>
    r.fulfill({ json: run }),
  );
  await page.route(`${environment}/runs/${runId}/lifecycle`, (r) =>
    r.fulfill({
      json: { run_id: runId, archived: false, purged: false, revision: 1 },
    }),
  );
  await page.route(`${environment}/runs/${runId}/history?*`, (r) =>
    r.fulfill({ json: { events: [], next_cursor: null } }),
  );
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("a reader sees only the Operate pages they can open", async ({
      page,
    }) => {
      await connected(page, { capabilities: ["run.read"] });
      await expect(
        page.getByRole("button", { name: "Runs", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Workers", exact: true }),
      ).toHaveCount(0);
      await expect(
        page.getByRole("button", { name: "Clusters", exact: true }),
      ).toHaveCount(0);
      if (viewport.width > 1280)
        expect(await navLabels(page)).toEqual([
          "Home",
          "Build",
          "Workflows",
          "Connections",
          "Work",
          "My tasks",
          "Email",
          "Operate",
          "Runs",
          "Settings",
          "Collapse sidebar",
        ]);
    });

    test("a target-scoped deployment reader still sees Clusters", async ({
      page,
    }) => {
      await connected(page, { capabilities: [] });
      // The last matching route wins: this grant replaces the default one.
      await page.route("**/studio/api/api/v1/identity", (r) =>
        r.fulfill({
          json: {
            principal_id: "human",
            kind: "human",
            grants: [
              {
                role: "deployment_reader",
                scope: {
                  tenant_id: "tenant",
                  project_id: "project",
                  environment_id: "development",
                },
                resources: [targetId],
                capabilities: ["deployment.read"],
              },
            ],
            workspaces: [],
            truncated: false,
          },
        }),
      );
      await page.reload();
      await page.getByRole("button", { name: "Clusters", exact: true }).click();
      await expect(page).toHaveURL(/\/operate\/clusters$/);
      await expect(page.locator("#main h1")).toHaveText("Clusters");
    });

    test("local authoring lists every page, and old addresses still open", async ({
      page,
    }) => {
      await offline(page);
      for (const [old, path, heading] of [
        ["/runs", "/operate/runs", "Runs"],
        ["/workers", "/operate/workers", "Workers"],
        ["/operations", "/operate/clusters", "Clusters"],
        ["/operate", "/operate/runs", "Runs"],
      ]) {
        await page.goto(old);
        await expect(page).toHaveURL(new RegExp(`${path}$`));
        await expect(page.locator("#main h1")).toHaveText(heading);
      }
      await page.getByRole("button", { name: "Clusters", exact: true }).click();
      await expect(page).toHaveURL(/\/operate\/clusters$/);
      await expect(page).toHaveTitle("Clusters | Firefly Weave Studio");
    });

    test("an old record address opens the record under Clusters", async ({
      page,
    }) => {
      await connected(page, { capabilities: ["deployment.read"] });
      await page.route(`**/deployment-targets/${targetId}`, (r) =>
        r.fulfill({
          json: {
            id: targetId,
            name: "owned-target",
            adapter: "docker-compose",
            external_identity: "owned-docker",
            boundary: "weave-owned",
            runner_principal_id: "44444444-4444-4444-8444-444444444444",
            capabilities: ["observe"],
            scope: {
              tenant_id: "tenant",
              project_id: "project",
              environment_id: "development",
            },
            revision: 1,
            disabled: false,
            created_at: "2026-10-03T10:00:00Z",
          },
        }),
      );
      await page.goto(`/operations/targets/${targetId}`);
      await expect(page).toHaveURL(
        new RegExp(`/operate/clusters/targets/${targetId}$`),
      );
      await expect(
        page.getByRole("heading", { name: "owned-target", exact: true }),
      ).toBeVisible();
      await page.getByRole("button", { name: "Back to Clusters" }).click();
      await expect(page).toHaveURL(/\/operate\/clusters$/);
    });

    test("a run has its own address that opens, closes and goes back", async ({
      page,
    }) => {
      await connected(page, { capabilities: ["run.read"] });
      await runRoutes(page);
      await page.goto(`/operate/runs/${runId}`);
      const detail = page.locator(".record-detail");
      await expect(detail.locator("#record-detail-title")).toHaveText(
        "order-7",
      );
      await detail.getByRole("button", { name: "Close detail" }).click();
      await expect(page).toHaveURL(/\/operate\/runs$/);
      await expect(detail).toHaveCount(0);
      await page.getByRole("button", { name: "order-7", exact: true }).click();
      await expect(page).toHaveURL(new RegExp(`/operate/runs/${runId}$`));
      await expect(detail.locator("#record-detail-title")).toHaveText(
        "order-7",
      );
      await page.goBack();
      await expect(page).toHaveURL(/\/operate\/runs$/);
      await expect(detail).toHaveCount(0);
    });
  });
