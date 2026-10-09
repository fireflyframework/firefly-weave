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
// The operations pages: per-view columns and one status vocabulary, the run
// detail as an operator page, live Runs filters, deciding a task with one
// inline confirmation, one "Send reply", and what local mode and a signed-out
// platform show instead of dead filters.
import { test, expect, Page, Request } from "@playwright/test";
import { allCapabilities, connected, offline } from "./support";
import { acmePlatform, platformHost } from "./platform-host";

const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";
const versionId = "11111111-1111-4111-8111-111111111111";
const uuid = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
const runs = [
  {
    id: "700587c4-1111-4111-8111-000000000104",
    business_key: "expense-104",
    correlation_key: "batch-7",
    artifact_digest: "sha256:0123456789abcdef",
    activation: {
      name: "expense-review",
      revision: 2,
      request: { version_id: versionId },
    },
    created_at: "2026-10-01T09:12:00Z",
    state: { status: "waiting", active: ["review"], steps: { check: {} } },
  },
  {
    id: "81c2d3e4-1111-4111-8111-000000000105",
    business_key: "expense-105",
    activation: { name: "expense-review", request: { version_id: versionId } },
    created_at: "2026-10-01T09:20:00Z",
    state: {
      status: "waiting",
      active: ["check"],
      incident: "The CRM answered 500 three times.",
      incidents: { a: { node_id: "check", code: "WV-TASK-RETRIES" } },
    },
  },
  {
    id: "92d3e4f5-1111-4111-8111-000000000106",
    created_at: "2026-10-01T10:02:00Z",
    state: { status: "failed", active: [] },
  },
];
const workflow = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "expense-review", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [
      { id: "check", kind: "transform", value: { literal: true } },
      {
        id: "review",
        kind: "humanTask",
        assignment: "reviewers",
        title: { literal: "Review" },
        context: { literal: {} },
        formSchema: { type: "object" },
        decisions: ["approve", "reject"],
      },
    ],
    output: { literal: {} },
  },
};
const task = {
  id: "task-1",
  run_id: runs[0].id,
  node_id: "review",
  revision: 1,
  status: "ready",
  title: "Review expense 104",
  context: { amount: 125, currency: "EUR", submittedBy: "jane@acme.example" },
  form_schema: { type: "object", properties: {} },
  decisions: ["approve", "reject"],
  claimant_id: null,
  due_at: "2026-10-03T17:00:00Z",
};

