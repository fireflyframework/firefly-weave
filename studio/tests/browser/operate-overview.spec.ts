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
// Operate › Overview: platform health, load, workers and clusters and what
// needs attention, from one environment summary. Every state renders on its
// own, each section shows its own access or loading problem, the page polls
// every 15 s only while visible, and its layout keeps one left and one right
// edge at every size. The platform is mocked; the summary is a typed literal.
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  test,
  expect,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test";
import type { OperationsSummary } from "../../src/app/operate/overview/overview-model";
import { connected } from "./support";
import {
  acme,
  acmePlatform,
  platformHost,
  type PlatformHost,
} from "./platform-host";

const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";
// Review captures, and the geometry each layout test measured, go to
// test-results/operate, or to the folder STUDIO_OPERATE_CAPTURES names.
const captures = resolve(
  process.env["STUDIO_OPERATE_CAPTURES"]?.trim() || "test-results/operate",
);
mkdirSync(captures, { recursive: true });
const every = [
  "status.read",
  "run.read",
  "incident.read",
  "deployment.read",
  "deployment.approve",
  "worker.drain",
  "catalog.read",
];
const id = (n: number) =>
  `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
const runId = id(7);

/** A summary observed now, with every section readable. */
function summary(
  overrides: Partial<OperationsSummary> = {},
): OperationsSummary {
  const now = Date.now();
  const at = (seconds: number) => new Date(now + seconds * 1000).toISOString();
  return {
    observed_at: at(0),
    api: {
      version: "0.1.0a15",
      build: null,
      instance: id(1),
      uptime_seconds: 7_200,
      compatibility: { mode: "ready", findings: 0 },
    },
    health: [
      { component: "database", state: "ok", latency_ms: 4.25 },
      {
        component: "recovery_loop",
        state: "ok",
        last_started_at: at(-3),
        last_ok_at: at(-3),
        consecutive_failures: 0,
        recent_failures: 0,
      },
      {
        component: "schedules",
        state: "ok",
        last_ok_at: at(-3),
        consecutive_failures: 0,
        recent_failures: 0,
      },
      {
        component: "outbox_loop",
        state: "degraded",
        last_started_at: at(-1),
        last_ok_at: at(-90),
        consecutive_failures: 4,
        recent_failures: 4,
      },
      {
        component: "provider_loop",
        state: "disabled",
        consecutive_failures: 0,
        recent_failures: 0,
      },
      {
        component: "kafka_loop",
        state: "disabled",
        consecutive_failures: 0,
        recent_failures: 0,
      },
      {
        component: "operations_loop",
        state: "ok",
        last_ok_at: at(-20),
        consecutive_failures: 0,
        recent_failures: 0,
      },
      {
        component: "alert_evaluator",
        state: "disabled",
        consecutive_failures: 0,
        recent_failures: 0,
      },
      { component: "native_executors", state: "ok" },
      { component: "telemetry", state: "disabled" },
    ],
    load: {
      runs_active: 1204,
      tasks_ready: 3,
      tasks_leased: 5,
      oldest_ready_seconds: 41,
      waits_active: 7,
      waits_overdue: 1,
      oldest_overdue_seconds: 95,
      outbox_pending: 2,
      outbox_oldest_seconds: 3,
      provider_pending: 0,
      sources_enabled: 4,
    },
    workers: {
      online: 3,
      offline: 1,
      draining: 0,
      capacity: 12,
      active_leases: 5,
    },
    targets: {
      total: 2,
      runner_online: 1,
      busy: 0,
      reconciliation_required: 1,
      awaiting_approval: 1,
    },
    incidents: { active: 1, opened_24h: 3 },
    attention: [
      {
        kind: "job",
        id: id(2),
        title: "Deployment job on alpha needs reconciliation",
      },
      {
        kind: "approval",
        id: id(3),
        title: "Approve the plan to update on alpha",
        expires_at: at(240),
      },
      {
        kind: "worker",
        id: id(4),
        title: "Offline worker still holds 2 active leases",
      },
      {
        kind: "incident",
        id: id(5),
        title: "Active incident in invoice-approval",
      },
      { kind: "runner", id: id(6), title: "No runner is online for bravo" },
      {
        kind: "compatibility",
        id: "compatibility",
        title: "Execution compatibility is restricted",
      },
      { kind: "run", id: runId, title: "A run of invoice-approval failed" },
    ],
    ...overrides,
  };
}
const quiet = (): OperationsSummary =>
  summary({
    load: {
      runs_active: 0,
      tasks_ready: 0,
      tasks_leased: 0,
      oldest_ready_seconds: null,
      waits_active: 0,
      waits_overdue: 0,
      oldest_overdue_seconds: null,
      outbox_pending: 0,
      outbox_oldest_seconds: null,
      provider_pending: 0,
      sources_enabled: 0,
    },
    workers: {
      online: 0,
      offline: 0,
      draining: 0,
      capacity: 0,
      active_leases: 0,
    },
    targets: {
      total: 0,
      runner_online: 0,
      busy: 0,
      reconciliation_required: 0,
      awaiting_approval: 0,
    },
    incidents: { active: 0, opened_24h: 0 },
    attention: [],
  });

interface Answer {
  status?: number;
  json: unknown;
  delay?: number;
}
/** Connects with `capabilities` and answers the summary from `answers`, the last one repeating. */
async function overview(
  page: Page,
  capabilities: string[],
  answers: (() => Answer)[] = [() => ({ json: summary() })],
) {
  const reads: number[] = [];
  const unexpected: string[] = [];
  // Set by pauseAtNextRead: the next read stops the clock before its answer.
  let pauseNext: (() => void) | null = null;
  page.on("request", (request) => {
    if (
      /\/(statistics|run-statistics|logs)(\?|$)/.test(
        new URL(request.url()).pathname,
      )
    )
      unexpected.push(request.url());
  });
  await connected(page, { capabilities });
  await page.route(
    `${environment}/operations/summary`,
    async (route: Route) => {
      reads.push(Date.now());
      const paused = pauseNext;
      pauseNext = null;
      if (paused) {
        // The clock was installed at the real time and has run with it since.
        await page.clock.pauseAt(Date.now() + 300);
        paused();
      }
      const answer = answers[Math.min(reads.length - 1, answers.length - 1)]();
      if (answer.delay)
        await new Promise((resolve) => setTimeout(resolve, answer.delay));
      await route
        .fulfill({ status: answer.status ?? 200, json: answer.json })
        .catch(() => undefined);
    },
  );
  /**
   * Stops the page clock just before the next summary answer, so the wait
   * that follows starts from a time only the test moves. Resolves once the
   * clock has stopped, with that read already counted.
   */
  const pauseAtNextRead = () =>
    new Promise<void>((paused) => {
      pauseNext = paused;
    });
  return { reads, unexpected, pauseAtNextRead };
}
const open = (page: Page) =>
  page.getByRole("button", { name: "Overview", exact: true }).click();
const band = (page: Page, name: string) =>
  page.locator("section.band").filter({
    has: page.getByRole("heading", { name, exact: true }),
  });
const shot = (page: Page, name: string) =>
  page.screenshot({ path: `${captures}/${name}.png` });

const round = (value: number) => Math.round(value * 100) / 100;
/** How far apart the values lie, in CSS pixels. */
const spread = (values: number[]) =>
  round(Math.max(...values) - Math.min(...values));
const most = (values: number[]) =>
  values.length ? round(Math.max(...values)) : 0;
/** Leaves what a layout test measured next to its capture. */
const record = (name: string, figures: Record<string, unknown>) =>
  writeFileSync(
    `${captures}/${name}.json`,
    JSON.stringify(figures, null, 2) + "\n",
  );
/**
 * The smallest side of any visible target, and every target under the 36 px
 * desktop control height, in CSS pixels.
 */
async function targetSizes(locator: Locator) {
  const targets = await locator.evaluateAll((nodes) =>
    nodes
      .filter((node) => node.getClientRects().length)
      .map((node) => {
        const r = node.getBoundingClientRect();
        return {
          name: (
            node.getAttribute("aria-label") ??
            node.textContent ??
            ""
          ).trim(),
          width: Math.round(r.width * 100) / 100,
          height: Math.round(r.height * 100) / 100,
        };
      }),
  );
  return {
    smallest: round(
      Math.min(
        ...targets.map((target) => Math.min(target.width, target.height)),
      ),
    ),
    under36: targets.filter(
      (target) => Math.min(target.width, target.height) < 35.5,
    ),
  };
}

/** Every box sampled in one frame. */
async function boxes(locator: Locator) {
  return locator.evaluateAll((nodes) =>
    nodes.map((node) => {
      const r = node.getBoundingClientRect();
      return {
        left: r.left,
        right: r.right,
        top: r.top,
        bottom: r.bottom,
        width: r.width,
        height: r.height,
      };
    }),
  );
}
/** No sideways scroll anywhere on the page, and no text cut off inside its box. */
async function expectNoClipping(page: Page) {
  const problems = await page.evaluate(() => {
    const found: string[] = [];
    const root = document.documentElement;
    if (root.scrollWidth > root.clientWidth + 1)
      found.push("page scrolls sideways");
    const content = document.querySelector<HTMLElement>(".page-content");
    if (content && content.scrollWidth > content.clientWidth + 1)
      found.push("content scrolls sideways");
    for (const node of document.querySelectorAll<HTMLElement>(
      "weave-overview-page *",
    )) {
      const style = getComputedStyle(node);
      if (style.display === "none" || !node.getClientRects().length) continue;
      if (node.closest("svg")) continue;
      if (
        node.scrollWidth > node.clientWidth + 1 &&
        style.overflowX !== "visible"
      )
        found.push(
          `${node.tagName.toLowerCase()}.${node.className} is cut off`,
        );
    }
    return found;
  });
  expect(problems).toEqual([]);
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
  { width: 360, height: 640 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    const size = `${viewport.width}x${viewport.height}`;

    test("Overview shows health, load, capacity and attention from one summary", async ({
      page,
    }) => {
      const { reads, unexpected } = await overview(page, every);
      await open(page);
      await expect(page).toHaveURL(/\/operate\/overview$/);
      await expect(page.locator("#main h1")).toHaveText("Overview");
      const tiles = page.locator("li.health-tile");
      await expect(tiles).toHaveCount(6);
      await expect(tiles.locator(".tile-label")).toHaveText([
        "API",
        "Database",
        "Scheduler",
        "Outbox",
        "Compatibility",
        "Telemetry export",
      ]);
      await expect(tiles.locator(".status-pill")).toHaveText([
        "OK",
        "OK",
        "OK",
        "Degraded",
        "OK",
        "Off",
      ]);
      await expect(tiles.nth(1)).toContainText("Latency 4 ms");
      await expect(tiles.nth(3)).toContainText("Last success 2 min ago");
      const load = band(page, "Load");
      await expect(load.locator(".figure-value")).toHaveText([
        "1,204",
        "3",
        "7",
        "2",
        "0",
      ]);
      await expect(load).toContainText("Oldest waits 41 s");
      await expect(load).toContainText("1 overdue, oldest by 1 min 35 s");
      // Counts line up in columns of tabular figures.
      expect(
        await load
          .locator(".figure-value")
          .first()
          .evaluate((node) => getComputedStyle(node).fontVariantNumeric),
      ).toBe("tabular-nums");
      const capacity = band(page, "Workers and clusters");
      await expect(capacity.locator(".panel").nth(0)).toContainText(
        /Online\s*3/,
      );
      await expect(capacity.locator(".meter")).toHaveAccessibleName(
        "3 ready, 7 free slots",
      );
      await expect(capacity.locator(".panel").nth(1)).toContainText(
        /Plans awaiting approval\s*1/,
      );
      await expect(capacity.locator(".panel").nth(2)).toContainText(
        /Opened in the last 24 hours\s*3/,
      );
      const attention = band(page, "Needs attention").locator(
        ".attention-item",
      );
      await expect(attention).toHaveCount(7);
      await expect(attention.nth(1)).toContainText("Expires in 4 min");
      // The 24-hour chart and the sparklines arrive with run statistics: none
      // is drawn yet, and nothing asks for statistics or logs.
      await expect(
        page.getByRole("heading", { name: "Last 24 h", exact: true }),
      ).toHaveCount(0);
      await expect(
        page.locator("weave-overview-page svg:not(weave-icon svg)"),
      ).toHaveCount(0);
      expect(unexpected).toEqual([]);
      await shot(page, `overview-${size}-ready`);
      await expectNoClipping(page);
      // Refresh reads the summary again, by a real click.
      const before = reads.length;
      await page.getByRole("button", { name: "Refresh", exact: true }).click();
      await expect.poll(() => reads.length).toBe(before + 1);
      await expect(page.locator(".refresh-status .updated")).toHaveText(
        "Updated just now",
      );
    });

    test("health Details list each loop's last success and failures in a row", async ({
      page,
    }) => {
      await overview(page, every);
      await open(page);
      const button = page.getByRole("button", {
        name: "Scheduler details",
        exact: true,
      });
      await button.click();
      const details = page.getByRole("dialog", { name: "Scheduler details" });
      await expect(details).toBeVisible();
      const rows = details.locator(".loop-row:not(.loop-head)");
      await expect(rows.locator(".loop-name")).toHaveText([
        "Recovery",
        "Schedules",
        "Providers",
        "Kafka",
        "Operations",
        "Alerts",
      ]);
      await expect(rows.nth(0)).toContainText("just now");
      await expect(rows.nth(2)).toContainText("Off");
      await expect(button).toHaveAttribute("aria-expanded", "true");
      // Inside the window at every size.
      const box = (await details.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(viewport.width + 1);
      await shot(page, `overview-${size}-details`);
      await page.keyboard.press("Escape");
      await expect(details).toHaveCount(0);
      await expect(button).toBeFocused();
      // Outbox details name its failures.
      await page
        .getByRole("button", { name: "Outbox details", exact: true })
        .click();
      await expect(
        page.getByRole("dialog", { name: "Outbox details" }),
      ).toContainText("4");
      await page.getByRole("button", { name: "Close Outbox details" }).click();
      await expect(page.getByRole("dialog")).toHaveCount(0);
    });

    test("attention items open their records", async ({ page }) => {
      await overview(page, every);
      await page.route(`${environment}/runs/${runId}`, (r) =>
        r.fulfill({
          json: {
            id: runId,
            business_key: "INV-1041",
            state: { status: "failed", active: [] },
          },
        }),
      );
      await page.route(`${environment}/runs/${runId}/*`, (r) =>
        r.fulfill({
          json: { events: [], next_cursor: null, items: [], complete: true },
        }),
      );
      await open(page);
      const attention = band(page, "Needs attention");
      await attention
        .getByRole("link", { name: "A run of invoice-approval failed" })
        .click();
      await expect(page).toHaveURL(new RegExp(`/operate/runs/${runId}$`));
      await expect(
        page.locator(".record-detail #record-detail-title"),
      ).toHaveText("INV-1041");
      // On a narrow screen the run opens as a sheet over the page.
      if (viewport.width < 1025) await page.keyboard.press("Escape");
      await open(page);
      await band(page, "Needs attention")
        .getByRole("link", {
          name: "Offline worker still holds 2 active leases",
        })
        .click();
      await expect(page).toHaveURL(new RegExp(`/operate/workers/${id(4)}$`));
      await expect(page.locator("#main h1")).toHaveText("Workers");
      // On a narrow screen the worker opens as a sheet over the page.
      if (viewport.width < 1025) await page.keyboard.press("Escape");
      await open(page);
      await band(page, "Needs attention")
        .getByRole("link", { name: "Approve the plan to update on alpha" })
        .click();
      await expect(page).toHaveURL(
        new RegExp(`/operate/clusters/plans/${id(3)}$`),
      );
      await expect(page.locator("#main h1")).toHaveText("Clusters");
      // The compatibility verdict is no record: it is not a link.
      await open(page);
      await expect(
        band(page, "Needs attention").getByRole("link", {
          name: "Execution compatibility is restricted",
        }),
      ).toHaveCount(0);
    });

    test("loading, empty, error and partial states each render on their own", async ({
      page,
    }) => {
      const outage = {
        status: 503,
        json: {
          code: "WV-UNAVAILABLE",
          message: "The summary is not available yet.",
        },
      };
      let answer: () => Answer = () => ({ json: quiet(), delay: 1500 });
      await overview(page, every, [() => answer()]);
      await open(page);
      // Loading: blocks shaped like the bands.
      await expect(
        page
          .getByRole("status")
          .filter({ hasText: "Loading platform health…" }),
      ).toBeVisible();
      await shot(page, `overview-${size}-loading`);
      // Empty: the reason and the next action.
      await expect(
        page.getByText(
          "No activity yet. Run a workflow to see throughput, failures and queues here.",
          { exact: true },
        ),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Open workflows" }),
      ).toBeVisible();
      await shot(page, `overview-${size}-empty`);
      // Error, after it had loaded: the last answer stays, with the code and Try again.
      answer = () => outage;
      await page.getByRole("button", { name: "Refresh", exact: true }).click();
      const problem = page
        .locator("weave-operate-state")
        .filter({ hasText: "Studio could not refresh the overview" });
      await expect(problem).toContainText("Support code: WV-UNAVAILABLE");
      await expect(band(page, "Health")).toBeVisible();
      await shot(page, `overview-${size}-refresh-error`);
      // Partial: two sections could not be computed; the rest is usable.
      answer = () => ({
        json: summary({
          load: { state: "unavailable" },
          attention: { state: "unavailable" },
        }),
      });
      await problem.getByRole("button", { name: "Try again" }).click();
      await expect(problem).toHaveCount(0);
      for (const name of ["Load", "Needs attention"])
        await expect(
          band(page, name).getByText("This section could not be loaded.", {
            exact: true,
          }),
        ).toBeVisible();
      await expect(band(page, "Workers and clusters")).toContainText(
        /Online\s*3/,
      );
      await expect(page.locator("li.health-tile")).toHaveCount(6);
      await shot(page, `overview-${size}-partial`);
      answer = () => ({ json: summary() });
      await band(page, "Load")
        .getByRole("button", { name: "Try again" })
        .click();
      await expect(band(page, "Load").locator(".figure")).toHaveCount(5);
    });

    test("a first read that fails shows its code and Try again", async ({
      page,
    }) => {
      let fail = true;
      await overview(page, every, [
        () =>
          fail
            ? {
                status: 500,
                json: {
                  code: "WV-INTERNAL",
                  message: "The platform could not answer.",
                },
              }
            : { json: summary() },
      ]);
      await open(page);
      const state = page.locator("weave-operate-state");
      await expect(state).toContainText("Support code: WV-INTERNAL");
      await expect(page.locator("li.health-tile")).toHaveCount(0);
      await shot(page, `overview-${size}-error`);
      fail = false;
      await state.getByRole("button", { name: "Try again" }).click();
      await expect(page.locator("li.health-tile")).toHaveCount(6);
    });

    test("a section the person may not read says which access it needs, never zero", async ({
      page,
    }) => {
      await overview(
        page,
        ["status.read", "run.read"],
        [
          () => ({
            json: summary({
              targets: { state: "forbidden" },
              incidents: { state: "forbidden" },
            }),
          }),
        ],
      );
      await open(page);
      const capacity = band(page, "Workers and clusters");
      const clusters = capacity.locator(".panel").nth(1);
      const incidents = capacity.locator(".panel").nth(2);
      await expect(clusters).toContainText(
        "You need deployment.read in this environment.",
      );
      await expect(incidents).toContainText(
        "You need incident.read in this environment.",
      );
      await expect(incidents.locator("dd")).toHaveCount(0);
      await expect(incidents).not.toContainText("0");
      await shot(page, `overview-${size}-access-sections`);
      // The same sections at zero read as zero.
      await page.unroute(`${environment}/operations/summary`);
      await page.route(`${environment}/operations/summary`, (r) =>
        r.fulfill({ json: quiet() }),
      );
      await page.getByRole("button", { name: "Refresh", exact: true }).click();
      await expect(incidents.locator("dd")).toHaveText(["0", "0"]);
      await expect(incidents).not.toContainText("You need incident.read");
    });

    test("without status.read the page names the access it needs and Overview leaves the menu", async ({
      page,
    }) => {
      const { reads } = await overview(page, ["run.read"]);
      await expect(
        page.getByRole("button", { name: "Runs", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Overview", exact: true }),
      ).toHaveCount(0);
      await page.goto("/operate/overview");
      await expect(page.locator("#main h1")).toHaveText("Overview");
      await expect(page.locator("weave-operate-state")).toContainText(
        "You need status.read in this environment.",
      );
      await shot(page, `overview-${size}-access`);
      expect(reads).toEqual([]);
    });
  });

test.describe("polling", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Overview reads every 15 s while visible, pauses when hidden and backs off to 60 s", async ({
    page,
  }) => {
    await page.clock.install();
    let failing = false;
    const later = summary();
    later.api.compatibility.findings = 2;
    const { reads, pauseAtNextRead } = await overview(page, every, [
      () => ({ json: summary() }),
      () =>
        failing
          ? { status: 503, json: { code: "WV-UNAVAILABLE", message: "Down" } }
          : { json: later },
    ]);
    await open(page);
    await expect(page.locator("li.health-tile")).toHaveCount(6);
    await expect.poll(() => reads.length).toBe(1);
    // Refresh stops the clock as its answer lands; once that answer is drawn
    // only the test moves time.
    const paused = pauseAtNextRead();
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await paused;
    await expect(async () => {
      await page.clock.runFor(16);
      await expect(
        page.locator('[data-tile="compatibility"] .tile-figure'),
      ).toHaveText("2 findings", { timeout: 100 });
    }).toPass({ timeout: 10_000, intervals: [50] });
    expect(reads.length).toBe(2);
    await page.clock.runFor(13_000);
    expect(reads.length).toBe(2);
    await page.clock.runFor(2_500);
    await expect.poll(() => reads.length).toBe(3);
    // Hidden: nothing is read, however long.
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => "hidden",
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await page.clock.runFor(120_000);
    expect(reads.length).toBe(3);
    // Shown again: the page catches up at once.
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => "visible",
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect.poll(() => reads.length).toBe(4);
    // Two minutes went by unread; the catch-up's answer makes it current.
    await expect(async () => {
      await page.clock.runFor(16);
      await expect(page.locator(".refresh-status .updated")).toHaveText(
        "Updated just now",
        { timeout: 100 },
      );
    }).toPass({ timeout: 10_000, intervals: [50] });
    // Failures wait twice as long each time, at most 60 s.
    failing = true;
    for (const [wait, expected] of [
      [15_000, 5],
      [30_000, 6],
      [60_000, 7],
      [60_000, 8],
    ] as const) {
      await page.clock.runFor(wait - 1_500);
      expect(reads.length).toBe(expected - 1);
      await page.clock.runFor(1_600);
      await expect.poll(() => reads.length).toBe(expected);
      // The failed answer lands before time moves on; the page draws when time moves.
      await expect(async () => {
        await page.clock.runFor(16);
        await expect(
          page
            .locator("weave-operate-state")
            .filter({ hasText: "Studio could not refresh" }),
        ).toBeVisible({ timeout: 100 });
      }).toPass({ timeout: 10_000, intervals: [50] });
    }
    // The last answer stays on screen while it fails.
    await expect(page.locator("li.health-tile")).toHaveCount(6);
  });
});

test.describe("switching workspace", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a slow answer for the workspace left behind never paints over the new one", async ({
    page,
  }) => {
    const statusCapabilities = [...every];
    const host: PlatformHost = await platformHost(page, {
      platforms: [acmePlatform()],
      active: "Acme",
      test: (current) => ({
        json: {
          session: current.session(),
          identity: {
            ...current.identity(),
            grants: current.identity().grants.map((grant) => ({
              ...grant,
              capabilities: statusCapabilities,
            })),
          },
          workspaces: current.current?.workspaces ?? [],
          truncated: false,
          workspace_revoked: false,
        },
      }),
    });
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/environments/*/operations/summary", async (route) => {
      const production = route.request().url().includes(acme[0].environment_id);
      if (production) await held;
      await route
        .fulfill({
          json: summary({
            api: {
              ...summary().api,
              version: production ? "9.9.9-production" : "0.1.0a15",
            },
          }),
        })
        .catch(() => undefined);
    });
    await page.getByRole("button", { name: "Overview", exact: true }).click();
    await expect(page.getByText("Loading platform health…")).toBeAttached();
    await page.locator(".platform-indicator").click();
    await page.getByRole("button", { name: "Switch workspace" }).click();
    await page
      .getByRole("radio", { name: "Acme / Payments / Staging" })
      .check();
    await page.getByRole("button", { name: "Start working" }).click();
    await page.getByRole("button", { name: "Overview", exact: true }).click();
    await page
      .getByRole("button", { name: "API details", exact: true })
      .click();
    await expect(
      page.getByRole("dialog", { name: "API details" }),
    ).toContainText("0.1.0a15");
    release();
    await page.waitForTimeout(500);
    await expect(
      page.getByRole("dialog", { name: "API details" }),
    ).toContainText("0.1.0a15");
    await expect(page.locator("weave-overview-page")).not.toContainText(
      "9.9.9-production",
    );
    expect(host.requests("/studio/scope").length).toBeGreaterThan(0);
  });
});

