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
// Operate › Runs: run summaries newest first, filters on the server (the
// shareable ones in the address, business and correlation keys never), one
// frozen chain of pages per refresh, a run's own address with its steps from
// runs.steps, bulk Cancel and Retry, the refresh intervals, and a workspace
// switch that leaves nothing behind. The platform is the run views fake.
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Locator, type Page } from "@playwright/test";
import { answerRunViews } from "../../src/app/operate/run-views-fake";
import {
  fixtureIds,
  runViewsFixture,
  type RunViewsFixture,
} from "../../src/app/operate/run-views.fixture";
import type { RunSummary } from "../../src/app/operate/run-contracts";
import { connected, selectChoice } from "./support";
import { acme, acmePlatform, platformHost } from "./platform-host";

const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";
// Review captures, and the geometry each layout test measured, go to
// test-results/operate, or to the folder STUDIO_OPERATE_CAPTURES names.
const captures = resolve(
  process.env["STUDIO_OPERATE_CAPTURES"]?.trim() || "test-results/operate",
);
mkdirSync(captures, { recursive: true });
const NOW = "2026-10-07T12:00:00Z";
const reader = ["run.read", "catalog.read"];
const operator = [
  ...reader,
  "run.cancel",
  "run.retry",
  "run.signal",
  "run.pause",
  "run.resume",
  "run.archive",
];
const shot = (page: Page, name: string) =>
  page.screenshot({ path: `${captures}/${name}.png` });

/** A copy of the fixture the test may change. */
const data = (): RunViewsFixture => structuredClone(runViewsFixture);
/** Another run of invoice-approval that started at `at`. */
function started(
  id: string,
  at: string,
  facts: Partial<RunSummary> = {},
): RunSummary {
  return {
    ...runViewsFixture.runs[0].summary,
    id,
    status: "succeeded",
    started_at: at,
    updated_at: at,
    ended_at: at,
    duration_ms: 1000,
    business_key: null,
    ...facts,
  };
}
const uuid = (n: number) =>
  `aaaaaaaa-0000-4000-8000-${String(n).padStart(12, "0")}`;

interface Platform {
  /** Stops the page clock a moment from now; only the test moves it after. */
  pause(): Promise<void>;
  /**
   * Stops the page clock just before the next answer of this kind, so the
   * wait that follows it starts from a time only the test moves. Resolves
   * once the clock has stopped, with that request already counted.
   */
  pauseAtNext(kind: "summaries" | "steps"): Promise<void>;
  /** Answers run summaries 503 while set. */
  failSummaries: boolean;
  requestedSummaries: URLSearchParams[];
  steps: URL[];
  logs: string[];
  views: Map<string, string>;
  fixture: RunViewsFixture;
  posts: { path: string; body: unknown }[];
  /** A run's own reads, in order: "view", "lifecycle" or "history". */
  details: string[];
  /** Run view reads answered so far. */
  viewsAnswered: number;
  /** Step reads answered so far. */
  stepsAnswered: number;
}
/**
 * Connects at `NOW` (the fixture's day) and answers run summaries and steps
 * from the fake, and each run's detail from its summary. Unless the identity
 * is delayed, it waits until Studio knows the person.
 */
async function platform(
  page: Page,
  capabilities: string[] = operator,
  fixture: RunViewsFixture = data(),
  { time = NOW, identityDelay = 0 } = {},
): Promise<Platform> {
  await page.clock.install({ time: new Date(time) });
  const installed = Date.now();
  const pause = () =>
    page.clock.pauseAt(
      new Date(Date.parse(time) + (Date.now() - installed) + 300),
    );
  let next: { kind: "summaries" | "steps"; paused: () => void } | null = null;
  const pauseAtNext = (kind: "summaries" | "steps") =>
    new Promise<void>((paused) => {
      next = { kind, paused };
    });
  const pauseIfAsked = async (kind: "summaries" | "steps") => {
    const asked = next;
    if (asked?.kind !== kind) return;
    next = null;
    await pause();
    asked.paused();
  };
  const identity = identityDelay
    ? null
    : page.waitForResponse("**/studio/api/api/v1/identity");
  await connected(page, { capabilities, identityDelay });
  await identity;
  const requestedSummaries: URLSearchParams[] = [];
  const steps: URL[] = [];
  const logs: string[] = [];
  const posts: { path: string; body: unknown }[] = [];
  const views = new Map<string, string>();
  const result: Platform = {
    pause,
    pauseAtNext,
    failSummaries: false,
    requestedSummaries,
    steps,
    logs,
    views,
    fixture,
    posts,
    details: [],
    viewsAnswered: 0,
    stepsAnswered: 0,
  };
  await page.route("**/run-summaries?**", async (route) => {
    const url = route.request().url();
    requestedSummaries.push(new URL(url).searchParams);
    await pauseIfAsked("summaries");
    if (result.failSummaries)
      return route.fulfill({
        status: 503,
        json: { code: "WV-UNAVAILABLE", message: "Down" },
      });
    const answer = answerRunViews(url, fixture);
    if (!answer) throw new Error("Expected run summary fixture route");
    await route.fulfill({ status: answer.status, json: answer.body });
  });
  await page.route("**/runs/*/steps**", async (route) => {
    const url = route.request().url();
    steps.push(new URL(url));
    await pauseIfAsked("steps");
    const answer = answerRunViews(url, fixture)!;
    await route.fulfill({ status: answer.status, json: answer.body });
    result.stepsAnswered++;
  });
  await page.route("**/runs/*/logs**", async (route) => {
    logs.push(route.request().url());
    await route.fulfill({
      status: 501,
      json: { code: "WV-UNAVAILABLE", message: "Not served" },
    });
  });
  await page.route(`${environment}/runs/*`, async (route) => {
    const request = route.request();
    const id = new URL(request.url()).pathname.split("/").pop()!;
    const run = fixture.runs.find((item) => item.summary.id === id)?.summary;
    if (!run)
      return route.fulfill({
        status: 404,
        json: { code: "WV-NOT-FOUND", message: "Run not found" },
      });
    const status = views.get(id) ?? run.status;
    result.details.push("view");
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
        state: { status, active: [], input: { invoice: id.slice(0, 4) } },
      },
    });
    result.viewsAnswered++;
  });
  await page.route(`${environment}/runs/*/lifecycle`, (route) => {
    result.details.push("lifecycle");
    return route.fulfill({
      json: { archived: false, purged: false, revision: 1 },
    });
  });
  await page.route(`${environment}/runs/*/history?*`, (route) => {
    result.details.push("history");
    return route.fulfill({ json: { events: [], next_cursor: null } });
  });
  await page.route(`${environment}/runs/*/cancel`, async (route) => {
    posts.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON(),
    });
    await route.fulfill({ json: { id: "cancelled" } });
  });
  await page.route(`${environment}/runs/*/retry`, async (route) => {
    posts.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON(),
    });
    await route.fulfill({
      status: 201,
      json: { id: uuid(posts.length), state: { status: "queued" } },
    });
  });
  return result;
}
const openRuns = (page: Page) =>
  page.getByRole("button", { name: "Runs", exact: true }).click();
const table = (page: Page) => page.getByRole("table", { name: "Runs" });
const rows = (page: Page) => table(page).locator(".run-row");
const names = (page: Page) => table(page).locator(".run-row .row-open");
const last = (platform: Platform) => platform.requestedSummaries.at(-1)!;
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
 * With the clock paused the page draws only when time moves: move it a frame
 * at a time until `check` holds.
 */
