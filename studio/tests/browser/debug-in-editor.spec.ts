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
// Debug in editor and Copy to editor: a run's data becomes the draft's test
// data, marked as run data, and Execute workflow runs with it on the Studio
// host (the repository's own Python) and stops where the run failed, with
// the run's error on that step. Test data already there asks Merge or
// Replace, and Undo puts it back.
import { expect, test, type Page } from "@playwright/test";
import { pinSample } from "./execute-po";
import { hasPython, localAuthoring } from "./local-authoring";
import {
  listedKinds,
  openRunsTab,
  RUN_IDS,
  RUNS_ENV,
  runsWorkflow,
  type RunFixture,
  type RunsPage,
  type RunsPlatform,
} from "./runs-po";
import { StepDetailsPage } from "./step-details-po";
import { lookupAction, workerAction, type CatalogEntry } from "./support";

test.skip(
  !hasPython,
  "Execute runs on the Studio host's Python, not found here.",
);
test.use({ viewport: { width: 1440, height: 900 } });

/** This computer's copy of the draft's test data. */
const kept = (page: Page) =>
  page.evaluate(() => {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)!;
      if (/testData\.[0-9a-f-]+$/.test(key))
        return JSON.parse(localStorage.getItem(key)!);
    }
    return null;
  });
/** A platform account Studio can keep test data for: its IDs are UUIDs. */
const ACCOUNT = {
  tenantId: "0b9b5a59-6c63-4f3e-9d58-6a3c8f0e2b11",
  projectId: "5f0c2d4e-7a1b-4c3d-8e9f-0a1b2c3d4e5f",
};
/**
 * The platform's actions, run by workers: the Studio host compiles the
 * workflow with them, and an action without test data stops the run there.
 */
const CATALOG: CatalogEntry[] = [
  {
    id: "a1",
    document: {
      ...lookupAction,
      spec: {
        ...workerAction.spec,
        implementation: {
          kind: "worker",
          taskType: "sql-lookup",
          taskVersion: "1.0.0",
        },
        inputSchema: lookupAction.spec.inputSchema,
        outputSchema: lookupAction.spec.outputSchema,
      },
    } as CatalogEntry["document"],
  },
  { id: "a2", document: workerAction },
];
/** The canvas tile's title, where it says a step has pinned test data. */
const tileTitle = (page: Page, id: string) =>
  page
    .locator(`[data-step="${id}"]`)
    .first()
    .evaluate(
      (element) =>
        (element.matches("[title]")
          ? element
          : element.querySelector("button[title]")
        )?.getAttribute("title") ?? "",
    );
interface Opened {
  runs: RunsPage;
  platform: RunsPlatform;
  /** The test data the platform was asked to keep, in order. */
  saved: Record<string, unknown>[];
}
/**
 * The runs' workflow as a draft kept on the platform, opened on its Runs
 * tab: its test data saves with it, and the platform has none at first.
 */
async function openDraftRuns(
  page: Page,
  options: { runs?: RunFixture[]; catalog?: CatalogEntry[] } = {},
): Promise<Opened> {
  const { runs, platform } = await openRunsTab(page, {
    runs: options.runs,
    account: ACCOUNT,
    catalog: options.catalog ?? CATALOG,
  });
  await expect(runs.rows).toHaveCount((options.runs ?? listedKinds).length + 1);
  const id = /\/workflows\/([^/]+)\/designer/.exec(
    new URL(page.url()).pathname,
  )![1];
  const saved: Record<string, unknown>[] = [];
  let revision = 1;
  await page.route(`**/projects/*/drafts/${id}`, (route) => {
    if (route.request().method() === "GET")
      return route.fulfill({ json: { id, revision, document: runsWorkflow } });
    revision += 1;
    return route.fulfill({
      json: { id, revision, document: route.request().postDataJSON().document },
    });
  });
  await page.route(`**/projects/*/drafts/${id}/studio/*`, (route) => {
    const kind = route.request().url().split("/").pop()!;
    if (route.request().method() === "GET")
      return route.fulfill({
        status: 404,
        json: { status: 404, code: "WV-NOT-FOUND", message: "Not found" },
      });
    const body = route.request().postDataJSON();
    if (kind === "test-data") saved.push(body.document);
    return route.fulfill({
      json: {
        draft_id: id,
        kind,
        revision: saved.length,
        draft_revision: revision,
        document: body.document,
        contains_run_data: false,
        source_environment_ids: [],
        updated_at: "2026-11-13T10:00:00Z",
        updated_by: "human",
      },
    });
  });
  await page.goto(`/workflows/${id}/designer?tab=runs`);
  await expect(runs.rows).toHaveCount((options.runs ?? listedKinds).length + 1);
  return { runs, platform, saved };
}
/** The Runs tab with `id` chosen and its data read. */
async function choose(page: Page, runs: RunsPage, id: string) {
  await expect(runs.rows).toHaveCount(listedKinds.length + 1);
  await runs.row(id).click();
  await expect(runs.canvas.locator(".run-header h2")).toHaveText(
    `Run ${id.slice(0, 8)}`,
  );
  await expect(runs.canvas.locator(".run-header .run-caption")).toBeVisible();
}
const executions = (calls: { path: string }[]) =>
  calls.filter((call) => call.path === "/studio/local/debug/execute");