test.describe("another person on the same environment", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the summary read for the person before, and a late answer for them, never show for the next one", async ({
    page,
  }) => {
    // The platform can't be reached at first, so Studio reads the identity
    // itself (jane); Try again then finds john signed in on the same
    // environment, while the page stays open.
    let person = "jane";
    let reachable = false;
    const withStatus = (host: PlatformHost) => ({
      ...host.identity(),
      principal_id: `principal-${person}`,
      grants: host.identity().grants.map((grant) => ({
        ...grant,
        capabilities: every,
      })),
    });
    const host: PlatformHost = await platformHost(page, {
      platforms: [acmePlatform()],
      active: "Acme",
      test: (current) =>
        reachable
          ? {
              json: {
                session: current.session(),
                identity: withStatus(current),
                workspaces: current.current?.workspaces ?? [],
                truncated: false,
                workspace_revoked: false,
              },
            }
          : {
              status: 503,
              json: { code: "WV-STUDIO-UPSTREAM", message: "Unreachable" },
            },
    });
    await page.route("**/studio/api/api/v1/identity", (route) =>
      route.fulfill({ json: withStatus(host) }),
    );
    let holdJane = false;
    let releaseJane: () => void = () => undefined;
    const janeHeld = new Promise<void>((resolve) => (releaseJane = resolve));
    let releaseJohn: () => void = () => undefined;
    const johnHeld = new Promise<void>((resolve) => (releaseJohn = resolve));
    const asked: string[] = [];
    await page.route("**/environments/*/operations/summary", async (route) => {
      const who = person;
      asked.push(who);
      if (who === "jane" && holdJane) await janeHeld;
      if (who === "john") await johnHeld;
      await route
        .fulfill({
          json: summary({
            api: { ...summary().api, version: `1.0.0-${who}` },
          }),
        })
        .catch(() => undefined);
    });
    await page.getByRole("button", { name: "Overview", exact: true }).click();
    const details = page.getByRole("button", {
      name: "API details",
      exact: true,
    });
    await details.click();
    await expect(
      page.getByRole("dialog", { name: "API details" }),
    ).toContainText("1.0.0-jane");
    await details.click();
    // A read for jane is still out when john turns out to be signed in.
    holdJane = true;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect.poll(() => asked.length).toBe(2);
    person = "john";
    reachable = true;
    await page
      .locator(".platform-banner")
      .getByRole("button", { name: "Try again" })
      .click();
    await expect(page.locator(".platform-banner")).toHaveCount(0);
    // Nothing read for jane stays while john's summary is on its way.
    await expect(page.getByText("Loading platform health…")).toBeAttached();
    await expect(details).toHaveCount(0);
    releaseJane();
    await expect.poll(() => asked.includes("john")).toBe(true);
    await expect(details).toHaveCount(0);
    releaseJohn();
    await details.click();
    await expect(
      page.getByRole("dialog", { name: "API details" }),
    ).toContainText("1.0.0-john");
    await expect(page.locator("weave-overview-page")).not.toContainText(
      "1.0.0-jane",
    );
  });
});