async function drawn(page: Page, check: () => Promise<void>) {
  await expect(async () => {
    await page.clock.runFor(16);
    await check();
  }).toPass({ timeout: 10_000, intervals: [50] });
}
/**
 * Refreshes the list with one more run in it and stops the clock as that
 * answer lands; returns once the page has drawn the run. Every wait after it
 * starts from a time only the test moves.
 */
async function refreshAndPause(page: Page, runs: Platform) {
  const shown = await rows(page).count();
  runs.fixture.runs.push({
    summary: started(
      uuid(800 + runs.requestedSummaries.length),
      "2026-10-07T11:59:00Z",
    ),
  });
  const paused = runs.pauseAtNext("summaries");
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await paused;
  await drawn(page, () =>
    expect(rows(page)).toHaveCount(shown + 1, { timeout: 100 }),
  );
}
/**
 * Waits until every run and step read asked so far is answered, then lets the
 * page take the answers (message tasks run while the clock is paused), so the
 * open run's next 2 s wait has started before the test moves time.
 */
async function readsSettled(page: Page, runs: Platform) {
  await expect
    .poll(
      () =>
        runs.viewsAnswered ===
          runs.details.filter((read) => read === "view").length &&
        runs.stepsAnswered === runs.steps.length,
    )
    .toBe(true);
  for (let turn = 0; turn < 3; turn++)
    await page.evaluate(
      () =>
        new Promise<void>((done) => {
          const channel = new MessageChannel();
          channel.port1.onmessage = () => done();
          channel.port2.postMessage(null);
        }),
    );
}
const statusChoice = (page: Page, name: string) =>
  page
    .getByRole("group", { name: "Status" })
    .getByRole("button", { name, exact: true });

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
  { width: 360, height: 640 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    const size = `${viewport.width}x${viewport.height}`;

    test("Runs lists summaries newest first and filters them on the server", async ({
      page,
    }) => {
      const runs = await platform(page);
      await openRuns(page);
      await expect(page).toHaveURL(/\/operate\/runs$/);
      await expect(rows(page)).toHaveCount(6);
      // Newest first; the unavailable run shows only its safe ID.
      await expect(names(page)).toHaveText([
        "invoice-approval",
        "invoice-approval",
        "notify-customer",
        "invoice-approval",
        "invoice-approval",
      ]);
      const unavailable = rows(page).last();
      await expect(unavailable).toContainText("Unavailable run");
      await expect(unavailable).toContainText(
        fixtureIds.unavailable.slice(0, 8),
      );
      await expect(unavailable).not.toContainText("invoice-approval");
      await expect(unavailable.locator(".status-pill")).toHaveCount(0);
      // The default window is the last 24 hours, ending when the page read it.
      const first = runs.requestedSummaries[0];
      const before = Date.parse(first.get("started_before")!);
      expect(before).toBeGreaterThanOrEqual(Date.parse(NOW));
      expect(before).toBeLessThan(Date.parse(NOW) + 30_000);
      expect(before - Date.parse(first.get("started_after")!)).toBe(86_400_000);
      expect(first.get("include_test")).toBe("false");
      expect(first.has("cursor")).toBe(false);
      // A succeeded run that handled errors says so in its status.
      await expect(
        rows(page).nth(1).locator(".status-pill").first(),
      ).toHaveText("Succeeded with errors");
      await shot(page, `runs-${size}-list`);
      // Narrow screens fold the filters behind one button.
      if (viewport.width < 768) {
        await expect(page.getByLabel("Origin", { exact: true })).toBeHidden();
        await page.getByRole("button", { name: "Filters (0)" }).click();
      }
      // Include test runs: the request asks, and the test runs join the list.
      await page.getByRole("checkbox", { name: "Include test runs" }).check();
      await expect.poll(() => last(runs).get("include_test")).toBe("true");
      await expect(rows(page)).toHaveCount(8);
      await expect(
        rows(page).first().locator(".tag").filter({ hasText: "Test" }),
      ).toBeVisible();
      await expect(page).toHaveURL(/\/operate\/runs\?include_test=true$/);
      await page.getByRole("checkbox", { name: "Include test runs" }).uncheck();
      await expect.poll(() => last(runs).get("include_test")).toBe("false");
      // Origin and status.
      await page.getByLabel("Origin", { exact: true }).selectOption("retry");
      await expect.poll(() => last(runs).get("origin")).toBe("retry");
      await expect(rows(page)).toHaveCount(1);
      await page.getByLabel("Origin", { exact: true }).selectOption("");
      await statusChoice(page, "Failed").click();
      await expect.poll(() => last(runs).getAll("status")).toEqual(["failed"]);
      await expect(rows(page)).toHaveCount(1);
      await statusChoice(page, "Running").click();
      await expect
        .poll(() => last(runs).getAll("status"))
        .toEqual(["running", "failed"]);
      await expect(rows(page)).toHaveCount(2);
      // Advanced filters: handled errors, top-level only, archived.
      await page.getByRole("button", { name: "More filters" }).click();
      await page.getByLabel("Handled errors").selectOption("true");
      await expect
        .poll(() => last(runs).get("has_handled_errors"))
        .toBe("true");
      await page.getByLabel("Handled errors").selectOption("");
      await statusChoice(page, "Failed").click();
      await statusChoice(page, "Running").click();
      await page.getByRole("checkbox", { name: "Top-level runs only" }).check();
      await expect.poll(() => last(runs).get("top_level_only")).toBe("true");
      await expect(
        names(page).filter({ hasText: "notify-customer" }),
      ).toHaveCount(0);
      await page
        .getByRole("checkbox", { name: "Include archived runs" })
        .check();
      await page.getByLabel("Time range").selectOption("7d");
      await expect.poll(() => last(runs).get("include_archived")).toBe("true");
      await expect(table(page)).toContainText(fixtureIds.archived.slice(0, 8));
      await expect(page).toHaveURL(/range=7d/);
      // Private keys reach the platform but never the address.
      await page.getByLabel("Business key").fill("INV-1041");
      await expect.poll(() => last(runs).get("business_key")).toBe("INV-1041");
      await page.getByLabel("Correlation key").fill("corr secret");
      await expect
        .poll(() => last(runs).get("correlation_key"))
        .toBe("corr secret");
      expect(page.url()).not.toContain("INV-1041");
      expect(page.url()).not.toContain("corr");
      await shot(page, `runs-${size}-filters`);
      // Clear filters resets every one, the advanced and private ones too.
      await page
        .getByRole("button", { name: /^Clear filters/ })
        .first()
        .click();
      await expect(page).toHaveURL(/\/operate\/runs$/);
      await expect.poll(() => last(runs).has("business_key")).toBe(false);
      expect(last(runs).has("correlation_key")).toBe(false);
      expect(last(runs).get("include_archived")).toBe("false");
      expect(last(runs).get("top_level_only")).toBe("false");
      await expect(page.getByLabel("Business key")).toHaveValue("");
      await expect(rows(page)).toHaveCount(6);
      // Every filter change started a new chain: no request carried a cursor.
      expect(runs.requestedSummaries.some((query) => query.has("cursor"))).toBe(
        false,
      );
    });

    test("a run's own address opens it with its real step timeline", async ({
      page,
    }) => {
      const runs = await platform(page);
      await page.goto(`/operate/runs/${fixtureIds.failed}`);
      const detail = page.locator(".record-detail");
      await expect(detail.locator("#record-detail-title")).toHaveText(
        "invoice-approval",
      );
      const steps = detail.getByRole("list", { name: "Step timeline" });
      await expect(steps).toBeVisible();
      await expect.poll(() => runs.steps.length).toBe(1);
      expect(runs.steps[0].searchParams.has("include")).toBe(false);
      // Loop iterations stay under their step.
      const send = steps.locator(".step-group").filter({ hasText: "send" });
      await expect(send.locator(".step-group-name")).toContainText(
        "4 instances",
      );
      await expect(send.locator(".step-label")).toHaveText([
        "Iteration 0",
        "Iteration 1",
        "Iteration 2",
        "Iteration 3",
      ]);
      await expect(
        send.locator(".step-row").last().locator(".status-pill"),
      ).toHaveText("Failed");
      await shot(page, `runs-${size}-detail`);
      // The existing run actions stay: a finished run offers Retry run.
      await expect(
        detail.getByRole("button", { name: "Retry run", exact: true }),
      ).toBeVisible();
      await expect(
        detail.getByRole("button", { name: "Cancel run", exact: true }),
      ).toHaveCount(0);
      // Outputs only on request, one step at a time.
      await detail.getByRole("tab", { name: "Input/Output" }).click();
      const outputs = detail.getByRole("list", { name: "Step outputs" });
      await outputs
        .locator(".step-output")
        .filter({ hasText: "fetch" })
        .getByRole("button", { name: "Show output" })
        .click();
      await expect(
        outputs
          .locator(".step-output")
          .filter({ hasText: "fetch" })
          .locator("pre"),
      ).toContainText('"INV-1041"');
      const output = runs.steps.at(-1)!;
      expect(output.searchParams.get("include")).toBe("output");
      expect(output.searchParams.get("step")).toBe("fetch");
      const secret = outputs
        .locator(".step-output")
        .filter({ hasText: "Iteration 2" });
      await secret.getByRole("button", { name: "Show output" }).click();
      await expect(secret).toContainText(
        "This output is classified as secret.",
      );
      // Logs are not served yet: the tab says so and asks for nothing.
      await detail.getByRole("tab", { name: "Logs" }).click();
      await expect(detail).toContainText(
        "Logs aren't available from this platform yet.",
      );
      expect(runs.logs).toEqual([]);
      // A finished run is not read again.
      const read = runs.steps.length;
      await page.clock.runFor(6_000);
      expect(runs.steps.length).toBe(read);
      // The step that called a workflow opens the run it called.
      await detail.getByRole("tab", { name: "Timeline" }).click();
      await detail
        .getByRole("button", { name: /^Open run 2d1e8a9b called by notify/ })
        .click();
      await expect(page).toHaveURL(
        new RegExp(`/operate/runs/${fixtureIds.child}$`),
      );
      await expect(detail.locator("#record-detail-title")).toHaveText(
        "notify-customer",
      );
    });

    test("an unreadable run, or none at all, says what access is needed", async ({
      page,
    }) => {
      await platform(page, ["status.read"]);
      await page.goto("/operate/runs");
      await expect(page.locator("#main h1")).toHaveText("Runs");
      await expect(page.locator("weave-operate-state")).toContainText(
        "You need run.read in this environment.",
      );
      await expect(
        page.getByRole("button", { name: "Runs", exact: true }),
      ).toHaveCount(0);
      await shot(page, `runs-${size}-access`);
    });
  });

