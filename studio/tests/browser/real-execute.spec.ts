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
// Execute on a REAL running local platform (no route mocks): a draft that
// uses a published worker action from the platform's catalog executes on the
// Studio host, and the platform runs, activates and publishes nothing: its
// run summaries and its audit log are the same before and after. Skipped
// unless WEAVE_E2E_PLATFORM_DIR and WEAVE_E2E_PERSON_FILE are set, as
// real-platform.spec.ts describes; build the app first (`npm run build`).
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { python, repository } from "../python-path";
import {
  connectAndSignIn,
  expectNoCredentialsLeft,
  pair,
  platformDir,
  removePlatform,
  session,
  setup,
  shot,
  uniqueName,
  useRealPlatformSuite,
} from "./real-platform-support";
import { StepDetailsPage, yamlFile } from "./step-details-po";
import { outputPane } from "./test-data-po";

useRealPlatformSuite("real-execute");

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

/**
 * Audit entries that would mean the platform ran, activated or published
 * something, read from the platform's own database with the runtime's
 * credentials (the platform has no API for its audit log).
 */
function startedOrPublished(): string[] {
  const read = (file: string) =>
    Object.fromEntries(
      readFileSync(join(platformDir, file), "utf8")
        .split("\n")
        .map((line: string) => line.replace(/^export\s+/, "").trim())
        .filter((line: string) => line.includes("="))
        .map((line: string) => {
          const at = line.indexOf("=");
          return [
            line.slice(0, at),
            line.slice(at + 1).replace(/^['"]|['"]$/g, ""),
          ];
        }),
    );
  // The migration role owns the runtime's tables; the runtime role may only
  // write the audit log.
  const env = {
    WEAVE_DATABASE_URL: read("runtime.env")[
      "WEAVE_MIGRATION_DATABASE_URL"
    ].replace("+asyncpg", ""),
  };
  const script = `
import asyncio, json, os, sys
import asyncpg
async def main():
    url = os.environ["DATABASE_URL"]
    connection = await asyncpg.connect(url)
    try:
        rows = await connection.fetch(
            "SELECT id::text, action FROM access_audit WHERE action LIKE 'run.%' "
            "OR action LIKE 'release.%' OR action LIKE 'trigger.%' OR action LIKE 'deployment%' "
            "OR action = 'definition.publish' ORDER BY id"
        )
    finally:
        await connection.close()
    print(json.dumps([f"{row['id']} {row['action']}" for row in rows]))
asyncio.run(main())
`;
  const result = spawnSync(python, ["-c", script], {
    cwd: repository,
    env: { ...process.env, DATABASE_URL: env["WEAVE_DATABASE_URL"] ?? "" },
    encoding: "utf8",
    timeout: 60_000,
  });
  expect(result.status, `reading the audit log: ${result.stderr}`).toBe(0);
  return JSON.parse(result.stdout) as string[];
}

test("executing a draft with a published worker action reads the catalog and starts nothing on the platform", async ({
  page,
}) => {
  test.setTimeout(420_000);
  const { person, environment } = setup();
  const project = `/api/v1/tenants/${environment.tenant_id}/projects/${environment.project_id}`;
  const runs = `${project}/environments/${environment.environment_id}/run-summaries?limit=100`;
  const s = await session(page);
  const name = uniqueName("e2e-execute");
  await pair(page, s.host!, "ex-01-pairing");
  await connectAndSignIn(page, name, person);

  // A worker action published to the platform's catalog for this test, on
  // the task type the platform's fixture worker serves (example-sleep).
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
        sideEffect: capability["sideEffect"],
        timeoutSeconds: capability["timeoutSeconds"],
        inputSchema: capability["inputSchema"],
        outputSchema: capability["outputSchema"],
      },
    }),
  });
  expect(published.status, published.text).toBe(201);
  // Studio reads the project's catalog when it opens: open it again so the
  // new action's output fields are known.
  await page.reload();

  // The draft: the action, pinned, and a transform that reads it.
  await page.getByRole("button", { name: "Home", exact: true }).click();
  await page.getByLabel("Choose a workflow file").setInputFiles(
    yamlFile(
      "execute-real.yaml",
      `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: execute-real, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - id: lookup
      kind: action
      uses: ${action}@1.0.0
      with: {object: {ms: {literal: 1}}}
    - id: shape
      kind: transform
      value: {object: {slept: {ref: /steps/lookup/output/slept_ms}}}
  output: {literal: null}
`,
    ) as { name: string; mimeType: string; buffer: Buffer },
  );
  await expect(page.locator(".editor-bar")).toBeVisible({ timeout: 30_000 });
  const details = new StepDetailsPage(page);
  await details.open("lookup");
  const pane = await outputPane(details);
  // The catalog that knows the new action's output fields may still be on
  // its way after the reload: a sample needs them.
  await expect(async () => {
    await pane.getByRole("button", { name: "Generate sample" }).click();
    await expect(pane.locator(".pinned-banner")).toBeVisible({
      timeout: 3_000,
    });
  }).toPass({ timeout: 90_000 });
  await details.close();

  const before = await bridge(page, runs);
  expect(before.status, before.text).toBe(200);
  const auditBefore = startedOrPublished();
  const platformChanges: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET" && path.startsWith("/studio/api/"))
      platformChanges.push(`${request.method()} ${path}`);
  });

  const executed = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/studio/local/debug/execute",
    { timeout: 120_000 },
  );
  await page
    .locator(".editor-bar")
    .getByRole("button", { name: "Execute workflow" })
    .click();
  const answer = await executed;
  expect(answer.status()).toBe(200);
  expect(((await answer.json()) as { status: string }).status).toBe(
    "completed",
  );
  await expect(
    page.getByRole("region", { name: "Run log" }).locator(".run-log-status"),
  ).toHaveText(/^Completed/, { timeout: 30_000 });
  await shot(page, "ex-02-executed");

  // Nothing ran, activated or was published on the platform.
  const after = await bridge(page, runs);
  expect(after.status, after.text).toBe(200);
  const items = (text: string) =>
    ((JSON.parse(text) as { items: { id: string }[] }).items ?? []).map(
      (item) => item.id,
    );
  expect(items(after.text)).toEqual(items(before.text));
  expect(startedOrPublished()).toEqual(auditBefore);
  expect(
    platformChanges.filter((call) => !/\/drafts\//.test(call)),
    "Studio's only writes to the platform are draft saves",
  ).toEqual([]);

  await removePlatform(page, name);
  expectNoCredentialsLeft();
});
