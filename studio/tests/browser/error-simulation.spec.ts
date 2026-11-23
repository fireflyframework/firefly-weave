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
// Execute workflow with a simulated failure that a step's On error handles,
// simulated by the repository's own Python through the local authoring
// routes: the run log says the run succeeded with errors and lists the error
// path under its step, and inside a loop only the failing item fails.
import { expect, test, type Page } from "@playwright/test";
import { openNewWorkflow } from "./canvas-po";
import { hasPython, localAuthoring } from "./local-authoring";
import { StepDetailsPage } from "./step-details-po";
import { outputPane } from "./test-data-po";

test.skip(!hasPython, "Create the Python development environment first.");
test.use({
  permissions: ["clipboard-read", "clipboard-write"],
  viewport: { width: 1440, height: 900 },
});

const transform = (id: string, extra: object = {}) => ({
  id,
  kind: "transform",
  value: { literal: { from: id } },
  ...extra,
});
const REFUSED = { code: "REQUEST_REFUSED", source: "manual" };
/** Pastes steps and their simulated failures into a new workflow, then executes it. */
async function executePasted(
  page: Page,
  steps: object[],
  errors: Record<string, object>,
) {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const canvas = await openNewWorkflow(page);
  await page.evaluate(
    (value) => navigator.clipboard.writeText(value),
    JSON.stringify({
      weaveClipboard: 1,
      steps,
      connections: {},
      llmProfiles: {},
      pins: {},
      errors,
    }),
  );
  await canvas.root.focus();
  await page.keyboard.press("ControlOrMeta+v");
  await expect(canvas.tile((steps[0] as { id: string }).id)).toBeVisible();
  const calls = await localAuthoring(page);
  await page
    .locator(".editor-bar")
    .getByRole("button", { name: "Execute workflow" })
    .click();
  await expect.poll(() => calls.length).toBeGreaterThan(0);
  return { canvas, log: page.getByRole("region", { name: "Run log" }) };
}

test("the run log says the run succeeded with errors, with the error path under its step", async ({
  page,
}) => {
  const { log } = await executePasted(
    page,
    [
      transform("submit", {
        onError: "errorOutput",
        errorPath: {
          steps: [transform("notify-team"), transform("flag-request")],
          output: { ref: "/steps/flag-request/output" },
        },
      }),
      transform("summarize"),
    ],
    { submit: REFUSED },
  );
  const status = log.locator(".run-log-status");
  await expect(status).toHaveText("Succeeded with errors · 4 steps");
  await expect(status).toHaveAttribute("data-tone", "warning");
  await expect(log.locator(".run-log-outcome")).toHaveText(
    "The simulated run succeeded with 1 handled error.",
  );
  const rows = log.getByRole("list", { name: "Steps that ran" });
  await expect(rows.locator(":scope > li")).toHaveText([
    /^\s*submit\s*Failed, continued\s*REQUEST_REFUSED\s*$/,
    /^\s*Error path\s*$/,
    /^\s*notify-team\s*1 field\s*$/,
    /^\s*flag-request\s*1 field\s*$/,
    /^\s*summarize\s*1 field\s*$/,
  ]);
  await expect(rows.locator('[data-entry="submit"]')).toHaveAttribute(
    "data-status",
    "handled",
  );
  await expect(rows.locator('[data-entry="submit"]')).toHaveAccessibleName(
    "submit, failed and continued, REQUEST_REFUSED",
  );
  // The path's steps say where they are; the heading is only drawn.
  await expect(rows.locator('[data-entry="notify-team"]')).toHaveAccessibleName(
    "notify-team, in the error path of submit, finished, 1 field",
  );
  await expect(rows.locator(".run-log-path")).toHaveAttribute(
    "aria-hidden",
    "true",
  );
  // Further in than the steps outside the path.
  const indent = async (id: string) =>
    (await rows.locator(`[data-entry="${id}"] .run-log-icon`).boundingBox())!.x;
  expect(await indent("notify-team")).toBe((await indent("summarize")) + 24);
});

test("a loop of three whose second item fails and continues finishes the other two", async ({
  page,
}) => {
  const { canvas, log } = await executePasted(
    page,
    [
      {
        id: "fetch",
        kind: "transform",
        value: { literal: { rows: ["a", "b", "c"] } },
      },
      {
        id: "orders",
        kind: "forEach",
        items: { ref: "/steps/fetch/output/rows" },
        concurrency: 1,
        maxItems: 1000,
        collect: "all",
        body: {
          steps: [transform("send", { onError: "continue" })],
          output: { ref: "/item" },
        },
      },
    ],
    { "send[1]": REFUSED },
  );
  const run = canvas.root.locator(
    '.tile-node[data-tile="orders"] .frame-header .frame-run',
  );
  await expect(run).toHaveText("· 3 of 3 · 1 error handled");
  await expect(run).toHaveAttribute("data-tone", "handled");
  await expect(log.locator(".run-log-status")).toHaveText(
    "Succeeded with errors · 3 steps",
  );
  const items = log.getByRole("list", { name: "Items of orders" });
  await expect(items.locator(":scope > li > .run-log-item")).toHaveText([
    /^\s*Item 1\s*send\s*$/,
    /^\s*Item 2\s*Failed, continued at send\s*REQUEST_REFUSED\s*$/,
    /^\s*Item 3\s*send\s*$/,
  ]);
  await expect(
    items.locator(":scope > li > .run-log-item").nth(1),
  ).toHaveAttribute("data-status", "handled");
  await expect(
    items.locator(":scope > li > .run-log-item").nth(2),
  ).toHaveAttribute("data-status", "completed");
});

