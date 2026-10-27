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
// Executing is a simulation on the Studio host: it never publishes the
// workflow's own actions first, never needs an account, and never asks the
// platform to run, publish or test anything.
import { expect, test } from "@playwright/test";
import {
  executions,
  openExecuteFixture,
  pinSample,
  shownExecute,
} from "./execute-po";
import { hasPython, localAuthoring } from "./local-authoring";
import { withOwnedSteps } from "./owned-actions";
import { openStepFixture, yamlFile } from "./step-details-po";
import { command } from "./support";

test.skip(!hasPython, "Create the Python development environment first.");
test.use({ viewport: { width: 1440, height: 900 } });

test("executing never publishes the workflow's own actions", async ({
  page,
}) => {
  const platform: string[] = [];
  page.on("request", (request) => {
    if (request.method() !== "GET" && request.url().includes("/api/v1/"))
      platform.push(`${request.method()} ${new URL(request.url()).pathname}`);
  });
  await withOwnedSteps(page);
  const calls = await localAuthoring(page);
  platform.length = 0;
  await command(page, "Execute workflow");
  await expect.poll(() => executions(calls).length).toBe(1);
  expect(
    platform.filter((call) =>
      /publish|\/actions|\/runs|activations|step-tests|test-activations/.test(
        call,
      ),
    ),
  ).toEqual([]);
  const definitions = executions(calls)[0].body["definitions"] as {
    metadata: { name: string };
  }[];
  expect(definitions.length).toBeGreaterThan(0);
  await expect(page.getByRole("dialog", { name: /^Publish / })).toHaveCount(0);
});

test("executing works offline with no account", async ({ page }) => {
  const details = await openStepFixture(
    page,
    yamlFile(
      "offline.yaml",
      `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: offline, version: 1.0.0}
spec:
  inputSchema: {type: object, properties: {amount: {type: number}}}
  outputSchema: {}
  steps:
    - id: first
      kind: transform
      value: {object: {amount: {ref: /input/amount}}}
  output: {literal: null}
`,
    ),
  );
  const calls = await localAuthoring(page);
  await command(page, "Execute workflow");
  await expect.poll(() => executions(calls).length).toBe(1);
  expect(executions(calls)[0].body["catalog"]).toBe("none");
  await expect(
    page.getByRole("region", { name: "Run log" }).locator(".run-log-status"),
  ).toHaveText("Completed · 1 step");
  await details.open("first");
});

test.describe("on a narrow window", () => {
  test.use({ viewport: { width: 600, height: 700 } });
  test("the workflow commands menu executes; the classic simulation isn't offered", async ({
    page,
  }) => {
    await openStepFixture(
      page,
      yamlFile(
        "offline.yaml",
        `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: offline, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - {id: first, kind: transform, value: {literal: 1}}
  output: {literal: null}
`,
      ),
    );
    const calls = await localAuthoring(page);
    const toolbar = page.getByRole("toolbar", { name: "Workflow commands" });
    await toolbar.getByRole("button", { name: "More", exact: true }).click();
    await expect(page.getByRole("menuitem", { name: /^Simulate/ })).toHaveCount(
      0,
    );
    await page.getByRole("menuitem", { name: /^Execute workflow/ }).click();
    await expect.poll(() => executions(calls).length).toBe(1);
  });
});

test("Execute asks first about text typed in Source, and executes the applied text", async ({
  page,
}) => {
  await openStepFixture(
    page,
    yamlFile(
      "typed.yaml",
      `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: typed, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - {id: first, kind: transform, value: {literal: 1}}
  output: {literal: null}
`,
    ),
  );
  const calls = await localAuthoring(page);
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  const box = page.getByRole("textbox", { name: "Workflow source" });
  await box.fill((await box.inputValue()).replace("literal: 1", "literal: 2"));
  const question = page.getByRole("dialog", {
    name: "Apply the changes typed in Source?",
  });
  await command(page, "Execute workflow");
  await question
    .getByRole("button", { name: "Keep editing", exact: true })
    .click();
  expect(executions(calls)).toHaveLength(0);
  await command(page, "Execute workflow");
  await question
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await expect.poll(() => executions(calls).length).toBe(1);
  expect(String(executions(calls)[0].body["source"])).toContain('"literal":2');
});

test("executing never asks the platform to run, publish or test anything", async ({
  page,
}) => {
  const changes: string[] = [];
  page.on("request", (request) => {
    if (
      ["POST", "PUT", "PATCH", "DELETE"].includes(request.method()) &&
      /\/(runs|activations|step-tests|test-activations|publish|debug)\b/.test(
        new URL(request.url()).pathname,
      )
    )
      changes.push(`${request.method()} ${new URL(request.url()).pathname}`);
  });
  const { details, calls } = await openExecuteFixture(page);
  await pinSample(details, "lookup");
  const answered = () => page.waitForResponse("**/studio/local/debug/execute");
  await details.open("score");
  let answer = answered();
  await (await shownExecute(details)).click();
  await answer;
  await details.close();
  answer = answered();
  await command(page, "Execute workflow");
  await answer;
  await details.open("lookup");
  await (await shownExecute(details)).click();
  // A pinned step asks first; answering executes it.
  answer = answered();
  await page.getByRole("button", { name: "Unpin and execute" }).click();
  await answer;
  expect(executions(calls)).toHaveLength(3);
  expect(changes.filter((call) => !call.includes("/studio/local/"))).toEqual(
    [],
  );
});

test("Ctrl/Cmd+Enter never starts a run", async ({ page }) => {
  const runs: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/runs\b/.test(request.url()))
      runs.push(request.url());
  });
  const { details, calls } = await openExecuteFixture(page);
  await page.locator('[data-step="score"] .tile-body').first().click();
  await page.keyboard.press("ControlOrMeta+Enter");
  await expect.poll(() => executions(calls).length).toBe(1);
  // The run ends (here: lookup needs test data) before step details open.
  await expect(
    page.getByRole("region", { name: "Run log" }).locator(".run-log-status"),
  ).toContainText("needs test data");
  await details.open("score");
  await page.keyboard.press("ControlOrMeta+Enter");
  await expect.poll(() => executions(calls).length).toBe(2);
  expect(runs).toEqual([]);
});

test("after sign-out no last results remain for that account", async ({
  page,
}) => {
  const { details } = await openExecuteFixture(page);
  const kept = () =>
    page.evaluate(() =>
      Object.keys(localStorage).filter((key) =>
        key.includes("weave.lastExecution."),
      ),
    );
  await details.open("score");
  const answer = page.waitForResponse("**/studio/local/debug/execute");
  await (await shownExecute(details)).click();
  await answer;
  // The account's last results are kept on this computer, under its prefix.
  await expect.poll(async () => (await kept()).length).toBe(1);
  await details.close();
  await page.route("**/studio/connection/logout", (route) =>
    route.fulfill({ json: { remote_revocation: "confirmed" } }),
  );
  await page.locator(".platform-indicator").click();
  await page
    .locator("#platform-menu")
    .getByRole("button", { name: "Sign out" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Sign out", exact: true })
    .click();
  await expect.poll(kept).toEqual([]);
});
