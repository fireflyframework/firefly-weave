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
// The run log after Execute workflow on a workflow that calls others: each
// called workflow shows under its call and opens to its steps, a call inside
// it under its own step; the keyboard opens and closes them; and test output
// set for a step inside one goes to that workflow's test data, where the next
// execution sends it. The repository's own host code answers
// (localAuthoring).
import { expect, test, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { executions } from "./execute-po";
import { localAuthoring } from "./local-authoring";
import {
  openStepFixture,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";
import { connected, defaultCatalog } from "./support";
import { lookupContract, PROJECT, TENANT } from "./test-data-po";

const workflow = (
  name: string,
  version: string,
  steps: Record<string, unknown>[],
  output: string,
  callable = true,
) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name, version },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    ...(callable ? { callable: {} } : {}),
    steps,
    output: { ref: `/steps/${output}/output` },
  },
});
const value = (id: string, literal: unknown) => ({
  id,
  kind: "transform",
  value: { literal },
});
const call = (id: string, uses: string) => ({
  id,
  kind: "callWorkflow",
  uses,
  mode: "wait",
  with: { literal: {} },
});
const lookup = (id: string) => ({
  id,
  kind: "action",
  uses: "crm.lookup@1.0.0",
  with: { object: { customerId: { literal: "c-1" } } },
});
const PROFILE = workflow(
  "customer-profile",
  "2.1.0",
  [value("lookup-profile", { tier: "gold" })],
  "lookup-profile",
);
const NOTIFY = workflow(
  "notify-customer",
  "1.0.0",
  [
    value("render", { text: "Hello" }),
    call("enrich", "customer-profile@2.1.0"),
    value("send", { sent: true }),
  ],
  "send",
);
// Its send step fails, so the call that waits for it fails too.
const NOTIFY_FAILING = workflow(
  "notify-customer",
  "1.0.0",
  [
    value("render", { text: "Hello" }),
    { id: "send", kind: "fail", code: "SMTP_REJECTED", message: "Refused." },
  ],
  "render",
);
// Its send step is an action: without test output, a test stops there.
const NOTIFY_SENDING = workflow(
  "notify-customer",
  "1.0.0",
  [value("render", { text: "Hello" }), lookup("send")],
  "send",
);

/**
 * Opens order-intake with the workflows it calls kept on this computer, as
 * drafts (seeded before the page loads), connected to a project whose
 * catalog serves crm.lookup.
 */
async function open(
  page: Page,
  steps: Record<string, unknown>[],
  drafts: Record<string, unknown>[],
) {
  const last = steps[steps.length - 1]["id"] as string;
  const file = yamlFile(
    "order-intake.yaml",
    stringify(workflow("order-intake", "1.2.0", steps, last, false)),
  );
  const details = await openStepFixture(page, file, async (p) => {
    await p.addInitScript(
      (kept) => {
        if (localStorage.getItem("local:weave.localDrafts.v1")) return;
        localStorage.setItem(
          "local:weave.localDrafts.v1",
          JSON.stringify(
            kept.map((draft, at) => ({
              id: `d-${at}`,
              name: draft.name,
              version: draft.version,
              savedAt: "2026-11-18T10:00:00Z",
            })),
          ),
        );
        kept.forEach((draft, at) =>
          localStorage.setItem(
            `local:weave.localDraft.d-${at}`,
            JSON.stringify({
              source: draft.source,
              format: "yaml",
              layout: null,
              savedAt: "2026-11-18T10:00:00Z",
            }),
          ),
        );
      },
      drafts.map((draft) => {
        const metadata = draft["metadata"] as {
          name: string;
          version: string;
        };
        return { ...metadata, source: stringify(draft) };
      }),
    );
    const result = await connected(p, {
      catalog: [...defaultCatalog, { id: "a3", document: lookupContract }],
      tenantId: TENANT,
      projectId: PROJECT,
    });
    await withLanguageFeatures(p, ["flow.callWorkflow"]);
    // The host's test data check for a called workflow's entries, as the
    // repository's check_test_data answers it: fine for a step the called
    // workflow's definition has, unresolved otherwise. Other entries go on
    // to the shared mock.
    await p.route("**/studio/local/testdata/check", async (route) => {
      const body = route.request().postDataJSON() as {
        definitions: Record<string, any>[];
        testData: { workflows?: Record<string, { mocks: object }> };
      };
      const entries = Object.entries(body.testData.workflows ?? {}).flatMap(
        ([reference, callee]) =>
          Object.keys(callee.mocks).map((key) => {
            const known = body.definitions.some(
              (item) =>
                item["kind"] === "Workflow" &&
                `${item["metadata"]?.["name"]}@${item["metadata"]?.["version"]}` ===
                  reference &&
                (item["spec"]?.["steps"] as { id: string }[]).some(
                  (step) => step.id === key,
                ),
            );
            const path = `/workflows/${reference}/mocks/${key}`;
            return known
              ? { path, ok: true }
              : { path, ok: false, code: "WV-STUDIO-TESTDATA-UNRESOLVED" };
          }),
      );
      if (!entries.length) return route.fallback();
      return route.fulfill({ json: { entries } });
    });
    return result;
  });
  const calls = await localAuthoring(page);
  return { details, calls };
}
const executeWorkflow = (page: Page) =>
  page
    .locator(".editor-bar")
    .getByRole("button", { name: "Execute workflow" })
    .click();