const outputRegion = (details: StepDetailsPage) =>
  details.dialog.locator('[data-region="output"]');
const toast = (page: Page) => page.locator(".toast");
/** The pin entries of each test data document the platform was asked to keep. */
const savedPins = (saved: Record<string, unknown>[]) =>
  saved.map((document) => Object.keys((document["pins"] as object) ?? {}));

test("Debug in editor stops at the step that failed, with the run's error", async ({
  page,
}) => {
  const { runs, saved } = await openDraftRuns(page);
  const calls = await localAuthoring(page);
  await choose(page, runs, RUN_IDS.failed);
  await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
  // No test data yet: no question. The Designer shows, and step details
  // open on send at its Output, with the run's error.
  const details = new StepDetailsPage(page);
  await expect(details.dialogFor("send")).toBeVisible();
  await expect(
    runs.tabs.getByRole("tab", { name: "Designer" }),
  ).toHaveAttribute("aria-selected", "true");
  const stopped = outputRegion(details).locator(".pane-run-stopped");
  await expect(stopped).toContainText(
    "In run 1c0d7f8a, this step failed: SMTP_REJECTED.",
  );
  await expect(stopped).toContainText(
    "Execute workflow gave every earlier step the run's data and stopped here.",
  );
  // Focus is in the Output region, on its first control.
  await expect
    .poll(() =>
      outputRegion(details).evaluate((region) =>
        region.contains(document.activeElement),
      ),
    )
    .toBe(true);
  await expect(toast(page)).toContainText(
    "Test data now holds run 1c0d7f8a's data. Execute workflow stopped at send, where the run failed.",
  );
  // What the host was given: fetch's output as a pin, the person's answer
  // and the signal as scripts.
  const body = executions(calls).at(-1)!.body as {
    mocks?: Record<string, unknown>;
    scripts?: { humans?: object; signals?: object };
  };
  expect(Object.keys(body.mocks ?? {})).toContain("node:fetch");
  expect(Object.keys(body.scripts?.humans ?? {})).toEqual(["approve"]);
  expect(Object.keys(body.scripts?.signals ?? {})).toEqual(["wait-for-reply"]);
  // The platform keeps it with the draft, every entry marked as run data
  // from this run's environment.
  await expect.poll(() => saved.length).toBeGreaterThan(0);
  const document = saved.at(-1) as {
    input: object;
    pins: Record<string, object>;
    humans: Record<string, object>;
    signals: Record<string, object>;
  };
  for (const entry of [
    document.input,
    document.pins["fetch"],
    document.humans["approve"],
    document.signals["wait-for-reply"],
  ])
    expect(entry).toMatchObject({
      source: "run",
      runId: RUN_IDS.failed,
      runEnvironmentId: RUNS_ENV,
    });
  expect(JSON.stringify(document)).not.toMatch(/actor_id|task_id|worker_id/);
  await details.close();
  expect(await tileTitle(page, "fetch")).toContain("Pinned test data");
});

