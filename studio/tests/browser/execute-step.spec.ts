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
// Execute step in step details: the split button, what it asks first, Stop,
// Ctrl/Cmd+Enter, and the answer in the Output pane. The host's simulation
// answers with the repository's own Python code; nothing reaches a platform.
import { expect, test } from "@playwright/test";
import { hasPython } from "./local-authoring";
import { readFileSync } from "node:fs";
import {
  executeFixture,
  shownExecute,
  validateWith,
  executeButton,
  executions,
  gateDialog,
  moreWays,
  openExecuteFixture,
  openExecuteWorkflow,
  pinSample,
  stopButton,
} from "./execute-po";
import { sizes, yamlFile } from "./step-details-po";
import { outputPane } from "./test-data-po";

test.skip(!hasPython, "Create the Python development environment first.");

const inputPane = async (details: {
  dialog: import("@playwright/test").Locator;
}) => {
  const dialog = details.dialog;
  const tabs = dialog
    .getByRole("tablist", { name: /^(Step details panes|Data)$/ })
    .getByRole("tab", { name: "Input" });
  if (await tabs.count()) await tabs.first().click();
  const pane = dialog.locator("weave-input-pane");
  await expect(pane).toBeVisible();
  return pane;
};

for (const size of sizes)
  test.describe(`Execute step at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("Execute step fills the Output with what the step returned", async ({
      page,
    }) => {
      const { details, calls } = await openExecuteFixture(page);
      await pinSample(details, "lookup");
      await details.open("score");
      const button = executeButton(details.dialog);
      await expect(button).toHaveText("Execute step");
      await button.click();
      const pane = await outputPane(details);
      await expect(pane.locator(".pane-source-tag")).toHaveText("Simulated");
      await pane.getByRole("radio", { name: "JSON" }).click();
      await expect(pane.locator(".pane-json")).toContainText('"tier"');
      await expect(button).toBeEnabled();
      await expect(button).toHaveText("Execute step");
      const sent = executions(calls);
      expect(sent).toHaveLength(1);
      expect(sent[0].body["target"]).toEqual({ mode: "step", stepId: "score" });
    });

    test("Execute previous steps fills the Input and stops before the step", async ({
      page,
    }) => {
      const { details, calls } = await openExecuteFixture(page);
      await pinSample(details, "lookup");
      await details.open("summarize");
      await moreWays(details.dialog).click();
      const items = page.getByRole("menuitem");
      await expect(items).toHaveCount(1);
      await expect(items.first()).toContainText("Execute previous steps");
      await items.first().click();
      await expect.poll(() => executions(calls).length).toBe(1);
      expect(executions(calls)[0].body["target"]).toEqual({
        mode: "before",
        stepId: "summarize",
      });
      const input = await inputPane(details);
      await expect(input.locator(".pane-source-tag")).toHaveText("Simulated");
      const output = await outputPane(details);
      await expect(output).toContainText("No output yet");
    });

    test("executing with an error asks to fix it first", async ({ page }) => {
      const { details, calls } = await openExecuteFixture(page);
      // The host's check finds an error in lookup, which score reads.
      await validateWith(page, [
        {
          code: "WV-SCHEMA-REQUIRED",
          severity: "error",
          stage: "schema",
          path: "/spec/steps/0/with",
          message: "Customer ID is required.",
        },
      ]);
      await details.open("score");
      await (await shownExecute(details)).click();
      const gate = gateDialog(page);
      await expect(gate.getByRole("heading")).toHaveText("Fix 1 issue first");
      const fix = gate.getByRole("button", { name: /^Fix lookup/ });
      await expect(fix).toHaveCount(1);
      await expect(fix).toBeFocused();
      await fix.click();
      await expect(gate).toBeHidden();
      await expect(details.dialogFor("lookup")).toBeVisible();
      expect(executions(calls)).toHaveLength(0);
    });

    test("executing a pinned step asks first", async ({ page }) => {
      const { details, calls } = await openExecuteFixture(page);
      await pinSample(details, "lookup");
      await details.open("lookup");
      await (await shownExecute(details)).click();
      const gate = gateDialog(page);
      await expect(gate.getByRole("heading")).toHaveText("Unpin and execute?");
      await expect(
        gate.getByRole("button", { name: "Unpin and execute" }),
      ).toBeFocused();
      await gate.getByRole("button", { name: "Cancel" }).click();
      await expect(gate).toBeHidden();
      const pane = await outputPane(details);
      await expect(pane.locator(".pinned-banner")).toBeVisible();
      expect(executions(calls)).toHaveLength(0);
      await (await shownExecute(details)).click();
      await gate.getByRole("button", { name: "Unpin and execute" }).click();
      await expect(page.locator(".toast-region")).toContainText(
        "Unpinned lookup.",
      );
      const after = await outputPane(details);
      await expect(after.locator(".pane-notice")).toContainText(
        "Step lookup needs test data.",
      );
      await expect(
        pane
          .locator(".pane-notice")
          .getByRole("button", { name: "Set test output" }),
      ).toBeVisible();
      await expect(
        pane
          .locator(".pane-notice")
          .getByRole("button", { name: "Generate sample" }),
      ).toBeVisible();
      expect(executions(calls)).toHaveLength(1);
      expect(executions(calls)[0].body["mocks"]).toEqual({});
    });

    test("Escape closes the question first", async ({ page }) => {
      const { details } = await openExecuteFixture(page);
      await pinSample(details, "lookup");
      await details.open("lookup");
      await (await shownExecute(details)).click();
      await expect(gateDialog(page)).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(gateDialog(page)).toBeHidden();
      await expect(details.dialog).toBeVisible();
      await expect(executeButton(details.dialog)).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(details.dialog).toBeHidden();
    });
  });

test("Ctrl/Cmd+Enter executes while typing; letters type", async ({ page }) => {
  const { details, calls } = await openExecuteFixture(page);
  await details.open("lookup");
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "Search output" }).click();
  const field = pane.getByRole("textbox", { name: "Search output" });
  await expect(field).toBeFocused();
  await page.keyboard.type("abp");
  await page.keyboard.press("ControlOrMeta+Enter");
  await expect.poll(() => executions(calls).length).toBe(1);
  await expect(field).toHaveValue("abp");
  await expect(page.locator(".toast-region")).not.toContainText(
    "Set test output or generate a sample to pin it.",
  );
});

test("Stop drops a slow answer", async ({ page }) => {
  const { details, calls } = await openExecuteFixture(page);
  await pinSample(details, "lookup");
  await details.open("score");
  let release: () => void = () => undefined;
  const held = new Promise<void>((done) => (release = done));
  await page.route("**/studio/local/debug/execute", async (route) => {
    await held;
    await route.fallback();
  });
  await (await shownExecute(details)).click();
  const stop = stopButton(details.dialog);
  await expect(stop).toBeVisible();
  await stop.click();
  await expect(executeButton(details.dialog)).toHaveText("Execute step");
  release();
  await expect.poll(() => executions(calls).length).toBe(1);
  await page.waitForTimeout(300);
  const pane = await outputPane(details);
  await expect(pane.locator(".pane-source-tag")).toHaveCount(0);
});

const DECISION = yamlFile(
  "decision.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: routed, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      amount: {type: number, title: Amount}
  outputSchema: {}
  steps:
    - id: route
      kind: switch
      cases:
        - when: {op: {name: gt, args: [{ref: /input/amount}, {literal: 1000}]}}
          steps:
            - id: big
              kind: transform
              value: {literal: 1}
          output: {literal: null}
      default: {steps: [], output: {literal: null}}
  output: {literal: null}
`,
);

