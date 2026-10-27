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
// Operate › Metrics: figures, six charts with table views and the workflow
// and step tables, from run statistics. The range and the workflow live in
// the address; each chart shows its own problem; tables export to CSV; the
// layout never scrolls sideways. The platform is mocked with typed literals.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page, type Route } from "@playwright/test";
import type { StatisticsSeries } from "../../src/app/operate/metrics/chart-model";
import type {
  SeriesAnswer,
  StepAnswer,
  WorkflowAnswer,
} from "../../src/app/operate/metrics/metrics-model";
import { connected } from "./support";

const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";
const captures = resolve(
  process.env["STUDIO_OPERATE_CAPTURES"]?.trim() || "test-results/operate",
);
mkdirSync(captures, { recursive: true });
const every = ["status.read", "run.read", "incident.read", "deployment.read"];
const UNITS: Record<string, StatisticsSeries["unit"]> = {
  failure_rate: "ratio",
  worker_utilization: "ratio",
  duration_p50: "ms",
  duration_p95: "ms",
  oldest_ready_seconds: "seconds",
};

function points(count: number, value: (index: number) => number | null) {
  const start = Date.parse("2026-10-25T15:00:00Z");
  return Array.from(
    { length: count },
    (_, index) =>
      [
        new Date(start + index * 3_600_000).toISOString().replace(".000", ""),
        value(index),
      ] as [string, number | null],
  );
}

/** A series answer for whatever the page asked: 24 hourly points per signal. */
function seriesAnswer(url: URL, empty = false): SeriesAnswer {
  const query = url.searchParams;
  const signals = query.getAll("signals");
  const group = query.get("group_by");
  const labels: StatisticsSeries["labels"][] =
    group === "task_type"
      ? [{ task_type: "email" }, { task_type: "(other)" }]
      : group === "workflow"
        ? [{ workflow: "orders" }, { workflow: "invoices" }]
        : [{}];
  const value = (signal: string, index: number): number | null => {
    if (empty) return signal.startsWith("runs_") ? 0 : null;
    switch (signal) {
      case "runs_started":
        return 4 + (index % 3);
      case "runs_succeeded":
        return 3 + (index % 3);
      case "runs_failed":
        return index % 4 === 0 ? 1 : 0;
      case "runs_cancelled":
        return 0;
      case "failure_rate":
        return index % 4 === 0 ? 0.25 : 0;
      case "duration_p50":
        return 450 + index * 10;
      case "duration_p95":
        return 12_000 + index * 100;
      case "tasks_ready":
        return index % 5;
      case "oldest_ready_seconds":
        return index === 23 ? 95 : 10;
      case "outbox_pending":
        return index % 2;
      case "worker_utilization":
        return 0.4;
      default:
        return null;
    }
  };
  return {
    from: query.get("from") ?? "",
    to: query.get("to") ?? "",
    granularity: "1h",
    complete_until: "2026-10-26T14:30:00Z",
    series: signals.flatMap((signal) =>
      labels.map((label) => ({
        signal,
        labels: label,
        unit: UNITS[signal] ?? "count",
        points: points(24, (index) => value(signal, index)),
      })),
    ),
  };
}

const window = {
  from: "2026-10-25T14:32:10Z",
  to: "2026-10-26T14:32:10Z",
  granularity: "1h" as const,
  complete_until: "2026-10-26T14:30:00Z",
};
const workflowAnswer: WorkflowAnswer = {
  ...window,
  workflows: [
    {
      workflow: "orders",
      versions: ["1.2.0", "1.1.0"],
      runs: 100,
      succeeded: 92,
      failed: 8,
      cancelled: 0,
      failure_rate: 0.08,
      duration_p50_ms: 450,
      duration_p95_ms: 12_000,
      last_run_at: "2026-10-26T14:31:00Z",
    },
    {
      workflow: "invoices",
      versions: ["2.0.0"],
      runs: 20,
      succeeded: 20,
      failed: 0,
      cancelled: 0,
      failure_rate: 0,
      duration_p50_ms: 900,
      duration_p95_ms: 2_000,
      last_run_at: "2026-10-26T13:00:00Z",
    },
  ],
  totals: {
    runs: 120,
    succeeded: 112,
    failed: 8,
    cancelled: 0,
    failure_rate: 0.0667,
    duration_p50_ms: 500,
    duration_p95_ms: 11_000,
  },
};
const stepAnswer: StepAnswer = {
  ...window,
  workflow: "orders",
  steps: [
    {
      node_id: "charge",
      kind: "action",
      executions: 100,
      failures: 8,
      duration_p50_ms: 120,
      duration_p95_ms: 2_500,
      queue_p50_ms: 15,
      queue_p95_ms: 600,
    },
    {
      node_id: "approve",
      kind: "human_task",
      executions: 92,
      failures: 0,
      duration_p50_ms: 60_000,
      duration_p95_ms: 3_600_000,
      queue_p50_ms: null,
      queue_p95_ms: null,
    },
  ],
};