test("back on the Runs tab, the Designer's run log takes none of the run's room", async ({
  page,
}) => {
  const { runs } = await openDraftRuns(page);
  await localAuthoring(page);
  await choose(page, runs, RUN_IDS.failed);
  await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
  const details = new StepDetailsPage(page);
  await expect(details.dialogFor("send")).toBeVisible();
  await details.close();
  // The run log docks beside the Designer's canvas.
  await expect(page.getByRole("region", { name: "Run log" })).toBeVisible();
  await runs.tabs.getByRole("tab", { name: "Runs" }).click();
  await choose(page, runs, RUN_IDS.failed);
  const right = (selector: string) =>
    page
      .locator(selector)
      .first()
      .evaluate((element) => Math.round(element.getBoundingClientRect().right));
  expect(await right("weave-runs-view")).toBe(await right(".canvas-panel"));
});

test("Copy to editor runs a succeeded run to its end", async ({ page }) => {
  const { runs } = await openDraftRuns(page);
  const calls = await localAuthoring(page);
  await choose(page, runs, RUN_IDS.retry);
  await runs.canvas.getByRole("button", { name: "Copy to editor" }).click();
  await expect(toast(page)).toHaveText(
    /Test data now holds run 3e2f9bac's data\.\s*Undo/,
  );
  await expect.poll(() => executions(calls).length).toBe(1);
  await expect(
    page.getByRole("region", { name: "Run log" }).locator(".run-log-status"),
  ).toHaveText(/^Completed/);
  // Nothing stopped: no step details open.
  await expect(new StepDetailsPage(page).dialog).toHaveCount(0);
  const data = await kept(page);
  expect(Object.keys(data.pins).sort()).toEqual(["fetch", "send"]);
});

test("with test data present it asks, and Merge keeps what the run didn't reach", async ({
  page,
}) => {
  const { runs, saved } = await openDraftRuns(page);
  await localAuthoring(page);
  // A sample pinned on send, which failed in the run: the run gives it nothing.
  await runs.tabs.getByRole("tab", { name: "Designer" }).click();
  // The Designer's own canvas, its tools back, has replaced the run's.
  await expect(
    page.getByRole("toolbar", { name: "Workflow commands" }),
  ).toBeVisible();
  const details = new StepDetailsPage(page);
  await pinSample(details, "send");
  await expect.poll(() => savedPins(saved).at(-1)).toEqual(["send"]);
  const original = saved.at(-1)!;

  const debug = async () => {
    await runs.tabs.getByRole("tab", { name: "Runs" }).click();
    await choose(page, runs, RUN_IDS.failed);
    await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
    const ask = page.getByRole("dialog", {
      name: "Use run 1c0d7f8a's data as test data?",
    });
    await expect(ask).toBeVisible();
    return ask;
  };
  const ask = await debug();
  await expect(ask).toContainText(
    "Your test data already has 1 entry. Merge keeps the ones for steps this run didn't reach; Replace removes them.",
  );
  await expect(ask).toContainText(
    "Studio copies the run's input, 1 pinned output, 1 person's answer and 1 signal. send failed in this run, so Execute stops there. check is worked out again from the copied data. Each entry is marked as data from a run in development, so only people who can read those runs can open it.",
  );
  // Merge loses nothing: the question starts on it.
  await expect(ask.getByRole("button", { name: "Merge" })).toBeFocused();
  await ask.getByRole("button", { name: "Merge" }).click();
  // send keeps its own sample, so Execute runs to the end.
  await expect(toast(page)).toHaveText(
    /^Test data now holds run 1c0d7f8a's data\.\s*Undo/,
  );
  await expect(details.dialog).toHaveCount(0);
  await expect
    .poll(() => savedPins(saved).at(-1)?.sort())
    .toEqual(["fetch", "send"]);
  expect(
    (saved.at(-1)!["pins"] as Record<string, { source: string }>)["send"],
  ).toMatchObject({ source: "schema" });

  // Undo puts the test data back as it was: the next save is the original.
  await toast(page).getByRole("button", { name: "Undo" }).click();
  // The draft saves at most every 10 seconds.
  await page.clock.runFor(11_000);
  await expect.poll(() => savedPins(saved).at(-1)).toEqual(["send"]);
  expect(saved.at(-1)!["pins"]).toEqual(original["pins"]);
  expect(saved.at(-1)!["input"]).toEqual(original["input"]);

  // Replace removes what the run didn't reach: Execute stops at send.
  const again = await debug();
  await again.getByRole("button", { name: "Replace" }).click();
  await expect(details.dialogFor("send")).toBeVisible();
  await page.clock.runFor(11_000);
  await expect.poll(() => savedPins(saved).at(-1)).toEqual(["fetch"]);
});