test("a step the test data doesn't reach says which path was taken", async ({
  page,
}) => {
  const { details } = await openExecuteWorkflow(page, DECISION);
  await details.open("big");
  await (await shownExecute(details)).click();
  const pane = await outputPane(details);
  await expect(pane.locator(".pane-notice")).toContainText(
    "With this test data, route takes Otherwise, so big isn't reached.",
  );
});

test("a secret in test input is refused and named, never shown", async ({
  page,
}) => {
  const answers: string[] = [];
  page.on("response", async (response) => {
    if (response.url().includes("/studio/local/debug/execute"))
      answers.push(await response.text().catch(() => ""));
  });
  const { details, calls } = await openExecuteFixture(page);
  // Studio's form never takes a secret, so the test puts one in the request the
  // host receives; the repository's real host code answers it.
  await page.route("**/studio/local/debug/execute", (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    return route.fallback({
      postData: JSON.stringify({
        ...body,
        input: { customerId: "c-1", amount: 1, password: "hunter2" },
      }),
    });
  });
  await pinSample(details, "lookup");
  await details.open("score");
  await (await shownExecute(details)).click();
  const pane = await outputPane(details);
  await expect(pane.locator(".pane-notice")).toContainText(
    "The test input holds a value marked secret. Remove it from the test input.",
  );
  expect(JSON.stringify(executions(calls).at(-1)!.body)).toContain("hunter2");
  expect(answers.length).toBeGreaterThan(0);
  for (const answer of answers) expect(answer).not.toContain("hunter2");
  await expect(details.dialog).not.toContainText("hunter2");
  await expect(page.locator(".toast-region")).not.toContainText("hunter2");
  // Nor in this computer's storage, the address, or anywhere on the page.
  const stored = await page.evaluate(() =>
    [localStorage, sessionStorage]
      .flatMap((store) =>
        Object.keys(store).map((key) => `${key}=${store.getItem(key)}`),
      )
      .join("\n"),
  );
  expect(stored).not.toContain("hunter2");
  expect(page.url()).not.toContain("hunter2");
  await expect(page.locator("body")).not.toContainText("hunter2");
});

