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
// The screens of calling a workflow, opened for the tour and the geometry
// spec: a call step with its Workflow list (the project's catalog lock is the
// shared corpus), the Called by a workflow trigger with its published callers,
// the run log after Execute workflow with the called workflows kept on this
// computer (the repository's own host code answers), and a call step's run on
// the Runs tab. The platform is mocked.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Locator, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { executions } from "./execute-po";
import { localAuthoring } from "./local-authoring";
import {
  CHILD_RUN,
  openRunsTab,
  RUN_IDS,
  runFixture,
  runsWorkflow,
  VERSIONS,
} from "./runs-po";
import {
  addCommonStep,
  openStepFixture,
  StepDetailsPage,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";
import { connected } from "./support";

const corpus = JSON.parse(
  readFileSync(resolve("tests/fixtures/callable-corpus.json"), "utf8"),
) as { lists: { lock: unknown }[] };

const workflow = (
  name: string,
  version: string,
  steps: unknown[],
  extra: Record<string, unknown> = {},
) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name, version },
  spec: {
    inputSchema: {
      type: "object",
      properties: { customerId: { type: "string" } },
    },
    outputSchema: { type: "object" },
    ...extra,
    steps,
    output: { literal: {} },
  },
});
const call = (id: string, uses: string, mode = "wait") => ({
  id,
  kind: "callWorkflow",
  uses,
  mode,
  with: { literal: {} },
});
const value = (id: string, literal: unknown) => ({
  id,
  kind: "transform",
  value: { literal },
});

/** order-intake with a Call a workflow step after fetch, connected to the corpus's project; its Workflow chosen. */
export async function openCallStep(page: Page): Promise<StepDetailsPage> {
  const details = await openStepFixture(
    page,
    yamlFile(
      "order-intake.yaml",
      stringify(
        workflow("order-intake", "1.2.0", [
          { id: "fetch", kind: "transform", value: { ref: "/input" } },
        ]),
      ),
    ),
    async (p) => {
      const result = await connected(p);
      await withLanguageFeatures(p, ["flow.callWorkflow"]);
      await p.route(/\/projects\/[^/]+\/catalog$/, (route) =>
        route.fulfill({ json: corpus.lists[0].lock }),
      );
      return result;
    },
  );
  await addCommonStep(page, "Call a workflow", "Add a step after fetch");
  await expect(details.dialogFor("call-workflow-1")).toBeVisible();
  await workflowOptions(page, details);
  await page
    .getByRole("listbox", { name: "Workflow options" })
    .getByRole("option", { name: "notify-customer 1.0.0" })
    .click();
  await expect(details.field("input")).toContainText(
    "From notify-customer's input",
  );
  return details;
}
/** Opens the call step's Workflow list. */
export async function workflowOptions(
  page: Page,
  details: StepDetailsPage,
): Promise<Locator> {
  await details.field("workflow").getByRole("combobox").click();
  const options = page.getByRole("listbox", { name: "Workflow options" });
  await expect(options).toBeVisible();
  return options;
}

/** Published workflows that call notify-customer: two allowed, an AI agent's tool, and one not allowed. */
const ALLOWED = ["order-intake", "billing-close", "support-desk"];
const published = (name: string, version: string, steps: unknown[]) => ({
  document: workflow(name, version, steps),
  digest: "0".repeat(64),
});
const CALLERS = {
  definitions: [
    published("order-intake", "1.2.0", [
      call("notify", "notify-customer@1.0.0"),
    ]),
    published("billing-close", "2.0.0", [
      call("notify-owner", "notify-customer@1.0.0", "detach"),
    ]),
    published("support-desk", "1.1.0", [
      {
        id: "assistant",
        kind: "agent",
        tools: [{ name: "notify", workflow: "notify-customer@1.0.0" }],
      },
    ]),
    published("returns-desk", "1.0.0", [
      call("tell-customer", "notify-customer@1.0.0"),
    ]),
  ],
  tasks: [],
  adapters: [],
  schemas: {},
};

/** notify-customer, callable by three workflows, its Called by a workflow trigger open in step details. */
export async function openCalledBy(page: Page): Promise<StepDetailsPage> {
  const details = await openStepFixture(
    page,
    yamlFile(
      "notify-customer.yaml",
      stringify(
        workflow(
          "notify-customer",
          "1.0.0",
          [{ id: "send", kind: "transform", value: { ref: "/input" } }],
          {
            callable: { allowedCallers: ALLOWED },
            inputSchema: {
              type: "object",
              required: ["customerId"],
              properties: {
                customerId: { type: "string" },
                message: { type: "string" },
              },
            },
          },
        ),
      ),
    ),
    async (p) => {
      const result = await connected(p);
      await withLanguageFeatures(p, ["flow.callWorkflow"]);
      await p.route(/\/projects\/[^/]+\/catalog$/, (route) =>
        route.fulfill({ json: CALLERS }),
      );
      return result;
    },
  );
  await page
    .locator('.tile-node[data-tile^="$trigger"]')
    .filter({ hasText: "Called by a workflow" })
    .locator(".tile-body")
    .dblclick();
  await expect(
    details.dialog.getByRole("heading", { name: "Who can call it" }),
  ).toBeVisible();
  await expect(details.dialog).toContainText("returns-desk");
  return details;
}

