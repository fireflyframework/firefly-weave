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
import { selectChoice } from "./support";
import { execFileSync } from "node:child_process";
import { test, expect, Page } from "@playwright/test";
import { chooseAction } from "./integrations-po";
import { python, pythonAvailable } from "../python-path";
const profile = {
  name: "Test platform",
  baseUrl: "https://weave.invalid",
  tenantId: "tenant",
  projectId: "project",
  environmentId: "development",
};
const capabilities = [
  "catalog.read",
  "definition.write",
  "definition.publish",
  "release.activate",
  "run.start",
  "run.archive",
  "run.purge",
  "human_task.read",
  "human_task.claim",
  "human_task.release",
  "human_task.complete",
  "email.read",
  "email.send",
];
async function connected(page: Page) {
  await page.route("**/studio/session", (r) =>
    r.fulfill({
      json: {
        paired: true,
        csrfToken: "test-csrf",
        version: "1",
        mode: "connected",
        profile,
      },
    }),
  );
  await page.route("**/studio/api/api/v1/identity", (r) =>
    r.fulfill({
      json: {
        principal_id: "human",
        kind: "human",
        grants: [
          {
            role: "test",
            scope: {
              tenant_id: "tenant",
              project_id: "project",
              environment_id: "development",
            },
            resources: [],
            capabilities,
          },
        ],
        workspaces: [],
        truncated: false,
      },
    }),
  );
  await page.route("**/studio/api/api/v1/**", (r) =>
    r.fulfill({ json: { items: [], next_cursor: null } }),
  );
  // The last registered matching route wins; explicit identity must follow the catch-all.
  await page.route("**/studio/api/api/v1/identity", (r) =>
    r.fulfill({
      json: {
        principal_id: "human",
        kind: "human",
        grants: [
          {
            role: "test",
            scope: {
              tenant_id: "tenant",
              project_id: "project",
              environment_id: "development",
            },
            resources: [],
            capabilities,
          },
        ],
        workspaces: [],
        truncated: false,
      },
    }),
  );
  await page.route("**/studio/local/validate", (r) =>
    r.fulfill({
      json: {
        validationOk: true,
        errorCount: 0,
        diagnostics: [],
        partial: true,
      },
    }),
  );
  await page.goto("/");
}
test("draft conflict retains source and lost save reconciles the same request identity", async ({
  page,
}) => {
  await connected(page);
  await page.getByRole("button", { name: "New workflow", exact: true }).click();
  await page.locator(".palette-step").filter({ hasText: "Transform" }).click();
  const keys: string[] = [];
  let calls = 0;
  await page.route("**/drafts/*", async (r) => {
    keys.push(r.request().headers()["idempotency-key"]);
    calls++;
    if (calls === 1) return r.abort("failed");
    await r.fulfill({
      json: {
        id: "draft",
        revision: 1,
        document: r.request().postDataJSON().document,
      },
    });
  });
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect(page.locator(".unknown-banner")).toContainText(
    'Studio didn\'t get an answer for "Save draft".',
  );
  await page.getByRole("button", { name: "Check now" }).click();
  await expect(page.getByRole("button", { name: "Check now" })).toHaveCount(0);
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  await page.route("**/drafts/*", (r) =>
    r.fulfill({ status: 409, json: { code: "WV-REVISION", revision: 2 } }),
  );
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "Draft changed on the server",
  );
  await page.getByRole("button", { name: "Keep editing locally" }).click();
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  await expect(
    page.getByRole("textbox", { name: "Workflow source" }),
  ).toHaveValue(/transform-1/);
});
test("task claim/form/decision uses actual revision and preserves revoked form", async ({
  page,
}) => {
  await connected(page);
  let task: any = {
    id: "task",
    run_id: "run",
    node_id: "review",
    revision: 1,
    status: "ready",
    title: "Review expense",
    context: { amount: 125 },
    form_schema: {
      type: "object",
      required: ["note"],
      properties: { note: { type: "string", title: "Review note" } },
    },
    decisions: ["approve", "reject"],
    claimant_id: null,
  };
  await page.route("**/human-tasks?*", (r) =>
    r.fulfill({ json: { items: [task], next_cursor: null } }),
  );
  await page.route("**/human-tasks/task", (r) => r.fulfill({ json: task }));
  await page.route("**/human-tasks/task/claim", (r) => {
    expect(r.request().postDataJSON()).toEqual({ expected_revision: 1 });
    task = { ...task, status: "claimed", claimant_id: "human", revision: 2 };
    return r.fulfill({ json: task });
  });
  await page.getByRole("button", { name: "My tasks", exact: true }).click();
  await page.locator(".resource-row").click();
  await page.getByRole("button", { name: "Claim task", exact: true }).click();
  await page.getByLabel("Review note").fill("Reviewed receipt");
  await page.route("**/human-tasks/task/complete", (r) => {
    expect(r.request().postDataJSON()).toEqual({
      expected_revision: 2,
      decision: "approve",
      data: { note: "Reviewed receipt" },
    });
    return r.fulfill({ status: 403, json: { code: "WV-DENIED" } });
  });
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  // Deciding asks once, inline, before anything is sent.
  await page
    .locator(".decision-confirm")
    .getByRole("button", { name: "Approve", exact: true })
    .click();
  await expect(page.locator(".error-banner")).toContainText(
    "This task changed or you lost access to it. Your answers are still here.",
  );
  await expect(page.getByLabel("Review note")).toHaveValue("Reviewed receipt");
  await page.screenshot({ path: "test-results/task-form.png" });
});
test("one Send reply queues and sends; an unknown send is never retried by itself", async ({
  page,
}) => {
  await connected(page);
  const conversation = {
    id: "thread",
    connection_revision_id: "connection",
    subject: "Expense receipt",
    created_at: "2026-10-01",
  };
  const detail = {
    conversation,
    messages: [
      {
        id: "message",
        direction: "inbound",
        state: "received",
        accepted_at: "2026-10-01",
        mail: {
          sender: "person@example.test",
          to: ["review@example.test"],
          subject: "Receipt attached",
          text: "Please review my expense.",
        },
      },
    ],
  };
  await page.route("**/email/conversations?*", (r) =>
    r.fulfill({ json: { items: [conversation], next_cursor: null } }),
  );
  await page.route("**/email/conversations/thread", (r) =>
    r.fulfill({ json: detail }),
  );
  let sends = 0;
  await page.route("**/email/conversations/thread/reply", (r) => {
    const body = r.request().postDataJSON();
    expect(body).toMatchObject({
      connection_revision_id: "connection",
      parent_message_id: "message",
      text: "Thanks, reviewing now.",
      reply_all: false,
    });
    expect(body.request_id).toBeTruthy();
    return r.fulfill({
      json: {
        id: "submission",
        state: "queued",
        conversation_id: "thread",
        message_id: "reply",
      },
    });
  });
  await page.route("**/email/submissions/submission/execute", (r) => {
    sends++;
    return r.fulfill({
      json: {
        id: "submission",
        state: "unknown",
        conversation_id: "thread",
        message_id: "reply",
      },
    });
  });
  await page.getByRole("button", { name: "Email", exact: true }).click();
  await page.locator(".resource-row").click();
  await page
    .getByRole("textbox", { name: "Email reply" })
    .fill("Thanks, reviewing now.");
  await page.getByRole("button", { name: "Send reply", exact: true }).click();
  await expect(page.locator(".submission-status")).toContainText(
    "We couldn't confirm it was sent. Check the status before sending again.",
  );
  expect(sends).toBe(1);
  await expect(page.getByRole("button", { name: "Send now" })).toHaveCount(0);
  await page.waitForTimeout(500);
  expect(sends).toBe(1);
  await page.screenshot({ path: "test-results/email-thread.png" });
});