test("Execute step on an earlier step uses only the test data of the steps it runs", async ({
  page,
}) => {
  const { details, calls } = await openExecuteFixture(page);
  await pinSample(details, "lookup");
  await pinSample(details, "notify");
  await details.open("score");
  await (await shownExecute(details)).click();
  const pane = await outputPane(details);
  await expect(pane.locator(".pane-source-tag")).toHaveText("Simulated");
  expect(Object.keys(executions(calls)[0].body["mocks"] as object)).toEqual([
    "node:lookup",
  ]);
});

test("Ctrl/Cmd+Enter in a field being edited executes what it holds", async ({
  page,
}) => {
  const { details, calls } = await openExecuteFixture(page);
  await details.open("summarize");
  const prompt = details.dialog.getByRole("textbox", { name: /^Prompt/ });
  await prompt.click();
  await page.keyboard.press("End");
  await page.keyboard.type(" quickly");
  await page.keyboard.press("ControlOrMeta+Enter");
  await expect.poll(() => executions(calls).length).toBe(1);
  expect(String(executions(calls)[0].body["source"])).toContain(
    "Summarize the order quickly",
  );
});

test("switching drafts while executing leaves the other draft's panes alone", async ({
  page,
}) => {
  const { details, calls } = await openExecuteFixture(page);
  await pinSample(details, "lookup");
  await details.open("score");
  let release: () => void = () => undefined;
  const held = new Promise<void>((done) => (release = done));
  await page.route("**/studio/local/debug/execute", async (route) => {
    await held;
    await route.fallback();
  });
  await (await shownExecute(details)).click();
  await expect(stopButton(details.dialog)).toBeVisible();
  await details.close();
  // Another workflow opens (a copy under another name) while the host is still answering.
  const copy = readFileSync(executeFixture, "utf8").replace(
    "name: tiered-orders",
    "name: other-orders",
  );
  await page
    .getByRole("link", { name: "Home" })
    .or(page.getByRole("button", { name: "Home" }))
    .first()
    .click();
  const leaving = page.getByRole("button", { name: /^(Leave|Keep|Discard)/ });
  if (
    await leaving
      .first()
      .isVisible()
      .catch(() => false)
  )
    await leaving.first().click();
  await page
    .getByLabel("Choose a workflow file")
    .setInputFiles(yamlFile("other.yaml", copy));
  const leave = page.getByRole("button", { name: /^(Leave|Open|Discard)/ });
  if (
    await leave
      .first()
      .isVisible()
      .catch(() => false)
  )
    await leave.first().click();
  await expect(page.locator(".editor-bar")).toContainText("other-orders");
  const canvas = page.getByLabel("Workflow canvas", { exact: true });
  const show = page.getByRole("button", { name: "Show canvas", exact: true });
  await expect(canvas.or(show).first()).toBeVisible();
  if (await show.isVisible()) await show.click();
  await expect(canvas).toBeVisible();
  const settings = page.getByRole("dialog", { name: "Workflow settings" });
  // A workflow opened from a file may open its settings a moment later.
  await settings
    .waitFor({ state: "visible", timeout: 3000 })
    .catch(() => undefined);
  if (await settings.isVisible().catch(() => false)) {
    await settings.getByRole("button", { name: "Close" }).click();
    await expect(settings).toBeHidden();
  }
  release();
  await expect.poll(() => executions(calls).length).toBe(1);
  await page.waitForTimeout(300);
  await details.open("score");
  const pane = await outputPane(details);
  await expect(pane.locator(".pane-source-tag")).toHaveCount(0);
  await expect(pane.locator(".pane-notice")).toHaveCount(0);
});