interface Mock {
  series?: (url: URL) => { status?: number; json: unknown };
  workflows?: () => { status?: number; json: unknown };
}
/** Connects and answers run statistics; returns every statistics request. */
async function metrics(page: Page, capabilities = every, mock: Mock = {}) {
  const requests: URL[] = [];
  await connected(page, { capabilities });
  await page.route(`${environment}/run-statistics/**`, (route: Route) => {
    const url = new URL(route.request().url());
    requests.push(url);
    const kind = url.pathname.split("/").pop();
    const answer =
      kind === "series"
        ? (mock.series?.(url) ?? { json: seriesAnswer(url) })
        : kind === "workflows"
          ? (mock.workflows?.() ?? { json: workflowAnswer })
          : { json: stepAnswer };
    return route.fulfill({ status: answer.status ?? 200, json: answer.json });
  });
  return requests;
}
const open = (page: Page) =>
  page.getByRole("button", { name: "Metrics", exact: true }).click();
const shot = (page: Page, name: string) =>
  page.screenshot({ path: `${captures}/${name}.png`, fullPage: true });

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
      "weave-metrics-page *",
    )) {
      const style = getComputedStyle(node);
      if (style.display === "none" || !node.getClientRects().length) continue;
      if (node.closest("svg") || node.closest(".metrics-table-wrap")) continue;
      if (
        node.scrollWidth > node.clientWidth + 1 &&
        style.overflowX !== "visible" &&
        style.textOverflow !== "ellipsis"
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
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    const size = `${viewport.width}x${viewport.height}`;

    test("Metrics shows the range's figures, six charts and the workflow table", async ({
      page,
    }) => {
      const requests = await metrics(page);
      await open(page);
      await expect(page).toHaveURL(/\/operate\/metrics$/);
      await expect(page.locator("#main h1")).toHaveText("Metrics");
      await expect(page).toHaveTitle("Metrics | Firefly Weave Studio");
      const figures = page.locator("li.kpi");
      await expect(figures.locator(".kpi-label")).toHaveText([
        "Runs started",
        "Success rate",
        "Failure rate",
        "p50 run duration",
        "p95 run duration",
        "Oldest ready task",
      ]);
      // 24 hourly buckets: 4, 5, 6, … runs started; 6 failures among the ended runs.
      await expect(figures.locator(".kpi-value")).toHaveText([
        "120",
        "94.1%",
        "5.9%",
        "500 ms",
        "11 s",
        "1 min 35 s",
      ]);
      await expect(page.locator(".metrics-note")).toHaveText(
        "Test runs are not included.",
      );
      await expect(page.locator(".freshness")).toHaveText(
        /^Data complete until \d{2}:\d{2}$/,
      );
      const charts = page.locator(".chart-card");
      await expect(charts).toHaveCount(6);
      for (const name of [
        "Throughput",
        "Failure rate",
        "Run duration, p50 and p95",
        "Ready tasks by task type",
        "Outbox backlog",
        "Worker utilization",
      ])
        await expect(
          page.getByRole("img", { name, exact: true }),
        ).toBeVisible();
      const throughput = charts.filter({ hasText: "Throughput" });
      await expect(throughput.locator(".legend-item")).toHaveText([
        "Succeeded",
        "Failed",
        "Canceled",
        "Runs started",
      ]);
      // Outcomes take their status colors; p95 is the p50 hue, dashed.
      const fills = await throughput
        .locator("rect.chart-column")
        .evaluateAll((nodes) => [
          ...new Set(nodes.map((node) => node.getAttribute("fill"))),
        ]);
      expect(fills.sort()).toEqual(["var(--danger)", "var(--success)"]);
      const durations = charts.filter({ hasText: "Run duration" });
      await expect(durations.locator("path.chart-line")).toHaveCount(2);
      expect(
        await durations
          .locator("path.chart-line")
          .evaluateAll((nodes) =>
            nodes.map((node) => [
              node.getAttribute("stroke"),
              node.getAttribute("stroke-dasharray"),
            ]),
          ),
      ).toEqual([
        ["var(--series-1)", null],
        ["var(--series-1)", "4 4"],
      ]);
      // Every bucket has tooltip text naming each series.
      await expect(
        throughput.locator("rect.chart-hover title").first(),
      ).toContainText("Succeeded: ");
      const table = page.locator("table.workflows-table");
      await expect(table.locator("tbody tr")).toHaveCount(2);
      await expect(table.locator("tbody tr").first()).toContainText("orders");
      await expect(table.locator("tbody tr").first()).toContainText("8%");
      // Seven series reads, one workflow table, no steps.
      expect(
        requests.filter((url) => url.pathname.endsWith("/series")).length,
      ).toBe(7);
      expect(
        requests.filter((url) => url.pathname.endsWith("/steps")).length,
      ).toBe(0);
      await shot(page, `metrics-${size}-ready`);
      await expectNoClipping(page);
      // Controls are 36 px with a fine pointer on a wide screen, 44 px otherwise.
      const target = viewport.width >= 768 ? 36 : 44;
      for (const height of await page
        .locator(".range-option, .chart-toggle, .table-actions button")
        .evaluateAll((nodes) =>
          nodes.map((node) => node.getBoundingClientRect().height),
        ))
        expect(Math.round(height)).toBeGreaterThanOrEqual(target);
      // The range buttons share one row and one height.
      const ranges = await page
        .locator(".range-option")
        .evaluateAll((nodes) =>
          nodes.map((node) => node.getBoundingClientRect().top),
        );
      expect(new Set(ranges.map(Math.round)).size).toBe(1);
    });

    test("a workflow opens its steps and Back returns to all workflows", async ({
      page,
    }) => {
      const requests = await metrics(page);
      await open(page);
      await page
        .locator("table.workflows-table")
        .getByRole("link", { name: "orders", exact: true })
        .click();
      await expect(page).toHaveURL(/\/operate\/metrics\?workflow=orders$/);
      await expect(
        page.getByRole("heading", { name: "Steps of orders" }),
      ).toBeVisible();
      const rows = page.locator("table.steps-table tbody tr");
      await expect(rows).toHaveCount(2);
      await expect(rows.first()).toContainText("charge");
      await expect(rows.nth(1)).toContainText("Human task");
      const steps = requests.find((url) => url.pathname.endsWith("/steps"));
      expect(steps?.searchParams.get("workflow")).toBe("orders");
      // Run charts follow the workflow; shared capacity stays environment-wide.
      const series = requests.filter(
        (url) =>
          url.pathname.endsWith("/series") &&
          url.searchParams.get("workflow") === "orders",
      );
      expect(
        series.map((url) => url.searchParams.getAll("signals")[0]).sort(),
      ).toEqual(["duration_p50", "failure_rate", "runs_succeeded"]);
      await expect(
        page.locator('li.kpi[data-kpi="p95"] .kpi-value'),
      ).toHaveText("12 s");
      await shot(page, `metrics-${size}-steps`);
      await expectNoClipping(page);
      await page.getByRole("link", { name: "Back to all workflows" }).click();
      await expect(page).toHaveURL(/\/operate\/metrics$/);
      await expect(page.locator("table.workflows-table")).toBeVisible();
    });
  });