test("a value marked secret in the run is left out and named", async ({
  page,
}) => {
  // The platform's sql.lookup marks the IDs it returns secret.
  const lookup = CATALOG[0].document as unknown as {
    spec: Record<string, unknown>;
  };
  const secret: CatalogEntry[] = [
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
    CATALOG[1],
  ];
  const { runs, saved } = await openDraftRuns(page, { catalog: secret });
  await localAuthoring(page);
  await choose(page, runs, RUN_IDS.failed);
  await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
  await expect(toast(page)).toContainText(
    "Test data now holds run 1c0d7f8a's data. Left out 1 value: fetch (marked secret).",
  );
  // Without fetch's output, Execute stops at fetch, which needs test data.
  const details = new StepDetailsPage(page);
  await expect(details.dialogFor("fetch")).toBeVisible();
  await expect(outputRegion(details).locator(".pane-run-stopped")).toHaveCount(
    0,
  );
  await expect.poll(() => saved.length).toBeGreaterThan(0);
  for (const document of saved) {
    expect(Object.keys((document["pins"] as object) ?? {})).not.toContain(
      "fetch",
    );
    expect(JSON.stringify(document)).not.toContain("c-1");
  }
  expect(JSON.stringify(await kept(page))).not.toContain("c-1");
});

test("switching workflow while the run's data loads changes nothing", async ({
  page,
}) => {
  const { runs, platform, saved } = await openDraftRuns(page);
  const calls = await localAuthoring(page);
  await choose(page, runs, RUN_IDS.failed);
  const release = platform.holdOutputs();
  await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
  await expect(
    runs.canvas.getByRole("button", { name: "Copying run data…" }),
  ).toHaveAttribute("aria-busy", "true");
  // Another workflow opens while the run's data is still being read.
  await page.getByRole("button", { name: "Workflows", exact: true }).click();
  await page
    .getByRole("button", { name: "New workflow", exact: true })
    .first()
    .click();
  await expect(page.locator(".editor-bar")).toBeVisible();
  release();
  await page.clock.runFor(2_000);
  await page.waitForLoadState("networkidle");
  expect(executions(calls)).toEqual([]);
  expect(saved).toEqual([]);
  expect(await kept(page)).toBeNull();
  await expect(toast(page)).toHaveCount(0);
});

test("a still-running run copies what it finished", async ({ page }) => {
  const { runs } = await openDraftRuns(page);
  await localAuthoring(page);
  // Opening the tab chose the newest run: the running one.
  await expect(runs.canvas.locator(".run-header")).toContainText(
    "Following this run",
  );
  await runs.canvas.getByRole("button", { name: "Copy to editor" }).click();
  await expect(toast(page)).toContainText(
    "Test data now holds run 4f3a0cbd's data.",
  );
  const data = await kept(page);
  expect(Object.keys(data.pins)).toEqual(["fetch"]);
  expect(data.humans).toBeUndefined();
  expect(data.signals).toBeUndefined();
  // Execute stops where the run's data ends: approve has no answer yet.
  await expect(new StepDetailsPage(page).dialogFor("approve")).toBeVisible();
});

test("the recomputed input matches what the simulation gave the step", async ({
  page,
}) => {
  const { runs } = await openDraftRuns(page);
  await localAuthoring(page);
  await choose(page, runs, RUN_IDS.failed);
  const details = new StepDetailsPage(page);
  await details.openWithKeyboard("send");
  const received = details.dialog.locator(
    '[data-region="input"] .pane-received',
  );
  await expect(received).toBeVisible();
  const recomputed = JSON.parse((await received.textContent()) ?? "null");
  expect(recomputed).toEqual({ customer: "Ada", total: 120.5 });
  await details.close();

  const answer = page.waitForResponse("**/studio/local/debug/execute");
  await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
  const trace = (
    (await (await answer).json()) as {
      trace: { nodeId: string; input?: unknown }[];
    }
  ).trace;
  expect(trace.find((entry) => entry.nodeId === "send")?.input).toEqual(
    recomputed,
  );
});
