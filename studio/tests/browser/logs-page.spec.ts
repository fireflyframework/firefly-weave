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
// Operate › Logs: entries across runs, newest first, with the last hour from
// Warning up and no test runs by default; filters in the address except the
// message, which never leaves the page; a message search over more than 6
// hours not sent; Follow every 10 seconds while the page is visible; Load
// older entries on the first page's own query; and the withheld, hidden,
// shortened, slow, empty, older-platform and no-access states. The platform
// is mocked.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page } from "@playwright/test";
import type { ExecutionLogEntry } from "../../src/app/operate/run-contracts";
import { connected } from "./support";
import { acmePlatform, platformHost, signInWaiting } from "./platform-host";

const captures = resolve(
  process.env["STUDIO_OPERATE_CAPTURES"]?.trim() || "test-results/operate",
);
mkdirSync(captures, { recursive: true });
const NOW = "2026-11-18T10:54:10Z";
const reader = ["run.read", "catalog.read", "status.read"];
const RUNNING = "8d2c6a1f-1b2c-4d3e-8f40-5a6b7c8d9e0f";
const FAILED = "1c0d7f8a-4d66-4e7c-8b62-5a2c1d3e8f12";
const WITHHELD = "7b9e3d21-2e4f-4c60-9bac-1d2e3f4a5b6c";

let sequence = 0;
const entry = (facts: Partial<ExecutionLogEntry>): ExecutionLogEntry => ({
  id: `bb000000-0000-4000-8000-${String(++sequence).padStart(12, "0")}`,
  at: "2026-11-18T10:54:02.661Z",
  source: "worker",
  level: "warning",
  code: null,
  message: "",
  node_id: "fetch",
  instance_key: "",
  attempt: 1,
  worker_id: "0f1e2d3c-4b5a-4968-8776-655443322110",
  fields: {},
  redactions: 0,
  truncated: false,
  run_id: RUNNING,
  workflow_name: "invoice-approval",
  workflow_version: "1.3.0",
  test: false,
  ...facts,
});
const long =
  "Query plan for the customer lookup: Seq Scan on customers (cost=0.00..18334.00 rows=1000000 width=244) " +
  "Filter: ((region)::text = 'EU'::text) Rows Removed by Filter: 982211 Planning Time: 0.114 ms " +
  "Execution Time: 412.880 ms; consider an index on customers(region) so the lookup stays under the step's 2 s budget";
/** The last hour at 10:54, newest first, as the mockups draw it. */
const lastHour = (): unknown[] => [
  entry({
    message: "The customer lookup skipped 2 records with an unexpected status",
    code: "CUSTOMER.SKIPPED",
    fields: { skipped: 2 },
    redactions: 2,
  }),
  entry({ at: "2026-11-18T10:53:58.129Z", message: long, truncated: true }),
  entry({
    at: "2026-11-18T10:53:51.447Z",
    message: "Slow answer from billing-db: 1.8 s",
  }),
  {
    id: WITHHELD,
    unavailable: true,
    omissions: [{ path: "", reason: "classification_unavailable" }],
  },
  entry({
    at: "2026-11-18T10:42:01.918Z",
    source: "engine",
    level: "error",
    code: "SMTP_REJECTED",
    message: "send failed: SMTP_REJECTED",
    node_id: "send",
    attempt: null,
    worker_id: null,
    run_id: FAILED,
  }),
  entry({
    at: "2026-11-18T10:42:01.702Z",
    source: "connector",
    code: "HTTP.RESPONSE",
    message: "POST smtp-relay.acme.example answered 550 in 312 ms",
    node_id: "send",
    run_id: FAILED,
  }),
];
const problems: Record<number, unknown> = {
  501: { code: "WV-UNAVAILABLE", message: "Not served" },
  403: { code: "WV-FORBIDDEN", message: "Forbidden" },
  503: {
    code: "WV-UNAVAILABLE",
    message:
      "Logs took too long to read. Narrow the time range or the filters, or try again.",
  },
};

