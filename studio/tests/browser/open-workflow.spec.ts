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
// Open workflow: from a call step, the workflow it calls opens, its draft on
// this computer first, otherwise its published version in the project (as
// Studio opens any published version, under an identity of its own). On the
// Runs tab, a call step's Output starts with the run it called and Open run.
// The platform is mocked.
import { expect, test, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { connected } from "./support";
import {
  openStepFixture,
  StepDetailsPage,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";
import {
  CHILD_RUN,
  openRunsTab,
  RUN_IDS,
  runFixture,
  runsWorkflow,
  VERSIONS,
} from "./runs-po";

const document = (name: string, version: string, steps: unknown[] = []) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name, version },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    callable: {},
    steps,
    output: { literal: {} },
  },
});
const caller = (uses: string) =>
  stringify({
    ...document("order-intake", "1.2.0", [
      {
        id: "notify",
        kind: "callWorkflow",
        uses,
        mode: "wait",
        with: { literal: {} },
      },
    ]),
  });
const draft = {
  entry: {
    id: "d-lookup",
    name: "lookup-customer",
    version: "0.1.0",
    savedAt: "2026-11-18T10:00:00Z",
  },
  document: {
    source: stringify(document("lookup-customer", "0.1.0")),
    format: "yaml",
    layout: null,
    savedAt: "2026-11-18T10:00:00Z",
  },
};

/** order-intake calling `uses`, connected to a project that publishes notify-customer 1.0.0; lookup-customer is a draft here. */
async function open(page: Page, uses: string) {
  const exports: string[] = [];
  const details = await openStepFixture(
    page,
    yamlFile("order-intake.yaml", caller(uses)),
    async (p) => {
      await p.addInitScript((kept) => {
        if (localStorage.getItem("local:weave.localDrafts.v1")) return;
        localStorage.setItem(
          "local:weave.localDrafts.v1",
          JSON.stringify([kept.entry]),
        );
        localStorage.setItem(
          `local:weave.localDraft.${kept.entry.id}`,
          JSON.stringify(kept.document),
        );
      }, draft);
      const result = await connected(p);
      await withLanguageFeatures(p, ["flow.callWorkflow"]);
      await p.route(/\/projects\/[^/]+\/catalog$/, (route) =>
        route.fulfill({
          json: {
            definitions: [
              {
                document: document("notify-customer", "1.0.0"),
                digest: "0".repeat(64),
              },
            ],
            tasks: [],
            adapters: [],
            schemas: {},
          },
        }),
      );
      await p.route(/\/projects\/[^/]+\/workflows\?/, (route) =>
        route.fulfill({
          json: {
            items: [
              {
                id: "v-notify",
                name: "notify-customer",
                version: "1.0.0",
                digest: "a".repeat(64),
              },
            ],
            next_cursor: null,
          },
        }),
      );
      await p.route(
        /\/projects\/[^/]+\/workflows\/v-notify\/export$/,
        (route) => {
          exports.push(route.request().url());
          return route.fulfill({
            json: {
              id: "v-notify",
              document: document("notify-customer", "1.0.0", [
                { id: "send", kind: "transform", value: { ref: "/input" } },
              ]),
            },
          });
        },
      );
      return result;
    },
  );
  await details.open("notify");
  return { details, exports };
}
const identity = (page: Page) => page.locator(".editor-identity");
const toast = (page: Page, text: string) =>
  page
    .getByRole("status")
    .or(page.getByRole("alert"))
    .filter({ hasText: text })
    .first();
const openWorkflow = (details: StepDetailsPage) =>
  details.field("workflow").getByRole("button", { name: "Open workflow" });

test.describe("opening the workflow a step calls", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a draft on this computer opens as that draft", async ({ page }) => {
    const { details, exports } = await open(page, "lookup-customer@0.1.0");
    await openWorkflow(details).click();
    await expect(identity(page)).toContainText("lookup-customer");
    await expect(details.dialog).toHaveCount(0);
    expect(exports).toEqual([]);
  });

  test("the call tile's More commands open the workflow too", async ({
    page,
  }) => {
    const { details } = await open(page, "lookup-customer@0.1.0");
    await details.close();
    await page
      .locator('.tile-node[data-tile="notify"] .tile-body')
      .click({ button: "right" });
    await page.getByRole("menuitem", { name: "Open workflow" }).click();
    await expect(identity(page)).toContainText("lookup-customer");
  });

  test("a published version opens under its own identity, and says changes start a new draft", async ({
    page,
  }) => {
    const { details, exports } = await open(page, "notify-customer@1.0.0");
    await openWorkflow(details).click();
    await expect(identity(page)).toContainText("notify-customer");
    await expect(
      toast(
        page,
        "Opened notify-customer 1.0.0 from this project. Your changes start a new draft.",
      ),
    ).toBeVisible();
    expect(exports).toHaveLength(1);
    await expect(page.locator('.tile-node[data-tile="send"]')).toBeVisible();
  });

  test("a version the project doesn't have says so and stays", async ({
    page,
  }) => {
    const { details } = await open(page, "gone-workflow@1.0.0");
    await openWorkflow(details).click();
    // The toast, not the field's hint, which says the same.
    await expect(
      toast(page, "Studio can't find gone-workflow 1.0.0 in this project."),
    ).toBeVisible();
    await expect(identity(page)).toContainText("order-intake");
  });
});

