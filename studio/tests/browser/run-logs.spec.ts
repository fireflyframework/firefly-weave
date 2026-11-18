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
// A run's Logs section in Operate › Runs: after Steps, every level and every
// step of the open run, newest at the bottom, read only once a run opens;
// filters that ask the platform again; log text shown as text; an older
// platform asked once; a running run's new entries arriving at the bottom;
// and the access, withheld, slow and empty states. The platform is mocked.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page, type Route } from "@playwright/test";
import { answerRunViews } from "../../src/app/operate/run-views-fake";
import {
  fixtureIds,
  runViewsFixture,
  type RunViewsFixture,
} from "../../src/app/operate/run-views.fixture";
import type { RunLogEntry } from "../../src/app/operate/run-contracts";
import { connected } from "./support";

const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";
const captures = resolve(
  process.env["STUDIO_OPERATE_CAPTURES"]?.trim() || "test-results/operate",
);
mkdirSync(captures, { recursive: true });
const NOW = "2026-10-07T12:00:00Z";
const reader = ["run.read", "catalog.read"];

let sequence = 0;
const entry = (facts: Partial<RunLogEntry>): RunLogEntry => ({
  id: `aa000000-0000-4000-8000-${String(++sequence).padStart(12, "0")}`,
  at: "2026-10-07T10:00:00.000Z",
  source: "engine",
  level: "info",
  code: null,
  message: "",
  node_id: null,
  instance_key: "",
  attempt: null,
  worker_id: null,
  fields: {},
  redactions: 0,
  truncated: false,
  ...facts,
});
const worker = "0f1e2d3c-4b5a-4968-8776-655443322110";
/** The failed run's entries, oldest first, as runs.logs returns them. */
const failedLogs = (): RunLogEntry[] => [
  entry({
    at: "2026-10-07T10:00:00.204Z",
    code: "RUN.STARTED",
    message: "Run started",
  }),
  entry({
    at: "2026-10-07T10:00:00.611Z",
    source: "worker",
    code: "CUSTOMER.LOOKED_UP",
    message: "Looked up 1 customer in billing-db",
    node_id: "fetch",
    attempt: 1,
    worker_id: worker,
    fields: { customers: 1 },
    redactions: 1,
  }),
  entry({
    at: "2026-10-07T10:00:04.720Z",
    code: "STEP.SUCCEEDED",
    message: "fetch finished",
    node_id: "fetch",
  }),
  entry({
    at: "2026-10-07T10:00:05.731Z",
    code: "CALL.COMPLETED",
    message: "notify call finished",
    node_id: "notify",
  }),
  entry({
    at: "2026-10-07T10:01:01.702Z",
    source: "connector",
    level: "warning",
    code: "HTTP.RESPONSE",
    message: "POST smtp-relay.acme.example answered 550 in 312 ms",
    node_id: "send",
    instance_key: "send[3]",
    attempt: 3,
    worker_id: worker,
    fields: { method: "POST", host: "smtp-relay.acme.example", status: 550 },
  }),
  entry({
    at: "2026-10-07T10:01:03.918Z",
    level: "error",
    code: "SMTP_REJECTED",
    message: "send failed: SMTP_REJECTED",
    node_id: "send",
    instance_key: "send[3]",
  }),
];
const levels = ["debug", "info", "warning", "error"];