interface Platform {
  /** Each execution_logs.list request, in order. */
  reads: URLSearchParams[];
  /** What the next reads answer: pages by cursor ("" first), or a status. */
  answer:
    | { pages: Record<string, { items: unknown[]; next: string | null }> }
    | number;
}
async function platform(
  page: Page,
  answer: Platform["answer"] = {
    pages: { "": { items: lastHour(), next: null } },
  },
  capabilities = reader,
): Promise<Platform> {
  await page.clock.install({ time: new Date(NOW) });
  const identity = page.waitForResponse("**/studio/api/api/v1/identity");
  await connected(page, { capabilities, environmentName: "local" });
  await identity;
  const result: Platform = { reads: [], answer };
  await page.route("**/execution-logs**", async (route) => {
    const query = new URL(route.request().url()).searchParams;
    result.reads.push(query);
    const now = result.answer;
    if (typeof now === "number")
      return route.fulfill({ status: now, json: problems[now] });
    const found = now.pages[query.get("cursor") ?? ""];
    await route.fulfill({
      json: { items: found?.items ?? [], next_cursor: found?.next ?? null },
    });
  });
  return result;
}
const openLogs = async (page: Page) => {
  await page.goto("/operate/logs");
  await expect(page.locator("#main h1")).toHaveText("Logs");
};
const table = (page: Page) => page.getByRole("table", { name: "Log entries" });
const rows = (page: Page) => table(page).locator(".entry:not(.head)");
const shot = (page: Page, name: string) =>
  page.screenshot({ path: `${captures}/${name}.png` });

test.use({ timezoneId: "UTC" });

