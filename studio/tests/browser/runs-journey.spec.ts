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
// The whole journey with nothing stored at the start: a workflow's runs, the
// test runs hidden and shown again, the failed run on the canvas, what two of
// its steps did, Debug in editor stopping where it failed, and then, from
// Operate, an older version's run opened in the newest draft.
import { expect, test, type Page } from "@playwright/test";
import { hasPython, localAuthoring } from "./local-authoring";
import {
  keepDrafts,
  listedKinds,
  openRunsTab,
  RUN_IDS,
  RUNS_ACCOUNT,
  runsWorkflow,
  WORKER_CATALOG,
  type RunsPage,
} from "./runs-po";
import { StepDetailsPage } from "./step-details-po";
import { showView } from "./support";

test.skip(
  !hasPython,
  "Execute runs on the Studio host's Python, not found here.",
);

/** On a narrow window, step details show one pane at a time. */
async function showPane(details: StepDetailsPage, name: "Input" | "Output") {
  const tab = details.dialog
    .getByRole("tablist", { name: "Step details panes" })
    .getByRole("tab", { name });
  if (await tab.isVisible()) await tab.click();
}
async function choose(page: Page, runs: RunsPage, id: string) {
  const back = page.getByRole("button", { name: "Back to runs" });
  if (await back.isVisible()) await back.click();
  await runs.row(id).click();
  await expect(runs.canvas.locator(".run-header h2")).toHaveText(
    `Run ${id.slice(0, 8)}`,
  );
  await expect(runs.canvas.locator(".run-header .run-caption")).toBeVisible();
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test(`from the Runs tab to the failing step and back from Operate at ${viewport.width}x${viewport.height}`, async ({
    page,
  }) => {
    test.setTimeout(120_000);
    await page.setViewportSize(viewport);
    // Each test starts in a fresh browser context: nothing is stored.
    const { runs } = await openRunsTab(page, {
      account: RUNS_ACCOUNT,
      catalog: WORKER_CATALOG,
    });
    const id = /\/workflows\/([^/]+)\/designer/.exec(
      new URL(page.url()).pathname,
    )![1];
    await keepDrafts(page, [{ id, document: runsWorkflow }]);
    await localAuthoring(page);
    await page.goto(`/workflows/${id}/designer?tab=runs`);
    await expect(runs.rows).toHaveCount(listedKinds.length + 1);

    // The test run hides and comes back.
    const back = page.getByRole("button", { name: "Back to runs" });
    if (await back.isVisible()) await back.click();
    const tests = runs.panel.getByRole("switch", {
      name: "Include test runs",
    });
    await tests.click();
    await expect(runs.row(RUN_IDS.test)).toHaveCount(0);
    await tests.click();
    await expect(runs.row(RUN_IDS.test)).toBeVisible();

    // The failed run: its header and what each step did.
    await choose(page, runs, RUN_IDS.failed);
    await expect(runs.canvas.locator(".run-header")).toContainText(
      "Failed at send: SMTP_REJECTED",
    );
    await expect(
      runs.canvas.locator('.tile-node[data-tile="send"] .tile-output'),
    ).toHaveText("SMTP_REJECTED");
    const details = new StepDetailsPage(page);
    await details.openWithKeyboard("fetch");
    await showPane(details, "Output");
    await details.dialog
      .locator('[data-region="output"]')
      .getByRole("radio", { name: "JSON" })
      .click();
    await expect(
      details.dialog.locator('[data-region="output"] .pane-json'),
    ).toContainText('"Ada"');
    await details.close();
    await details.openWithKeyboard("send");
    await showPane(details, "Input");
    await expect(
      details.dialog.locator('[data-region="input"] .pane-received'),
    ).toHaveText(JSON.stringify({ customer: "Ada", total: 120.5 }, null, 2));
    await details.close();

    // Debug in editor: step details at send, with the run's error.
    await runs.canvas.getByRole("button", { name: "Debug in editor" }).click();
    await expect(details.dialogFor("send")).toBeVisible();
    await showPane(details, "Output");
    await expect(details.dialog).toContainText(
      "In run 1c0d7f8a, this step failed: SMTP_REJECTED.",
    );
    await details.close();

    // From Operate: the older version's run opens in this draft, the newest.
    await page.goto(`/operate/runs/${RUN_IDS.older}`);
    const detail = page.locator(".record-detail");
    await expect(detail.locator("#record-detail-title")).toHaveText(
      "invoice-approval",
    );
    await detail.getByRole("button", { name: "Copy to editor" }).click();
    await expect(page).toHaveURL(new RegExp(`/workflows/${id}/designer`));
    // The draft holds the failed run's data: Merge keeps what this run
    // didn't reach.
    await page
      .getByRole("dialog", { name: "Use run 2d1e8a9b's data as test data?" })
      .getByRole("button", { name: "Merge" })
      .click();
    await expect(page.locator(".toast")).toContainText(
      "Test data now holds run 2d1e8a9b's data.",
    );
    await showView(page, "Runs");
    await choose(page, runs, RUN_IDS.older);
    await expect(runs.canvas.locator(".run-banner")).toContainText(
      viewport.width < 768
        ? "This run used 1.2.0."
        : "This run used 1.2.0; you're editing the draft of 1.3.0.",
    );
  });
