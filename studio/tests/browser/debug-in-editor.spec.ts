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
  keepDrafts,
  listedKinds,
  openRunsTab,
  RUN_IDS,
  RUNS_ACCOUNT,
  RUNS_ENV,
  runsWorkflow,
  WORKER_CATALOG,
  type RunFixture,
  type RunsPage,
  type RunsPlatform,
} from "./runs-po";
import { StepDetailsPage } from "./step-details-po";
import type { CatalogEntry } from "./support";

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
  options: {
    runs?: RunFixture[];
    catalog?: CatalogEntry[];
    /** The draft the platform keeps, when not the runs' own version. */
    draft?: unknown;
  } = {},
): Promise<Opened> {
  const { runs, platform } = await openRunsTab(page, {
    runs: options.runs,
    account: RUNS_ACCOUNT,
    catalog: options.catalog ?? WORKER_CATALOG,
  });
  await expect(runs.rows).toHaveCount((options.runs ?? listedKinds).length + 1);
  const id = /\/workflows\/([^/]+)\/designer/.exec(
    new URL(page.url()).pathname,
  )![1];
  const { saved } = await keepDrafts(page, [
    { id, document: options.draft ?? runsWorkflow },
  ]);
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
  // The stopped state: what send needs, and how to go on.
  const output = outputRegion(details);
  await expect(output.locator(".pane-sub .pill")).toHaveText("Stopped here");
  await expect(output.locator(".pane-stopped-why")).toHaveText(
    "send needs test data",
  );
  await expect(output).toContainText(
    "send has no test output, so the simulation can't run it. Fix the step, then set a test output or generate a sample to go on.",
  );
  await expect(
    output.getByRole("button", { name: "Set test output" }),
  ).toBeVisible();
  await expect(
    output.getByRole("button", { name: "Generate sample" }),
  ).toBeVisible();
  await expect(output).not.toContainText("No output yet");
  // What send was given, as data: Input opens on JSON.
  await expect(
    details.dialog
      .locator('[data-region="input"]')
      .getByRole("radio", { name: "JSON" }),
  ).toHaveAttribute("aria-checked", "true");
  // Focus is in the Output region, on its first control.
  await expect
    .poll(() =>
      outputRegion(details).evaluate((region) =>
        region.contains(document.activeElement),
      ),
    )
    .toBe(true);
  await expect(toast(page).locator("strong")).toHaveText(
    "Test data now holds run 1c0d7f8a's data.",
  );
  await expect(toast(page).locator(".toast-detail")).toHaveText(
    "Execute workflow stopped at send, where the run failed.",
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

test("back on the Runs tab, the canvas shows only the run, with all its room", async ({
  page,
}) => {
  const { runs } = await openDraftRuns(page);
  await localAuthoring(page);
  await choose(page, runs, RUN_IDS.failed);
  await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
  const details = new StepDetailsPage(page);
  await expect(details.dialogFor("send")).toBeVisible();
  await details.close();
  // The run log docks beside the Designer's canvas, and the tiles show what
  // the execution gave each step.
  await expect(page.getByRole("region", { name: "Run log" })).toBeVisible();
  await expect(
    page.locator('.tile-node[data-tile="wait-for-reply"] .tile-output'),
  ).toBeVisible();
  await runs.tabs.getByRole("tab", { name: "Runs" }).click();
  await choose(page, runs, RUN_IDS.running);
  // The running run hasn't reached wait-for-reply: nothing under it, and
  // nothing from the editor's execution under fetch either.
  await expect(
    runs.canvas.locator('.tile-node[data-tile="wait-for-reply"] .tile-output'),
  ).toHaveCount(0);
  await expect(
    runs.canvas.locator('.tile-node[data-tile="fetch"] .tile-output'),
  ).toHaveCount(0);
  // send was selected in the Designer: the Runs canvas draws no selection.
  await expect(
    runs.canvas.locator('.tile-node[data-tile="send"] .tile-body'),
  ).toHaveAttribute("aria-pressed", "false");
  const right = (selector: string) =>
    page
      .locator(selector)
      .first()
      .evaluate((element) => Math.round(element.getBoundingClientRect().right));
  expect(await right("weave-runs-view")).toBe(await right(".canvas-panel"));
});

test("on the Runs tab, keys never act on the Designer's selection or pins", async ({
  page,
}) => {
  const { runs, saved } = await openDraftRuns(page);
  await localAuthoring(page);
  await choose(page, runs, RUN_IDS.failed);
  await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
  const details = new StepDetailsPage(page);
  await expect(details.dialogFor("send")).toBeVisible();
  await details.close();
  // The Designer keeps send selected, and the run's data pinned fetch.
  await expect.poll(() => savedPins(saved).at(-1)).toContain("fetch");
  const saves = saved.length;
  await runs.tabs.getByRole("tab", { name: "Runs" }).click();
  await choose(page, runs, RUN_IDS.failed);
  // The first Escape on the canvas goes back to the run's row.
  await details.node("send").focus();
  await page.keyboard.press("Escape");
  await expect(runs.row(RUN_IDS.failed)).toBeFocused();
  // P on a pinned step changes none of the draft's test data from the
  // read-only tab: it says why, and nothing is saved (a change saves after
  // 800 ms).
  await details.node("fetch").focus();
  await page.keyboard.press("p");
  await expect(
    toast(page).filter({
      hasText: "Go back to editing to change the workflow.",
    }),
  ).toBeVisible();
  await page.clock.runFor(3_000);
  expect(saved.length).toBe(saves);
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

/** The runs' workflow with a step after send, which no run reaches. */
const withNotify = {
  ...runsWorkflow,
  spec: {
    ...runsWorkflow.spec,
    steps: [
      ...runsWorkflow.spec.steps,
      {
        id: "notify",
        kind: "action",
        uses: "crm.lookup@2.0.0",
        with: { object: { customer: { literal: "ops" } } },
      },
    ],
  },
};

test("with test data present it asks, and Merge keeps what the run didn't reach", async ({
  page,
}) => {
  const { runs, saved } = await openDraftRuns(page, { draft: withNotify });
  await localAuthoring(page);
  // Samples pinned on send, where the run failed, and on notify, which the
  // run never reached.
  await runs.tabs.getByRole("tab", { name: "Designer" }).click();
  await expect(
    page.getByRole("toolbar", { name: "Workflow commands" }),
  ).toBeVisible();
  const details = new StepDetailsPage(page);
  await pinSample(details, "send");
  await pinSample(details, "notify");
  await page.clock.runFor(11_000);
  await expect
    .poll(() => savedPins(saved).at(-1)?.sort())
    .toEqual(["notify", "send"]);
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
  await expect(ask.locator(".dialog-message p")).toHaveText([
    "Your test data already has 2 entries. Merge keeps the ones for steps this run didn't reach. Replace removes them.",
    "Studio copies the run's input, fetch's output, approve's answer and wait-for-reply's signal. Execute stops at send, where the run failed. Studio works out check again from the copied data.",
    "Each entry is marked as data from a run in development, so only people who can read those runs can open it.",
  ]);
  // While the question is open the button keeps its name, unavailable.
  const main = runs.canvas.locator(".run-main");
  await expect(main).toHaveText("Debug in editor");
  await expect(main).toHaveAttribute("aria-disabled", "true");
  // Merge loses nothing the run didn't reach: the question starts on it.
  await expect(ask.getByRole("button", { name: "Merge" })).toBeFocused();
  await ask.getByRole("button", { name: "Merge" }).click();
  // send's sample is gone with the rest of what the run reached: Execute
  // stops where the run failed.
  await expect(details.dialogFor("send")).toBeVisible();
  await expect(outputRegion(details)).toContainText(
    "In run 1c0d7f8a, this step failed: SMTP_REJECTED.",
  );
  await expect
    .poll(async () => Object.keys((await kept(page))?.pins ?? {}).sort())
    .toEqual(["fetch", "notify"]);
  expect((await kept(page)).pins.notify).toMatchObject({ source: "schema" });

  // Undo puts the test data back as it was: the next save is the original.
  await toast(page).getByRole("button", { name: "Undo" }).click();
  await page.clock.runFor(11_000);
  await expect
    .poll(() => savedPins(saved).at(-1)?.sort())
    .toEqual(["notify", "send"]);
  expect(saved.at(-1)!["pins"]).toEqual(original["pins"]);
  expect(saved.at(-1)!["input"]).toEqual(original["input"]);

  // Replace removes notify's sample too.
  await details.close();
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
  const lookup = WORKER_CATALOG[0].document as unknown as {
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
    WORKER_CATALOG[1],
  ];
  const { runs, saved } = await openDraftRuns(page, { catalog: secret });
  await localAuthoring(page);
  await choose(page, runs, RUN_IDS.failed);
  await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
  await expect(toast(page).locator("strong")).toHaveText(
    "Test data now holds run 1c0d7f8a's data.",
  );
  await expect(toast(page).locator(".toast-detail")).toHaveText(
    "Left out 1 value: fetch (marked secret).",
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

test("a refused read of the run's data forgets the run and keeps nothing", async ({
  page,
}) => {
  const { runs, platform, saved } = await openDraftRuns(page);
  const calls = await localAuthoring(page);
  await choose(page, runs, RUN_IDS.failed);
  platform.outputFailure = {
    status: 403,
    code: "WV-FORBIDDEN",
    message: "Not allowed.",
  };
  await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
  await expect(runs.panel).toContainText("You need run.read in development.");
  await expect(runs.canvas.locator(".run-header")).toHaveCount(0);
  await page.clock.runFor(11_000);
  expect(saved).toEqual([]);
  expect(executions(calls)).toEqual([]);
  expect(await kept(page)).toBeNull();
});

test("a host check that fails says why, and keeps nothing", async ({
  page,
}) => {
  const { runs, saved } = await openDraftRuns(page);
  const calls = await localAuthoring(page);
  await page.route("**/studio/local/testdata/check", (route) =>
    route.fulfill({
      status: 422,
      contentType: "application/problem+json",
      body: JSON.stringify({
        status: 422,
        code: "WV-STUDIO-REQUEST",
        message: "The check couldn't read this test data.",
      }),
    }),
  );
  const unhandled: string[] = [];
  page.on("pageerror", (error) => unhandled.push(String(error)));
  await choose(page, runs, RUN_IDS.failed);
  await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
  await expect(page.getByRole("alert").locator(".toast")).toContainText(
    "Studio could not read that request. Reload the page and try again.",
  );
  // The button is ready again.
  await expect(
    runs.canvas.getByRole("button", { name: "Debug in editor" }),
  ).not.toHaveAttribute("aria-disabled", "true");
  await page.clock.runFor(11_000);
  expect(saved).toEqual([]);
  expect(executions(calls)).toEqual([]);
  expect(unhandled).toEqual([]);
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
