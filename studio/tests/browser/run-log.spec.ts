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
// The run log beside the canvas: what ran, entries that open their step,
// out-of-date marks, the questions a run asks when the test data doesn't
// answer a signal or a person, and stepping through with breakpoints.
import { expect, test, type Page } from "@playwright/test";
import {
  executions,
  moreWays,
  openExecuteWorkflow,
  shownExecute,
} from "./execute-po";
import { hasPython } from "./local-authoring";
import { applySource } from "./owned-actions";
import { yamlFile } from "./step-details-po";
import { sourceText } from "./support";
import { outputPane } from "./test-data-po";

test.skip(!hasPython, "Create the Python development environment first.");
test.use({ viewport: { width: 1440, height: 900 } });

const CHAIN = yamlFile(
  "chain.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: chain, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      amount: {type: number, title: Amount}
  outputSchema: {}
  steps:
    - id: first
      kind: transform
      value: {object: {amount: {ref: /input/amount}}}
    - id: second
      kind: transform
      value: {object: {copied: {ref: /steps/first/output/amount}}}
    - id: third
      kind: transform
      value: {object: {again: {ref: /steps/second/output/copied}}}
  output: {literal: null}
`,
);
const ASKING = yamlFile(
  "asking.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: asking, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - {id: pause, kind: wait, durationSeconds: 300}
    - id: paid
      kind: signal
      name: order-paid
      timeoutSeconds: 600
      payloadSchema: {type: object}
    - id: approval
      kind: humanTask
      assignment: reviewers
      title: {literal: Approve}
      context: {literal: {}}
      formSchema: {type: object}
      decisions: [approve, reject]
  output: {literal: null}
`,
);
const runLog = (page: Page) => page.getByRole("region", { name: "Run log" });
const entry = (page: Page, id: string) =>
  runLog(page).locator(`[data-entry="${id}"]`);
const executeInBar = (page: Page) =>
  page.locator(".editor-bar").getByRole("button", { name: "Execute workflow" });
const tileBody = (page: Page, id: string) =>
  page.locator(`[data-step="${id}"] .tile-body`).first();

test("the run log lists what ran, opens a step, and marks out-of-date entries", async ({
  page,
}) => {
  const { details } = await openExecuteWorkflow(page, CHAIN);
  await executeInBar(page).click();
  const log = runLog(page);
  await expect(log.locator(".run-log-status")).toHaveText(
    "Completed · 3 steps",
  );
  await expect(log.locator(".run-log-entry")).toHaveCount(3);
  await expect(entry(page, "second")).toContainText("1 field");
  await entry(page, "second").focus();
  await page.keyboard.press("Enter");
  await expect(details.dialogFor("second")).toBeVisible();
  await details.close();
  // second reads another field now: it and what reads it are out of date.
  const source = await sourceText(page);
  await applySource(
    page,
    source.replace(
      "copied: {ref: /steps/first/output/amount}",
      "copied: {ref: /steps/first/output}",
    ),
  );
  await expect(entry(page, "second")).toContainText("Out of date");
  await expect(entry(page, "first")).not.toContainText("Out of date");
});

test("a signal and a person the test data doesn't answer are asked in the run log, saved, and not asked again", async ({
  page,
}) => {
  const { calls } = await openExecuteWorkflow(page, ASKING);
  await executeInBar(page).click();
  const log = runLog(page);
  await expect(log.locator(".run-log-status")).toHaveText(
    "Waiting · Send signal order-paid",
  );
  await log.getByLabel("Payload (JSON)").fill('{"paid": true}');
  await log.getByRole("button", { name: "Send" }).click();
  await expect(log.locator(".run-log-status")).toHaveText(
    "Waiting · Decide for approval",
  );
  await log.getByRole("radio", { name: "approve" }).check();
  await log.getByRole("button", { name: "Decide" }).click();
  await expect(log.locator(".run-log-status")).toHaveText(
    "Completed · 3 steps",
  );
  // The answers are test data now: the next execution asks nothing.
  await executeInBar(page).click();
  await expect.poll(() => executions(calls).length).toBe(4);
  await expect(log.locator(".run-log-status")).toHaveText(
    "Completed · 3 steps",
  );
  const scripts = executions(calls).at(-1)!.body["scripts"] as {
    signals: Record<string, { payload: unknown }>;
    humans: Record<string, { decision: string }>;
  };
  expect(scripts.signals["paid"].payload).toEqual({ paid: true });
  expect(scripts.humans["approval"].decision).toBe("approve");
});

test("Execute previous steps opens the run log paused at the step", async ({
  page,
}) => {
  const { details } = await openExecuteWorkflow(page, CHAIN);
  await details.open("second");
  await shownExecute(details);
  await moreWays(details.dialog).click();
  await page.getByRole("menuitem", { name: /Execute previous steps/ }).click();
  await expect(runLog(page).locator(".run-log-status")).toHaveText(
    "Paused before second",
  );
});

