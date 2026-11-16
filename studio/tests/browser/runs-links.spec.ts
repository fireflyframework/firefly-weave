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
// Links to the Runs tab: a link opens the tab and its run read-only, the
// address follows the chosen run without adding history, leaving the tab
// drops the run, a reload keeps both, and a run elsewhere says where it is.
// Switching environment closes the run and the list.
import { expect, test, type Page } from "@playwright/test";
import { useNewEditor } from "./canvas-po";
import { DesignerPage } from "./designer-po";
import { acme, acmePlatform, platformHost } from "./platform-host";
import {
  listedKinds,
  openRunsTab,
  routeRuns,
  RUN_IDS,
  runFixture,
  runsPage,
  runsWorkflow,
} from "./runs-po";
import { allCapabilities, newWorkflow } from "./support";

test.use({ viewport: { width: 1440, height: 900 } });

/** The open draft's ID, from the address. */
const draftOf = (page: Page) =>
  /\/workflows\/([^/]+)\/designer/.exec(new URL(page.url()).pathname)![1];
/** Answers the draft's own read, so a reload opens it again. */
async function keepDraft(page: Page, id: string) {
  await page.route(`**/projects/*/drafts/${id}`, (route) =>
    route.request().method() === "GET"
      ? route.fulfill({
          json: { id, revision: 1, document: runsWorkflow },
        })
      : route.fallback(),
  );
}
const testDataWrites = (page: Page) => {
  const writes: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "GET" && /test-?data/i.test(request.url()))
      writes.push(request.url());
  });
  return writes;
};

test("a link opens the run and changes no test data", async ({ page }) => {
  const { runs } = await openRunsTab(page);
  await expect(runs.rows).toHaveCount(listedKinds.length + 1);
  const id = draftOf(page);
  await keepDraft(page, id);
  const writes = testDataWrites(page);
  await page.goto(`/workflows/${id}/designer?tab=runs&run=${RUN_IDS.failed}`);
  await expect(runs.tabs.getByRole("tab", { name: "Runs" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(runs.canvas.locator(".run-header h2")).toHaveText(
    "Run 1c0d7f8a",
  );
  await expect(runs.row(RUN_IDS.failed)).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await page.clock.runFor(5_000);
  expect(writes).toEqual([]);
});

test("choosing a run replaces the address, and Back leaves the workflow", async ({
  page,
}) => {
  const { runs } = await openRunsTab(page);
  await expect(runs.rows).toHaveCount(listedKinds.length + 1);
  const id = draftOf(page);
  const length = await page.evaluate(() => history.length);
  await runs.row(RUN_IDS.failed).click();
  await expect(page).toHaveURL(
    new RegExp(`/workflows/${id}/designer\\?tab=runs&run=${RUN_IDS.failed}$`),
  );
  await runs.row(RUN_IDS.retry).click();
  await expect(page).toHaveURL(new RegExp(`run=${RUN_IDS.retry}$`));
  expect(await page.evaluate(() => history.length)).toBe(length);
  await page.goBack();
  await expect(page).not.toHaveURL(/tab=runs/);
});

test("leaving the Runs tab drops the run from the address", async ({
  page,
}) => {
  const { runs } = await openRunsTab(page);
  await runs.row(RUN_IDS.failed).click();
  await expect(page).toHaveURL(/run=/);
  const id = draftOf(page);
  await runs.tabs.getByRole("tab", { name: "Designer" }).click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${id}/designer$`));
});

test("a reload keeps the tab and the run", async ({ page }) => {
  const { runs } = await openRunsTab(page);
  await runs.row(RUN_IDS.retry).click();
  await expect(page).toHaveURL(new RegExp(`run=${RUN_IDS.retry}$`));
  await keepDraft(page, draftOf(page));
  await page.reload();
  await expect(runs.tabs.getByRole("tab", { name: "Runs" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(runs.canvas.locator(".run-header h2")).toHaveText(
    "Run 3e2f9bac",
  );
});

test("a run from another environment says where it is", async ({ page }) => {
  const { runs } = await openRunsTab(page);
  await expect(runs.rows).toHaveCount(listedKinds.length + 1);
  const id = draftOf(page);
  await keepDraft(page, id);
  const elsewhere = "9f9f9f9f-1111-4222-8333-444455556666";
  await page.goto(`/workflows/${id}/designer?tab=runs&run=${elsewhere}`);
  await expect(runs.canvas).toContainText("Run 9f9f9f9f isn't in development.");
  await expect(runs.canvas).toContainText(
    "Switch to its environment to open it.",
  );
  // A link to anything but a run ID opens the tab without a run.
  await page.goto(`/workflows/${id}/designer?tab=runs&run=../settings`);
  await expect(runs.tabs.getByRole("tab", { name: "Runs" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});

test("switching environment closes the run and empties the list", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-10-07T12:00:00Z") });
  await useNewEditor(page);
  await platformHost(page, {
    platforms: [acmePlatform()],
    active: "Acme",
    test: (host) => ({
      json: {
        session: host.session(),
        identity: {
          ...host.identity(),
          grants: host.identity().grants.map((grant) => ({
            ...grant,
            capabilities: [...allCapabilities, "compile"],
          })),
        },
        workspaces: host.current?.workspaces ?? [],
        truncated: false,
        workspace_revoked: false,
      },
    }),
  });
  const platform = await routeRuns(page, listedKinds.map(runFixture));
  // Only Production has runs; Staging has none.
  await page.route("**/environments/*/run-summaries**", (route) =>
    route.request().url().includes(acme[0].environment_id)
      ? route.fallback()
      : route.fulfill({ json: { items: [], next_cursor: null } }),
  );
  void platform;
  await newWorkflow(page);
  await new DesignerPage(page).setSource(JSON.stringify(runsWorkflow, null, 2));
  const runs = runsPage(page);
  await runs.tabs.getByRole("tab", { name: "Runs" }).click();
  await expect(runs.rows).toHaveCount(listedKinds.length + 1);
  await runs.row(RUN_IDS.failed).click();
  await expect(page).toHaveURL(/run=/);
  await page.locator(".platform-indicator").click();
  await page.getByRole("button", { name: "Switch workspace" }).click();
  await page.getByRole("radio", { name: "Acme / Payments / Staging" }).check();
  await page.getByRole("button", { name: "Start working" }).click();
  await expect(runs.panel).toContainText("No runs of invoice-approval yet.");
  await expect(runs.rows).toHaveCount(0);
  await expect(runs.canvas.locator(".run-header")).toHaveCount(0);
  await expect(page).not.toHaveURL(/run=/);
});
