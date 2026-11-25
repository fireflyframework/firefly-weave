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
// A simulated failure in step details: a step pasted with its hand-set
// failure brings it into the test data, and the Output pane says the step
// fails when executed in Studio, with its code, and removes it with Undo.
import { expect, test, type Page } from "@playwright/test";
import { openNewWorkflow } from "./canvas-po";
import { StepDetailsPage } from "./step-details-po";
import { outputPane } from "./test-data-po";
import { localAuthoring } from "./local-authoring";

test.use({ permissions: ["clipboard-read", "clipboard-write"] });

const pasted = (onError: string | null, errors?: object) =>
  JSON.stringify({
    weaveClipboard: 1,
    steps: [
      {
        id: "submit",
        kind: "transform",
        value: { object: { sent: { literal: true } } },
        ...(onError ? { onError } : {}),
      },
    ],
    connections: {},
    llmProfiles: {},
    pins: {},
    ...(errors ? { errors } : {}),
  });
const PASTED = pasted("continue", {
  submit: { code: "REQUEST_REFUSED", outcome: "unknown", source: "manual" },
});
/** A new workflow with submit pasted in, its step details open on Output. */
async function pastedStep(page: Page, text: string) {
  const canvas = await openNewWorkflow(page);
  await page.evaluate((value) => navigator.clipboard.writeText(value), text);
  await canvas.root.focus();
  await page.keyboard.press("ControlOrMeta+v");
  await expect(canvas.tile("submit")).toBeVisible();
  const details = new StepDetailsPage(page);
  await details.open("submit");
  return outputPane(details);
}

test("a pasted step keeps its simulated failure, shown over its output and removed with Undo", async ({
  page,
}) => {
  const pane = await pastedStep(page, PASTED);
  const banner = pane.locator("weave-failure-banner");
  await expect(banner.getByRole("status")).toHaveText(
    "This step fails when you execute it in Studio. Real runs ignore it.",
  );
  // A transform's failure didn't start, whatever outcome was pasted with it:
  // the platform reports it so.
  await expect(banner.locator(".failure-code")).toHaveText(
    "REQUEST_REFUSED · Didn't start",
  );
  await expect(
    pane.locator(".pill", { hasText: "Fails when executed" }),
  ).toBeVisible();
  await banner.getByRole("button", { name: "Remove" }).click();
  await expect(banner).toHaveCount(0);
  const toast = page
    .locator(".toast")
    .filter({ hasText: "Removed the simulated failure of submit." });
  await toast.getByRole("button", { name: "Undo" }).click();
  await expect(banner.locator(".failure-code")).toHaveText(
    "REQUEST_REFUSED · Didn't start",
  );
});

test("Simulate a failure… sets the code, checking it first; a transform's failure didn't start, before Execute and after it", async ({
  page,
}) => {
  const pane = await pastedStep(page, pasted("continue"));
  const more = pane.getByRole("button", { name: "More output actions" });
  await more.click();
  await page.getByRole("menuitem", { name: "Simulate a failure…" }).click();
  const dialog = page.getByRole("dialog", {
    name: "Simulate a failure of submit",
  });
  await expect(dialog).toBeVisible();
  const code = dialog.getByLabel("Code");
  // It starts on the first suggestion, selected, so typing replaces it.
  await expect(code).toBeFocused();
  await expect(code).toHaveValue("WV-EXPR-MISSING");
  // A transform changes nothing outside the run: the platform says its
  // failure didn't start, so there is no outcome to choose.
  await expect(dialog.getByRole("group", { name: "Outcome" })).toHaveCount(0);
  await page.keyboard.type("request refused");
  await dialog.getByRole("button", { name: "Simulate failure" }).click();
  await expect(dialog.getByRole("alert")).toHaveText(
    "Use capital letters, digits, dots, dashes and underscores, starting with a letter.",
  );
  await code.fill("REQUEST_REFUSED");
  await dialog.getByRole("button", { name: "Simulate failure" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(more).toBeFocused();
  const banner = pane.locator("weave-failure-banner");
  await expect(banner.locator(".failure-code")).toHaveText(
    "REQUEST_REFUSED · Didn't start",
  );
  // Edit opens it on the failure as it is; Escape leaves it unchanged.
  await banner.getByRole("button", { name: "Edit" }).click();
  await expect(dialog.getByLabel("Code")).toHaveValue("REQUEST_REFUSED");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(banner.locator(".failure-code")).toHaveText(
    "REQUEST_REFUSED · Didn't start",
  );
  // Executed by the real Python host, it reads the same after Execute.
  const details = new StepDetailsPage(page);
  await details.close();
  const calls = await localAuthoring(page);
  await page
    .locator(".editor-bar")
    .getByRole("button", { name: "Execute workflow" })
    .click();
  await expect.poll(() => calls.length).toBeGreaterThan(0);
  expect((calls[0].body as { errors?: object }).errors).toEqual({
    "node:submit": { code: "REQUEST_REFUSED", outcome: "not_started" },
  });
  await details.open("submit");
  const after = await outputPane(details);
  await expect(after.locator("weave-error-banner p").nth(1)).toHaveText(
    "Outcome: Didn't start. Attempts: 1.",
  );
});

test("an item's failure edited to every item becomes the step's, with nothing to undo", async ({
  page,
}) => {
  const canvas = await openNewWorkflow(page);
  await page.evaluate(
    (value) => navigator.clipboard.writeText(value),
    JSON.stringify({
      weaveClipboard: 1,
      steps: [
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
            steps: [
              {
                id: "send",
                kind: "transform",
                value: { literal: { sent: true } },
                onError: "continue",
              },
            ],
            output: { ref: "/item" },
          },
        },
      ],
      connections: {},
      llmProfiles: {},
      pins: {},
      errors: { "send[2]": { code: "REQUEST_REFUSED", source: "manual" } },
    }),
  );
  await canvas.root.focus();
  await page.keyboard.press("ControlOrMeta+v");
  await expect(canvas.tile("fetch")).toBeVisible();
  const details = new StepDetailsPage(page);
  await details.openWithKeyboard("send");
  const pane = await outputPane(details);
  const items = pane.getByRole("group", { name: "Items of orders" });
  for (let i = 0; i < 2; i++)
    await items.getByRole("button", { name: "Next item" }).click();
  await expect(
    items.getByRole("button", { name: /Choose an item$/ }),
  ).toHaveAccessibleName(/^Item 3\b/);
  const banner = pane.locator("weave-failure-banner");
  await expect(banner.getByRole("status")).toHaveText(
    "This item fails when you execute it in Studio. Real runs ignore it.",
  );
  await banner.getByRole("button", { name: "Edit" }).click();
  const dialog = page.getByRole("dialog", {
    name: "Simulate a failure of send",
  });
  await dialog.getByRole("radio", { name: "Every item" }).check();
  await dialog.getByRole("button", { name: "Simulate failure" }).click();
  await expect(banner.getByRole("status")).toHaveText(
    "Every item fails when you execute it in Studio. Real runs ignore it.",
  );
  // Edit opens on the failure for every item. By then a toast for the edit
  // would show: there is none, as the edit removed nothing to undo (the
  // paste's own toast may still be up).
  await banner.getByRole("button", { name: "Edit" }).click();
  await expect(dialog.getByRole("radio", { name: "Every item" })).toBeChecked();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".toast", { hasText: "Removed" })).toHaveCount(0);
});

