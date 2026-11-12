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
// Operate against a REAL running local platform, through the real Studio host
// and real sign-in: no route interception, fake summary or network shaping.
// Overview shows live figures within a second of opening, every scheduled
// refresh of Overview, Runs, Workers and Incidents stays under 50,000 bytes,
// a reader without deployment or incident access sees those sections as
// needing access, and Runs lists the newest run first without test runs.
// Uses the variables of real-platform.spec.ts, plus:
//
//   WEAVE_E2E_VIEWER_FILE          A person with only the viewer role (the
//                                  acceptance harness's viewer).
//   WEAVE_E2E_OPERATIONS_EVIDENCE  A folder that receives the measured paint
//                                  time and poll sizes as JSON.
import { test, expect, Page, Response } from "@playwright/test";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import {
  connectAndSignIn,
  expectNoCredentialsLeft,
  openSidebar,
  pair,
  platformAnswer,
  removePlatform,
  runSummaries,
  session,
  setup,
  shot,
  startDemoRun,
  uniqueName,
  useRealPlatformSuite,
} from "./real-platform-support";

useRealPlatformSuite("operate-real");

const viewerFile = process.env["WEAVE_E2E_VIEWER_FILE"]?.trim() ?? "";
const evidence = process.env["WEAVE_E2E_OPERATIONS_EVIDENCE"]?.trim() ?? "";
const POLL_BYTES = 50_000;

/** Adds `value` under `key` in the evidence file, when a folder is given. */
function record(key: string, value: unknown) {
  if (!evidence) return;
  mkdirSync(evidence, { recursive: true });
  const file = join(evidence, "operate-real.json");
  let current: Record<string, unknown> = {};
  try {
    current = JSON.parse(readFileSync(file, "utf8")) as Record<string, unknown>;
  } catch {
    current = {};
  }
  writeFileSync(
    file,
    JSON.stringify({ ...current, [key]: value }, null, 2) + "\n",
  );
}

/** The path with every UUID replaced: scope and record IDs stay out of evidence. */
const redacted = (url: string) =>
  new URL(url).pathname.replace(
    /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g,
    ":id",
  );

interface Poll {
  page: string;
  started: number;
  ended: number;
  bytes: number;
  requests: string[];
}

/**
 * Records every platform answer the pages read through the host, grouping
 * answers that arrive within a second of each other as one refresh.
 */
function pollRecorder(page: Page) {
  const answers: { at: number; url: string; bytes: number }[] = [];
  let current = "";
  const pending: Promise<void>[] = [];
  page.on("response", (response: Response) => {
    const url = response.url();
    if (!url.includes("/studio/api/") || response.request().method() !== "GET")
      return;
    pending.push(
      response
        .body()
        .then((body) => {
          answers.push({ at: Date.now(), url, bytes: body.byteLength });
        })
        .catch(() => undefined),
    );
  });
  return {
    async start(name: string) {
      await Promise.all(pending);
      current = name;
      return answers.length;
    },
    async polls(from: number): Promise<Poll[]> {
      await Promise.all(pending);
      const polls: Poll[] = [];
      for (const answer of answers.slice(from)) {
        const last = polls.at(-1);
        if (last && answer.at - last.ended < 1000) {
          last.ended = answer.at;
          last.bytes += answer.bytes;
          last.requests.push(redacted(answer.url));
        } else
          polls.push({
            page: current,
            started: answer.at,
            ended: answer.at,
            bytes: answer.bytes,
            requests: [redacted(answer.url)],
          });
      }
      return polls;
    },
  };
}

/** Requests a metadata refresh must never make: run payloads, history or step output. */
const payload = (path: string) =>
  /\/history|\/export|\/logs|include=output|\/runs\/:id$/.test(path);

/**
 * Milliseconds from choosing Overview until a paint after its live figures
 * show: the clock starts at the click and stops two frames after the
 * live-figures container is visible.
 */
async function overviewPaint(page: Page): Promise<number> {
  await page.evaluate(() => performance.mark("overview-open"));
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  await expect(page.getByTestId("overview-meaningful")).toBeVisible();
  return page.evaluate(
    () =>
      new Promise<number>((resolve) => {
        requestAnimationFrame(() =>
          requestAnimationFrame(() =>
            resolve(
              performance.now() -
                performance.getEntriesByName("overview-open").at(-1)!.startTime,
            ),
          ),
        );
      }),
  );
}

