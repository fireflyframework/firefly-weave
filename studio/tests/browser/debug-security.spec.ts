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
// The rules for a run's data in test data, end to end: a value the platform
// marks secret never reaches test data, storage, the console or the address;
// Debug in editor is refused, reading nothing, where test data can't be kept
// on the platform or the role can't change it; a refused read shows nothing
// of the run; signing out forgets the run and the run's data kept on this
// computer; and the run's error is said by its code only.
import { expect, test, type Page } from "@playwright/test";
import { useNewEditor } from "./canvas-po";
import { DesignerPage } from "./designer-po";
import { hasPython, localAuthoring } from "./local-authoring";
import { acmePlatform, platformHost } from "./platform-host";
import {
  keepDrafts,
  listedKinds,
  NOW,
  openRunsTab,
  RUN_IDS,
  RUNS_ACCOUNT,
  RUNS_ENV,
  routeRuns,
  runFixture,
  runsPage,
  runsWorkflow,
  WORKER_CATALOG,
  type RunFixture,
  type RunsPage,
} from "./runs-po";
import { StepDetailsPage } from "./step-details-po";
import {
  allCapabilities,
  connected,
  newWorkflow,
  type CatalogEntry,
} from "./support";

test.skip(
  !hasPython,
  "Execute runs on the Studio host's Python, not found here.",
);
test.use({ viewport: { width: 1440, height: 900 } });

const MARKER = "s3cr3t-marker-91";
const ROLE_CANT = "Your role can't change this workflow's test data.";

/** The runs, the failed one changed by `change` (as JSON text). */
const runsWith = (change: (text: string) => string): RunFixture[] =>
  listedKinds.map((kind) => {
    const run = runFixture(kind);
    return kind === "failed" ? JSON.parse(change(JSON.stringify(run))) : run;
  });
/** The runs' workflow as a platform draft under an account Studio keeps test data for, on its Runs tab. */
async function openDraftRuns(
  page: Page,
  options: {
    runs?: RunFixture[];
    catalog?: CatalogEntry[];
    capabilities?: string[];
  } = {},
) {
  const { runs, platform } = await openRunsTab(page, {
    runs: options.runs,
    account: RUNS_ACCOUNT,
    catalog: options.catalog ?? WORKER_CATALOG,
    capabilities: options.capabilities,
  });
  await expect(runs.rows).toHaveCount(listedKinds.length + 1);
  const id = /\/workflows\/([^/]+)\/designer/.exec(
    new URL(page.url()).pathname,
  )![1];
  const kept = await keepDrafts(page, [{ id, document: runsWorkflow }]);
  await page.goto(`/workflows/${id}/designer?tab=runs`);
  await expect(runs.rows).toHaveCount(listedKinds.length + 1);
  return { runs, platform, kept };
}
async function choose(page: Page, runs: RunsPage, id: string) {
  await runs.row(id).click();
  await expect(runs.canvas.locator(".run-header h2")).toHaveText(
    `Run ${id.slice(0, 8)}`,
  );
  await expect(runs.canvas.locator(".run-header .run-caption")).toBeVisible();
}
/** Every key and value this page keeps in browser storage. */
const storage = (page: Page) =>
  page.evaluate(() =>
    [localStorage, sessionStorage].flatMap((store) =>
      Object.keys(store).flatMap((key) => [key, store.getItem(key) ?? ""]),
    ),
  );
const outputReads = (requests: URL[]) =>
  requests.filter((url) => url.searchParams.get("include") === "output");

test("a value marked secret in a run never reaches test data", async ({
  page,
}) => {
  // The platform's sql.lookup marks the IDs it returns secret; the failed
  // run's fetch returned one.
  const lookup = WORKER_CATALOG[0].document as unknown as {
    spec: Record<string, unknown>;
  };
  const catalog: CatalogEntry[] = [
    {
      id: "a1",
      document: {
        ...lookup,
        spec: {
          ...lookup.spec,
          outputSchema: {
            type: "object",
            properties: {
              rows: {
                type: "array",
                items: {
                  type: "object",
                  properties: { id: { type: "string", "x-secret": true } },
                },
              },
            },
          },
        },
      } as unknown as CatalogEntry["document"],
    },
    WORKER_CATALOG[1],
  ];
  const messages: string[] = [];
  page.on("console", (message) => messages.push(message.text()));
  const { runs, kept } = await openDraftRuns(page, {
    catalog,
    runs: runsWith((text) => text.replaceAll('"c-1"', `"${MARKER}"`)),
  });
  await localAuthoring(page);
  await choose(page, runs, RUN_IDS.failed);
  await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
  await expect(page.locator(".toast-detail")).toHaveText(
    "Left out 1 value: fetch (marked secret).",
  );
  await expect(new StepDetailsPage(page).dialogFor("fetch")).toBeVisible();
  await page.clock.runFor(11_000);
  await expect.poll(() => kept.saved.length).toBeGreaterThan(0);
  for (const document of kept.saved)
    expect(JSON.stringify(document)).not.toContain(MARKER);
  for (const item of await storage(page)) expect(item).not.toContain(MARKER);
  for (const message of messages) expect(message).not.toContain(MARKER);
  expect(page.url()).not.toContain(MARKER);
});