/** invoice-approval, its send step a call to notify-customer 1.0.0 that waits (or doesn't). */
const callingVersion = (mode: "wait" | "detach") => ({
  ...runsWorkflow,
  spec: {
    ...runsWorkflow.spec,
    steps: runsWorkflow.spec.steps.map((item) =>
      item.id === "send"
        ? {
            id: "send",
            kind: "callWorkflow",
            uses: "notify-customer@1.0.0",
            mode,
            with: { literal: {} },
          }
        : item,
    ),
  },
});
const CALLEE_VERSION = "5c4d3e2f-1a0b-4c9d-8e7f-6a5b4c3d2e1f";

async function openCallRun(
  page: Page,
  mode: "wait" | "detach",
  kind: "call" | "failed" = "call",
) {
  const version = callingVersion(mode);
  const run = kind === "call" ? RUN_IDS.call : RUN_IDS.failed;
  const { runs } = await openRunsTab(page, {
    runs: [runFixture(kind)],
    workflow: version,
    before: async () => {
      await page.route(
        `**/projects/*/workflows/${VERSIONS.current}/export`,
        (route) =>
          route.fulfill({ json: { id: VERSIONS.current, document: version } }),
      );
      await page.route("**/environments/*/run-summaries**", (route) => {
        const url = new URL(route.request().url());
        if (url.searchParams.get("caller_run_id") !== RUN_IDS.call)
          return route.fallback();
        return route.fulfill({
          json: {
            items: [
              {
                id: CHILD_RUN,
                workflow: {
                  name: "notify-customer",
                  version: "1.0.0",
                  definition_version_id: CALLEE_VERSION,
                },
                activation_id: "a-notify",
                activation: { name: "notify-customer", revision: 1 },
                status: "failed",
                paused: false,
                test: false,
                origin: "call",
                caller: null,
                retried_from_run_id: null,
                started_at: "2026-10-07T11:00:00Z",
                updated_at: "2026-10-07T11:00:02Z",
                ended_at: "2026-10-07T11:00:02Z",
                duration_ms: 2000,
                business_key: null,
                correlation_key: null,
                failed_step: {
                  node_id: "send",
                  instance_key: "send",
                  error_code: "SMTP_REJECTED",
                },
                active_incidents: 1,
                archived: false,
              },
            ],
            next_cursor: null,
          },
        });
      });
      await page.route(`**/environments/*/runs/${CHILD_RUN}`, (route) =>
        route.fulfill({
          json: {
            id: CHILD_RUN,
            activation: { request: { version_id: CALLEE_VERSION } },
            state: { status: "failed", steps: {}, active: [] },
          },
        }),
      );
      await page.route(
        `**/projects/*/workflows/${CALLEE_VERSION}/export`,
        (route) =>
          route.fulfill({
            json: {
              id: CALLEE_VERSION,
              document: document("notify-customer", "1.0.0"),
            },
          }),
      );
      await page.route(/\/projects\/[^/]+\/drafts(\?|$)/, (route) =>
        route.fulfill({ json: { items: [], next_cursor: null } }),
      );
    },
  });
  await expect(runs.rows.first()).toBeVisible();
  await runs.row(run).click();
  await expect(
    runs.canvas.locator(".run-header").getByRole("heading"),
  ).toHaveText(`Run ${run.slice(0, 8)}`);
  const details = new StepDetailsPage(page);
  await details.openWithKeyboard("send");
  return details.dialog.locator('[data-region="output"]');
}

test.describe("a call step on the Runs tab", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Output starts with the run it called and how it went, and Open run opens it in the editor", async ({
    page,
  }) => {
    const output = await openCallRun(page, "wait");
    const card = output.getByRole("group", { name: "Called run" });
    await expect(card.locator(".pane-child-title")).toHaveText(
      "Ran as run 9b1c4e2a of notify-customer",
    );
    await expect(card.locator(".pane-child-line")).toHaveText(
      "notify-customer 1.0.0 · failed at send: SMTP_REJECTED · took 2 s",
    );
    await expect(
      output.getByRole("button", { name: "Open in Operate" }),
    ).toHaveCount(0);
    // Read only here, Open workflow still opens the called workflow, and
    // the field's commands sit under it with their reasons.
    const field = page.locator('[data-param="workflow"]');
    const openCallee = field.getByRole("button", { name: "Open workflow" });
    await expect(openCallee).not.toHaveAttribute("aria-disabled", "true");
    const value = (await field.locator("input.param-input").boundingBox())!;
    expect((await openCallee.boundingBox())!.y).toBeGreaterThan(
      value.y + value.height,
    );
    await card.getByRole("button", { name: "Open run" }).click();
    // notify-customer has no draft here: Studio offers one from the version the run used.
    await expect(
      page.getByRole("dialog", { name: "Create a draft from 1.0.0?" }),
    ).toBeVisible();
  });

  test("a call that doesn't wait says it started the run", async ({ page }) => {
    const output = await openCallRun(page, "detach");
    const card = output.getByRole("group", { name: "Called run" });
    await expect(card.locator(".pane-child-title")).toHaveText(
      "Started run 9b1c4e2a of notify-customer",
    );
    await expect(card.locator(".pane-child-line")).toHaveText(
      "invoice-approval didn't wait for it",
    );
  });

  test("a call that failed says If it fails stopped the workflow", async ({
    page,
  }) => {
    const output = await openCallRun(page, "wait", "failed");
    const notice = output.locator(".pane-run-notice[data-tone=danger] p");
    await expect(notice).toHaveText([
      "send failed in run 1c0d7f8a: its call failed.",
      "If it fails is Stop this workflow, so invoice-approval stopped here. Open the run to see the step that failed.",
    ]);
  });
});