test("overview first meaningful paint is under one second", async ({
  page,
}) => {
  test.setTimeout(240_000);
  const { person } = setup();
  const s = await session(page);
  const name = uniqueName("e2e-overview");
  await pair(page, s.host!, "overview-pairing");
  await connectAndSignIn(page, name, person);
  // Recorded, not gated: Overview opened while Home is still reading shares the
  // host with Home's reads.
  await openSidebar(page, "Home");
  record(
    "overview_paint_with_home_loading_ms",
    Math.round(await overviewPaint(page)),
  );
  await openSidebar(page, "Home");
  await expect(page.locator("weave-home-dashboard")).toBeVisible();
  // Home has finished its own reads: the gated clock measures Overview alone.
  await expect(page.locator("weave-home-dashboard")).not.toContainText(
    "Loading your",
    { timeout: 30_000 },
  );
  await page.waitForLoadState("networkidle");

  const requests: { url: string; started: number; ended: number }[] = [];
  page.on("requestfinished", (request) => {
    const timing = request.timing();
    requests.push({
      url: redacted(request.url()),
      started: timing.startTime,
      ended: timing.startTime + timing.responseEnd,
    });
  });
  const opened = Date.now();
  const paintMs = await overviewPaint(page);
  record("overview_first_meaningful_paint_ms", Math.round(paintMs));
  record(
    "overview_requests",
    requests
      .filter((item) => item.started >= opened - 50)
      .map((item) => ({
        url: item.url,
        after_open_ms: Math.round(item.started - opened),
        duration_ms: Math.round(item.ended - item.started),
      })),
  );
  await shot(page, "overview-live");
  expect(paintMs).toBeLessThan(1000);
  // Live figures: the platform's own health, not a placeholder.
  await expect(page.getByTestId("overview-meaningful")).toContainText(
    "Database",
  );
  expect(s.errors).toEqual([]);
  await removePlatform(page, name);
  expectNoCredentialsLeft();
});

test("operate polls stay under fifty kilobytes", async ({ page }) => {
  test.setTimeout(420_000);
  const { person } = setup();
  const s = await session(page);
  const name = uniqueName("e2e-polls");
  await pair(page, s.host!, "polls-pairing");
  await connectAndSignIn(page, name, person);
  const recorder = pollRecorder(page);
  const measured: Poll[] = [];
  for (const [label, wait] of [
    ["Overview", 50_000],
    ["Runs", 35_000],
    ["Workers", 35_000],
    ["Incidents", 50_000],
  ] as const) {
    const from = await recorder.start(label);
    await openSidebar(page, label);
    await page.waitForTimeout(wait);
    const polls = await recorder.polls(from);
    // The first answer opens the page; at least three scheduled refreshes follow.
    expect(polls.length, `${label} refreshes`).toBeGreaterThanOrEqual(4);
    for (const poll of polls) {
      expect(poll.bytes, `${label} refresh ${poll.requests}`).toBeLessThan(
        POLL_BYTES,
      );
      for (const path of poll.requests)
        expect(payload(path), `${label} read ${path}`).toBe(false);
    }
    measured.push(...polls);
    await shot(page, `polls-${label.toLowerCase()}`);
  }
  record(
    "polls",
    measured.map((poll) => ({
      ...poll,
      started: new Date(poll.started).toISOString(),
      ended: new Date(poll.ended).toISOString(),
    })),
  );
  expect(s.errors).toEqual([]);
  await removePlatform(page, name);
  expectNoCredentialsLeft();
});

