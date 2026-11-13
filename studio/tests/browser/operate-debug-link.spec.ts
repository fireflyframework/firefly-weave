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
// Operate's run detail opens a run in its workflow's newest draft: Debug in
// editor for a failed run, Copy to editor for any other. The draft open in
// the editor is used when it is that workflow's; without any draft, Studio
// offers to create one from the version the run used.
import { expect, test, type Page } from "@playwright/test";
import { useNewEditor } from "./canvas-po";
import { hasPython, localAuthoring } from "./local-authoring";
import {
  keepDrafts,
  listedKinds,
  NOW,
  RUN_IDS,
  RUNS_ACCOUNT,
  RUNS_ENV,
  routeRuns,
  runFixture,
  runsPage,
  runsWorkflow,
  runsWorkflowV120,
  WORKER_CATALOG,
} from "./runs-po";
import { acme, acmePlatform, platformHost } from "./platform-host";
import { StepDetailsPage } from "./step-details-po";
import { allCapabilities, connected } from "./support";

test.skip(
  !hasPython,
  "Execute runs on the Studio host's Python, not found here.",
);
test.use({ viewport: { width: 1440, height: 900 } });

const draft = (n: number) =>
  `${String(n).padStart(8, "0")}-1111-4111-8111-111111111111`;
/** Two drafts of 1.3.0 (revisions 2 and 5), one of 1.2.0 with more revisions, and another workflow's. */
const DRAFTS = [
  { id: draft(1), revision: 9, document: runsWorkflowV120 },
  { id: draft(2), revision: 2, document: runsWorkflow },
  { id: draft(3), revision: 5, document: runsWorkflow },
  {
    id: draft(4),
    revision: 1,
    document: {
      ...runsWorkflow,
      metadata: { name: "other", version: "9.0.0" },
    },
  },
];

/** Connected under an account Studio keeps test data for, with the runs and `drafts` on the platform. */
async function platform(
  page: Page,
  drafts = DRAFTS,
  capabilities = [...allCapabilities, "compile"],
) {
  await page.clock.install({ time: new Date(NOW) });
  await useNewEditor(page);
  await connected(page, {
    capabilities,
    environmentId: RUNS_ENV,
    environmentName: "development",
    catalog: WORKER_CATALOG,
    ...RUNS_ACCOUNT,
  });
  await routeRuns(page, listedKinds.map(runFixture));
  return keepDrafts(page, drafts);
}
/** Operate's detail of run `id`. */
async function openDetail(page: Page, id: string) {
  await page.goto(`/operate/runs/${id}`);
  const detail = page.locator(".record-detail");
  await expect(detail.locator("#record-detail-title")).toHaveText(
    "invoice-approval",
  );
  return detail;
}

test("Debug in editor opens the newest draft on the run and debugs it", async ({
  page,
}) => {
  const kept = await platform(page);
  await localAuthoring(page);
  const detail = await openDetail(page, RUN_IDS.failed);
  await detail.getByRole("button", { name: "Debug in editor" }).click();
  // The newest draft of 1.3.0 (the highest version, then its latest
  // revision) opens on the run, and Debug runs there.
  await expect(page).toHaveURL(new RegExp(`/workflows/${draft(3)}/designer`));
  // Then Debug runs there and stops where the run failed.
  const details = new StepDetailsPage(page);
  await expect(details.dialogFor("send")).toBeVisible();
  await expect(details.dialog).toContainText(
    "In run 1c0d7f8a, this step failed: SMTP_REJECTED.",
  );
  expect(kept.created).toEqual([]);
});

test("Copy to editor on a succeeded run copies it in the newest draft", async ({
  page,
}) => {
  const kept = await platform(page);
  await localAuthoring(page);
  const detail = await openDetail(page, RUN_IDS.retry);
  await expect(
    detail.getByRole("button", { name: "Debug in editor" }),
  ).toHaveCount(0);
  await detail.getByRole("button", { name: "Copy to editor" }).click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${draft(3)}/designer`));
  await expect(page.locator(".toast")).toContainText(
    "Test data now holds run 3e2f9bac's data.",
  );
  await page.clock.runFor(11_000);
  await expect.poll(() => kept.saved.length).toBeGreaterThan(0);
  expect(Object.keys(kept.saved.at(-1)!["pins"] as object).sort()).toEqual([
    "fetch",
    "send",
  ]);
});

test("without a draft it offers to create one from the run's version", async ({
  page,
}) => {
  const kept = await platform(page, []);
  await localAuthoring(page);
  const detail = await openDetail(page, RUN_IDS.failed);
  await detail.getByRole("button", { name: "Debug in editor" }).click();
  const ask = page.getByRole("dialog", { name: "Create a draft from 1.3.0?" });
  await expect(ask.locator(".dialog-message p")).toHaveText([
    "invoice-approval has no draft here. Create a draft from 1.3.0 to read this run in the editor?",
    "The draft starts as a copy of 1.3.0, the version this run used; published versions don't change.",
  ]);
  await ask.getByRole("button", { name: "Create draft" }).click();
  await expect.poll(() => kept.created.length).toBe(1);
  const [created] = kept.created;
  expect(created.id).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
  );
  expect(created.document).toEqual(runsWorkflow);
  await expect(page).toHaveURL(new RegExp(`/workflows/${created.id}/designer`));
  await expect(new StepDetailsPage(page).dialogFor("send")).toBeVisible();
});

test("the workflow open in the editor is used when it is the run's", async ({
  page,
}) => {
  const kept = await platform(page);
  await localAuthoring(page);
  await page.goto(`/workflows/${draft(2)}/designer`);
  await expect(page.locator(".editor-bar")).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(
    "invoice-approval",
  );
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  await page
    .getByRole("table", { name: "Runs" })
    .locator(".run-row")
    .filter({ hasText: "Failed" })
    .locator(".row-open")
    .first()
    .click();
  const detail = page.locator(".record-detail");
  await expect(detail.locator("#record-detail-title")).toHaveText(
    "invoice-approval",
  );
  const listed = kept.listed;
  await detail.getByRole("button", { name: "Debug in editor" }).click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${draft(2)}/designer`));
  await expect(new StepDetailsPage(page).dialogFor("send")).toBeVisible();
  expect(kept.listed).toBe(listed);
});