test.describe("range and address", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the range is written to the address and read back from it", async ({
    page,
  }) => {
    const requests = await metrics(page);
    await page.goto("/operate/metrics?range=30d");
    await expect(page.locator("#main h1")).toHaveText("Metrics");
    await expect(
      page.getByRole("button", { name: "30 d", exact: true }),
    ).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator("li.kpi")).toHaveCount(6);
    const first = requests.find((url) => url.pathname.endsWith("/workflows"))!;
    const span =
      Date.parse(first.searchParams.get("to")!) -
      Date.parse(first.searchParams.get("from")!);
    expect(span).toBe(30 * 24 * 3_600_000);
    requests.length = 0;
    await page.getByRole("button", { name: "7 d", exact: true }).click();
    await expect(page).toHaveURL(/\/operate\/metrics\?range=7d$/);
    await expect
      .poll(() => requests.filter((url) => url.pathname.endsWith("/workflows")))
      .toHaveLength(1);
    const after = requests.find((url) => url.pathname.endsWith("/workflows"))!;
    expect(
      Date.parse(after.searchParams.get("to")!) -
        Date.parse(after.searchParams.get("from")!),
    ).toBe(7 * 24 * 3_600_000);
    // Back returns to the range before.
    await page.goBack();
    await expect(page).toHaveURL(/\/operate\/metrics\?range=30d$/);
    await expect(
      page.getByRole("button", { name: "30 d", exact: true }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  test("every chart can be read as a table, and tables export to CSV", async ({
    page,
  }) => {
    await metrics(page);
    await open(page);
    const throughput = page
      .locator(".chart-card")
      .filter({ hasText: "Throughput" });
    const toggle = throughput.getByRole("button", { name: "Show as table" });
    await toggle.click();
    await expect(
      throughput.getByRole("button", { name: "Show as chart" }),
    ).toHaveAttribute("aria-pressed", "true");
    const table = throughput.locator("table.chart-table");
    await expect(table.locator("thead th")).toHaveText([
      "Time",
      "Succeeded",
      "Failed",
      "Canceled",
      "Runs started",
    ]);
    await expect(table.locator("tbody tr")).toHaveCount(24);
    const download = page.waitForEvent("download");
    await page
      .locator(".metrics-table-band")
      .getByRole("button", { name: "Export CSV" })
      .click();
    const file = await download;
    expect(file.suggestedFilename()).toBe("metrics-workflows-24h.csv");
    const text = await (await file.createReadStream()).toArray();
    expect(Buffer.concat(text).toString("utf8").split("\r\n")[0]).toBe(
      "Workflow,Versions,Runs,Succeeded,Failed,Canceled,Failure rate,p50 duration (ms),p95 duration (ms),Last run",
    );
  });
});