test("a viewer sees the sections they may not read as needing access", async ({
  page,
}) => {
  test.setTimeout(240_000);
  test.skip(!viewerFile, "Set WEAVE_E2E_VIEWER_FILE to a viewer person.");
  const viewer = JSON.parse(readFileSync(viewerFile, "utf8")) as ReturnType<
    typeof setup
  >["person"];
  const s = await session(page);
  const name = uniqueName("e2e-viewer");
  await pair(page, s.host!, "viewer-pairing");
  await connectAndSignIn(page, name, viewer);
  const answered = platformAnswer(page, "/operations/summary");
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  const response = await answered;
  expect(response.status(), "the platform's summary").toBe(200);
  const summary = (await response.json()) as Record<string, unknown>;
  expect(summary["targets"]).toEqual({ state: "forbidden" });
  expect(summary["incidents"]).toEqual({ state: "forbidden" });
  // The last day's runs need only status.read, which a viewer has.
  expect(summary).toHaveProperty("runs_24h.started");
  const live = page.getByTestId("overview-meaningful");
  await expect(live).toBeVisible();
  await expect(live).toContainText("deployment.read");
  await expect(live).toContainText("incident.read");
  await shot(page, "viewer-overview");
  expect(s.errors).toEqual([]);
  await removePlatform(page, name);
  expectNoCredentialsLeft();
});

test("Runs lists the newest run first without test runs, and Refresh finds a run started after it opened", async ({
  page,
}) => {
  test.setTimeout(240_000);
  const { person } = setup();
  const s = await session(page);
  const name = uniqueName("e2e-runs");
  await pair(page, s.host!, "runs-pairing");
  await connectAndSignIn(page, name, person);
  const opened = runSummaries(page);
  await openSidebar(page, "Runs");
  const first = await opened;
  expect(first.status(), "the platform's run summaries").toBe(200);
  const query = new URL(first.url()).searchParams;
  expect(query.get("include_test") ?? "false").toBe("false");
  expect(query.get("order") ?? "started_desc").toBe("started_desc");
  const listing = (await first.json()) as {
    items: { id: string; test?: boolean; started_at?: string }[];
  };
  expect(listing.items.length).toBeGreaterThan(0);
  expect(listing.items.every((item) => item.test !== true)).toBe(true);
  const times = listing.items
    .map((item) => item.started_at)
    .filter((value): value is string => !!value)
    .map((value) => Date.parse(value));
  expect(times).toEqual([...times].sort((a, b) => b - a));
  await page.getByRole("switch", { name: "Auto-refresh" }).uncheck();

  const runId = await startDemoRun(page);
  const refreshed = runSummaries(page);
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  const second = await refreshed;
  expect(second.status(), "the platform's run summaries").toBe(200);
  const newest = (await second.json()) as { items: { id: string }[] };
  expect(newest.items[0]?.id, "the newest run").toBe(runId);
  await expect(
    page.locator(".runs-table .run-row").first().locator(".cell-run code"),
  ).toHaveText(runId.slice(0, 8), { timeout: 30_000 });
  await shot(page, "runs-newest");
  expect(s.errors).toEqual([]);
  await removePlatform(page, name);
  expectNoCredentialsLeft();
});

test("Metrics shows the failure rate the J5.4 runs produced", async ({
  page,
}) => {
  test.setTimeout(240_000);
  // J5.4 runs before the browser suites and records its workflow and figures next to this suite's evidence.
  const file = evidence ? join(evidence, "..", "operations-metrics.json") : "";
  let counted: { row: { workflow: string; runs: number } } | null = null;
  try {
    counted = JSON.parse(readFileSync(file, "utf8"));
  } catch {
    counted = null;
  }
  test.skip(!counted, "J5.4 did not record its runs in this acceptance run");
  const { person } = setup();
  const s = await session(page);
  const name = uniqueName("e2e-metrics");
  await pair(page, s.host!, "metrics-pairing");
  await connectAndSignIn(page, name, person);
  await openSidebar(page, "Metrics");
  await page
    .getByRole("combobox", { name: "Workflow" })
    .selectOption(counted!.row.workflow);
  const figure = (id: string) =>
    page.locator(`li.kpi[data-kpi="${id}"] .kpi-value`);
  // 20 runs, 3 of them timed out: 15% in Studio, as the API answered 0.15.
  await expect(figure("started")).toHaveText("20", { timeout: 30_000 });
  await expect(figure("failure")).toHaveText("15%");
  await shot(page, "metrics-live");
  record("metrics_failure_rate_text", await figure("failure").textContent());
  expect(s.errors).toEqual([]);
  await removePlatform(page, name);
  expectNoCredentialsLeft();
});