interface Platform {
  /** Each runs.logs request, in order. */
  logs: URL[];
  /** The entries runs.logs answers per run; a number answers that status. */
  answers: Map<string, RunLogEntry[] | number>;
  fixture: RunViewsFixture;
}
/** Connects at NOW and answers run summaries, steps, run details and logs. */
async function platform(
  page: Page,
  answers: Record<string, RunLogEntry[] | number> = {
    [fixtureIds.failed]: failedLogs(),
  },
  capabilities = reader,
): Promise<Platform> {
  await page.clock.install({ time: new Date(NOW) });
  const identity = page.waitForResponse("**/studio/api/api/v1/identity");
  await connected(page, { capabilities });
  await identity;
  const fixture = structuredClone(runViewsFixture);
  const result: Platform = {
    logs: [],
    answers: new Map(Object.entries(answers)),
    fixture,
  };
  const views = async (route: Route) => {
    const answer = answerRunViews(route.request().url(), fixture)!;
    await route.fulfill({ status: answer.status, json: answer.body });
  };
  await page.route("**/run-summaries?**", views);
  await page.route("**/runs/*/steps**", views);
  await page.route("**/runs/*/logs**", async (route) => {
    const url = new URL(route.request().url());
    result.logs.push(url);
    const id = url.pathname.split("/").at(-2)!;
    const answer = result.answers.get(id) ?? [];
    if (typeof answer === "number")
      return route.fulfill({
        status: answer,
        json: {
          501: { code: "WV-UNAVAILABLE", message: "Not served" },
          403: { code: "WV-FORBIDDEN", message: "Forbidden" },
          409: {
            code: "WV-LEGACY-UNAVAILABLE",
            message: "Step classification is unavailable",
          },
          503: {
            code: "WV-UNAVAILABLE",
            message:
              "Logs took too long to read. Narrow the time range or the filters, or try again.",
          },
        }[answer],
      });
    const level = url.searchParams.get("level");
    const step = url.searchParams.get("node_id");
    const items = answer.filter(
      (item) =>
        (!level || levels.indexOf(item.level) >= levels.indexOf(level)) &&
        (!step || item.node_id === step),
    );
    await route.fulfill({ json: { items, next_cursor: null } });
  });
  await page.route(`${environment}/runs/*`, async (route) => {
    const id = new URL(route.request().url()).pathname.split("/").pop()!;
    const run = fixture.runs.find((item) => item.summary.id === id)?.summary;
    if (!run)
      return route.fulfill({
        status: 404,
        json: { code: "WV-NOT-FOUND", message: "Run not found" },
      });
    await route.fulfill({
      json: {
        id,
        business_key: run.business_key,
        correlation_key: run.correlation_key,
        artifact_digest: "sha256:" + "0".repeat(64),
        activation: {
          id: run.activation_id,
          name: run.workflow.name,
          revision: run.activation.revision,
        },
        state: { status: run.status, active: [], input: {} },
      },
    });
  });
  await page.route(`${environment}/runs/*/lifecycle`, (route) =>
    route.fulfill({ json: { archived: false, purged: false, revision: 1 } }),
  );
  await page.route(`${environment}/runs/*/history?*`, (route) =>
    route.fulfill({ json: { events: [], next_cursor: null } }),
  );
  return result;
}
const section = (page: Page) =>
  page.getByRole("region", { name: "Logs", exact: true });
const entries = (page: Page) =>
  section(page).getByRole("table", { name: "Log entries" });
const rowsOf = (page: Page) => entries(page).locator(".entry:not(.head)");
/** Opens a run listed on the page by its ID (another row may name it as its origin). */
const openListed = (page: Page, id: string) =>
  page.locator(`.run-row:has(.cell-run code[title="${id}"]) .row-open`).click();

test.use({ timezoneId: "UTC" });