for (const size of [
  { width: 1440, height: 900 },
  { width: 360, height: 640 },
]) {
  test.describe(`Operate › Logs at ${size.width} px`, () => {
    test.use({ viewport: size });
    const phone = size.width < 768;

    test("opens on the last hour from Warning up, without test runs", async ({
      page,
    }) => {
      const logs = await platform(page);
      await openLogs(page);
      await expect(page.locator("#main")).toContainText(
        "Log entries from every run in this environment.",
      );
      await expect(page.getByRole("tablist")).toHaveCount(0);
      await expect(rows(page)).toHaveCount(6);
      if (phone)
        await expect(page.locator(".filter-summary")).toContainText(
          "Filters: the defaults (last hour, Warning or higher, test runs off)",
        );
      else {
        await expect(
          page.getByLabel("Time", { exact: true }).locator("option:checked"),
        ).toHaveText("Last hour");
        await expect(
          page.getByLabel("Level", { exact: true }).locator("option:checked"),
        ).toHaveText("Warning or higher");
        await expect(page.getByLabel("Include test runs")).not.toBeChecked();
        await expect(
          page.getByRole("switch", { name: "Follow" }),
        ).not.toBeChecked();
      }
      const query = logs.reads[0];
      expect(query.get("level")).toBe("warning");
      expect(query.has("include_test")).toBe(false);
      expect(query.has("contains")).toBe(false);
      expect(
        Date.parse(query.get("to")!) - Date.parse(query.get("from")!),
      ).toBe(3_600_000);
      const first = rows(page).first();
      await expect(first.locator(".t")).toHaveText("10:54:02.661");
      await expect(first.locator(".lv")).toHaveText("Warning");
      await expect(first.locator(".wf")).toContainText(
        "invoice-approval 1.3.0",
      );
      await expect(
        first.getByRole("link", { name: "Open run 8d2c6a1f" }),
      ).toBeVisible();
      await expect(first.locator(".flag")).toHaveText("2 values hidden");
      await expect(rows(page).nth(1).locator(".flag")).toHaveText("Shortened");
      await expect(rows(page).nth(1).locator(".msg-text")).toHaveText(long);
      const withheld = rows(page).nth(3);
      await expect(withheld).toHaveText(/This run's details are withheld\./);
      await expect(withheld).not.toContainText("invoice-approval");
      await expect(
        withheld.getByRole("cell", { name: "Time withheld" }),
      ).toHaveCount(1);
      await expect(page.locator("#main")).toContainText(
        "6 entries in the last hour · entries are kept for 7 days",
      );
      // Nothing sideways: the longest message wraps inside its cell.
      expect(
        await page.evaluate(
          () => document.scrollingElement!.scrollWidth <= window.innerWidth,
        ),
      ).toBe(true);
      await shot(page, `logs-page-${size.width}`);
    });

    test("searches messages over 6 hours only after asking for a shorter range", async ({
      page,
    }) => {
      const logs = await platform(page);
      await openLogs(page);
      await expect(rows(page)).toHaveCount(6);
      if (phone)
        await page.getByRole("button", { name: "Change filters" }).click();
      await page.getByLabel("Time", { exact: true }).selectOption("24h");
      const message = page.getByLabel("Message contains");
      await message.fill("SMTP");
      await page.clock.runFor(500);
      await expect(page.locator("#logs-message-problem")).toHaveText(
        "Narrow the time range to 6 hours or less to search messages.",
      );
      await expect(message).toHaveAttribute("aria-invalid", "true");
      expect(logs.reads.some((query) => query.has("contains"))).toBe(false);
      await page.getByLabel("Time", { exact: true }).selectOption("6h");
      await expect.poll(() => logs.reads.at(-1)?.get("contains")).toBe("SMTP");
      await expect(page.locator("#logs-message-problem")).toHaveCount(0);
    });
  });
}

test.describe("Operate › Logs", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("comes after Metrics under Operate for a person with run.read", async ({
    page,
  }) => {
    await platform(page);
    const nav = page.getByRole("navigation", { name: "Main navigation" });
    const labels = await nav.locator("button span").allTextContents();
    expect(labels.indexOf("Logs")).toBe(labels.indexOf("Metrics") + 1);
    await nav.getByRole("button", { name: "Logs", exact: true }).click();
    await expect(page).toHaveURL(/\/operate\/logs$/);
    await expect(page).toHaveTitle("Logs | Firefly Weave Studio");
  });

  test("the message filter never reaches the address bar or history", async ({
    page,
  }) => {
    const logs = await platform(page);
    await openLogs(page);
    await page.getByLabel("Level", { exact: true }).selectOption("error");
    await expect(page).toHaveURL(/\/operate\/logs\?level=error$/);
    const before = await page.evaluate(() => history.length);
    await page.getByLabel("Message contains").fill("canary-message-7f3a");
    await page.clock.runFor(500);
    await expect
      .poll(() => logs.reads.at(-1)?.get("contains"))
      .toBe("canary-message-7f3a");
    await page.getByLabel("Workflow").selectOption("invoice-approval");
    await expect(page).toHaveURL(
      /\/operate\/logs\?level=error&workflow=invoice-approval$/,
    );
    const kept = await page.evaluate(() => ({
      href: location.href,
      entries: history.length,
      state: JSON.stringify(history.state),
      local: JSON.stringify({ ...localStorage }),
      session: JSON.stringify({ ...sessionStorage }),
    }));
    expect(kept.entries).toBe(before + 1);
    for (const value of Object.values(kept))
      expect(String(value)).not.toContain("canary-message-7f3a");
    // Back returns to the earlier filters; the message stays on screen only.
    await page.goBack();
    await expect(page).toHaveURL(/\/operate\/logs\?level=error$/);
    await expect(
      page.getByLabel("Workflow").locator("option:checked"),
    ).toHaveText("Any workflow");
  });

  test("Follow asks every 10 seconds while visible", async ({ page }) => {
    const hour = lastHour();
    const logs = await platform(page, {
      pages: { "": { items: hour, next: null } },
    });
    await openLogs(page);
    await expect(rows(page)).toHaveCount(6);
    await page.clock.runFor(30_000);
    expect(logs.reads).toHaveLength(1);
    const heading = page.locator(".operate-heading");
    await expect(heading).not.toContainText("Following");
    await page.getByRole("switch", { name: "Follow" }).check();
    await expect.poll(() => logs.reads.length).toBe(2);
    // The heading says so beside its freshness, with the live dot.
    await expect(heading.locator(".logs-follow")).toHaveText("Following");
    await expect(heading.locator(".logs-follow .live-dot")).toBeVisible();
    logs.answer = {
      pages: {
        "": {
          items: [
            entry({
              at: "2026-11-18T10:54:12.000Z",
              message: "A newer warning",
            }),
            ...hour,
          ],
          next: null,
        },
      },
    };
    await page.clock.runFor(10_000);
    await expect.poll(() => logs.reads.length).toBe(3);
    await expect(rows(page)).toHaveCount(7);
    await expect(rows(page).first().locator(".msg-text")).toHaveText(
      "A newer warning",
    );
    // Hidden, the page asks nothing; shown again, it catches up at once.
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => "hidden",
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await page.clock.runFor(40_000);
    expect(logs.reads).toHaveLength(3);
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => "visible",
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect.poll(() => logs.reads.length).toBe(4);
    await page.getByRole("switch", { name: "Follow" }).uncheck();
    await page.clock.runFor(30_000);
    expect(logs.reads).toHaveLength(4);
    await expect(heading).not.toContainText("Following");
  });

  test("Follow stops once the logs are refused", async ({ page }) => {
    const logs = await platform(page);
    await openLogs(page);
    await expect(rows(page)).toHaveCount(6);
    await page.getByRole("switch", { name: "Follow" }).check();
    await expect.poll(() => logs.reads.length).toBe(2);
    logs.answer = 403;
    await page.clock.runFor(10_000);
    await expect(page.locator("#main")).toContainText("You need run.read");
    const asked = logs.reads.length;
    await page.clock.runFor(120_000);
    expect(logs.reads).toHaveLength(asked);
  });

  test("keeps every filter but the message in the address, and Back restores them", async ({
    page,
  }) => {
    const logs = await platform(page);
    await page.goto(
      "/operate/logs?range=6h&level=all&workflow=invoice-approval&step=send&code=SMTP_REJECTED&test=1",
    );
    await expect(rows(page)).toHaveCount(6);
    const query = logs.reads[0];
    expect(Object.fromEntries(query)).toMatchObject({
      workflow: "invoice-approval",
      node_id: "send",
      code: "SMTP_REJECTED",
      include_test: "true",
    });
    expect(query.has("level")).toBe(false);
    expect(Date.parse(query.get("to")!) - Date.parse(query.get("from")!)).toBe(
      6 * 3_600_000,
    );
    await expect(page.getByLabel("Step", { exact: true })).toHaveValue("send");
    await expect(page.getByLabel("Code", { exact: true })).toHaveValue(
      "SMTP_REJECTED",
    );
    await expect(page.getByLabel("Include test runs")).toBeChecked();
    // A step name the platform can't take is said, not sent.
    await page.getByLabel("Step", { exact: true }).fill("not a step");
    await page.getByLabel("Step", { exact: true }).press("Enter");
    await expect(page.locator("#logs-step-problem")).toBeVisible();
    await expect(page).toHaveURL(/step=send/);
    await page.getByLabel("Step", { exact: true }).fill("");
    await page.getByLabel("Step", { exact: true }).press("Enter");
    await expect(page).not.toHaveURL(/step=/);
    await expect.poll(() => logs.reads.at(-1)?.has("node_id")).toBe(false);
    await page.goBack();
    await expect(page).toHaveURL(/step=send/);
    await expect(page.getByLabel("Step", { exact: true })).toHaveValue("send");
  });

  test("Load older entries continues with the first page's own query", async ({
    page,
  }) => {
    const logs = await platform(page, {
      pages: {
        "": { items: lastHour().slice(0, 3), next: "older1" },
        older1: { items: lastHour().slice(3), next: null },
      },
    });
    await openLogs(page);
    await expect(rows(page)).toHaveCount(3);
    await expect(page.locator("#main")).toContainText(
      "The newest 3 entries in the last hour · entries are kept for 7 days",
    );
    await page.clock.runFor(5_000);
    await page.getByRole("button", { name: "Load older entries" }).click();
    await expect(rows(page)).toHaveCount(6);
    const [first, second] = logs.reads;
    expect(second.get("cursor")).toBe("older1");
    second.delete("cursor");
    expect(second.toString()).toBe(first.toString());
    await expect(
      page.getByRole("button", { name: "Load older entries" }),
    ).toHaveCount(0);
  });

  test("Tab reads the filters, Follow, the run links and Load older entries in order", async ({
    page,
  }) => {
    await platform(page, {
      pages: { "": { items: lastHour(), next: "older1" } },
    });
    await openLogs(page);
    await expect(rows(page)).toHaveCount(6);
    await page.getByLabel("Time", { exact: true }).focus();
    const order: string[] = [];
    for (let i = 0; i < 14; i++) {
      order.push(
        await page.evaluate(() => {
          const element = document.activeElement as HTMLElement;
          return (
            element.getAttribute("aria-label") ||
            element.id ||
            element.closest("label")?.textContent?.trim() ||
            element.textContent?.trim() ||
            ""
          );
        }),
      );
      await page.keyboard.press("Tab");
    }
    expect(order).toEqual([
      "logs-time",
      "logs-level",
      "logs-workflow",
      "logs-step",
      "logs-code",
      "logs-message",
      "Include test runs",
      "Follow",
      "Open run 8d2c6a1f",
      "Open run 8d2c6a1f",
      "Open run 8d2c6a1f",
      "Open run 1c0d7f8a",
      "Open run 1c0d7f8a",
      "Load older entries",
    ]);
  });

  test("a run's link opens the run's page", async ({ page }) => {
    await platform(page);
    await page.route("**/run-summaries?**", (route) =>
      route.fulfill({ json: { items: [], next_cursor: null } }),
    );
    await openLogs(page);
    await rows(page)
      .nth(4)
      .getByRole("link", { name: "Open run 1c0d7f8a" })
      .click();
    await expect(page).toHaveURL(new RegExp(`/operate/runs/${FAILED}$`));
  });

  test("markup in a log message shows as text", async ({ page }) => {
    const markup =
      '<img src="x" onerror="window.__logged = 1"><a href="https://example.org">link</a>';
    await platform(page, {
      pages: { "": { items: [entry({ message: markup })], next: null } },
    });
    await openLogs(page);
    await expect(rows(page).first().locator(".msg-text")).toHaveText(markup);
    await expect(table(page).locator("img, .msg a")).toHaveCount(0);
    expect(
      await page.evaluate(() => (window as { __logged?: number }).__logged),
    ).toBeUndefined();
  });

  test("markup in a workflow, a step name, a field name and a field value shows as text", async ({
    page,
  }) => {
    const workflow = "<i>workflow</i>";
    const step = "<b>step</b>";
    const name = '<img src="x" onerror="window.__logged = 3">';
    const value =
      '<script>window.__logged = 4</script><a href="https://example.org">link</a>';
    await platform(page, {
      pages: {
        "": {
          items: [
            entry({
              message: "Looked up",
              workflow_name: workflow,
              node_id: step,
              fields: { [name]: value },
            }),
          ],
          next: null,
        },
      },
    });
    await openLogs(page);
    const row = rows(page).first();
    await expect(row.locator(".wf")).toContainText(`${workflow} 1.3.0`);
    await expect(row.locator(".step")).toHaveText(step);
    await expect(row.locator(".details")).toHaveText(`${name}=${value}`);
    await expect(table(page).locator("img, b, i, script, .msg a")).toHaveCount(
      0,
    );
    await expect(
      page.locator("#logs-filters").locator("img, b, i, script"),
    ).toHaveCount(0);
    expect(
      await page.evaluate(() => (window as { __logged?: number }).__logged),
    ).toBeUndefined();
  });

  test("says when a read took too long, and tries again", async ({ page }) => {
    const logs = await platform(page, 503);
    await openLogs(page);
    const slow = page.getByRole("alert");
    await expect(slow).toContainText(
      "Logs took too long to read. Narrow the time range or the filters, or try again.",
    );
    await shot(page, "logs-page-1440-slow");
    logs.answer = { pages: { "": { items: lastHour(), next: null } } };
    await slow.getByRole("button", { name: "Try again" }).click();
    await expect(rows(page)).toHaveCount(6);
  });

  test("says when no entry matches", async ({ page }) => {
    await platform(page, { pages: {} });
    await openLogs(page);
    await expect(page.locator("#main")).toContainText(
      "No log entries match. Workers write entries while steps run; entries are kept for 7 days.",
    );
    await shot(page, "logs-page-1440-empty");
  });

  test("an older platform says it can't show logs and is asked once", async ({
    page,
  }) => {
    const logs = await platform(page, 501);
    await openLogs(page);
    await expect(page.locator("#main")).toContainText(
      "This platform can't show logs yet.",
    );
    await expect(page.getByLabel("Level", { exact: true })).toHaveCount(0);
    await shot(page, "logs-page-1440-unserved");
    await page.clock.runFor(60_000);
    await page.getByRole("button", { name: "Metrics", exact: true }).click();
    await page.getByRole("button", { name: "Logs", exact: true }).click();
    await expect(page.locator("#main")).toContainText(
      "This platform can't show logs yet.",
    );
    expect(logs.reads).toHaveLength(1);
  });

  test("without run.read, there is no Logs entry and the page says what is needed", async ({
    page,
  }) => {
    await platform(page, undefined, ["status.read"]);
    const nav = page.getByRole("navigation", { name: "Main navigation" });
    await expect(
      nav.getByRole("button", { name: "Metrics", exact: true }),
    ).toBeVisible();
    await expect(
      nav.getByRole("button", { name: "Logs", exact: true }),
    ).toHaveCount(0);
    await openLogs(page);
    await expect(page.locator("#main")).toContainText(
      "You need run.read in local. Ask an administrator in Settings › People and access.",
    );
    await shot(page, "logs-page-1440-access");
  });
});