test("Debug is refused, reading nothing, where test data can't be kept on the platform", async ({
  page,
}) => {
  // A new workflow under an account Studio can't keep test data for.
  const { runs, platform } = await openRunsTab(page);
  await choose(page, runs, RUN_IDS.failed);
  const debug = runs.canvas.getByRole("button", { name: "Debug in editor" });
  await expect(debug).toHaveAttribute("aria-disabled", "true");
  await expect(debug).toHaveAttribute("aria-describedby", "run-refusal");
  await expect(runs.canvas.locator("#run-refusal")).toHaveText(
    "This workflow is kept on this computer only. Save it to the platform to copy a run's data into its test data.",
  );
  await debug.click({ force: true });
  await page.clock.runFor(2_000);
  expect(outputReads(platform.requests)).toEqual([]);
  await expect(page.locator(".toast")).toHaveCount(0);
});

test("Debug is refused without the roles to change test data", async ({
  page,
}) => {
  const { runs, platform, kept } = await openDraftRuns(page, {
    capabilities: [...allCapabilities, "compile"].filter(
      (capability) => capability !== "definition.write",
    ),
  });
  await choose(page, runs, RUN_IDS.failed);
  const debug = runs.canvas.getByRole("button", { name: "Debug in editor" });
  await expect(runs.canvas.locator("#run-refusal")).toHaveText(ROLE_CANT);
  await debug.click({ force: true });
  await page.clock.runFor(2_000);
  expect(outputReads(platform.requests)).toEqual([]);
  expect(kept.saved).toEqual([]);
});

test("a role the platform refuses at the save keeps nothing there", async ({
  page,
}) => {
  // Studio's list of capabilities is stale: the platform refuses the save.
  const { runs, kept } = await openDraftRuns(page);
  await localAuthoring(page);
  kept.refuseTestData = {
    status: 403,
    code: "WV-FORBIDDEN",
    message: "You can't change this draft's test data.",
  };
  await choose(page, runs, RUN_IDS.failed);
  await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
  await expect(new StepDetailsPage(page).dialogFor("send")).toBeVisible();
  await new StepDetailsPage(page).close();
  await page.clock.runFor(11_000);
  await expect(page.getByText("Not saved: retry")).toBeVisible();
  expect(kept.saved).toEqual([]);
});

test("a refused run read shows the access state and nothing of the run", async ({
  page,
}) => {
  const { runs, platform } = await openDraftRuns(page);
  platform.readFailure = {
    status: 403,
    code: "WV-FORBIDDEN",
    message: "Not allowed.",
  };
  await runs.row(RUN_IDS.failed).click();
  await expect(runs.panel).toContainText("You need run.read in development.");
  await expect(runs.rows).toHaveCount(0);
  await expect(runs.canvas.locator(".run-header")).toHaveCount(0);
  await expect(runs.canvas.locator(".tile-output")).toHaveCount(0);
  expect(outputReads(platform.requests)).toEqual([]);
});