test("run keys group separate executions and filters reset the cursor", async ({
  page,
}) => {
  await connected(page);
  const keys: string[] = [];
  const starts: any[] = [];
  const queries: URL[] = [];
  await page.route("**/activations?*", (r) =>
    r.fulfill({
      json: {
        items: [{ id: "activation", name: "Expense approval", revision: 1 }],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/runs?*", (r) => {
    queries.push(new URL(r.request().url()));
    return r.fulfill({ json: { items: [], next_cursor: "old-page" } });
  });
  await page.route("**/environments/development/runs", (r) => {
    starts.push(r.request().postDataJSON());
    keys.push(r.request().headers()["idempotency-key"]);
    return r.fulfill({
      status: 201,
      json: {
        id: "run-" + starts.length,
        state: { status: "queued" },
        business_key: "expense-104",
        correlation_key: "batch-2026",
      },
    });
  });
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  for (let i = 0; i < 2; i++) {
    await page.getByRole("button", { name: "Start run", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Start a run" });
    await selectChoice(
      dialog.getByLabel("Version to run", { exact: true }),
      "activation",
    );
    await dialog.getByText("Add a business key (optional)").click();
    await dialog
      .getByLabel("Business key", { exact: true })
      .fill("expense-104");
    await dialog
      .getByLabel("Correlation key", { exact: true })
      .fill("batch-2026");
    await dialog
      .getByRole("button", { name: "Start run", exact: true })
      .click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
  }
  expect(starts).toEqual([
    {
      activation_id: "activation",
      input: {},
      business_key: "expense-104",
      correlation_key: "batch-2026",
    },
    {
      activation_id: "activation",
      input: {},
      business_key: "expense-104",
      correlation_key: "batch-2026",
    },
  ]);
  expect(keys[0]).not.toBe(keys[1]);
  // Filters apply as they change: no Apply button.
  await page.getByLabel("Search by business key").fill("expense-104");
  await page.getByRole("button", { name: "More filters" }).click();
  await page.getByLabel("Correlation key", { exact: true }).fill("batch-2026");
  await page.getByRole("button", { name: "Waiting", exact: true }).click();
  await expect
    .poll(() => queries.at(-1)?.searchParams.get("status"))
    .toBe("waiting");
  await expect
    .poll(() => queries.at(-1)?.searchParams.get("correlation_key"))
    .toBe("batch-2026");
  expect(queries.at(-1)?.searchParams.get("business_key")).toBe("expense-104");
  expect(queries.at(-1)?.searchParams.has("cursor")).toBe(false);
  await page.getByLabel("Search by business key").fill("");
  await expect
    .poll(() => queries.at(-1)?.searchParams.has("business_key"))
    .toBe(false);
});

test("archive and purge require terminal state, revision and exact run ID confirmation", async ({
  page,
}) => {
  await connected(page);
  const id = "00000000-0000-0000-0000-000000000104";
  let lifecycle: any = {
    run_id: id,
    archived: false,
    revision: 1,
    purged: false,
  };
  const run = {
    id,
    business_key: "expense-104",
    correlation_key: "batch",
    state: { status: "succeeded", active: [] },
    activation: { request: { version_id: "version" } },
  };
  await page.route("**/runs?*", (r) =>
    r.fulfill({ json: { items: [run], next_cursor: null } }),
  );
  await page.route("**/runs/" + id, (r) => r.fulfill({ json: run }));
  await page.route("**/runs/" + id + "/history?*", (r) =>
    r.fulfill({ json: { events: [], next_cursor: null } }),
  );
  await page.route("**/runs/" + id + "/lifecycle", (r) =>
    r.fulfill({ json: lifecycle }),
  );
  await page.route("**/workflows/version/export", (r) =>
    r.fulfill({
      json: {
        document: {
          apiVersion: "weave/v1alpha1",
          kind: "Workflow",
          metadata: { name: "expense-approval", version: "1.0.0" },
          spec: { steps: [], output: { literal: {} } },
        },
      },
    }),
  );
  await page.route("**/runs/" + id + "/archive", (r) => {
    expect(r.request().postDataJSON()).toEqual({
      expected_revision: 1,
      reason: "Work finished",
    });
    lifecycle = { ...lifecycle, archived: true, revision: 2 };
    return r.fulfill({ json: lifecycle });
  });
  let purges = 0;
  await page.route("**/runs/" + id + "/purge", (r) => {
    purges++;
    expect(r.request().postDataJSON()).toEqual({
      expected_revision: 2,
      reason: "Retention policy",
      confirm_run_id: id,
    });
    lifecycle = { ...lifecycle, purged: true, revision: 3 };
    return r.fulfill({ json: lifecycle });
  });
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  await page.locator(".resource-row").click();
  // In-app dialogs replace window.prompt(), which the desktop webview answers with null.
  const dialog = page.getByRole("dialog");
  await page.getByRole("button", { name: "Archive run", exact: true }).click();
  await dialog.getByLabel("Reason").fill("Work finished");
  await dialog
    .getByRole("button", { name: "Archive run", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Restore run", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Delete run data", exact: true })
    .click();
  await expect(dialog).toContainText("Delete this run's data?");
  await dialog.getByLabel("Reason").fill("Retention policy");
  await dialog.getByLabel("Run ID").fill("wrong-id");
  await expect(
    dialog.getByRole("button", { name: "Delete run data", exact: true }),
  ).toBeDisabled();
  await expect(dialog).toContainText("Type the exact run ID to confirm.");
  expect(purges).toBe(0);
  await dialog.getByLabel("Run ID").fill(id);
  await dialog
    .getByRole("button", { name: "Delete run data", exact: true })
    .click();
  await expect.poll(() => purges).toBe(1);
  await expect(page.locator(".run-archive")).toContainText(
    "Data deleted. The audit record stays.",
  );
  await page.screenshot({ path: "test-results/run-lifecycle.png" });
});

test("task JSON validity and task identity protect submitted data", async ({
  page,
}) => {
  await connected(page);
  const base = {
    run_id: "run",
    node_id: "review",
    revision: 1,
    status: "claimed",
    claimant_id: "human",
    decisions: ["approve"],
  };
  const tasks = [
    {
      ...base,
      id: "first",
      title: "First review",
      form_schema: {
        type: "object",
        // A union stays a JSON field the person types.
        properties: {
          details: { type: ["object", "array"] },
          old: { type: "string" },
        },
      },
    },
    {
      ...base,
      id: "second",
      title: "Second review",
      form_schema: { type: "object", properties: { note: { type: "string" } } },
    },
  ];
  await page.route("**/human-tasks?*", (r) =>
    r.fulfill({ json: { items: tasks, next_cursor: null } }),
  );
  for (const task of tasks)
    await page.route(`**/human-tasks/${task.id}`, (r) =>
      r.fulfill({ json: task }),
    );
  let commands = 0;
  await page.route("**/human-tasks/*/complete", (r) => {
    commands++;
    expect(r.request().postDataJSON().data).toEqual({ note: "New task only" });
    return r.fulfill({
      json: { ...tasks[1], status: "completed", revision: 2 },
    });
  });
  await page.getByRole("button", { name: "My tasks", exact: true }).click();
  await page
    .locator(".resource-row")
    .filter({ hasText: "First review" })
    .click();
  await page.getByLabel(/^Details(\s*\(optional\))?$/).fill('{"valid":true}');
  await page.getByLabel(/^Old(\s*\(optional\))?$/).fill("Must not survive");
  await page.getByLabel(/^Details(\s*\(optional\))?$/).fill("{");
  // Invalid JSON keeps the decision buttons disabled, and they say why (F9).
  const approve = page.getByRole("button", { name: "Approve", exact: true });
  await expect(approve).toBeDisabled();
  await expect(approve).toHaveAccessibleDescription(
    "Correct the fields marked with an error.",
  );
  expect(commands).toBe(0);
  expect(
    await page
      .getByLabel(/^Details(\s*\(optional\))?$/)
      .evaluate((el: HTMLTextAreaElement) => el.validity.valid),
  ).toBe(false);
  await page
    .locator(".resource-row")
    .filter({ hasText: "Second review" })
    .click();
  await page.getByLabel(/^Note(\s*\(optional\))?$/).fill("New task only");
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await page
    .locator(".decision-confirm")
    .getByRole("button", { name: "Approve", exact: true })
    .click();
  await expect.poll(() => commands).toBe(1);
});

test("catalog integration fields compile against the real Python action contract", async ({
  page,
}) => {
  await connected(page);
  const inputSchema = {
    type: "object",
    required: ["customer"],
    properties: {
      customer: {
        type: "string",
        title: "Customer identifier",
        minLength: 1,
        description: "The customer record to retrieve",
      },
    },
  };
  const action = {
    apiVersion: "weave/v1alpha1",
    kind: "Action",
    metadata: { name: "lookup-customer", version: "1.0.0" },
    spec: {
      implementation: {
        kind: "connector",
        uses: "crm@1.0.0",
        action: "lookup",
      },
      sideEffect: "read_only",
      timeoutSeconds: 10,
      inputSchema,
      outputSchema: { type: "object" },
    },
  };
  await page.route("**/actions?*", (r) =>
    r.fulfill({
      json: {
        items: [{ id: "action", name: "lookup-customer", version: "1.0.0" }],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/actions/action/export", (r) =>
    r.fulfill({
      json: {
        id: "action",
        name: "lookup-customer",
        version: "1.0.0",
        document: action,
      },
    }),
  );
  await page.getByRole("button", { name: "New workflow", exact: true }).click();
  await page
    .locator(".palette-step")
    .filter({ hasText: "Call an action" })
    .click();
  await chooseAction(page, "lookup-customer@1.0.0");
  await page.locator(".action-details > summary").click();
  await expect(page.getByText("crm@1.0.0", { exact: true })).toBeVisible();
  await page.locator(".inspector-header h2").click();
  await page
    .getByRole("textbox", { name: "Customer identifier", exact: true })
    .fill("customer-104");
  await page.locator(".inspector-header h2").click();
  await page
    .getByRole("button", {
      name: "Use action output as workflow result",
      exact: true,
    })
    .click();
  await page
    .getByRole("dialog", { name: "Replace the workflow result?" })
    .getByRole("button", { name: "Replace result", exact: true })
    .click();
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  const source = await page
    .getByRole("textbox", { name: "Workflow source" })
    .inputValue();
  expect(source).toContain("uses: lookup-customer@1.0.0");
  expect(source).toContain("customer: customer-104");
  if (pythonAvailable()) {
    const script = `import json,sys
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
p=json.load(sys.stdin)
a=p['action']
c={'apiVersion':'weave/v1alpha1','kind':'Connector','metadata':{'name':'crm','version':'1.0.0'},'spec':{'adapter':'http','configSchema':{},'authSchema':{},'compatibility':{'apiVersion':'weave/v1alpha1'},'limits':{'maxRequestBytes':10000,'maxResponseBytes':10000,'maxTimeoutSeconds':10},'actions':{'lookup':{'inputSchema':a['spec']['inputSchema'],'outputSchema':a['spec']['outputSchema'],'sideEffect':'read_only','timeoutSeconds':10}}}}
r=compile_source(p['source'],format='yaml',catalog=CatalogSnapshot.from_definitions([load_definition(a),load_definition(c)],adapters=['http']))
print(r.to_bytes().decode())`;
    const result = JSON.parse(
      execFileSync(python, ["-c", script], {
        input: JSON.stringify({ source, action }),
        encoding: "utf8",
      }),
    );
    expect(result.validationOk, JSON.stringify(result.diagnostics)).toBe(true);
    expect(result.partial).toBe(false);
  }
  await page.getByRole("tab", { name: "Designer", exact: true }).click();
  await page.screenshot({ path: "test-results/catalog-integration.png" });
});