async function runPlatform(page: Page) {
  const queries: URL[] = [];
  await connected(page, {
    capabilities: [
      ...allCapabilities,
      "run.pause",
      "run.resume",
      "run.archive",
      "human_task.claim",
      "human_task.complete",
    ],
  });
  await page.route("**/projects/project/workflows?*", (r) =>
    r.fulfill({
      json: {
        items: [{ id: versionId, name: "expense-review", version: "1.0.0" }],
        next_cursor: null,
      },
    }),
  );
  await page.route(`${environment}/runs?*`, (r) => {
    const url = new URL(r.request().url());
    queries.push(url);
    const status = url.searchParams.get("status");
    const key = url.searchParams.get("business_key");
    const items = runs.filter(
      (run) =>
        (!status || run.state.status === status) &&
        (!key || run.business_key?.includes(key)),
    );
    return r.fulfill({ json: { items, next_cursor: null } });
  });
  for (const run of runs) {
    await page.route(`${environment}/runs/${run.id}`, (r) =>
      r.fulfill({ json: run }),
    );
    await page.route(`${environment}/runs/${run.id}/lifecycle`, (r) =>
      r.fulfill({ json: { archived: false, purged: false, revision: 1 } }),
    );
    await page.route(`${environment}/runs/${run.id}/history?*`, (r) =>
      r.fulfill({
        json: {
          events: [
            {
              id: "e1",
              type: "started",
              timestamp: "2026-10-01T09:12:00Z",
              data: {},
            },
            {
              id: "e2",
              type: "task_completed",
              timestamp: "2026-10-01T09:12:01Z",
              data: { node_id: "check" },
            },
          ],
          next_cursor: null,
        },
      }),
    );
  }
  await page.route(`**/projects/project/workflows/${versionId}/export`, (r) =>
    r.fulfill({ json: { id: versionId, document: workflow } }),
  );
  await page.route(`${environment}/human-tasks?*`, (r) => {
    const status = new URL(r.request().url()).searchParams.get("status");
    return r.fulfill({
      json: {
        items: !status || status === task.status ? [task] : [],
        next_cursor: null,
      },
    });
  });
  return queries;
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("runs read as workflows with tone pills, never as UUIDs", async ({
      page,
    }) => {
      await runPlatform(page);
      await page.getByRole("button", { name: "Runs", exact: true }).click();
      const rows = page.locator(".resource-row");
      await expect(rows).toHaveCount(3);
      await expect(
        page.getByRole("status").filter({ hasText: /runs$/ }),
      ).toHaveText("3 runs");
      await expect(rows.first()).toContainText("expense-review 1.0.0");
      await expect(rows.first()).toContainText("expense-104");
      await expect(rows.last()).toContainText("Run 92d3e4f5");
      for (const secondary of await rows.locator("small").allTextContents())
        expect(secondary).not.toMatch(uuid);
      await expect(rows.last().locator(".status-pill")).toHaveAttribute(
        "data-tone",
        "danger",
      );
      await expect(rows.last().locator(".status-pill")).toHaveText("Failed");
      if (viewport.width > 1024)
        await expect(
          page.getByRole("columnheader").allTextContents(),
        ).resolves.toEqual(["Run", "Status", "Started", "Duration"]);
    });

    test("a run's detail leads with what it waits for and hides the JSON", async ({
      page,
    }) => {
      await runPlatform(page);
      await page.getByRole("button", { name: "Runs", exact: true }).click();
      await page.locator(".resource-row").first().click();
      const detail = page.locator(".record-detail");
      await expect(detail.getByRole("heading", { level: 2 })).toHaveText(
        "expense-review 1.0.0",
      );
      // "Now" names the task and opens it.
      const now = detail.getByRole("region", { name: "Now" });
      await expect(now).toContainText(
        "Waiting for Review expense 104, assigned to reviewers",
      );
      // Exactly one of Pause and Resume.
      await expect(
        detail.getByRole("button", { name: "Pause run" }),
      ).toHaveCount(1);
      await expect(
        detail.getByRole("button", { name: "Resume run" }),
      ).toHaveCount(0);
      // The live thread: gold where the run is, a check where it has been.
      const graph = detail.getByRole("group", {
        name: "Workflow version for this run",
      });
      await expect(graph.locator(".run-node.is-live")).toContainText("review");
      await expect(graph.locator(".run-node.is-done")).toContainText("check");
      const title = await graph
        .locator(".run-node strong")
        .first()
        .evaluate((e) => parseFloat(getComputedStyle(e).fontSize));
      expect(title).toBeGreaterThanOrEqual(12);
      await expect(graph).toHaveAttribute("tabindex", "0");
      // The timeline in sentences; raw JSON only on request.
      await expect(detail.locator(".timeline")).toContainText("Run started");
      await expect(detail.locator(".timeline")).toContainText("check finished");
      for (const pre of await detail.locator("pre").all())
        await expect(pre).toBeHidden();
      await detail.getByText("Technical details").click();
      await expect(detail.locator(".technical pre")).toBeVisible();
      await expect(detail).not.toContainText(
        "API authorization is checked on every read and action.",
      );
      if (viewport.width === 1440) {
        const box = (await detail.boundingBox())!;
        expect(box.width).toBeGreaterThanOrEqual(900);
      }
      await page.route(`${environment}/human-tasks/task-1`, (r) =>
        r.fulfill({ json: task }),
      );
      await now.getByRole("button", { name: "Open task" }).click();
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(
        "My tasks",
      );
      await expect(
        page.locator(".record-detail").getByRole("heading", { level: 2 }),
      ).toHaveText("Review expense 104");
    });

    test("a run opened before identity arrives gains its readable task", async ({
      page,
    }) => {
      await page.addInitScript(() => {
        const fetchOriginal = window.fetch.bind(window);
        let release!: () => void;
        const identity = new Promise<void>((resolve) => (release = resolve));
        Object.assign(window, { releaseRunIdentity: release });
        window.fetch = async (...args: Parameters<typeof fetch>) => {
          const response = await fetchOriginal(...args);
          if (String(args[0]).endsWith("/studio/api/api/v1/identity"))
            await identity;
          return response;
        };
      });
      await runPlatform(page);
      await page.getByRole("button", { name: "Runs", exact: true }).click();
      await page.locator(".resource-row").first().click();
      const detail = page.locator(".record-detail");
      await expect(
        detail.getByRole("group", { name: "Workflow version for this run" }),
      ).toBeVisible();
      const now = detail.getByRole("region", { name: "Now" });
      await expect(now).toContainText("Waiting for a person at review");
      await page.evaluate(() =>
        (
          window as unknown as { releaseRunIdentity: () => void }
        ).releaseRunIdentity(),
      );
      await expect(
        detail.getByRole("button", { name: "Pause run" }),
      ).toBeVisible();
      await expect(now).toContainText(
        "Waiting for Review expense 104, assigned to reviewers",
      );
      await expect(
        now.getByRole("button", { name: "Open task", exact: true }),
      ).toBeVisible();
    });

    test("a task response for a previous run cannot enrich the selected run", async ({
      page,
    }) => {
      await runPlatform(page);
      await expect(
        page.getByRole("button", { name: "My tasks", exact: true }),
      ).toBeVisible();
      let release!: () => void;
      const gate = new Promise<void>((resolve) => (release = resolve));
      let started!: () => void;
      const requested = new Promise<void>((resolve) => (started = resolve));
      let finished!: () => void;
      const fulfilled = new Promise<void>((resolve) => (finished = resolve));
      await page.route(`${environment}/human-tasks?*`, async (r) => {
        if (new URL(r.request().url()).searchParams.get("status") !== "ready")
          return r.fulfill({ json: { items: [], next_cursor: null } });
        started();
        await gate;
        await r.fulfill({ json: { items: [task], next_cursor: null } });
        finished();
      });
      await page.getByRole("button", { name: "Runs", exact: true }).click();
      await page.locator(".resource-row").first().click();
      await requested;
      await page
        .getByRole("button", { name: "Close detail", exact: true })
        .click();
      await page.locator(".resource-row").nth(1).click();
      const detail = page.locator(".record-detail");
      await expect(detail).toContainText("expense-105");
      release();
      await fulfilled;
      await page.evaluate(
        () =>
          new Promise<void>((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
          ),
      );
      await expect(detail).not.toContainText("Review expense 104");
      await expect(
        detail.getByRole("button", { name: "Open task", exact: true }),
      ).toHaveCount(0);
    });

    test("an incident says where the run stopped and why", async ({ page }) => {
      await runPlatform(page);
      await page.getByRole("button", { name: "Runs", exact: true }).click();
      await page
        .locator(".resource-row")
        .filter({ hasText: "expense-105" })
        .click();
      const incident = page.locator(".record-detail .incident");
      await expect(incident).toContainText("This run stopped at check.");
      await expect(incident).toContainText("The CRM answered 500 three times.");
    });

    test("Runs filters apply as they change", async ({ page }) => {
      const queries = await runPlatform(page);
      await page.getByRole("button", { name: "Runs", exact: true }).click();
      await expect(page.locator(".resource-row")).toHaveCount(3);
      if (viewport.width <= 1024) {
        const toggle = page.getByRole("button", { name: "Filters (0)" });
        await toggle.click();
        await expect(toggle).toHaveAttribute("aria-expanded", "true");
      }
      await page.getByRole("button", { name: "Failed", exact: true }).click();
      await expect
        .poll(() => queries.at(-1)?.searchParams.get("status"))
        .toBe("failed");
      await expect(page.locator(".resource-row")).toHaveCount(1);
      await page.getByRole("button", { name: "All", exact: true }).click();
      await page.getByLabel("Search by business key").fill("expense-104");
      await expect
        .poll(() => queries.at(-1)?.searchParams.get("business_key"))
        .toBe("expense-104");
      await expect(page.locator(".resource-row")).toHaveCount(1);
      await page.getByRole("button", { name: "More filters" }).click();
      await page.getByLabel("Include archived runs").check();
      await expect
        .poll(() => queries.at(-1)?.searchParams.get("include_archived"))
        .toBe("true");
      await expect(
        page.getByRole("button", { name: "Apply filters" }),
      ).toHaveCount(0);
    });

    test("deciding a task asks once inline and shows the outcome", async ({
      page,
    }) => {
      await runPlatform(page);
      let current: Record<string, unknown> = { ...task };
      await page.route(`${environment}/human-tasks/task-1`, (r) =>
        r.fulfill({ json: current }),
      );
      await page.route(`${environment}/human-tasks/task-1/claim`, (r) => {
        current = {
          ...current,
          status: "claimed",
          claimant_id: "human",
          revision: 2,
        };
        return r.fulfill({ json: current });
      });
      const completes: Request[] = [];
      await page.route(`${environment}/human-tasks/task-1/complete`, (r) => {
        completes.push(r.request());
        current = { ...current, status: "completed", revision: 3 };
        return r.fulfill({ json: current });
      });
      await page.getByRole("button", { name: "My tasks", exact: true }).click();
      await expect(page.locator(".resource-row .status-pill")).toHaveText(
        "Ready to claim",
      );
      await page.locator(".resource-row").click();
      const detail = page.locator(".record-detail");
      await expect(detail).toContainText("€125.00");
      await expect(detail).toContainText("jane@acme.example");
      await expect(detail).not.toContainText("Acting as");
      await detail.getByRole("button", { name: "Claim task" }).click();
      const toast = page.locator(".toast");
      await expect(toast).toContainText(
        "Task claimed. Fill in the form, then choose a decision.",
        { timeout: 500 },
      );
      // The toast is announced by the polite live region that holds it.
      await expect(
        page.getByRole("status").filter({ has: toast }),
      ).toHaveAttribute("aria-atomic", "true");
      await detail
        .getByRole("button", { name: "Approve", exact: true })
        .click();
      const confirm = detail.locator(".decision-confirm");
      await expect(confirm).toContainText(
        "Approve Review expense 104? You can't change this later.",
      );
      expect(completes).toHaveLength(0);
      await confirm.getByRole("button", { name: "Back" }).click();
      await expect(confirm).toHaveCount(0);
      await detail
        .getByRole("button", { name: "Approve", exact: true })
        .click();
      await confirm
        .getByRole("button", { name: "Approve", exact: true })
        .click();
      await expect(toast).toContainText("Approved Review expense 104.");
      expect(completes).toHaveLength(1);
      expect(completes[0].postDataJSON()).toEqual({
        expected_revision: 2,
        decision: "approve",
        data: {},
      });
      // The toast goes away on its own.
      await expect(toast).toHaveCount(0, { timeout: 8000 });
    });

    test("one Send reply queues and sends, and an unknown send says what to do", async ({
      page,
    }) => {
      await connected(page, {
        capabilities: [...allCapabilities, "email.read", "email.send"],
      });
      const conversation = {
        id: "thread",
        connection_revision_id: "connection",
        subject: "Expense receipt",
        created_at: "2026-10-01T09:00:00Z",
      };
      await page.route("**/email/conversations?*", (r) =>
        r.fulfill({ json: { items: [conversation], next_cursor: null } }),
      );
      await page.route("**/email/conversations/thread", (r) =>
        r.fulfill({
          json: {
            conversation,
            messages: [
              {
                id: "message",
                direction: "inbound",
                state: "received",
                accepted_at: "2026-10-01T09:00:00Z",
                mail: {
                  sender: "person@example.test",
                  to: ["review@example.test"],
                  subject: "Receipt attached",
                  text: "Please review my expense.",
                },
              },
            ],
          },
        }),
      );
      await page.route("**/email/conversations/thread/reply", (r) =>
        r.fulfill({ json: { id: "submission", state: "queued" } }),
      );
      let sends = 0;
      await page.route("**/email/submissions/submission/execute", (r) => {
        sends++;
        return r.fulfill({ json: { id: "submission", state: "unknown" } });
      });
      await page.getByRole("button", { name: "Email", exact: true }).click();
      await page.locator(".resource-row").click();
      const detail = page.locator(".record-detail");
      await expect(detail).toContainText(
        "Mail servers don't confirm delivery or reading.",
      );
      await expect(detail).not.toContainText("SMTP");
      await detail
        .getByRole("textbox", { name: "Email reply" })
        .fill("Thanks, reviewing now.");
      await detail.getByRole("button", { name: "Send reply" }).click();
      await expect(detail).toContainText(
        "We couldn't confirm it was sent. Check the status before sending again.",
      );
      expect(sends).toBe(1);
      await expect(
        detail.getByRole("button", { name: "Check status" }),
      ).toBeVisible();
      await expect(
        detail.getByRole("button", { name: "Queue reply" }),
      ).toHaveCount(0);
    });
  });