test.describe("layout", () => {
  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 600, height: 500 },
    { width: 360, height: 640 },
  ])
    test(`bands share one left and right edge and rows line up at ${viewport.width}x${viewport.height}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      await overview(page, every);
      await open(page);
      await expect(page.locator("li.health-tile")).toHaveCount(6);
      const bands = await boxes(
        page.locator("weave-overview-page section.band"),
      );
      expect(bands).toHaveLength(4);
      // One left edge and one right edge, bands stacked top to bottom.
      for (const box of bands) {
        expect(Math.abs(box.left - bands[0].left)).toBeLessThanOrEqual(1);
        expect(Math.abs(box.right - bands[0].right)).toBeLessThanOrEqual(1);
      }
      for (let i = 1; i < bands.length; i++)
        expect(bands[i].top).toBeGreaterThanOrEqual(bands[i - 1].bottom - 1);
      // Health tiles fill the band edge to edge, rows of equal height.
      const tiles = await boxes(page.locator("li.health-tile"));
      const columns =
        viewport.width >= 1440 ? 6 : viewport.width >= 600 ? 2 : 1;
      const first = tiles.filter(
        (tile) => Math.abs(tile.top - tiles[0].top) <= 1,
      );
      expect(first).toHaveLength(columns);
      expect(Math.abs(first[0].left - bands[0].left)).toBeLessThanOrEqual(1);
      expect(
        Math.abs(first[columns - 1].right - bands[0].right),
      ).toBeLessThanOrEqual(1);
      for (const tile of first)
        expect(Math.abs(tile.height - first[0].height)).toBeLessThanOrEqual(1);
      // A tile's label and its Details button share a center line, and each
      // line of a tile sits level with the same line of the tiles beside it.
      const lines = await page.locator("li.health-tile").evaluateAll((nodes) =>
        nodes.map((node) => {
          const box = (selector: string) =>
            node.querySelector(selector)!.getBoundingClientRect();
          const center = (r: DOMRect) => r.top + r.height / 2;
          return {
            label: center(box(".tile-label")),
            details: center(box(".details-toggle")),
            state: box(".tile-state").top,
            figure: box(".tile-figure").top,
            top: node.getBoundingClientRect().top,
          };
        }),
      );
      for (const line of lines)
        expect(Math.abs(line.label - line.details)).toBeLessThanOrEqual(1);
      // Details buttons are full-size controls: 36 px with a mouse, 44 px narrow.
      for (const box of await boxes(page.locator(".details-toggle"))) {
        const side = viewport.width >= 768 ? 35.5 : 43.5;
        expect(box.width).toBeGreaterThanOrEqual(side);
        expect(box.height).toBeGreaterThanOrEqual(side);
      }
      for (const line of lines.filter(
        (item) => Math.abs(item.top - lines[0].top) <= 1,
      )) {
        expect(Math.abs(line.state - lines[0].state)).toBeLessThanOrEqual(1);
        expect(Math.abs(line.figure - lines[0].figure)).toBeLessThanOrEqual(1);
        expect(Math.abs(line.label - lines[0].label)).toBeLessThanOrEqual(1);
      }
      // Count rows: 32 px on a wide screen with a mouse, 44 px narrow; label and value centered together.
      const rows = await page.locator(".count-row").evaluateAll((nodes) =>
        nodes.map((node) => {
          const r = node.getBoundingClientRect();
          const center = (child: Element) => {
            const box = child.getBoundingClientRect();
            return box.top + box.height / 2;
          };
          return {
            height: r.height,
            dt: center(node.querySelector("dt")!),
            dd: center(node.querySelector("dd")!),
          };
        }),
      );
      for (const row of rows) {
        expect(Math.abs(row.dt - row.dd)).toBeLessThanOrEqual(1);
        expect(row.height).toBeGreaterThanOrEqual(
          viewport.width >= 768 ? 31.5 : 43.5,
        );
        if (viewport.width >= 768) expect(row.height).toBeLessThanOrEqual(33.5);
      }
      // Attention rows: icon, kind and title on one center line when wide.
      let attention: number[][] = [];
      if (viewport.width >= 768) {
        const lines = await page
          .locator(".attention-item")
          .evaluateAll((nodes) =>
            nodes.map((node) =>
              [".attention-icon", ".attention-kind", ".attention-title"].map(
                (selector) => {
                  const r = node
                    .querySelector(selector)!
                    .getBoundingClientRect();
                  return r.top + r.height / 2;
                },
              ),
            ),
          );
        for (const [icon, kind, title] of lines) {
          expect(Math.abs(icon - title)).toBeLessThanOrEqual(1);
          expect(Math.abs(kind - title)).toBeLessThanOrEqual(1);
        }
        attention = lines;
      }
      // Touch-size targets on a phone.
      if (viewport.width <= 360) {
        const small = await page
          .locator("weave-overview-page :is(button, a)")
          .evaluateAll((nodes) =>
            nodes
              .filter((node) => node.getClientRects().length)
              .map((node) => {
                const r = node.getBoundingClientRect();
                return {
                  name: node.textContent?.trim(),
                  height: r.height,
                  width: r.width,
                };
              })
              .filter((target) => target.height < 43.5 || target.width < 43.5),
          );
        expect(small).toEqual([]);
      }
      await expectNoClipping(page);
      const row0 = lines.filter(
        (item) => Math.abs(item.top - lines[0].top) <= 1,
      );
      record(`overview-${viewport.width}x${viewport.height}-layout`, {
        bandLeftSpread: spread(bands.map((box) => box.left)),
        bandRightSpread: spread(bands.map((box) => box.right)),
        tileColumns: columns,
        tileToBandLeft: round(Math.abs(first[0].left - bands[0].left)),
        tileToBandRight: round(
          Math.abs(first[columns - 1].right - bands[0].right),
        ),
        tileHeightSpread: spread(first.map((tile) => tile.height)),
        labelToDetailsCenter: most(
          lines.map((line) => Math.abs(line.label - line.details)),
        ),
        firstRowLineSpread: Math.max(
          spread(row0.map((line) => line.label)),
          spread(row0.map((line) => line.state)),
          spread(row0.map((line) => line.figure)),
        ),
        countRowHeight: [
          round(Math.min(...rows.map((row) => row.height))),
          round(Math.max(...rows.map((row) => row.height))),
        ],
        countLabelToValueCenter: most(
          rows.map((row) => Math.abs(row.dt - row.dd)),
        ),
        attentionCenterSpread: most(attention.map((line) => spread(line))),
        targets: await targetSizes(
          page.locator("weave-overview-page :is(button, a)"),
        ),
      });
      await shot(page, `overview-${viewport.width}x${viewport.height}-layout`);
      await page
        .locator(".page-content")
        .evaluate((node) => node.scrollTo(0, node.scrollHeight));
      await shot(
        page,
        `overview-${viewport.width}x${viewport.height}-layout-end`,
      );
    });
});