test.describe("states", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a reader without status.read sees no entry and an access state at the address", async ({
    page,
  }) => {
    const requests = await metrics(page, ["run.read"]);
    await expect(
      page.getByRole("button", { name: "Runs", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Metrics", exact: true }),
    ).toHaveCount(0);
    await page.goto("/operate/metrics");
    await expect(page.getByText("You need")).toContainText("status.read");
    expect(requests).toEqual([]);
  });

  test("a platform without run statistics says so plainly and stops asking", async ({
    page,
  }) => {
    // A fake clock from the start, so the page's own timers are the ones it moves.
    await page.clock.install();
    const requests = await metrics(page, every, {
      series: () => ({
        status: 501,
        json: { code: "WV-UNAVAILABLE", message: "Not served" },
      }),
      workflows: () => ({
        status: 501,
        json: { code: "WV-UNAVAILABLE", message: "Not served" },
      }),
    });
    await open(page);
    await expect(
      page.getByRole("heading", {
        name: "This platform doesn't keep run statistics yet",
      }),
    ).toBeVisible();
    await expect(page.locator(".operate-problem")).toHaveCount(0);
    const asked = requests.length;
    await page.clock.runFor(130_000);
    expect(requests.length).toBe(asked);
  });

  test("a new range shows each chart loading, never failed", async ({
    page,
  }) => {
    let hold: (() => void) | null = null;
    await metrics(page, every, {
      series: (url) => ({ json: seriesAnswer(url) }),
    });
    await open(page);
    await expect(page.locator("svg.chart-frame")).toHaveCount(6);
    // Hold the next answers until the cards have been looked at.
    const held = new Promise<void>((resolve) => (hold = resolve));
    await page.route(
      `${environment}/run-statistics/series?*`,
      async (route) => {
        await held;
        await route.fallback();
      },
    );
    await page.getByRole("button", { name: "7 d", exact: true }).click();
    await expect(
      page.locator(".chart-card weave-operate-state .skeleton"),
    ).toHaveCount(6);
    await expect(
      page.getByText("This section could not be loaded."),
    ).toHaveCount(0);
    hold!();
    await expect(page.locator("svg.chart-frame")).toHaveCount(6);
  });

  test("a range without runs says so and keeps the charts", async ({
    page,
  }) => {
    await metrics(page, every, {
      series: (url) => ({ json: seriesAnswer(url, true) }),
      workflows: () => ({ json: { ...window, workflows: [], totals: null } }),
    });
    await open(page);
    await expect(page.locator(".metrics-empty")).toHaveText(
      /No runs in this range yet\. Run a workflow to see throughput, failures and durations here\./,
    );
    await expect(page.locator("li.kpi .kpi-value")).toHaveText([
      "0",
      "—",
      "—",
      "—",
      "—",
      "—",
    ]);
    await expect(page.locator(".chart-card")).toHaveCount(6);
    await expect(
      page.locator(".chart-card").filter({ hasText: "Outbox backlog" }),
    ).toContainText("No outbox backlog in this range");
    await expect(page.locator(".band-note")).toHaveText(
      "No workflow ran in this range.",
    );
  });

  test("a failed read shows its code with Try again, and one failed chart only its own problem", async ({
    page,
  }) => {
    let broken = true;
    await metrics(page, every, {
      series: (url) =>
        broken || url.searchParams.getAll("signals").includes("outbox_pending")
          ? {
              status: 503,
              json: { code: "WV-UNAVAILABLE", message: "Try again later" },
            }
          : { json: seriesAnswer(url) },
      workflows: () =>
        broken
          ? {
              status: 503,
              json: { code: "WV-UNAVAILABLE", message: "Try again later" },
            }
          : { json: workflowAnswer },
    });
    await open(page);
    await expect(page.locator(".operate-problem")).toContainText(
      "Support code: WV-UNAVAILABLE",
    );
    broken = false;
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(page.locator("li.kpi")).toHaveCount(6);
    const outbox = page
      .locator(".chart-card")
      .filter({ hasText: "Outbox backlog" });
    await expect(outbox).toContainText("This section could not be loaded.");
    await expect(page.locator("svg.chart-frame")).toHaveCount(5);
  });
});