test("local mode: operations pages offer a way to connect, not dead filters", async ({
  page,
}) => {
  await offline(page);
  for (const [view, heading] of [
    ["Runs", "Runs appear here once you connect"],
    ["My tasks", "Your tasks appear here once you connect"],
    ["Email", "Email threads appear here once you connect"],
    ["Connections", "Connections live on a platform"],
    ["Workers", "Workers appear here once you connect"],
  ]) {
    await page.getByRole("button", { name: view, exact: true }).click();
    await expect(
      page.locator(".empty-state").getByRole("heading", { level: 2 }),
    ).toHaveText(heading);
    await expect(
      page.getByRole("button", { name: "Connect to a platform" }),
    ).toHaveClass(/primary/);
    await expect(page.getByRole("searchbox")).toHaveCount(0);
    await expect(page.locator(".search-field")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Refresh" })).toHaveCount(0);
  }
  await page.getByRole("button", { name: "Connect to a platform" }).click();
  await expect(page.locator("weave-connection-wizard")).toBeVisible();
});

test("signed out: a calm banner offers sign-in and the filters wait", async ({
  page,
}) => {
  await platformHost(page, { platforms: [acmePlatform()], active: "Acme" });
  await page.locator(".platform-indicator").click();
  await page
    .locator("#platform-menu")
    .getByRole("button", { name: "Sign out" })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Sign out", exact: true })
    .click();
  await expect(page.locator(".toast")).toContainText("Signed out of Acme.");
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  const banner = page.locator(".records-banner");
  await expect(banner).toContainText(
    "You're signed out of Acme. Sign in to see runs and tasks.",
  );
  await expect(banner.getByRole("button", { name: "Sign in" })).toBeVisible();
  await expect(page.getByLabel("Search by business key")).toBeDisabled();
});

test("the unknown-outcome banner asks to check, and checking resends the same request", async ({
  page,
}) => {
  await connected(page, {
    capabilities: [...allCapabilities, "run.pause", "run.resume"],
  });
  const run = runs[0];
  await page.route(`${environment}/runs?*`, (r) =>
    r.fulfill({ json: { items: [run], next_cursor: null } }),
  );
  await page.route(`${environment}/runs/${run.id}`, (r) =>
    r.fulfill({ json: run }),
  );
  const keys: string[] = [];
  await page.route(`${environment}/runs/${run.id}/pause`, (r) => {
    keys.push(r.request().headers()["idempotency-key"]);
    if (keys.length === 1) return r.abort("failed");
    return r.fulfill({
      json: { ...run, state: { ...run.state, manual_paused: true } },
    });
  });
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  await page.locator(".resource-row").click();
  await page.getByRole("button", { name: "Pause run" }).click();
  const dialog = page.getByRole("dialog", { name: "Pause this run?" });
  // A reason is required, and the dialog says so in plain words.
  await dialog.getByLabel("Reason").fill(" ");
  await expect(dialog).toContainText("Enter a reason.");
  await dialog.getByLabel("Reason").fill("Waiting for finance");
  await dialog.getByRole("button", { name: "Pause run" }).click();
  const banner = page.locator(".unknown-banner");
  await expect(banner).toContainText(
    'Studio didn\'t get an answer for "Pause run".',
  );
  await expect(banner).toContainText(
    "It may have worked. Check before you do anything else.",
  );
  await banner.getByRole("button", { name: "Check now" }).click();
  await expect(banner).toHaveCount(0);
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  await expect(page.locator(".toast")).toContainText(
    "Checked. Pause run finished.",
  );
});