test.describe("chains and refresh", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("test_refresh_discovers_a_run_started_after_the_page_opened", async ({
    page,
  }) => {
    const fixture = data();
    const runs = await platform(page, reader, fixture);
    await openRuns(page);
    await expect(rows(page)).toHaveCount(6);
    await refreshAndPause(page, runs);
    const opened = runs.requestedSummaries.length;
    // A run starts five seconds after the page opened.
    const fresh = uuid(900);
    fixture.runs.push({
      summary: started(fresh, "2026-10-07T12:00:05Z", {
        business_key: "NEW-1",
      }),
    });
    await page.clock.runFor(7_000);
    expect(runs.requestedSummaries.length).toBe(opened);
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect.poll(() => runs.requestedSummaries.length).toBe(opened + 1);
    const second = last(runs);
    expect(Date.parse(second.get("started_before")!)).toBeGreaterThan(
      Date.parse("2026-10-07T12:00:05Z"),
    );
    expect(second.has("cursor")).toBe(false);
    await drawn(page, () =>
      expect(rows(page).first()).toContainText(fresh.slice(0, 8), {
        timeout: 100,
      }),
    );
    // The automatic refresh, ten seconds later, finds the next one too.
    const later = uuid(901);
    fixture.runs.push({ summary: started(later, "2026-10-07T12:00:12Z") });
    await page.clock.runFor(10_500);
    await expect.poll(() => runs.requestedSummaries.length).toBe(opened + 2);
    expect(Date.parse(last(runs).get("started_before")!)).toBeGreaterThan(
      Date.parse("2026-10-07T12:00:12Z"),
    );
    await drawn(page, () =>
      expect(rows(page).first()).toContainText(later.slice(0, 8), {
        timeout: 100,
      }),
    );
  });

  test("a person who becomes known after the list loaded keeps its rows, the open run and focus", async ({
    page,
  }) => {
    const runs = await platform(page, reader, data(), {
      identityDelay: 3_000,
    });
    await openRuns(page);
    await expect(rows(page)).toHaveCount(6);
    expect(runs.requestedSummaries).toHaveLength(1);
    // A list drawn again from scratch would lose this mark.
    await rows(page).evaluateAll((nodes) =>
      nodes.forEach((node) => node.setAttribute("data-kept", "")),
    );
    const opener = names(page).nth(3);
    await opener.click();
    const title = page.locator(".record-detail #record-detail-title");
    await expect(title).toHaveText("invoice-approval");
    await expect(opener).toBeFocused();
    const button = await opener.elementHandle();
    runs.fixture.runs.push({
      summary: started(uuid(902), "2026-10-07T11:59:30Z", {
        business_key: "AFTER",
      }),
    });
    // Studio learns who is signed in: the first page is read again as them, in place.
    await expect
      .poll(() => runs.requestedSummaries.length, { timeout: 10_000 })
      .toBe(2);
    await expect(rows(page).first()).toContainText("AFTER");
    await expect(page.locator(".run-row[data-kept]")).toHaveCount(6);
    expect(
      await button!.evaluate((node) => node === document.activeElement),
    ).toBe(true);
    await expect(title).toHaveText("invoice-approval");
    await expect(
      page.locator(".record-detail").getByRole("list", {
        name: "Step timeline",
      }),
    ).toBeVisible();
  });

  test("Load more continues one frozen chain; Refresh starts a new one", async ({
    page,
  }) => {
    const fixture = data();
    for (let i = 0; i < 70; i++)
      fixture.runs.push({
        summary: started(
          uuid(i),
          new Date(
            Date.parse("2026-10-07T02:00:00Z") + i * 60_000,
          ).toISOString(),
        ),
      });
    const runs = await platform(page, reader, fixture);
    await openRuns(page);
    await expect(rows(page)).toHaveCount(50);
    await runs.pause();
    // The list refreshes by itself as time passes; a refresh between pages
    // would move the window.
    await page.clock.runFor(30_000);
    await expect.poll(() => runs.requestedSummaries.length).toBeGreaterThan(1);
    // Load more continues the list it was shown with: the last first page
    // read before it, whose window it keeps.
    await page.getByRole("button", { name: "Load more runs" }).click();
    await expect
      .poll(() => runs.requestedSummaries.some((query) => query.has("cursor")))
      .toBe(true);
    const at = runs.requestedSummaries.findIndex((query) =>
      query.has("cursor"),
    );
    const next = runs.requestedSummaries[at];
    const chain = runs.requestedSummaries
      .slice(0, at)
      .filter((query) => !query.has("cursor"))
      .at(-1)!;
    expect(next.get("started_before")).toBe(chain.get("started_before"));
    expect(next.get("started_after")).toBe(chain.get("started_after"));
    await drawn(page, () =>
      expect(rows(page)).toHaveCount(76, { timeout: 100 }),
    );
    const ids = await rows(page)
      .locator(".cell-run code")
      .evaluateAll((nodes) => nodes.map((node) => node.getAttribute("title")));
    expect(new Set(ids).size).toBe(ids.length);
    await expect(
      page.getByRole("button", { name: "Load more runs" }),
    ).toHaveCount(0);
    // While more than the first page is open the list does not refresh by itself.
    const settled = runs.requestedSummaries.length;
    await page.clock.runFor(25_000);
    expect(runs.requestedSummaries.length).toBe(settled);
    // Refresh: a new first page from now, no cursor, and the chain starts again.
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect.poll(() => runs.requestedSummaries.length).toBe(settled + 1);
    expect(last(runs).has("cursor")).toBe(false);
    expect(Date.parse(last(runs).get("started_before")!)).toBeGreaterThan(
      Date.parse(next.get("started_before")!),
    );
    await drawn(page, async () => {
      await expect(rows(page)).toHaveCount(50, { timeout: 100 });
      await expect(
        page.getByRole("button", { name: "Load more runs" }),
      ).toBeVisible({ timeout: 100 });
    });
  });

  test("a Load more answered after a refresh never appends to the new list", async ({
    page,
  }) => {
    const fixture = data();
    for (let i = 0; i < 70; i++)
      fixture.runs.push({
        summary: started(
          uuid(i),
          new Date(
            Date.parse("2026-10-07T02:00:00Z") + i * 60_000,
          ).toISOString(),
        ),
      });
    const runs = await platform(page, reader, fixture);
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route("**/run-summaries?**", async (route) => {
      const url = route.request().url();
      runs.requestedSummaries.push(new URL(url).searchParams);
      if (new URL(url).searchParams.has("cursor")) await held;
      const answer = answerRunViews(url, fixture)!;
      await route
        .fulfill({ status: answer.status, json: answer.body })
        .catch(() => undefined);
    });
    await openRuns(page);
    await expect(rows(page)).toHaveCount(50);
    await page.getByRole("button", { name: "Load more runs" }).click();
    await expect
      .poll(() => runs.requestedSummaries.some((query) => query.has("cursor")))
      .toBe(true);
    // A refresh replaces the list while the next page is still on its way.
    fixture.runs.push({
      summary: started(uuid(990), "2026-10-07T11:59:00Z", {
        business_key: "LATEST",
      }),
    });
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(rows(page).first()).toContainText("LATEST");
    release();
    await page.waitForTimeout(500);
    await expect(rows(page)).toHaveCount(50);
    await expect(rows(page).first()).toContainText("LATEST");
  });

  test("a failed refresh drops the old cursor instead of reviving it", async ({
    page,
  }) => {
    const fixture = data();
    for (let i = 0; i < 60; i++)
      fixture.runs.push({
        summary: started(
          uuid(i),
          new Date(
            Date.parse("2026-10-07T02:00:00Z") + i * 60_000,
          ).toISOString(),
        ),
      });
    const runs = await platform(page, reader, fixture);
    await openRuns(page);
    await expect(
      page.getByRole("button", { name: "Load more runs" }),
    ).toBeVisible();
    runs.failSummaries = true;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(
      page
        .locator("weave-operate-state")
        .filter({ hasText: "Studio could not refresh the runs" }),
    ).toBeVisible();
    // The rows stay to read; the chain they came from cannot continue.
    await expect(rows(page)).toHaveCount(50);
    await expect(
      page.getByRole("button", { name: "Load more runs" }),
    ).toHaveCount(0);
    runs.failSummaries = false;
    await page
      .locator("weave-operate-state")
      .filter({ hasText: "Studio could not refresh the runs" })
      .getByRole("button", { name: "Try again" })
      .click();
    await expect(
      page.getByRole("button", { name: "Load more runs" }),
    ).toBeVisible();
    expect(last(runs).has("cursor")).toBe(false);
  });

  test("Select every run takes at most 50 for one bulk action", async ({
    page,
  }) => {
    const fixture = data();
    for (let i = 0; i < 60; i++)
      fixture.runs.push({
        summary: started(
          uuid(i),
          new Date(
            Date.parse("2026-10-07T02:00:00Z") + i * 60_000,
          ).toISOString(),
          { status: "running", ended_at: null, duration_ms: null },
        ),
      });
    await platform(page, operator, fixture);
    await openRuns(page);
    await expect(rows(page)).toHaveCount(50);
    await page.getByRole("button", { name: "Load more runs" }).click();
    await expect(rows(page)).toHaveCount(66);
    await page
      .getByRole("checkbox", {
        name: "Select every run on this page, up to 50",
      })
      .check();
    await expect(
      page.getByRole("region", { name: "Selected runs" }),
    ).toContainText("50 selected");
    // No more can join a full selection.
    await expect(rows(page).nth(60).getByRole("checkbox")).toBeDisabled();
  });

  test("a custom range keeps its bounds however much time passes", async ({
    page,
  }) => {
    const runs = await platform(page, reader);
    await page.goto(
      "/operate/runs?started_after=2026-10-06T00%3A00%3A00Z&started_before=2026-10-07T10%3A30%3A00Z",
    );
    await expect(page.getByLabel("Time range")).toHaveValue("custom");
    await expect(rows(page)).toHaveCount(4);
    await runs.pause();
    await page.clock.runFor(10_500);
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect
      .poll(() => runs.requestedSummaries.length)
      .toBeGreaterThanOrEqual(3);
    for (const query of runs.requestedSummaries) {
      expect(query.get("started_after")).toBe("2026-10-06T00:00:00Z");
      expect(query.get("started_before")).toBe("2026-10-07T10:30:00Z");
    }
  });

  test("the list reads every 10 s with Auto-refresh, pauses when hidden or off, and backs off to 60 s", async ({
    page,
  }) => {
    const runs = await platform(page, reader);
    await openRuns(page);
    await expect(rows(page)).toHaveCount(6);
    await expect.poll(() => runs.requestedSummaries.length).toBe(1);
    await refreshAndPause(page, runs);
    expect(runs.requestedSummaries.length).toBe(2);
    await page.clock.runFor(8_000);
    expect(runs.requestedSummaries.length).toBe(2);
    await page.clock.runFor(2_500);
    await expect.poll(() => runs.requestedSummaries.length).toBe(3);
    // Auto-refresh off: nothing by itself; on again: at once.
    await page.getByRole("switch", { name: "Auto-refresh" }).uncheck();
    await page.clock.runFor(40_000);
    expect(runs.requestedSummaries.length).toBe(3);
    await page.getByRole("switch", { name: "Auto-refresh" }).check();
    await expect.poll(() => runs.requestedSummaries.length).toBe(4);
    // Hidden: nothing; shown: it catches up.
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => "hidden",
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await page.clock.runFor(60_000);
    expect(runs.requestedSummaries.length).toBe(4);
    // A run started meanwhile shows once the catch-up has landed.
    runs.fixture.runs.push({
      summary: started(uuid(899), "2026-10-07T11:59:30Z"),
    });
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => "visible",
      });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await expect.poll(() => runs.requestedSummaries.length).toBe(5);
    await drawn(page, () =>
      expect(rows(page)).toHaveCount(8, { timeout: 100 }),
    );
    // Errors: 10, then 20, 40 and at most 60 s; the rows stay.
    runs.failSummaries = true;
    for (const [wait, expected] of [
      [10_000, 6],
      [20_000, 7],
      [40_000, 8],
      [60_000, 9],
    ] as const) {
      await page.clock.runFor(wait - 1_500);
      expect(runs.requestedSummaries.length).toBe(expected - 1);
      await page.clock.runFor(1_600);
      await expect.poll(() => runs.requestedSummaries.length).toBe(expected);
      await drawn(page, () =>
        expect(
          page
            .locator("weave-operate-state")
            .filter({ hasText: "Studio could not refresh the runs" }),
        ).toBeVisible({ timeout: 100 }),
      );
    }
    await expect(rows(page)).toHaveCount(8);
  });

  test("an active run's detail reads every 2 s until it finishes", async ({
    page,
  }) => {
    const runs = await platform(page);
    await openRuns(page);
    await names(page).first().click();
    await expect(page).toHaveURL(
      new RegExp(`/operate/runs/${fixtureIds.running}$`),
    );
    const detail = page.locator(".record-detail");
    await expect(
      detail.getByRole("list", { name: "Step timeline" }),
    ).toBeVisible();
    await expect(
      detail.getByRole("button", { name: "Cancel run", exact: true }),
    ).toBeVisible();
    // An agent's turns stay under their step, and the running ones are open bars.
    await expect(detail.locator(".step-group-name")).toContainText(
      "3 instances",
    );
    await expect(detail.locator(".step-run.open")).toHaveCount(2);
    // Another turn starts; the read that finds it stops the clock as it is
    // answered, and once it is drawn only the test moves time.
    const turns = runs.fixture.steps[fixtureIds.running].items;
    turns.push({ ...turns[2], instance_key: "support#3" });
    await runs.pauseAtNext("steps");
    await drawn(page, () =>
      expect(detail.locator(".step-group-name")).toContainText("4 instances", {
        timeout: 100,
      }),
    );
    await readsSettled(page, runs);
    const before = runs.steps.length;
    await page.clock.runFor(2_100);
    await expect.poll(() => runs.steps.length).toBe(before + 1);
    await readsSettled(page, runs);
    await page.clock.runFor(2_100);
    await expect.poll(() => runs.steps.length).toBe(before + 2);
    await readsSettled(page, runs);
    // It finishes: one last read, then nothing more.
    runs.views.set(fixtureIds.running, "succeeded");
    await page.clock.runFor(2_100);
    await drawn(page, () =>
      expect(detail.locator(".run-meta .status-pill")).toHaveText("Succeeded", {
        timeout: 100,
      }),
    );
    const settled = runs.steps.length;
    await page.clock.runFor(10_000);
    expect(runs.steps.length).toBe(settled);
  });

  test("each 2 s read of an active run asks only for what changed", async ({
    page,
  }) => {
    const runs = await platform(page);
    const versionId =
      runViewsFixture.runs[0].summary.workflow.definition_version_id;
    let status = "running";
    // The open run names its version: its drawing is exported once.
    await page.route(
      `${environment}/runs/${fixtureIds.running}`,
      async (route) => {
        runs.details.push("view");
        await route.fulfill({
          json: {
            id: fixtureIds.running,
            business_key: null,
            correlation_key: null,
            artifact_digest: "sha256:" + "0".repeat(64),
            activation: {
              id: "act-1",
              name: "invoice-approval",
              revision: 1,
              request: { version_id: versionId },
            },
            state: { status, active: [], input: {} },
          },
        });
        runs.viewsAnswered++;
      },
    );
    const exports: string[] = [];
    await page.route(`**/workflows/${versionId}/export`, (route) => {
      exports.push(route.request().url());
      return route.fulfill({
        json: {
          document: {
            apiVersion: "weave/v1alpha1",
            kind: "Workflow",
            metadata: { name: "invoice-approval", version: "1.3.0" },
            spec: {
              steps: [],
              output: { literal: {} },
            },
          },
        },
      });
    });
    await openRuns(page);
    await names(page).first().click();
    const detail = page.locator(".record-detail");
    await expect(
      detail.getByRole("list", { name: "Step timeline" }),
    ).toBeVisible();
    await expect.poll(() => exports.length).toBe(1);
    await expect
      .poll(() => runs.details.join(" "))
      .toBe("view lifecycle history");
    await runs.pause();
    await readsSettled(page, runs);
    // Nothing changed: the run and its steps only.
    let from = runs.details.length;
    await page.clock.runFor(2_100);
    await expect.poll(() => runs.details.slice(from)).toEqual(["view"]);
    await readsSettled(page, runs);
    // The run changed: it shows at once, with its lifecycle and history again;
    // its version is not exported again.
    status = "waiting";
    from = runs.details.length;
    await page.clock.runFor(2_100);
    await expect
      .poll(() => [...runs.details.slice(from)].sort())
      .toEqual(["history", "lifecycle", "view"]);
    await drawn(page, () =>
      expect(detail.locator(".run-meta .status-pill")).toHaveText("Waiting", {
        timeout: 100,
      }),
    );
    await readsSettled(page, runs);
    from = runs.details.length;
    await page.clock.runFor(2_100);
    await expect.poll(() => runs.details.slice(from)).toEqual(["view"]);
    expect(exports).toHaveLength(1);
  });

  test("steps added with Load more steps stay through the 2 s reads of an active run", async ({
    page,
  }) => {
    const fixture = data();
    const turns = fixture.steps[fixtureIds.running].items;
    for (let i = 3; i < 260; i++) {
      const at = new Date(
        Date.parse("2026-10-07T11:31:00Z") + i * 1000,
      ).toISOString();
      turns.push({
        ...turns[1],
        instance_key: `support#${i}`,
        scheduled_at: at,
        started_at: at,
        ended_at: at,
      });
    }
    const runs = await platform(page, operator, fixture);
    await openRuns(page);
    await names(page).first().click();
    await expect(page).toHaveURL(
      new RegExp(`/operate/runs/${fixtureIds.running}$`),
    );
    const detail = page.locator(".record-detail");
    const stepRows = detail.locator(".step-row");
    await expect(stepRows).toHaveCount(200);
    await detail.getByRole("button", { name: "Load more steps" }).click();
    await expect(stepRows).toHaveCount(260);
    await expect(
      detail.getByRole("button", { name: "Load more steps" }),
    ).toHaveCount(0);
    // The next 2 s read takes both pages again; the rows stay.
    await runs.pause();
    const before = runs.steps.length;
    await page.clock.runFor(2_100);
    await expect.poll(() => runs.steps.length).toBe(before + 2);
    expect(runs.steps.at(-2)!.searchParams.has("cursor")).toBe(false);
    expect(runs.steps.at(-1)!.searchParams.has("cursor")).toBe(true);
    await drawn(page, () =>
      expect(stepRows).toHaveCount(260, { timeout: 100 }),
    );
    await expect(
      detail.getByRole("button", { name: "Load more steps" }),
    ).toHaveCount(0);
  });

  test("bulk Cancel and Retry take the selected runs one by one and report each", async ({
    page,
  }) => {
    const runs = await platform(page);
    await openRuns(page);
    await expect(rows(page)).toHaveCount(6);
    const select = (index: number) =>
      rows(page).nth(index).getByRole("checkbox");
    await select(0).check();
    await select(3).check();
    const bar = page.getByRole("region", { name: "Selected runs" });
    await expect(bar).toContainText("2 selected");
    await bar.getByRole("button", { name: "Cancel runs" }).click();
    const dialog = page.getByRole("dialog", { name: "Cancel 2 runs?" });
    // The reason is required: nothing is sent without one.
    const confirm = dialog.getByRole("button", { name: "Cancel runs" });
    await expect(confirm).toBeDisabled();
    await dialog.getByLabel("Reason").fill("   ");
    await expect(confirm).toBeDisabled();
    await dialog.getByLabel("Reason").fill("Duplicate invoices");
    await expect(confirm).toBeEnabled();
    await dialog.getByRole("button", { name: "Cancel runs" }).click();
    const results = page.getByRole("region", { name: "Cancel runs" });
    await expect(results.locator(".bulk-result")).toHaveCount(2);
    await expect(results.locator(".bulk-result").nth(0)).toContainText(
      "Canceled.",
    );
    await expect(results.locator(".bulk-result").nth(1)).toContainText(
      "Not canceled: it has already finished.",
    );
    expect(runs.posts).toEqual([
      {
        path: expect.stringContaining(`/runs/${fixtureIds.running}/cancel`),
        body: { reason: "Duplicate invoices" },
      },
    ]);
    await shot(page, "runs-1440x900-bulk");
    await select(1).check();
    await select(3).check();
    await page
      .getByRole("region", { name: "Selected runs" })
      .getByRole("button", { name: "Retry runs" })
      .click();
    await page
      .getByRole("dialog", { name: "Retry 2 runs?" })
      .getByRole("button", { name: "Retry runs" })
      .click();
    const retried = page.getByRole("region", { name: "Retry runs" });
    await expect(
      retried.locator(".bulk-result").filter({ hasText: "Retried as run" }),
    ).toHaveCount(2);
    const retries = runs.posts.filter((post) => post.path.endsWith("/retry"));
    expect(retries).toHaveLength(2);
    expect(retries[0].body).toMatchObject({
      activation_id: expect.any(String),
      input: { invoice: expect.any(String) },
    });
  });

  test("Back and Forward follow the filters and the run in the address", async ({
    page,
  }) => {
    const runs = await platform(page, reader);
    await page.goto("/operate/runs?status=failed");
    await expect(statusChoice(page, "Failed")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(rows(page)).toHaveCount(1);
    expect(runs.requestedSummaries[0].getAll("status")).toEqual(["failed"]);
    await names(page).first().click();
    await expect(page).toHaveURL(
      new RegExp(`/operate/runs/${fixtureIds.failed}$`),
    );
    // Opening a run keeps the list as it was filtered.
    await expect(rows(page)).toHaveCount(1);
    expect(last(runs).getAll("status")).toEqual(["failed"]);
    await page.goBack();
    await expect(page).toHaveURL(/\/operate\/runs\?status=failed$/);
    await expect(page.locator(".record-detail")).toHaveCount(0);
    await page.goForward();
    await expect(
      page.locator(".record-detail #record-detail-title"),
    ).toHaveText("invoice-approval");
    await page.goBack();
    await page.goto("/operate/runs");
    await expect(statusChoice(page, "Failed")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    await page.goBack();
    await expect(page).toHaveURL(/\/operate\/runs\?status=failed$/);
    await expect(statusChoice(page, "Failed")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect.poll(() => last(runs).getAll("status")).toEqual(["failed"]);
  });

  test("switching workspace while a slow list returns leaves no rows, keys, selection or cursor behind", async ({
    page,
  }) => {
    await page.clock.install({ time: new Date(NOW) });
    const fixture = data();
    for (let i = 0; i < 60; i++)
      fixture.runs.push({
        summary: started(
          uuid(i),
          new Date(
            Date.parse("2026-10-07T02:00:00Z") + i * 60_000,
          ).toISOString(),
        ),
      });
    await platformHost(page, {
      platforms: [acmePlatform()],
      active: "Acme",
      test: (host) => ({
        json: {
          session: host.session(),
          identity: {
            ...host.identity(),
            grants: host
              .identity()
              .grants.map((grant) => ({ ...grant, capabilities: operator })),
          },
          workspaces: host.current?.workspaces ?? [],
          truncated: false,
          workspace_revoked: false,
        },
      }),
    });
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => (release = resolve));
    const requests: URL[] = [];
    await page.route("**/run-summaries?**", async (route) => {
      const url = new URL(route.request().url());
      requests.push(url);
      const production = url.pathname.includes(acme[0].environment_id);
      if (production && url.searchParams.get("business_key") === "secret-key")
        await held;
      const answer = production
        ? answerRunViews(route.request().url(), fixture)!
        : {
            status: 200,
            body: {
              items: [
                started(uuid(500), "2026-10-07T11:59:00Z", {
                  business_key: "STAGING-1",
                }),
              ],
              next_cursor: null,
            },
          };
      await route
        .fulfill({ status: answer.status, json: answer.body })
        .catch(() => undefined);
    });
    await page.getByRole("button", { name: "Runs", exact: true }).click();
    await expect(rows(page)).toHaveCount(50);
    await rows(page).nth(1).getByRole("checkbox").check();
    await expect(
      page.getByRole("region", { name: "Selected runs" }),
    ).toContainText("1 selected");
    await page.getByLabel("Business key").fill("secret-key");
    await expect
      .poll(() => requests.at(-1)?.searchParams.get("business_key"))
      .toBe("secret-key");
    // Switch workspace while that read is still out.
    await page.locator(".platform-indicator").click();
    await page.getByRole("button", { name: "Switch workspace" }).click();
    await page
      .getByRole("radio", { name: "Acme / Payments / Staging" })
      .check();
    await page.getByRole("button", { name: "Start working" }).click();
    await page.getByRole("button", { name: "Runs", exact: true }).click();
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first()).toContainText("STAGING-1");
    await expect(page.getByLabel("Business key")).toHaveValue("");
    await expect(
      page.getByRole("region", { name: "Selected runs" }),
    ).toHaveCount(0);
    const staging = requests.filter((url) =>
      url.pathname.includes(acme[1].environment_id),
    );
    expect(staging.length).toBeGreaterThan(0);
    for (const url of staging) {
      expect(url.searchParams.has("cursor")).toBe(false);
      expect(url.searchParams.has("business_key")).toBe(false);
    }
    release();
    await page.waitForTimeout(500);
    await expect(rows(page)).toHaveCount(1);
    await expect(page.locator("weave-runs-page")).not.toContainText(
      "secret-key",
    );
  });
});