test("a transform that stops the run says why it can't simulate a failure", async ({
  page,
}) => {
  const pane = await pastedStep(page, pasted(null));
  await pane.getByRole("button", { name: "More output actions" }).click();
  const item = page.getByRole("menuitem", { name: /^Simulate a failure…/ });
  await expect(item).toHaveAttribute("aria-disabled", "true");
  await expect(item).toContainText(
    "This step's failure would stop the run. Choose Continue or Continue with error output first.",
  );
});

test("inside the error path, the Input pane starts on the failed step's error, simulated", async ({
  page,
}) => {
  const canvas = await openNewWorkflow(page);
  const text = JSON.stringify({
    weaveClipboard: 1,
    steps: [
      {
        id: "submit",
        kind: "transform",
        value: { object: { sent: { literal: true } } },
        onError: "errorOutput",
        errorPath: {
          steps: [
            {
              id: "notify-team",
              kind: "transform",
              value: { ref: "/steps/submit/error/code" },
            },
          ],
          output: { literal: null },
        },
      },
    ],
    connections: {},
    llmProfiles: {},
    pins: {},
    errors: { submit: { code: "WV-EXPR-MISSING", source: "manual" } },
  });
  await page.evaluate((value) => navigator.clipboard.writeText(value), text);
  await canvas.root.focus();
  await page.keyboard.press("ControlOrMeta+v");
  await expect(canvas.tile("notify-team")).toBeVisible();
  const details = new StepDetailsPage(page);
  await details.open("notify-team");
  const tabs = details.dialog
    .getByRole("tablist", { name: /^(Step details panes|Data)$/ })
    .getByRole("tab", { name: "Input" });
  if (await tabs.count()) await tabs.first().click();
  const pane = details.dialog.locator("weave-input-pane");
  await expect(pane.getByLabel("Input source")).toContainText(
    "Error of submit",
  );
  await expect(
    pane.locator(".pane-source-tag", { hasText: "Simulated failure" }),
  ).toBeVisible();
  await pane.getByRole("radio", { name: "JSON" }).click();
  await expect(pane).toContainText("A value the step reads is missing.");
});

test("Execute workflow fails the step as simulated, and its tile says it failed and continued", async ({
  page,
}) => {
  const canvas = await openNewWorkflow(page);
  await page.evaluate(
    (value) => navigator.clipboard.writeText(value),
    pasted("continue", {
      submit: { code: "REQUEST_REFUSED", source: "manual" },
    }),
  );
  await canvas.root.focus();
  await page.keyboard.press("ControlOrMeta+v");
  await expect(canvas.tile("submit")).toBeVisible();
  // The real Python host executes it.
  const calls = await localAuthoring(page);
  await page
    .locator(".editor-bar")
    .getByRole("button", { name: "Execute workflow" })
    .click();
  await expect.poll(() => calls.length).toBeGreaterThan(0);
  expect((calls[0].body as { errors?: object }).errors).toEqual({
    "node:submit": { code: "REQUEST_REFUSED" },
  });
  const badge = canvas
    .tile("submit")
    .locator('.tile-badge[data-badge="handled"]');
  await expect(badge).toBeVisible();
  await expect(badge).toHaveAttribute("data-tone", "warning");
  // Under it, the code alone in the warning tone; its name says the rest.
  const line = canvas.tile("submit").locator(".tile-output");
  await expect(line).toHaveText("REQUEST_REFUSED");
  await expect(line).toHaveAttribute("data-tone", "warning");
  await expect(canvas.tileBody("submit")).toHaveAccessibleName(
    /^submit, continues on error, Transform, .*failed and continued, REQUEST_REFUSED, step 1 of 1 in Main sequence$/,
  );
  // Its tooltip says what failed, and that the run went on.
  await expect(canvas.tileBody("submit")).toHaveAttribute(
    "title",
    "submit, Transform. Failed: REQUEST_REFUSED, The step failed with code REQUEST_REFUSED. The run continued (On error: Continue).",
  );
});