test("signing out forgets the chosen run, the list and the run's data kept here", async ({
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
  await keepDrafts(page, []);
  await localAuthoring(page);
  await newWorkflow(page);
  await new DesignerPage(page).setSource(JSON.stringify(runsWorkflow, null, 2));
  const runs = runsPage(page);
  await runs.tabs.getByRole("tab", { name: "Runs" }).click();
  await expect(runs.rows).toHaveCount(listedKinds.length + 1);
  await choose(page, runs, RUN_IDS.failed);
  await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
  await expect(page.locator(".toast")).toContainText(
    "Test data now holds run 1c0d7f8a's data.",
  );
  const runData = async () =>
    (await storage(page)).filter((item) => item.includes('"source":"run"'));
  await expect.poll(runData).not.toEqual([]);
  // Execute stopped where the run's data ended: its step details open.
  const details = new StepDetailsPage(page);
  await expect(details.dialog).toBeVisible();
  await details.close();

  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Sign out of Acme" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Sign out" })
    .click();
  await expect(
    page.getByRole("button", { name: "Sign in to Acme" }),
  ).toBeVisible();
  // Nothing of the run's data stays on this computer.
  await expect.poll(runData).toEqual([]);
  // Back to the workflow: no list and no run without an account.
  await page.goBack();
  await runs.tabs.getByRole("tab", { name: "Runs" }).click();
  await expect(runs.panel).toBeVisible();
  await page.clock.runFor(6_000);
  await expect(runs.rows).toHaveCount(0);
  await expect(runs.panel.locator(".runs-body")).not.toContainText(
    "invoice-approval",
  );
  await expect(runs.canvas.locator(".run-header")).toHaveCount(0);
});

test("the run's error is said by its code, never the platform's message", async ({
  page,
}) => {
  const said = "Mailbox ada@example.com rejected the message";
  const { runs } = await openDraftRuns(page, {
    runs: runsWith((text) =>
      text.replace(
        '"code":"SMTP_REJECTED"}',
        `"code":"SMTP_REJECTED","message":"${said}"}`,
      ),
    ),
  });
  await localAuthoring(page);
  await choose(page, runs, RUN_IDS.failed);
  await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
  const details = new StepDetailsPage(page);
  await expect(details.dialogFor("send")).toBeVisible();
  await expect(details.dialog).toContainText(
    "In run 1c0d7f8a, this step failed: SMTP_REJECTED.",
  );
  await expect(page.locator("body")).not.toContainText("ada@example.com");
});

test("a Debug request whose run didn't load never starts Debug later", async ({
  page,
}) => {
  // Debug in editor starts only from a click: a request from Operate whose
  // run didn't load in the editor never starts Debug when the run is chosen
  // later for another reason.
  const draft = "00000003-1111-4111-8111-111111111111";
  await page.clock.install({ time: new Date(NOW) });
  await useNewEditor(page);
  await connected(page, {
    capabilities: [...allCapabilities, "compile"],
    environmentId: RUNS_ENV,
    environmentName: "development",
    catalog: WORKER_CATALOG,
    ...RUNS_ACCOUNT,
  });
  const platform = await routeRuns(page, listedKinds.map(runFixture));
  const kept = await keepDrafts(page, [
    { id: draft, revision: 5, document: runsWorkflow },
  ]);
  // The editor's own read of the run fails once (Operate's read before it
  // succeeds): the run doesn't load where Debug was handed over.
  let failed = false;
  await page.route(new RegExp(`/runs/${RUN_IDS.failed}$`), async (route) => {
    if (
      route.request().method() === "GET" &&
      !failed &&
      page.url().includes("/workflows/")
    ) {
      failed = true;
      return route.fulfill({
        status: 500,
        contentType: "application/problem+json",
        body: JSON.stringify({ code: "WV-INTERNAL", message: "Try again." }),
      });
    }
    return route.fallback();
  });
  await localAuthoring(page);

  await page.goto(`/operate/runs/${RUN_IDS.failed}`);
  await page
    .locator(".record-detail")
    .getByRole("button", { name: "Debug in editor" })
    .click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${draft}/designer`));
  const runs = runsPage(page);
  await expect(
    page.getByText("Studio can't show this run's data."),
  ).toBeVisible();

  // The person leaves the Runs tab, comes back, and chooses the run to read it.
  await runs.tabs.getByRole("tab", { name: "Designer" }).click();
  await runs.tabs.getByRole("tab", { name: "Runs" }).click();
  await expect(runs.rows).toHaveCount(listedKinds.length + 1);
  await expect(runs.canvas.locator(".run-header .run-caption")).toBeVisible();
  await runs.row(RUN_IDS.failed).click();
  await expect(runs.canvas.locator(".run-header h2")).toHaveText(
    `Run ${RUN_IDS.failed.slice(0, 8)}`,
  );
  // Long enough for Debug to copy, execute and say so if it started.
  await page.clock.runFor(11_000);
  expect(outputReads(platform.requests)).toEqual([]);
  expect(kept.saved).toEqual([]);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(runs.tabs.getByRole("tab", { name: "Runs" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});
