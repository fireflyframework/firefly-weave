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
// Tables kept on this computer travel with every execution: a draft, or a
// copy of a version Studio read; after sign-out none of an account's remain.
import { expect, test, type Page } from "@playwright/test";
import { allCapabilities, connected } from "./support";
import {
  openStepFixture,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";
import {
  DecisionGridPage,
  paymentPolicy,
  routeTables,
} from "./decision-grid-po";
import { executions, shownExecute } from "./execute-po";
import { hasPython, localAuthoring } from "./local-authoring";

test.skip(!hasPython, "Executing runs the repository's Python.");
test.use({ viewport: { width: 1440, height: 900 } });

const workflow = (version: string) =>
  yamlFile(
    "orders.yaml",
    `apiVersion: weave/v1alpha1
kind: Workflow
metadata: { name: orders, version: 1.0.0 }
spec:
  inputSchema:
    type: object
    required: [amount]
    properties:
      amount: { type: number }
      tier: { type: string }
  outputSchema: { type: object }
  steps:
    - id: score
      kind: decisionTable
      uses: payment-policy@${version}
      with: { ref: /input }
  output: { ref: /steps/score/output }
`,
  );

async function keep(page: Page, key: string, entry: object) {
  await page.addInitScript(
    ([name, value]) => localStorage.setItem(name, value),
    [key, JSON.stringify(entry)] as const,
  );
}
const definitionsSent = (calls: Awaited<ReturnType<typeof localAuthoring>>) =>
  (executions(calls).at(-1)?.body["definitions"] ?? []) as {
    kind: string;
    metadata: { name: string; version: string };
  }[];

test("Execute step uses the table draft on this computer", async ({ page }) => {
  const draft = paymentPolicy("1.2.1");
  // The draft answers big amounts with a limit of 7777.
  (draft.spec["rules"] as { output: unknown }[])[0].output = {
    literal: { approve: true, limit: 7777 },
  };
  await keep(page, "local:weave.tableDraft.payment-policy", {
    kind: "draft",
    name: "payment-policy",
    version: "1.2.1",
    base: "1.2.0",
    document: draft,
    savedAt: "2026-10-27T08:00:00.000Z",
  });
  const details = await openStepFixture(page, workflow("1.2.1"));
  const calls = await localAuthoring(page);
  await details.open("score");
  await (await shownExecute(details)).click();
  await expect.poll(() => executions(calls).length).toBe(1);
  expect(
    definitionsSent(calls).map(
      (d) => `${d.metadata.name}@${d.metadata.version}`,
    ),
  ).toEqual(["payment-policy@1.2.1"]);
  await expect(details.dialog.locator(".sd-output")).not.toContainText(
    "isn't available",
  );
});

test("a version read once executes offline", async ({ page }) => {
  await keep(page, "local:weave.tableCopy.payment-policy_v1.2.0", {
    kind: "copy",
    name: "payment-policy",
    version: "1.2.0",
    document: paymentPolicy("1.2.0"),
    readAt: "2026-10-27T08:00:00.000Z",
  });
  const details = await openStepFixture(page, workflow("1.2.0"));
  const calls = await localAuthoring(page);
  await details.open("score");
  await (await shownExecute(details)).click();
  await expect.poll(() => executions(calls).length).toBe(1);
  expect(
    definitionsSent(calls).map(
      (d) => `${d.metadata.name}@${d.metadata.version}`,
    ),
  ).toEqual(["payment-policy@1.2.0"]);
});

test("a version never read says how to get it", async ({ page }) => {
  const details = await openStepFixture(page, workflow("1.2.0"));
  await localAuthoring(page);
  await details.open("score");
  const answer = page.waitForResponse("**/studio/local/debug/execute");
  await (await shownExecute(details)).click();
  // The host can't resolve a table Studio never read: nothing is stubbed.
  const body = (await (await answer).json()) as {
    compile: { diagnostics: { code: string; path: string }[] };
  };
  expect(body.compile.diagnostics).toContainEqual(
    expect.objectContaining({
      code: "WV-COMP-UNKNOWN_RESOURCE",
      path: "/spec/steps/0/uses",
    }),
  );
  await expect(details.dialog).toContainText(
    "The workflow doesn't compile. Fix the issues it lists, then execute again.",
  );
});

test("after sign-out no table draft remains for that account", async ({
  page,
}) => {
  const TENANT = "00000000-0000-4000-8000-000000000021";
  const PROJECT = "00000000-0000-4000-8000-000000000022";
  const details = await openStepFixture(page, workflow("1.2.0"), async (p) => {
    const result = await connected(p, {
      capabilities: allCapabilities,
      tenantId: TENANT,
      projectId: PROJECT,
    });
    await withLanguageFeatures(p);
    return result;
  });
  await routeTables(page, [paymentPolicy("1.2.0")], PROJECT);
  await details.open("score");
  const grid = new DecisionGridPage(page);
  await grid.open();
  await grid.edit(1, 1, "<= 900");
  await expect(grid.root).toContainText(
    "payment-policy 1.2.1 · on this computer",
  );
  await grid.back();
  await details.close();
  const kept = () =>
    page.evaluate(() =>
      Object.keys(localStorage).filter((key) => key.includes("weave.table")),
    );
  await expect.poll(async () => (await kept()).length).toBeGreaterThan(0);
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