test("Next steps one step, Continue runs to a breakpoint set with F9", async ({
  page,
}) => {
  await openExecuteWorkflow(page, CHAIN);
  await tileBody(page, "second").click();
  await page.keyboard.press("F9");
  await executeInBar(page).click();
  const log = runLog(page);
  await expect(log.locator(".run-log-status")).toHaveText(
    "Paused before second",
  );
  await expect(
    entry(page, "second").locator(".run-log-breakpoint"),
  ).toHaveCount(1);
  await log.getByRole("button", { name: "Next", exact: true }).click();
  await expect(log.locator(".run-log-status")).not.toHaveText(
    "Paused before second",
  );
  await log.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(log.locator(".run-log-status")).toHaveText(/^Completed/);
  await log.getByRole("button", { name: "Stop", exact: true }).click();
  await expect(
    log.getByRole("button", { name: "Next", exact: true }),
  ).toHaveCount(0);
});

test("L toggles the run log, I and O open an entry's data", async ({
  page,
}) => {
  const { details } = await openExecuteWorkflow(page, CHAIN);
  await executeInBar(page).click();
  await expect(runLog(page)).toBeVisible();
  await tileBody(page, "first").click();
  await page.keyboard.press("l");
  await expect(runLog(page)).toHaveCount(0);
  await tileBody(page, "first").click();
  await page.keyboard.press("l");
  await expect(runLog(page)).toBeVisible();
  await entry(page, "second").focus();
  await page.keyboard.press("o");
  await expect(details.dialogFor("second")).toBeVisible();
  await expect(details.dialog.locator('[data-region="output"]')).toBeVisible();
});

test("a stale session revision shows the latest without losing the person's place", async ({
  page,
}) => {
  await openExecuteWorkflow(page, CHAIN);
  await tileBody(page, "second").click();
  await page.keyboard.press("F9");
  await executeInBar(page).click();
  const log = runLog(page);
  await expect(log.locator(".run-log-status")).toHaveText(
    "Paused before second",
  );
  await page.route("**/studio/local/debug/sessions/*/commands", (route) =>
    route.fulfill({
      status: 409,
      json: { status: 409, code: "WV-DEBUG-REVISION", message: "Changed" },
    }),
  );
  await log.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page.locator(".toast-region")).toContainText(
    "The run log changed; showing the latest.",
  );
  await expect(log.locator(".run-log-status")).toHaveText(
    "Paused before second",
  );
});

test("the classic simulation panel isn't used with the new editor", async ({
  page,
}) => {
  const platform: string[] = [];
  page.on("request", (request) => {
    if (/\/api\/v1\/.*\/debug\/sessions/.test(request.url()))
      platform.push(request.url());
  });
  await openExecuteWorkflow(page, CHAIN);
  await executeInBar(page).click();
  await expect(runLog(page)).toBeVisible();
  await expect(
    page.getByRole("dialog", { name: "Simulate this workflow" }),
  ).toHaveCount(0);
  expect(platform).toEqual([]);
});

test("a signal's test answer edited in step details is used by the next execution", async ({
  page,
}) => {
  const { details, calls } = await openExecuteWorkflow(page, ASKING);
  await details.open("paid");
  const pane = await outputPane(details);
  const answer = pane.getByRole("region", { name: "Test answer" });
  await answer.getByLabel("Payload (JSON)").fill('{"paid": true}');
  await answer.getByRole("button", { name: "Save answer" }).click();
  await expect(
    answer.getByRole("button", { name: "Remove answer" }),
  ).toBeVisible();
  await details.close();
  await executeInBar(page).click();
  await expect(runLog(page).locator(".run-log-status")).toHaveText(
    "Waiting · Decide for approval",
  );
  const scripts = executions(calls).at(-1)!.body["scripts"] as {
    signals: Record<string, { payload: unknown }>;
  };
  expect(scripts.signals["paid"].payload).toEqual({ paid: true });
});

test("letting a person's task expire takes its expiry path", async ({
  page,
}) => {
  const expiring = yamlFile(
    "expiring.yaml",
    `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: expiring, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - id: approval
      kind: humanTask
      assignment: reviewers
      title: {literal: Approve}
      context: {literal: {}}
      formSchema: {type: object}
      decisions: [approve, reject]
      expirySeconds: 3600
  output: {literal: null}
`,
  );
  const { details } = await openExecuteWorkflow(page, expiring);
  await details.open("approval");
  const pane = await outputPane(details);
  const answer = pane.getByRole("region", { name: "Test answer" });
  await answer.getByLabel("Let it expire instead").check();
  await answer.getByRole("button", { name: "Save answer" }).click();
  await expect(
    answer.getByRole("button", { name: "Remove answer" }),
  ).toBeVisible();
  await details.close();
  await executeInBar(page).click();
  await expect(runLog(page).locator(".run-log-status")).toHaveText(
    "Failed at approval",
  );
});

test("an answer holding a value marked secret is refused and never sent", async ({
  page,
}) => {
  const secret = yamlFile(
    "secret-signal.yaml",
    `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: secret-signal, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - id: paid
      kind: signal
      name: order-paid
      timeoutSeconds: 600
      payloadSchema:
        type: object
        properties:
          code: {type: string, x-secret: true}
  output: {literal: null}
`,
  );
  const { calls } = await openExecuteWorkflow(page, secret);
  await executeInBar(page).click();
  const log = runLog(page);
  await expect(log.locator(".run-log-status")).toHaveText(
    "Waiting · Send signal order-paid",
  );
  await log.getByLabel("Payload (JSON)").fill('{"code": "7731"}');
  await log.getByRole("button", { name: "Send" }).click();
  await expect(page.locator(".toast-region")).toContainText("secret");
  expect(executions(calls)).toHaveLength(1);
  expect(JSON.stringify(calls)).not.toContain("7731");
});
