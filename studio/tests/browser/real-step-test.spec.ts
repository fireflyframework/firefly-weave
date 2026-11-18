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
// Execute in local on a REAL running local platform (no route mocks): one
// step of a saved draft runs as a test run through the platform's runtime
// and its fixture worker, Studio shows what it returned with the run it came
// from, and Pin output keeps it as test data from that run. Skipped unless
// WEAVE_E2E_PLATFORM_DIR and WEAVE_E2E_PERSON_FILE are set, as
// real-platform.spec.ts describes; build the app first (`npm run build`).
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { repository } from "../python-path";
import {
  connectAndSignIn,
  expectNoCredentialsLeft,
  pair,
  removePlatform,
  session,
  setup,
  shot,
  uniqueName,
  useRealPlatformSuite,
} from "./real-platform-support";
import { StepDetailsPage, yamlFile } from "./step-details-po";
import { executeIn, showOutput } from "./step-tests-po";

useRealPlatformSuite("real-step-test");

/** A platform call through the page's own Studio session, as any client makes it. */
async function bridge(
  page: Page,
  path: string,
  method = "GET",
  body?: unknown,
) {
  return page.evaluate(
    async ({ path, method, body }) => {
      const session = (await (
        await fetch("/studio/session", { credentials: "same-origin" })
      ).json()) as { csrfToken?: string };
      const response = await fetch(`/studio/api${path}`, {
        method,
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-Weave-CSRF": session.csrfToken ?? "",
          ...(method === "GET"
            ? {}
            : { "Idempotency-Key": crypto.randomUUID() }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      return { status: response.status, text: await response.text() };
    },
    { path, method, body },
  );
}

test("a step executed in local runs on the platform as a test run, and its output can be pinned", async ({
  page,
}) => {
  test.setTimeout(600_000);
  const { person, environment } = setup();
  const project = `/api/v1/tenants/${environment.tenant_id}/projects/${environment.project_id}`;
  const scope = `${project}/environments/${environment.environment_id}`;
  const s = await session(page);
  const name = uniqueName("e2e-step-test");
  await pair(page, s.host!, "st-01-pairing");
  await connectAndSignIn(page, name, person);

  // The environment takes test calls, and this person may run steps there.
  const policy = await bridge(page, `${scope}/test-policy`);
  expect(policy.status, policy.text).toBe(200);
  expect((JSON.parse(policy.text) as { test_calls: string }).test_calls).toBe(
    "enabled",
  );

  // A worker action published for this test, on the task type the
  // platform's fixture worker serves.
  const capability = (
    JSON.parse(
      readFileSync(
        join(repository, "tests/fixtures/operations-worker/manifest.json"),
        "utf8",
      ),
    ) as { capabilities: Record<string, unknown>[] }
  ).capabilities[0];
  const action = `e2e.sleep-${uniqueName("a").slice(2)}`;
  const published = await bridge(page, `${project}/actions`, "POST", {
    format: "json",
    source: JSON.stringify({
      apiVersion: "weave/v1alpha1",
      kind: "Action",
      metadata: { name: action, version: "1.0.0" },
      spec: {
        implementation: {
          kind: "worker",
          taskType: capability["taskType"],
          taskVersion: capability["taskVersion"],
        },
        sideEffect: "read_only",
        timeoutSeconds: capability["timeoutSeconds"],
        inputSchema: capability["inputSchema"],
        outputSchema: capability["outputSchema"],
      },
    }),
  });
  expect(published.status, published.text).toBe(201);
  await page.reload();

  await page.getByRole("button", { name: "Home", exact: true }).click();
  await page.getByLabel("Choose a workflow file").setInputFiles(
    yamlFile(
      "step-test-real.yaml",
      `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: step-test-real, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - id: lookup
      kind: action
      uses: ${action}@1.0.0
      with: {object: {ms: {literal: 1}}}
  output: {literal: null}
`,
    ) as { name: string; mimeType: string; buffer: Buffer },
  );
  await expect(page.locator(".editor-bar")).toBeVisible({ timeout: 30_000 });
  const details = new StepDetailsPage(page);
  await details.open("lookup");

  const started = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      new URL(r.url()).pathname.endsWith("/step-tests"),
    { timeout: 120_000 },
  );
  // The catalog that knows the new action may still be on its way.
  await expect(async () => {
    const item = await executeIn(details, "local");
    await expect(item).toBeEnabled({ timeout: 3_000 });
    await item.click();
  }).toPass({ timeout: 90_000 });
  const answer = await started;
  expect(answer.status()).toBe(202);
  const accepted = (await answer.json()) as { run_id: string };

  const output = await showOutput(details);
  await expect(output).toContainText(
    `From local · run ${accepted.run_id.slice(0, 8)}`,
    { timeout: 180_000 },
  );
  await shot(page, "st-02-real-result");
  await output.getByRole("button", { name: "Pin output" }).click();
  await expect(output).toContainText("Pinned");
  await shot(page, "st-03-pinned");

  // The platform lists it as a test run of this workflow.
  const runs = await bridge(
    page,
    `${scope}/run-summaries?limit=50&include_test=true`,
  );
  expect(runs.status, runs.text).toBe(200);
  const listed = (
    JSON.parse(runs.text) as { items: { id: string; test: boolean }[] }
  ).items.find((run) => run.id === accepted.run_id);
  expect(listed?.test).toBe(true);

  await details.close();
  await removePlatform(page, name);
  expectNoCredentialsLeft();
});
