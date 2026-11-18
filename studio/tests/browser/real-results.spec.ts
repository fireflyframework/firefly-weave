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
// What a step's real result looks like in step details: where it came from,
// why the platform left an output out, how a failed run ended, files it
// returned, and that it stays in memory until Pin output keeps it as test data
// from that run.
import { expect, test, type Page } from "@playwright/test";
import {
  PRODUCTION,
  bindable,
  connectionSelect,
  crmWorkflow,
  keptOnThisComputer,
  openConnected,
} from "./connections-po";
import {
  RUN_ID,
  accepted,
  executeIn,
  output,
  routeStepTests,
  sent,
  succeeded,
  view,
} from "./step-tests-po";
import { allCapabilities } from "./support";

test.use({ viewport: { width: 1440, height: 900 } });

async function executed(page: Page, last: unknown, capabilities?: string[]) {
  const console: string[] = [];
  page.on("console", (message) => console.push(message.text()));
  const { details } = await openConnected(
    page,
    {
      bindable: { [PRODUCTION]: [bindable(1)] },
      ...(capabilities ? { capabilities } : {}),
    },
    crmWorkflow(
      {},
      { with: { object: { customerId: { literal: "c-1042" } } } },
    ),
  );
  const steps = await routeStepTests(page, {
    starts: [{ status: 202, json: accepted }],
    views: [view(), last],
  });
  await details.open("crm-lookup");
  await connectionSelect(details).click();
  await page
    .getByRole("listbox", { name: "Connection options" })
    .getByRole("option")
    .filter({ hasText: /^acme-crm-1/ })
    .click();
  await (await executeIn(details, "Production")).click();
  await sent(steps, 1);
  return { details, steps, console };
}

test("a real result shows where it came from and can be pinned", async ({
  page,
}) => {
  const { details, steps } = await executed(
    page,
    succeeded({ name: "Ana", open: 2 }),
  );
  const pane = output(details);
  await expect(pane).toContainText(
    `From Production · run ${RUN_ID.slice(0, 8)}`,
  );
  await pane.getByRole("button", { name: "Pin output" }).click();
  await expect(pane).toContainText("Pinned");
  await expect
    .poll(
      () =>
        steps.documents.find((request) =>
          (request.postData() ?? "").includes('"test-call"'),
        ),
      { timeout: 15_000 },
    )
    .toBeTruthy();
  const saved = steps.documents.find((request) =>
    (request.postData() ?? "").includes('"test-call"'),
  )!;
  expect(saved.url()).toMatch(/\/studio\/test-data$/);
  const document = (
    saved.postDataJSON() as { document: { pins: Record<string, unknown> } }
  ).document;
  expect(document.pins["crm-lookup"]).toMatchObject({
    output: { name: "Ana", open: 2 },
    source: "test-call",
    runId: RUN_ID,
    runEnvironmentId: PRODUCTION,
  });
});

test("an output the platform left out says why", async ({ page }) => {
  const { details } = await executed(
    page,
    succeeded(null, { omitted: "classified_secret" }),
  );
  const pane = output(details);
  await expect(pane).toContainText("This output is classified as secret.");
  await expect(
    pane.getByRole("button", { name: "Pin output" }),
  ).toHaveAttribute("aria-disabled", "true");
});

test("a failed test says so", async ({ page }) => {
  const { details } = await executed(
    page,
    view({
      status: "failed",
      error: { code: "HTTP_STATUS", message: "The API answered 500." },
      ended_at: "2026-11-17T10:00:02Z",
    }),
  );
  await expect(output(details)).toContainText(
    "crm-lookup failed in Production: HTTP_STATUS. The API answered 500.",
  );
});

test("a timed-out test says so", async ({ page }) => {
  const { details } = await executed(
    page,
    view({ status: "timed_out", ended_at: "2026-11-17T10:00:30Z" }),
  );
  await expect(output(details)).toContainText(
    "crm-lookup didn't finish in Production within 30 s.",
  );
});

test("a real result isn't kept anywhere until pinned", async ({ page }) => {
  const { details, console } = await executed(
    page,
    succeeded({ name: "REAL-RESULT-5d1", open: 1 }),
  );
  await expect(output(details)).toContainText("REAL-RESULT-5d1");
  await details.close();
  await expect
    .poll(async () => keptOnThisComputer(page))
    .not.toContain("REAL-RESULT-5d1");
  const databases = await page.evaluate(async () => {
    const names = (await indexedDB.databases?.()) ?? [];
    return names.map((db) => db.name ?? "");
  });
  expect(databases.join(" ")).not.toContain("REAL-RESULT-5d1");
  expect(page.url()).not.toContain("REAL-RESULT-5d1");
  expect(console.join("\n")).not.toContain("REAL-RESULT-5d1");
  await page.reload();
  expect(await keptOnThisComputer(page)).not.toContain("REAL-RESULT-5d1");
});

test("a file in a real result can be downloaded", async ({ page }) => {
  const invoice = {
    kind: "weave/file",
    id: "8e1f0c4a-1111-4222-8333-444455556666",
    filename: "invoice.pdf",
    contentType: "application/pdf",
    sizeBytes: 122_880,
    sha256: "ab".repeat(32),
  };
  const { details } = await executed(page, succeeded({ attachment: invoice }), [
    ...allCapabilities,
    "compile",
    "step.test",
    "file.read",
  ]);
  const pane = output(details);
  await expect(pane).toContainText(
    "attachment · invoice.pdf · 120 KiB · application/pdf",
  );
  await expect(
    pane.getByRole("button", { name: "Download invoice.pdf" }),
  ).toBeVisible();
});