test.describe("Operate › Logs on a phone", () => {
  test.use({ viewport: { width: 360, height: 640 }, hasTouch: true });

  test("folds the filters behind a summary that names what changed", async ({
    page,
  }) => {
    await platform(page);
    await openLogs(page);
    const toggle = page.getByRole("button", { name: "Change filters" });
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByLabel("Time", { exact: true })).toBeHidden();
    const height = (await toggle.boundingBox())!.height;
    expect(height).toBeGreaterThanOrEqual(44);
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByLabel("Time", { exact: true })).toBeFocused();
    await page.getByLabel("Time", { exact: true }).selectOption("6h");
    await page.getByLabel("Level", { exact: true }).selectOption("error");
    await page.getByLabel("Message contains").fill("SMTP");
    await page.getByLabel("Include test runs").check();
    await page.getByRole("switch", { name: "Follow" }).check();
    await toggle.click();
    await expect(page.locator(".filter-summary")).toContainText(
      "Filters: last 6 hours · Error only · message contains “SMTP” · test runs included · following every 10 seconds",
    );
    await shot(page, "logs-page-360-summary");
  });
});

test.describe("another person on the same environment", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a message typed for the person before is never sent for the next one", async ({
    page,
  }) => {
    await page.clock.install({ time: new Date(NOW) });
    // Jane is signed in with a sign-in waiting in the background; John
    // finishes it on the same environment while the page stays open.
    let person = "jane";
    const host = await platformHost(page, {
      platforms: [acmePlatform()],
      active: "Acme",
      test: (current) => ({
        json: {
          session: current.session(),
          identity: {
            ...current.identity(),
            principal_id: `principal-${person}`,
          },
          workspaces: current.current?.workspaces ?? [],
          truncated: false,
          workspace_revoked: false,
        },
      }),
    });
    const reads: { person: string; query: URLSearchParams }[] = [];
    await page.route("**/execution-logs**", async (route) => {
      reads.push({
        person,
        query: new URL(route.request().url()).searchParams,
      });
      await route
        .fulfill({ json: { items: [], next_cursor: null } })
        .catch(() => undefined);
    });
    const john = await signInWaiting(page, host, () =>
      page.getByRole("button", { name: "Logs", exact: true }).click(),
    );
    const message = page.getByLabel("Message contains");
    await message.fill("jane-private-search");
    await page.clock.runFor(500);
    await expect
      .poll(() => reads.at(-1)?.query.get("contains"))
      .toBe("jane-private-search");
    await page.getByLabel("Step", { exact: true }).fill("not a step");
    await page.getByLabel("Step", { exact: true }).press("Enter");
    await expect(page.locator("#logs-step-problem")).toBeVisible();
    // Her last search is out when John turns out to be signed in.
    await message.fill("jane-private-search-2");
    await page.clock.runFor(500);
    await expect
      .poll(() => reads.at(-1)?.query.get("contains"))
      .toBe("jane-private-search-2");
    person = "john";
    await john();
    await expect(message).toHaveValue("");
    await expect(page.getByLabel("Step", { exact: true })).toHaveValue("");
    await expect(page.locator("#logs-step-problem")).toHaveCount(0);
    await page.clock.runFor(1_000);
    await expect
      .poll(() => reads.filter((read) => read.person === "john").length)
      .toBeGreaterThan(0);
    for (const read of reads.filter((item) => item.person === "john"))
      expect(read.query.has("contains"), read.query.toString()).toBe(false);
  });
});