test.describe("commands on the page", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  const versionId = "44444444-4444-4444-8444-444444444444";
  /** Start run's dialog: one activation whose version takes no input. */
  async function startable(page: Page) {
    await page.route(`${environment}/activations?*`, (route) =>
      route.fulfill({
        json: {
          items: [
            {
              id: "act-1",
              name: "invoice-approval",
              revision: 1,
              request: { version_id: versionId },
            },
          ],
          next_cursor: null,
        },
      }),
    );
    await page.route(`**/projects/project/workflows/${versionId}/export`, (r) =>
      r.fulfill({
        json: {
          document: {
            apiVersion: "weave/v1alpha1",
            kind: "Workflow",
            metadata: { name: "invoice-approval", version: "1.3.0" },
            spec: {
              inputSchema: { type: "object", properties: {} },
              outputSchema: { type: "object" },
              steps: [],
              output: { literal: {} },
            },
          },
        },
      }),
    );
  }
  /** Starts a run from the page; the clock stops as it is sent, so only the command moves the list. */
  async function startOne(page: Page, runs: Platform) {
    await page.getByRole("button", { name: "Start run", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Start a run" });
    await selectChoice(
      dialog.getByLabel("Version to run", { exact: true }),
      "act-1",
    );
    await runs.pause();
    await dialog.getByRole("button", { name: "Start run" }).click();
  }

  test("canceling the open run reads the list again at once", async ({
    page,
  }) => {
    const runs = await platform(page);
    await page.route(`${environment}/runs/*/cancel`, async (route) => {
      const id = new URL(route.request().url()).pathname.split("/").at(-2)!;
      const run = runs.fixture.runs.find((item) => item.summary.id === id)!;
      run.summary = {
        ...run.summary,
        status: "cancelled",
        ended_at: run.summary.started_at,
        duration_ms: 1000,
      };
      runs.views.set(id, "cancelled");
      await route.fulfill({ json: { id, state: { status: "cancelled" } } });
    });
    await openRuns(page);
    await expect(rows(page)).toHaveCount(6);
    await names(page).first().click();
    const detail = page.locator(".record-detail");
    await expect(
      detail.getByRole("button", { name: "Cancel run", exact: true }),
    ).toBeVisible();
    await detail
      .getByRole("button", { name: "Cancel run", exact: true })
      .click();
    const dialog = page.getByRole("dialog", { name: "Cancel this run?" });
    await dialog.getByLabel("Reason").fill("Duplicate invoice");
    // Only the command moves the list: no clock passes from here on.
    await runs.pause();
    const before = runs.requestedSummaries.length;
    await dialog.getByRole("button", { name: "Cancel run" }).click();
    await expect.poll(() => runs.requestedSummaries.length).toBe(before + 1);
    expect(last(runs).has("cursor")).toBe(false);
    await drawn(page, () =>
      expect(rows(page).first().locator(".cell-status")).toContainText(
        "Canceled",
        { timeout: 100 },
      ),
    );
  });

  test("archiving the open run takes it off the list at once", async ({
    page,
  }) => {
    const runs = await platform(page);
    await page.route(`${environment}/runs/*/archive`, async (route) => {
      const id = new URL(route.request().url()).pathname.split("/").at(-2)!;
      const run = runs.fixture.runs.find((item) => item.summary.id === id)!;
      run.summary = { ...run.summary, archived: true };
      await route.fulfill({ json: { archived: true, revision: 2 } });
    });
    await openRuns(page);
    await expect(rows(page)).toHaveCount(6);
    const failed = rows(page).filter({
      has: page.locator(`.cell-run code[title="${fixtureIds.failed}"]`),
    });
    await expect(failed).toHaveCount(1);
    await failed.locator(".row-open").click();
    const detail = page.locator(".record-detail");
    await expect(
      detail.getByRole("button", { name: "Archive run", exact: true }),
    ).toBeEnabled();
    await detail
      .getByRole("button", { name: "Archive run", exact: true })
      .click();
    const dialog = page.getByRole("dialog", { name: "Archive this run?" });
    await dialog.getByLabel("Reason").fill("Handled by hand");
    await runs.pause();
    const before = runs.requestedSummaries.length;
    await dialog.getByRole("button", { name: "Archive run" }).click();
    await expect.poll(() => runs.requestedSummaries.length).toBe(before + 1);
    await drawn(page, () =>
      expect(rows(page)).toHaveCount(5, { timeout: 100 }),
    );
    await expect(failed).toHaveCount(0);
  });

  test("with a run open beside the list, an unavailable row keeps its ID and explanation", async ({
    page,
  }) => {
    await platform(page);
    await openRuns(page);
    await names(page).first().click();
    await expect(
      page.locator(".record-detail #record-detail-title"),
    ).toHaveText("invoice-approval");
    const unavailable = rows(page).filter({ hasText: "Unavailable run" });
    await expect(unavailable.locator(".cell-meta")).toBeVisible();
    await expect(unavailable.locator(".cell-meta")).toContainText(
      "Studio can't show this run's details.",
    );
    await expect(unavailable.locator(".cell-meta code")).toHaveText(
      fixtureIds.unavailable.slice(0, 8),
    );
  });

  test("a run started here shows first, also before the list includes it", async ({
    page,
  }) => {
    const runs = await platform(page, [...operator, "run.start"]);
    await startable(page);
    const fresh = uuid(950);
    let listNow = true;
    await page.route(`${environment}/runs`, async (route) => {
      if (listNow)
        runs.fixture.runs.push({
          summary: started(fresh, "2026-10-07T11:59:50Z", {
            status: "queued",
            ended_at: null,
            duration_ms: null,
            business_key: "STARTED-1",
          }),
        });
      await route.fulfill({
        status: 201,
        json: { id: listNow ? fresh : uuid(951), state: { status: "queued" } },
      });
    });
    await openRuns(page);
    await expect(rows(page)).toHaveCount(6);
    // The first page already lists it: it is the first row.
    let before = runs.requestedSummaries.length;
    await startOne(page, runs);
    await expect.poll(() => runs.requestedSummaries.length).toBe(before + 1);
    await drawn(page, () =>
      expect(rows(page).first()).toContainText("STARTED-1", { timeout: 100 }),
    );
    await expect(rows(page)).toHaveCount(7);
    // A first page that doesn't list it yet: it still shows first, as just started.
    listNow = false;
    await page.clock.resume();
    before = runs.requestedSummaries.length;
    await startOne(page, runs);
    await expect.poll(() => runs.requestedSummaries.length).toBe(before + 1);
    const pending = rows(page).first();
    await drawn(page, () =>
      expect(pending).toContainText("Just started.", { timeout: 100 }),
    );
    await expect(pending).toContainText(uuid(951).slice(0, 8));
    await expect(pending.locator(".status-pill")).toHaveText("Queued");
    await expect(rows(page)).toHaveCount(8);
    // Once a first page lists it, it is an ordinary row, shown once.
    runs.fixture.runs.push({
      summary: started(uuid(951), "2026-10-07T11:59:55Z", {
        status: "queued",
        ended_at: null,
        duration_ms: null,
        business_key: "STARTED-2",
      }),
    });
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await drawn(page, () =>
      expect(rows(page).first()).toContainText("STARTED-2", { timeout: 100 }),
    );
    await expect(rows(page)).toHaveCount(8);
    await expect(page.locator(".run-row.started")).toHaveCount(0);
  });
});

test.describe("another person on the same environment", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("rows, keys and late answers read for the person before never show for the next one", async ({
    page,
  }) => {
    await page.clock.install({ time: new Date(NOW) });
    // The platform can't be reached at first, so Studio reads the identity
    // itself (jane); Try again then finds john signed in on the same
    // environment, while the page stays open.
    let person = "jane";
    let reachable = false;
    await platformHost(page, {
      platforms: [acmePlatform()],
      active: "Acme",
      test: (host) =>
        reachable
          ? {
              json: {
                session: host.session(),
                identity: {
                  ...host.identity(),
                  principal_id: `principal-${person}`,
                },
                workspaces: host.current?.workspaces ?? [],
                truncated: false,
                workspace_revoked: false,
              },
            }
          : {
              status: 503,
              json: { code: "WV-STUDIO-UPSTREAM", message: "Unreachable" },
            },
    });
    let releaseJane: () => void = () => undefined;
    const janeHeld = new Promise<void>((resolve) => (releaseJane = resolve));
    let releaseJohn: () => void = () => undefined;
    const johnHeld = new Promise<void>((resolve) => (releaseJohn = resolve));
    const requests: { person: string; query: URLSearchParams }[] = [];
    await page.route("**/run-summaries?**", async (route) => {
      const asked = person;
      const query = new URL(route.request().url()).searchParams;
      requests.push({ person: asked, query });
      if (asked === "jane" && query.get("business_key") === "jane-secret")
        await janeHeld;
      if (asked === "john") await johnHeld;
      await route
        .fulfill({
          json: {
            items: [
              started(
                uuid(asked === "jane" ? 600 : 700),
                "2026-10-07T11:59:00Z",
                {
                  business_key: asked === "jane" ? "JANE-1" : "JOHN-1",
                },
              ),
            ],
            next_cursor: null,
          },
        })
        .catch(() => undefined);
    });
    await page.getByRole("button", { name: "Runs", exact: true }).click();
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first()).toContainText("JANE-1");
    // A read for jane is still out when john turns out to be signed in.
    await page.getByLabel("Business key").fill("jane-secret");
    await expect
      .poll(() => requests.at(-1)?.query.get("business_key"))
      .toBe("jane-secret");
    person = "john";
    reachable = true;
    await page
      .locator(".platform-banner")
      .getByRole("button", { name: "Try again" })
      .click();
    // Nothing read for jane stays, though her read is still out.
    await expect(page.locator(".platform-banner")).toHaveCount(0);
    await expect(rows(page)).toHaveCount(0);
    await expect(page.getByLabel("Business key")).toHaveValue("");
    // Her answer lands after the change: it is dropped, and john's first
    // page is read next.
    releaseJane();
    await expect
      .poll(
        () => requests.filter((request) => request.person === "john").length,
      )
      .toBeGreaterThan(0);
    await expect(rows(page)).toHaveCount(0);
    await expect(page.locator("weave-runs-page")).not.toContainText("JANE-1");
    releaseJohn();
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first()).toContainText("JOHN-1");
    await expect(page.locator("weave-runs-page")).not.toContainText("JANE-1");
    for (const request of requests.filter((item) => item.person === "john")) {
      expect(request.query.has("business_key")).toBe(false);
      expect(request.query.has("cursor")).toBe(false);
    }
  });
});

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
        height: r.height,
        width: r.width,
      };
    }),
  );
}
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
      "weave-runs-page *",
    )) {
      const style = getComputedStyle(node);
      if (
        style.display === "none" ||
        !node.getClientRects().length ||
        node.closest("svg, pre")
      )
        continue;
      if (["SELECT", "INPUT", "TEXTAREA"].includes(node.tagName)) continue;
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

test.describe("layout", () => {
  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 600, height: 500 },
    { width: 360, height: 640 },
  ])
    test(`one left edge, centered rows and touch targets at ${viewport.width}x${viewport.height}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      await platform(page);
      await openRuns(page);
      await expect(rows(page)).toHaveCount(6);
      // The filters and the table share the page's left and right edges.
      const [filters] = await boxes(page.locator(".run-filters"));
      const [list] = await boxes(table(page));
      expect(Math.abs(filters.left - list.left)).toBeLessThanOrEqual(1);
      expect(Math.abs(filters.right - list.right)).toBeLessThanOrEqual(1);
      // Every row spans the table; its cells share one center line on a wide screen.
      const row = await boxes(rows(page));
      for (const box of row) {
        expect(Math.abs(box.left - list.left)).toBeLessThanOrEqual(2);
        expect(Math.abs(box.right - list.right)).toBeLessThanOrEqual(2);
      }
      let rowCenters: number[][] = [];
      if (viewport.width >= 1440) {
        const centers = await rows(page).evaluateAll((nodes) =>
          nodes.slice(0, 5).map((node) =>
            [
              ".cell-status .status-pill",
              ".row-open",
              ".cell-origin > *",
              ".cell-started time",
              ".cell-duration",
              ".cell-run code",
              ".copy-id",
            ].map((selector) => {
              const r = node.querySelector(selector)!.getBoundingClientRect();
              return r.top + r.height / 2;
            }),
          ),
        );
        for (const line of centers)
          for (const center of line)
            expect(Math.abs(center - line[0])).toBeLessThanOrEqual(1);
        rowCenters = centers;
        // n8n-like density: 32 px rows, their separator included.
        for (const box of row)
          expect(Math.abs(box.height - 32)).toBeLessThanOrEqual(0.5);
      }
      if (viewport.width <= 360) {
        // The workflow name opens its run from anywhere in the row: the row is its target.
        for (const box of row) expect(box.height).toBeGreaterThanOrEqual(43.5);
        const small = await page
          .locator(
            "weave-runs-page :is(button:not(.row-open), a, select, label.select-box, label.checkbox-field)",
          )
          .evaluateAll((nodes) =>
            nodes
              .filter(
                (node) =>
                  node.getClientRects().length &&
                  getComputedStyle(node).visibility !== "hidden",
              )
              .map((node) => {
                const r = node.getBoundingClientRect();
                return {
                  name:
                    (node.textContent ?? "").trim() ||
                    node.getAttribute("aria-label"),
                  height: r.height,
                };
              })
              .filter((target) => target.height < 43.5),
          );
        expect(small).toEqual([]);
      }
      await expectNoClipping(page);
      // Every visible list control except the row's own name: the smallest
      // height, and those under the 36 px desktop control height.
      const controls = await page
        .locator(
          "weave-runs-page :is(button:not(.row-open), a, select, label.select-box, label.checkbox-field)",
        )
        .evaluateAll((nodes) =>
          nodes
            .filter(
              (node) =>
                node.getClientRects().length &&
                getComputedStyle(node).visibility !== "hidden",
            )
            .map((node) => ({
              name: (
                node.getAttribute("aria-label") ??
                node.textContent ??
                ""
              ).trim(),
              className: node.className,
              height:
                Math.round(node.getBoundingClientRect().height * 100) / 100,
            })),
        );
      await shot(page, `runs-${viewport.width}x${viewport.height}-layout`);
      await names(page).nth(3).click();
      await expect(
        page.locator(".record-detail #record-detail-title"),
      ).toHaveText("invoice-approval");
      await expect(
        page.getByRole("list", { name: "Step timeline" }),
      ).toBeVisible();
      // Every step's bar shares one scale: the same left and right edge.
      const tracks = await boxes(page.locator(".step-track"));
      expect(tracks.length).toBeGreaterThan(3);
      for (const track of tracks) {
        expect(Math.abs(track.left - tracks[0].left)).toBeLessThanOrEqual(1);
        expect(Math.abs(track.right - tracks[0].right)).toBeLessThanOrEqual(1);
      }
      let stepCenters: number[][] = [];
      if (viewport.width >= 1440) {
        const steps = await page.locator(".step-row").evaluateAll((nodes) =>
          nodes.map((node) =>
            [".step-name", ".status-pill", ".step-track", ".step-time"].map(
              (selector) => {
                const r = node.querySelector(selector)!.getBoundingClientRect();
                return r.top + r.height / 2;
              },
            ),
          ),
        );
        for (const line of steps)
          for (const center of line)
            expect(Math.abs(center - line[0])).toBeLessThanOrEqual(1);
        stepCenters = steps;
      }
      await expectNoClipping(page);
      record(`runs-${viewport.width}x${viewport.height}-layout`, {
        filtersToTableLeft: round(Math.abs(filters.left - list.left)),
        filtersToTableRight: round(Math.abs(filters.right - list.right)),
        rowToTableEdge: most(
          row.flatMap((box) => [
            Math.abs(box.left - list.left),
            Math.abs(box.right - list.right),
          ]),
        ),
        rowHeight: [
          round(Math.min(...row.map((box) => box.height))),
          round(Math.max(...row.map((box) => box.height))),
        ],
        rowCenterSpread: most(rowCenters.map((line) => spread(line))),
        smallestListTarget: round(
          Math.min(...controls.map((control) => control.height)),
        ),
        listTargetsUnder36: controls.filter((control) => control.height < 35.5),
        rowHeights: row.map((box) => round(box.height)),
        stepTrackLeftSpread: spread(tracks.map((track) => track.left)),
        stepTrackRightSpread: spread(tracks.map((track) => track.right)),
        stepCenterSpread: most(stepCenters.map((line) => spread(line))),
      });
      await shot(
        page,
        `runs-${viewport.width}x${viewport.height}-layout-detail`,
      );
    });
});