test("after Execute workflow, the Output pane says the step failed and continued, in one banner, over the error path's result", async ({
  page,
}) => {
  await executePasted(
    page,
    [
      transform("submit", {
        onError: "errorOutput",
        errorPath: {
          steps: [transform("flag-request")],
          output: { ref: "/steps/flag-request/output" },
        },
      }),
      transform("lookup", { onError: "continue" }),
    ],
    { submit: REFUSED, lookup: REFUSED },
  );
  const details = new StepDetailsPage(page);
  await details.open("submit");
  const pane = await outputPane(details);
  await expect(pane.locator(".pane-sub .pill").first()).toHaveText(
    "Failed, continued",
  );
  await expect(pane.locator(".pane-sub .pane-source-tag")).toHaveText(
    "Simulated · just now",
  );
  // One banner: what happened, then that it was simulated; never the setting's banner too.
  await expect(pane.locator(".pinned-banner")).toHaveCount(1);
  const banner = pane.locator("weave-error-banner");
  await expect(banner.locator("p")).toHaveText([
    "This step failed: REQUEST_REFUSED. The step failed with code REQUEST_REFUSED.",
    // A transform has no effect: the platform says it didn't start.
    "Outcome: Didn't start. Attempts: 1.",
    "The run continued (On error: Continue with error output).",
    /^\s*You simulated this failure. Real runs ignore it.\s*Remove\s*Edit\s*$/,
  ]);
  await expect(pane.locator(".pane-result-caption")).toHaveText(
    "Result on error · from flag-request",
  );
  await pane.getByRole("radio", { name: "JSON" }).click();
  await expect(pane.getByLabel("Output data")).toContainText(
    '"from": "flag-request"',
  );
  // Remove takes the failure away; the banner then says only what happened.
  await banner.getByRole("button", { name: "Remove" }).click();
  await expect(banner.locator(".error-banner-simulated")).toHaveCount(0);
  await expect(banner.getByRole("status")).toContainText(
    "This step failed: REQUEST_REFUSED.",
  );

  // Continue: the output is null, and later steps can read the error.
  await details.close();
  await details.open("lookup");
  const next = await outputPane(details);
  await expect(next.locator("weave-error-banner p").nth(2)).toHaveText(
    "The run continued (On error: Continue).",
  );
  await expect(next.locator(".pane-result-caption")).toHaveText("Output");
  await expect(next.getByLabel("Output data")).toHaveText("null");
  await expect(next.locator(".pane-result-note")).toHaveText(
    "Later steps read null here and can read lookup's error.",
  );
});

test("Execute step inside an error path its step won't take asks to simulate that step's failure first", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const canvas = await openNewWorkflow(page);
  await page.evaluate(
    (value) => navigator.clipboard.writeText(value),
    JSON.stringify({
      weaveClipboard: 1,
      steps: [
        transform("submit", {
          onError: "errorOutput",
          errorPath: {
            steps: [transform("notify-team")],
            output: { ref: "/steps/notify-team/output" },
          },
        }),
      ],
      connections: {},
      llmProfiles: {},
      pins: {},
    }),
  );
  await canvas.root.focus();
  await page.keyboard.press("ControlOrMeta+v");
  await expect(canvas.tile("notify-team")).toBeVisible();
  const calls = await localAuthoring(page);
  const details = new StepDetailsPage(page);
  await details.open("notify-team");
  const execute = details.dialog.locator(".sd-execute > button").first();
  await execute.click();
  const asked = page.getByRole("dialog", {
    name: "Simulate a failure of submit first",
  });
  await expect(asked).toBeVisible();
  await expect(asked).toHaveAccessibleDescription(
    "notify-team runs only when submit fails. With this test data submit succeeds, so notify-team isn't reached.",
  );
  await expect(
    asked
      .getByRole("list", { name: "Where notify-team is" })
      .getByRole("listitem"),
  ).toHaveText([/submit/, /Error/, /notify-team/]);
  // It changes nothing by itself, so focus starts on it; Escape is Cancel.
  const simulate = asked.getByRole("button", { name: "Simulate a failure" });
  await expect(simulate).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(asked).toHaveCount(0);
  await expect(execute).toBeFocused();
  expect(calls).toHaveLength(0);

  // Simulate a failure opens its dialog for submit; then the path runs.
  await execute.click();
  await simulate.click();
  const dialog = page.getByRole("dialog", {
    name: "Simulate a failure of submit",
  });
  await expect(dialog.getByLabel("Code")).toBeFocused();
  await dialog.getByLabel("Code").fill("REQUEST_REFUSED");
  await dialog.getByRole("button", { name: "Simulate failure" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(execute).toBeFocused();
  await execute.click();
  await expect.poll(() => calls.length).toBe(1);
  expect((calls[0].body as { errors?: object }).errors).toEqual({
    "node:submit": { code: "REQUEST_REFUSED" },
  });
  const pane = await outputPane(details);
  await pane.getByRole("radio", { name: "JSON" }).click();
  await expect(pane.getByLabel("Output data")).toContainText(
    '"from": "notify-team"',
  );
});