test("a reader without catalog.read sees neither button", async ({ page }) => {
  await platform(page, DRAFTS, ["run.read"]);
  const detail = await openDetail(page, RUN_IDS.failed);
  await expect(detail.locator(".run-controls")).toBeVisible();
  await expect(
    detail.getByRole("button", { name: /Debug in editor|Copy to editor/ }),
  ).toHaveCount(0);
});

test("a Debug request is forgotten when the environment changes before its run loads", async ({
  page,
}) => {
  await page.clock.install({ time: new Date(NOW) });
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
  const kept = await keepDrafts(page, DRAFTS.slice(2, 3));
  await localAuthoring(page);
  // The editor's read of the run waits until released; Operate's doesn't.
  let release = () => {};
  const held = new Promise<void>((done) => (release = done));
  await page.route(new RegExp(`/runs/${RUN_IDS.failed}$`), async (route) => {
    if (page.url().includes("/workflows/")) await held;
    return route.fallback();
  });
  const detail = await openDetail(page, RUN_IDS.failed);
  await detail.getByRole("button", { name: "Debug in editor" }).click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${draft(3)}/designer`));
  // Another environment while the run loads, then back.
  const workspace = async (name: string) => {
    await page.locator(".platform-indicator").click();
    await page.getByRole("button", { name: "Switch workspace" }).click();
    await page.getByRole("radio", { name }).check();
    await page.getByRole("button", { name: "Start working" }).click();
  };
  await workspace("Acme / Payments / Staging");
  const runs = runsPage(page);
  await expect(runs.panel).toContainText("No runs of invoice-approval yet.");
  release();
  await workspace("Acme / Payments / Production");
  await expect(runs.rows).toHaveCount(listedKinds.length + 1);
  await runs.row(RUN_IDS.failed).click();
  await expect(runs.canvas.locator(".run-header h2")).toHaveText(
    "Run 1c0d7f8a",
  );
  await page.clock.runFor(11_000);
  expect(
    platform.requests.filter(
      (url) => url.searchParams.get("include") === "output",
    ),
  ).toEqual([]);
  expect(kept.saved).toEqual([]);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("Operate says when the run or its version can't be read, and opens nothing", async ({
  page,
}) => {
  await platform(page);
  const detail = await openDetail(page, RUN_IDS.failed);
  const toast = page.getByRole("alert").locator(".toast");
  // The run's read answers something that isn't a run.
  await page.route(new RegExp(`/runs/${RUN_IDS.failed}$`), (route) =>
    route.fulfill({ json: { id: "not-a-run" } }),
  );
  await detail.getByRole("button", { name: "Debug in editor" }).click();
  await expect(toast).toHaveText(
    /Studio couldn't open run 1c0d7f8a in the editor: the platform's answer wasn't a run\./,
  );
  await page.unroute(new RegExp(`/runs/${RUN_IDS.failed}$`));
  // The version it used answers no workflow.
  await page.route("**/projects/*/workflows/*/export", (route) =>
    route.fulfill({ json: { document: {} } }),
  );
  await page.locator(".toast-dismiss").click();
  await detail.getByRole("button", { name: "Debug in editor" }).click();
  await expect(toast).toHaveText(
    /Studio couldn't open run 1c0d7f8a in the editor: its version couldn't be read\./,
  );
  await expect(page).not.toHaveURL(/\/workflows\//);
});

test("another environment while the drafts are read opens nothing", async ({
  page,
}) => {
  await page.clock.install({ time: new Date(NOW) });
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
  await routeRuns(page, listedKinds.map(runFixture));
  const kept = await keepDrafts(page, DRAFTS);
  // The drafts list waits until released.
  let release = () => {};
  const held = new Promise<void>((done) => (release = done));
  await page.route(
    (url) => /\/projects\/[^/]+\/drafts$/.test(url.pathname),
    async (route) => {
      await held;
      return route.fallback();
    },
  );
  const detail = await openDetail(page, RUN_IDS.failed);
  await detail.getByRole("button", { name: "Debug in editor" }).click();
  await expect.poll(() => kept.listed).toBe(0);
  await page.locator(".platform-indicator").click();
  await page.getByRole("button", { name: "Switch workspace" }).click();
  await page.getByRole("radio", { name: "Acme / Payments / Staging" }).check();
  await page.getByRole("button", { name: "Start working" }).click();
  release();
  await expect.poll(() => kept.listed).toBeGreaterThan(0);
  await page.clock.runFor(2_000);
  await expect(page).not.toHaveURL(/\/workflows\//);
  await expect(page.locator(".toast")).toHaveCount(0);
  expect(kept.created).toEqual([]);
});