for (const size of [
  { width: 1440, height: 900 },
  { width: 360, height: 640 },
]) {
  test.describe(`a run's Logs section at ${size.width} px`, () => {
    test.use({ viewport: size });

    test("a run's logs show time, level, source, step, message and hidden values", async ({
      page,
    }) => {
      const runs = await platform(page);
      await page.goto(`/operate/runs/${fixtureIds.failed}`);
      const logs = section(page);
      await expect(logs).toBeVisible();
      // Logs follows Steps in the run's detail.
      const order = await page
        .locator(".record-detail")
        .evaluate((detail) =>
          [...detail.querySelectorAll("section h3")].map((h) =>
            h.textContent?.trim(),
          ),
        );
      expect(order.indexOf("Logs")).toBe(order.indexOf("Steps") + 1);
      await expect(rowsOf(page)).toHaveCount(6);
      const second = rowsOf(page).nth(1);
      await expect(second.locator(".t")).toHaveText("10:00:00.611");
      await expect(second.locator(".lv")).toHaveText("Info");
      await expect(second.locator(".src")).toHaveText("Worker");
      await expect(second.locator(".step")).toHaveText("fetch");
      await expect(second.locator(".msg")).toContainText(
        "Looked up 1 customer in billing-db",
      );
      await expect(second.locator(".flag")).toHaveText("1 value hidden");
      await expect(second.locator(".details")).toHaveText(
        "CUSTOMER.LOOKED_UP  customers=1",
      );
      // Newest at the bottom.
      await expect(rowsOf(page).last().locator(".msg")).toContainText(
        "send failed: SMTP_REJECTED",
      );
      await expect(rowsOf(page).last().locator(".lv")).toHaveText("Error");
      await expect(rowsOf(page).first().locator(".step")).toHaveText("—");
      await expect(logs).toContainText(
        "6 entries · engine entries come from the run's history; worker and connector entries are kept for 7 days.",
      );
      expect(runs.logs).toHaveLength(1);
      expect(runs.logs[0].searchParams.has("level")).toBe(false);
      expect(runs.logs[0].searchParams.has("node_id")).toBe(false);
      await logs.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${captures}/run-logs-${size.width}.png` });
      await logs.screenshot({
        path: `${captures}/run-logs-${size.width}-section.png`,
      });
    });

    test("the level filter defaults to everything for one run, and each filter asks again", async ({
      page,
    }) => {
      const runs = await platform(page);
      await page.goto(`/operate/runs/${fixtureIds.failed}`);
      const level = section(page).getByLabel("Level");
      const step = section(page).getByLabel("Step");
      await expect(level).toHaveValue("");
      await expect(level.locator("option:checked")).toHaveText("Everything");
      await expect(step.locator("option:checked")).toHaveText("All steps");
      await expect(rowsOf(page)).toHaveCount(6);
      await level.selectOption({ label: "Warning or higher" });
      await expect(rowsOf(page)).toHaveCount(2);
      expect(runs.logs.at(-1)!.searchParams.get("level")).toBe("warning");
      await step.selectOption("fetch");
      await expect(section(page)).toContainText("No log entries match.");
      expect(runs.logs.at(-1)!.searchParams.get("node_id")).toBe("fetch");
      await level.selectOption({ label: "Everything" });
      await expect(rowsOf(page)).toHaveCount(2);
      expect(runs.logs.at(-1)!.searchParams.has("level")).toBe(false);
    });
  });
}

test.describe("a run's Logs section", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("reads logs only once a run opens", async ({ page }) => {
    const runs = await platform(page);
    await page.goto("/operate/runs");
    await expect(page.locator(".run-row").first()).toBeVisible();
    await page.clock.runFor(12_000);
    expect(runs.logs).toEqual([]);
    await openListed(page, fixtureIds.failed);
    await expect(rowsOf(page)).toHaveCount(6);
    expect(runs.logs).toHaveLength(1);
    // A finished run's logs are not read again.
    await page.clock.runFor(12_000);
    expect(runs.logs).toHaveLength(1);
  });

  test("markup in a log message shows as text", async ({ page }) => {
    const markup =
      '<img src="x" onerror="window.__logged = 1"><b>bold</b> & <script>window.__logged = 2</script>';
    await platform(page, {
      [fixtureIds.failed]: [
        entry({ source: "worker", message: markup, node_id: "fetch" }),
      ],
    });
    await page.goto(`/operate/runs/${fixtureIds.failed}`);
    const message = rowsOf(page).first().locator(".msg-text");
    await expect(message).toHaveText(markup);
    await expect(entries(page).locator("img, b, script")).toHaveCount(0);
    expect(
      await page.evaluate(() => (window as { __logged?: number }).__logged),
    ).toBeUndefined();
  });

  test("an older platform says it can't show logs and is asked once", async ({
    page,
  }) => {
    const runs = await platform(page, {
      [fixtureIds.failed]: 501,
      [fixtureIds.succeeded]: 501,
    });
    await page.goto(`/operate/runs/${fixtureIds.failed}`);
    await expect(section(page)).toContainText(
      "This platform can't show logs yet.",
    );
    await expect(section(page).getByLabel("Level")).toHaveCount(0);
    await openListed(page, fixtureIds.succeeded);
    await expect(page.locator("#record-detail-title")).toBeVisible();
    await expect(section(page)).toContainText(
      "This platform can't show logs yet.",
    );
    expect(runs.logs).toHaveLength(1);
  });

  test("a running run's new entries arrive at the bottom", async ({ page }) => {
    const first = entry({
      at: "2026-10-07T11:30:02.000Z",
      source: "worker",
      message: "Prompt assembled",
      node_id: "support",
      instance_key: "support#1",
    });
    const runs = await platform(page, { [fixtureIds.running]: [first] });
    await page.goto(`/operate/runs/${fixtureIds.running}`);
    await expect(rowsOf(page)).toHaveCount(1);
    runs.answers.set(fixtureIds.running, [
      first,
      entry({
        at: "2026-10-07T11:30:05.000Z",
        source: "ai",
        code: "AI.USAGE",
        message: "AI turn finished",
        node_id: "support",
        instance_key: "support#2",
        fields: { input_tokens: 1004, output_tokens: 41 },
      }),
    ]);
    await page.clock.runFor(5_500);
    await expect(rowsOf(page)).toHaveCount(2);
    await expect(rowsOf(page).last().locator(".msg")).toContainText(
      "AI turn finished",
    );
    await expect(rowsOf(page).first().locator(".msg")).toContainText(
      "Prompt assembled",
    );
  });

  test("says what it can't show: no access, a withheld run, a slow read and no entries", async ({
    page,
  }) => {
    const runs = await platform(page, {
      [fixtureIds.failed]: 403,
      [fixtureIds.succeeded]: 409,
      [fixtureIds.child]: 503,
      [fixtureIds.retry]: [],
    });
    await page.goto(`/operate/runs/${fixtureIds.failed}`);
    await expect(section(page)).toContainText(
      "You need run.read in this environment. Ask an administrator in Settings › People and access.",
    );
    await openListed(page, fixtureIds.succeeded);
    await expect(section(page)).toContainText(
      "This run's details are withheld.",
    );
    await openListed(page, fixtureIds.child);
    const slow = section(page).getByRole("alert");
    await expect(slow).toContainText(
      "Logs took too long to read. Narrow the time range or the filters, or try again.",
    );
    runs.answers.set(fixtureIds.child, failedLogs());
    await slow.getByRole("button", { name: "Try again" }).click();
    await expect(rowsOf(page)).toHaveCount(6);
    await openListed(page, fixtureIds.retry);
    await expect(section(page)).toContainText(
      "No log entries match. Workers write entries while steps run; entries are kept for 7 days.",
    );
  });
});