const runLog = (page: Page) => page.getByRole("region", { name: "Run log" });

test.describe("called workflows in the run log", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a called workflow opens to its steps, and a call inside it to its own", async ({
    page,
  }) => {
    const { calls } = await open(
      page,
      [
        value("check", { ok: true }),
        call("notify", "notify-customer@1.0.0"),
        value("summarize", { done: true }),
      ],
      [NOTIFY, PROFILE],
    );
    await executeWorkflow(page);
    await expect.poll(() => executions(calls).length).toBe(1);
    const log = runLog(page);
    await expect(log.locator(".run-log-status")).toHaveText(
      "Completed · 3 steps",
    );
    await expect(log.locator(".run-log-note")).toHaveText(
      "Executed with a sample input. Add test input to choose your own. notify-customer and customer-profile ran on this computer.",
    );
    // Under the call, closed: everything in it finished.
    const notify = log.getByRole("button", {
      name: "notify-customer 1.0.0, completed",
    });
    await expect(notify).toHaveAttribute("aria-expanded", "false");
    await expect(log.getByText("render", { exact: true })).toHaveCount(0);
    // Its steps are only its own: order-intake's list keeps its three.
    await expect(log.locator(".run-log-entry[data-entry]")).toHaveCount(3);
    await notify.focus();
    await page.keyboard.press("Enter");
    await expect(notify).toHaveAttribute("aria-expanded", "true");
    const inside = log.getByRole("list", { name: "Steps of notify-customer" });
    await expect(
      inside.locator(":scope > li > .run-log-inside strong"),
    ).toHaveText(["render", "enrich", "send"]);
    // A call inside it, under its own step; → opens it, ← closes it.
    const profile = inside.getByRole("button", {
      name: "customer-profile 2.1.0, completed",
    });
    await profile.focus();
    await page.keyboard.press("ArrowRight");
    await expect(profile).toHaveAttribute("aria-expanded", "true");
    await expect(
      log
        .getByRole("list", { name: "Steps of customer-profile" })
        .getByText("lookup-profile"),
    ).toBeVisible();
    await page.keyboard.press("ArrowLeft");
    await expect(profile).toHaveAttribute("aria-expanded", "false");
    // Escape inside a called workflow's steps closes it, and focus goes back to its row.
    await page.keyboard.press("Escape");
    await expect(notify).toHaveAttribute("aria-expanded", "false");
    await expect(notify).toBeFocused();
  });

  test("a called workflow that failed opens by itself on the step that failed, and its call says where", async ({
    page,
  }) => {
    const { calls } = await open(
      page,
      [call("notify", "notify-customer@1.0.0"), value("summarize", {})],
      [NOTIFY_FAILING],
    );
    await executeWorkflow(page);
    await expect.poll(() => executions(calls).length).toBe(1);
    const log = runLog(page);
    await expect(log.locator(".run-log-status")).toHaveText("Failed at notify");
    await expect(log.locator('[data-entry="notify"]')).toContainText(
      "Failed in notify-customer",
    );
    const notify = log.getByRole("button", {
      name: "notify-customer 1.0.0, failed",
    });
    await expect(notify).toHaveAttribute("aria-expanded", "true");
    const inside = log.getByRole("list", { name: "Steps of notify-customer" });
    await expect(
      inside.locator('.run-log-inside[data-status="failed"] strong'),
    ).toHaveText("send");
  });

  test("test output for a step inside a called workflow goes to its test data, and the next execution sends it", async ({
    page,
  }) => {
    const { calls } = await open(
      page,
      [call("notify", "notify-customer@1.0.0"), lookup("audit")],
      [NOTIFY_SENDING],
    );
    await executeWorkflow(page);
    await expect.poll(() => executions(calls).length).toBe(1);
    const log = runLog(page);
    await expect(log.locator(".run-log-status")).toHaveText(
      "send in notify-customer needs test data",
    );
    // It stopped inside notify-customer: open on the step that needs test output.
    const notify = log.getByRole("button", {
      name: "notify-customer 1.0.0, waiting",
    });
    await expect(notify).toHaveAttribute("aria-expanded", "true");
    const set = log.getByRole("button", {
      name: "Set test output for send in notify-customer",
    });
    await expect(set).toBeVisible();
    await set.click();
    const editor = log.getByRole("textbox", {
      name: "Test output for send (JSON)",
    });
    await expect(editor).toBeFocused();
    await editor.fill('{ "customerId": "c-1", "found": true }');
    await log.getByRole("button", { name: "Save", exact: true }).click();
    // Saved under notify-customer's test data, then executed again with it.
    await expect.poll(() => executions(calls).length).toBe(2);
    expect(
      (
        executions(calls)[1].body["workflows"] as Record<
          string,
          { mocks: unknown }
        >
      )["notify-customer@1.0.0"].mocks,
    ).toEqual({ "node:send": { customerId: "c-1", found: true } });
    await expect(
      log.getByRole("button", { name: "notify-customer 1.0.0, completed" }),
    ).toBeVisible();
    // order-intake now stops at its own step, after the call.
    await expect(log.locator(".run-log-status")).toHaveText(
      "audit needs test data",
    );
  });
});