test.describe("time west of UTC", () => {
  test.use({
    viewport: { width: 1440, height: 900 },
    timezoneId: "America/Los_Angeles",
  });

  test("daily buckets keep their UTC day and hourly ones over days carry the date", async ({
    page,
  }) => {
    await metrics(page, every, {
      series: (url) => {
        const daily = url.searchParams.get("from")?.startsWith("2026-09");
        const answer = seriesAnswer(url);
        if (url.searchParams.get("from")?.startsWith("2026-10-19"))
          // Seven days hourly: 48 points are enough to span more than a day.
          return {
            json: {
              ...answer,
              series: answer.series.map((item) => ({
                ...item,
                points: [...item.points, ...item.points].map(
                  ([, value], index) =>
                    [
                      new Date(Date.UTC(2026, 9, 25, 15) + index * 3_600_000)
                        .toISOString()
                        .replace(".000", ""),
                      value,
                    ] as [string, number | null],
                ),
              })),
            },
          };
        if (!daily) return { json: answer };
        return {
          json: {
            ...answer,
            granularity: "1d",
            series: answer.series.map((item) => ({
              ...item,
              points: [0, 1, 2].map(
                (index) =>
                  [
                    new Date(Date.UTC(2026, 9, 24 + index))
                      .toISOString()
                      .replace(".000", ""),
                    item.points[index][1],
                  ] as [string, number | null],
              ),
            })),
          },
        };
      },
    });
    await page.clock.setFixedTime(new Date("2026-10-26T14:32:10Z"));
    await page.goto("/operate/metrics?range=30d");
    const throughput = page
      .locator(".chart-card")
      .filter({ hasText: "Throughput" });
    await expect(throughput.locator(".chart-note")).toHaveText("Days in UTC");
    await throughput.getByRole("button", { name: "Show as table" }).click();
    await expect(throughput.locator("table.chart-table tbody th")).toHaveText([
      "Oct 24",
      "Oct 25",
      "Oct 26",
    ]);
    // A 7-day hourly range: every row names its day as well as its hour.
    await page.goto("/operate/metrics?range=7d");
    const hourly = page
      .locator(".chart-card")
      .filter({ hasText: "Throughput" });
    await hourly.getByRole("button", { name: "Show as table" }).click();
    await expect(
      hourly.locator("table.chart-table tbody th").first(),
    ).toHaveText("Oct 25, 08:00");
    await expect(hourly.locator(".chart-note")).toHaveCount(0);
  });
});