const PROFILE = workflow(
  "customer-profile",
  "2.1.0",
  [value("lookup-profile", { tier: "gold" })],
  { callable: {} },
);
const NOTIFY = workflow(
  "notify-customer",
  "1.0.0",
  [
    value("render", { text: "Hello" }),
    call("enrich", "customer-profile@2.1.0"),
    value("send", { sent: true }),
  ],
  { callable: {} },
);
const NOTIFY_FAILING = workflow(
  "notify-customer",
  "1.0.0",
  [
    value("render", { text: "Hello" }),
    call("enrich", "customer-profile@2.1.0"),
    {
      id: "send",
      kind: "fail",
      code: "SMTP_REJECTED",
      message: "The mail relay refused the message.",
    },
  ],
  { callable: {} },
);

/**
 * order-intake calling notify-customer, which calls customer-profile, both
 * kept on this computer; Execute workflow has run and the run log shows
 * notify-customer under its call, open with customer-profile open inside it.
 * `failing` makes notify-customer's send fail.
 */
export async function openCallRunLog(
  page: Page,
  failing = false,
): Promise<Locator> {
  const drafts = [failing ? NOTIFY_FAILING : NOTIFY, PROFILE];
  await openStepFixture(
    page,
    yamlFile(
      "order-intake.yaml",
      stringify(
        workflow("order-intake", "1.2.0", [
          value("check", { ok: true }),
          call("notify", "notify-customer@1.0.0"),
          value("summarize", { done: true }),
        ]),
      ),
    ),
    async (p) => {
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
        drafts.map((draft) => ({
          ...draft.metadata,
          source: stringify(draft),
        })),
      );
      const result = await connected(p);
      await withLanguageFeatures(p, ["flow.callWorkflow"]);
      return result;
    },
  );
  const calls = await localAuthoring(page);
  await page
    .locator(".editor-bar")
    .getByRole("button", { name: "Execute workflow" })
    .click();
  await expect.poll(() => executions(calls).length).toBe(1);
  const log = page.getByRole("region", { name: "Run log" });
  const notify = log.getByRole("button", {
    name: /^notify-customer 1\.0\.0, /,
  });
  if ((await notify.getAttribute("aria-expanded")) !== "true")
    await notify.click();
  const profile = log.getByRole("button", {
    name: "customer-profile 2.1.0, completed",
  });
  await profile.click();
  await expect(profile).toHaveAttribute("aria-expanded", "true");
  return log;
}

/**
 * The Runs tab on invoice-approval's run whose send step called
 * notify-customer and continued with the result status when it failed; the
 * call step's step details open on Output, with the child run's card.
 */
export async function openCallRun(
  page: Page,
  mode: "wait" | "detach" = "wait",
): Promise<{ details: StepDetailsPage; card: Locator }> {
  const CALLEE = "5c4d3e2f-1a0b-4c9d-8e7f-6a5b4c3d2e1f";
  const version = {
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
              ...(mode === "wait" ? { onFailure: "continue" } : {}),
              with: { literal: {} },
            }
          : item,
      ),
    },
  };
  const { runs } = await openRunsTab(page, {
    runs: [runFixture("call")],
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
                  definition_version_id: CALLEE,
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
      await page.route(/\/projects\/[^/]+\/drafts(\?|$)/, (route) =>
        route.fulfill({ json: { items: [], next_cursor: null } }),
      );
    },
  });
  await expect(runs.rows.first()).toBeVisible();
  await runs.row(RUN_IDS.call).click();
  // The run is drawn before its steps take the keyboard.
  await expect(runs.canvas.locator(".run-header h2")).toHaveText(
    `Run ${RUN_IDS.call.slice(0, 8)}`,
  );
  await expect(runs.canvas.locator(".run-header .run-caption")).toBeVisible();
  const details = new StepDetailsPage(page);
  await details.openWithKeyboard("send");
  const card = details.dialog
    .locator('[data-region="output"]')
    .getByRole("group", { name: "Called run" });
  await expect(card.locator(".pane-child-line")).not.toBeEmpty();
  return { details, card };
}
