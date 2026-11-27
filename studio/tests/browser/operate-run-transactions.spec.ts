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
// Operate › Runs › a run's Transactions section: the summary, folded
// completed transactions, each record with what stays applied in doubt,
// the record's three layouts, and the section's own states (no access, a
// failed read with its back-off, answers for another run). The platform is
// mocked with the run's records as runs.transactions answers them.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page, type Route } from "@playwright/test";
import type { RunStatus } from "../../src/app/operate/run-contracts";
import type { RunTransactionView } from "../../src/app/operate/transactions/transaction-contracts";
import {
  completed,
  inDoubt,
  inDoubtIncident,
  loop,
  loopRunId,
  loopSummary,
  resolvingRun,
} from "../run-transactions-fixture";
import { called } from "../transaction-incidents-fixture";
import { connected } from "./support";

const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";
const captures = resolve(
  process.env["STUDIO_OPERATE_CAPTURES"]?.trim() || "test-results/operate",
);
mkdirSync(captures, { recursive: true });
const NOW = "2026-11-22T10:54:00Z";
const otherRun = "f3d9a0c4-1b2c-4d3e-8f4a-5b6c7d8e9f01";
const reader = ["run.read", "catalog.read", "incident.read"];

interface Platform {
  /** Each transactions read, by run ID and cursor. */
  reads: { run: string; cursor: string | null }[];
  /** Answers each read; the default answers the run's records. */
  answer: (run: string, route: Route, cursor: string | null) => Promise<void>;
  /** The run's status, as runs.read reports it. */
  status: string;
}

/** Opens Runs with `runId` open on a platform that answers its transactions. */
async function platform(
  page: Page,
  records: Record<string, RunTransactionView[]> = { [loopRunId]: loop },
): Promise<Platform> {
  await page.clock.install({ time: new Date(NOW) });
  await connected(page, { capabilities: reader });
  const result: Platform = {
    reads: [],
    status: "suspended",
    answer: (run, route) =>
      route.fulfill({
        json: { items: records[run] ?? [], next_cursor: null },
      }),
  };
  await page.route(`${environment}/runs/*`, (route) => {
    const id = new URL(route.request().url()).pathname.split("/").pop()!;
    return route.fulfill({
      json: {
        id,
        business_key: null,
        correlation_key: null,
        activation: { id: "act", name: "move-each", revision: 1 },
        state: { status: result.status, active: [], input: {} },
      },
    });
  });
  await page.route(`${environment}/runs/*/lifecycle`, (route) =>
    route.fulfill({ json: { archived: false, purged: false, revision: 1 } }),
  );
  await page.route(`${environment}/runs/*/history?*`, (route) =>
    route.fulfill({ json: { events: [], next_cursor: null } }),
  );
  // The Runs list beside the detail lists the run it shows.
  await page.route("**/run-summaries?**", (route) =>
    route.fulfill({
      json: {
        items: [
          loopSummary(result.status as RunStatus, records[loopRunId] ?? []),
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route(`${environment}/runs/*/transactions?*`, async (route) => {
    const url = new URL(route.request().url());
    const run = decodeURIComponent(url.pathname.split("/").at(-2)!);
    const cursor = url.searchParams.get("cursor");
    expect(url.searchParams.get("limit")).toBe("50");
    result.reads.push({ run, cursor });
    await result.answer(run, route, cursor);
  });
  return result;
}
const openRun = (page: Page, id = loopRunId) =>
  page.goto(`/operate/runs/${id}`);
const section = (page: Page) =>
  page.getByRole("region", { name: "Transactions" });
const block = (page: Page, key: string) =>
  section(page).getByRole("article", { name: `Transaction ${key}` });
const record = (page: Page, key: string) =>
  block(page, key).getByRole("table", { name: `Steps of transaction ${key}` });
const stepRow = (page: Page, key: string, step: string) =>
  record(page, key)
    .getByRole("row")
    .filter({ has: page.getByRole("cell", { name: step, exact: false }) })
    .filter({ hasText: step });

test.use({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC" });

test("a run's transactions: the summary, a folded one, an undone one and one in doubt", async ({
  page,
}) => {
  await platform(page);
  await openRun(page);
  await expect(
    section(page).getByRole("heading", { name: "Transactions" }),
  ).toBeVisible();
  await expect(section(page).locator(".txn-summary")).toHaveText(
    "5 transactions: 1 in doubt, 1 undone, 1 reconciled, 2 completed",
  );
  // A completed transaction nobody had to resolve is one line until opened.
  const first = block(page, "move[1]");
  await expect(first.locator(".txn-folded")).toHaveText(
    /Business key T-1050\s*3 steps\s*Completed at 10:31:05/,
  );
  await expect(record(page, "move[1]")).toHaveCount(0);
  const chevron = first.getByRole("button", {
    name: "Show transaction move[1]",
  });
  await expect(chevron).toHaveAttribute("aria-expanded", "false");
  await chevron.click();
  await expect(record(page, "move[1]")).toBeVisible();
  await expect(
    first.getByRole("button", { name: "Hide transaction move[1]" }),
  ).toHaveAttribute("aria-expanded", "true");
  // Undone, with why and what each step did.
  const second = block(page, "move[2]");
  await expect(second.locator(".status-pill")).toHaveText("Undone");
  await expect(second.locator(".txn-meta")).toHaveText(
    /Business key T-1051\s*Opened at 10:31:02/,
  );
  await expect(second.locator(".txn-cause")).toHaveText(
    "Undone after add[2] failed: UNAVAILABLE",
  );
  await expect(stepRow(page, "move[2]", "hold[2]")).toContainText("Released");
  await expect(stepRow(page, "move[2]", "submit[2]")).toContainText(
    "Not started",
  );
  // In doubt: the danger border, Open incident, and a reservation still held.
  const third = block(page, "move[3]");
  await expect(third.locator(".status-pill")).toHaveText(
    "In doubt · action needed",
  );
  const border = await third.evaluate((element) => [
    getComputedStyle(element).borderTopColor,
    getComputedStyle(element).getPropertyValue("--danger-bd").trim(),
  ]);
  const danger = await page.evaluate((value) => {
    const probe = document.createElement("span");
    probe.style.color = value;
    document.body.append(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  }, border[1]);
  expect(border[0]).toBe(danger);
  await expect(third.locator(".txn-cause")).toHaveText(
    "In doubt: A step's undo couldn't finish, so the transaction is in doubt.",
  );
  const hold = stepRow(page, "move[3]", "hold[3]");
  await expect(hold.locator(".c-forward")).toContainText("Done");
  await expect(hold.locator(".c-forward weave-icon")).toHaveCount(0);
  await expect(hold.locator(".c-undo")).toContainText("Still reserved");
  const add = stepRow(page, "move[3]", "add[3]");
  await expect(add.locator(".c-undo")).toContainText("Undo failed");
  await expect(add.locator(".c-undo")).toContainText("3 tries");
  await expect(add).toHaveClass(/stuck/);
  await expect(add.locator(".c-key code")).toHaveText(
    `${loopRunId}:add[3]#undo`,
  );
  await expect(
    add.getByRole("button", {
      name: `Copy operation key ${loopRunId}:add[3]#undo`,
    }),
  ).toBeVisible();
  await expect(add.locator(".c-step")).toContainText("Child run 9a01c7d2");
  // The summary's "1 in doubt" moves focus to that transaction.
  await section(page).getByRole("button", { name: "1 in doubt" }).click();
  await expect(
    third.getByRole("heading", { name: /Transaction move\[3\]/ }),
  ).toBeFocused();
  await page.screenshot({
    path: `${captures}/run-transactions-loop.png`,
    fullPage: true,
  });
  // Open incident opens the inbox on that incident, and Back the run again.
  await page.route(`${environment}/incidents/${inDoubtIncident}`, (route) =>
    route.fulfill({ json: { ...called, id: inDoubtIncident } }),
  );
  await third.getByRole("button", { name: "Open incident" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "Incidents" }),
  ).toBeVisible();
  await expect(page).toHaveURL(
    new RegExp(`/operate/incidents\\?incident=${inDoubtIncident}$`),
  );
  await expect(page.locator("#incident-detail-title")).toHaveText(
    "Incident WV-TXN-UNDO-STUCK",
  );
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/operate/runs/${loopRunId}$`));
  await expect(section(page).locator(".txn-summary")).toBeVisible();
});

test("a reconciled transaction says who resolved it, why and with what evidence, as plain text", async ({
  page,
}) => {
  await platform(page);
  await openRun(page);
  const fourth = block(page, "move[4]");
  await expect(fourth.locator(".txn-timeline li")).toHaveText([
    /10:31:02\s*Opened/,
    /10:48:17\s*Reconciled/,
    /10:48:17\s*Resolved: Mark as reconciled as undone/,
  ]);
  const resolution = fourth.locator(".txn-resolution");
  await expect(resolution).toContainText("Mark as reconciled as undone");
  await expect(resolution).toContainText(
    "Checked the target system: the quantity was removed by hand at 10:41.",
  );
  const evidence = resolution.locator("dd", { hasText: "Ticket OPS-2291" });
  await expect(evidence).toHaveText("Ticket OPS-2291 <b>line 88123</b>");
  await expect(evidence.locator("b")).toHaveCount(0);
  await expect(block(page, "move[5]").locator(".txn-timeline li")).toHaveText([
    /Opened/,
    /Decided to finish/,
    /Resolved: Retry the confirm/,
    /Completed/,
  ]);
  // A triggered workflow's resolve step applied it: By names that run, which opens.
  const by = block(page, "move[5]").locator(".txn-resolution dd").nth(1);
  const link = by.getByRole("button", {
    name: "Workflow run 5c90e1b7",
    exact: true,
  });
  await expect(by).toHaveText("Workflow run 5c90e1b7 · 10:40:00");
  await link.click();
  await expect(page).toHaveURL(new RegExp(`/operate/runs/${resolvingRun}$`));
});

test("the record has five columns from 720 px, four below, and step rows under 560 px", async ({
  page,
}) => {
  await platform(page);
  await openRun(page);
  await expect(record(page, "move[3]")).toBeVisible();
  const columns = async (width: number) => {
    await section(page).evaluate(
      (element, value) => ((element as HTMLElement).style.width = `${value}px`),
      width,
    );
    const row = stepRow(page, "move[3]", "add[3]");
    return row.evaluate((element) => {
      const style = getComputedStyle(element);
      return {
        columns: style.gridTemplateColumns.split(" ").length,
        overflow: element.scrollWidth - element.clientWidth,
      };
    });
  };
  expect(await columns(760)).toEqual({ columns: 5, overflow: 0 });
  await expect(
    record(page, "move[3]").getByRole("columnheader", { name: "Forward" }),
  ).toBeVisible();
  expect(await columns(600)).toEqual({ columns: 4, overflow: 0 });
  // What happened stands over Forward and Undo or confirm on screen only: a
  // screen reader hears each of the five cells under its own header.
  await expect(
    record(page, "move[3]").locator(".txn-head .c-happened"),
  ).toBeVisible();
  await expect(record(page, "move[3]")).toMatchAriaSnapshot(`
    - table "Steps of transaction move[3]":
      - row:
        - /children: equal
        - columnheader "Step"
        - columnheader "How it's undone"
        - columnheader "Forward"
        - columnheader "Undo or confirm"
        - columnheader "Operation key"
      - row /^add\\[3\\]/:
        - /children: equal
        - cell /^add\\[3\\]/
        - cell "Reversible"
        - cell "Done"
        - cell "Undo failed 3 tries"
        - cell /add\\[3\\]#undo/
  `);
  await page.screenshot({ path: `${captures}/run-transactions-600.png` });
  const rows = await columns(360);
  expect(rows.columns).toBe(2);
  expect(rows.overflow).toBe(0);
  // Step rows name each part on the row itself.
  await expect(stepRow(page, "move[3]", "add[3]")).toContainText(
    /Forward\s*Done/,
  );
  await expect(stepRow(page, "move[3]", "add[3]")).toContainText(
    /Undo or confirm\s*Undo failed/,
  );
  await page.screenshot({ path: `${captures}/run-transactions-360.png` });
});

test("without run.read the section says so, and the rest of the run stays", async ({
  page,
}) => {
  const runs = await platform(page);
  runs.answer = (_run, route) =>
    route.fulfill({
      status: 403,
      json: { code: "WV-FORBIDDEN", message: "Forbidden" },
    });
  await openRun(page);
  await expect(section(page)).toContainText(
    "You need run.read in this environment. Ask an administrator in Settings › People and access.",
  );
  await expect(
    page
      .getByRole("heading", { name: "Workflow", exact: false })
      .or(page.getByRole("heading", { name: "Events" }))
      .first(),
  ).toBeVisible();
  await page.screenshot({ path: `${captures}/run-transactions-access.png` });
});

test("a failed read says when it tries again, waiting 5 s and then 10 s", async ({
  page,
}) => {
  const installed = Date.now();
  const runs = await platform(page);
  runs.status = "succeeded";
  // A retried read is answered only once the test lets it, at a moment only the test moves.
  let held: (() => void) | null = null;
  const release = () => {
    held?.();
    held = null;
  };
  runs.answer = async (_run, route) => {
    if (runs.reads.length > 1)
      await new Promise<void>((resolve) => (held = resolve));
    await route.fulfill({
      status: 503,
      json: { code: "WV-UNAVAILABLE", message: "Down" },
    });
  };
  await openRun(page);
  const problem = section(page).getByRole("alert");
  await expect(problem).toContainText(
    "Studio couldn't read this run's transactions. It tries again in 5 s.",
  );
  await expect(problem).toContainText("Support code: WV-UNAVAILABLE");
  // A wait and its unit stay on one line.
  expect(await problem.locator("p").first().textContent()).toContain("in 5 s.");
  await page.screenshot({ path: `${captures}/run-transactions-failed.png` });
  // Stop the page's clock a moment after the first failure; only the test moves it now.
  await page.clock.pauseAt(
    new Date(Date.parse(NOW) + (Date.now() - installed) + 300),
  );
  const before = runs.reads.length;
  // The first failure landed under 1.5 s before the pause: 5 s after it is 3.5 s to 5 s from here.
  await page.clock.runFor(3_400);
  expect(runs.reads.length).toBe(before);
  await page.clock.runFor(1_700);
  await expect.poll(() => runs.reads.length).toBe(before + 1);
  release();
  // The paused page draws only when time moves: a frame at a time, well under the wait.
  const frames = (check: () => Promise<void>) =>
    expect(async () => {
      await page.clock.runFor(16);
      await check();
    }).toPass({ timeout: 10_000, intervals: [50] });
  const drawn = (text: string) =>
    frames(() => expect(problem).toContainText(text, { timeout: 100 }));
  await drawn("It tries again in 10 s.");
  await page.clock.runFor(9_500);
  expect(runs.reads.length).toBe(before + 1);
  await page.clock.runFor(600);
  await expect.poll(() => runs.reads.length).toBe(before + 2);
  release();
  await drawn("It tries again in 20 s.");
  // Try now reads at once, and an answer puts the records back.
  runs.answer = (run, route) =>
    route.fulfill({
      json: { items: run === loopRunId ? loop : [], next_cursor: null },
    });
  await section(page).getByRole("button", { name: "Try now" }).click();
  await frames(() =>
    expect(section(page).locator(".txn-summary")).toHaveText(
      /^\s*5 transactions/,
      { timeout: 100 },
    ),
  );
  await expect(problem).toHaveCount(0);
});

test("an answer for a run that is no longer open is dropped", async ({
  page,
}) => {
  const runs = await platform(page, { [loopRunId]: loop, [otherRun]: [] });
  let release!: () => void;
  const held = new Promise<void>((resolve) => (release = resolve));
  runs.answer = async (run, route) => {
    if (run === loopRunId) await held;
    await route.fulfill({
      json: { items: run === loopRunId ? loop : [], next_cursor: null },
    });
  };
  await openRun(page);
  await expect
    .poll(() => runs.reads.map((item) => item.run))
    .toContain(loopRunId);
  await openRun(page, otherRun);
  await expect
    .poll(() => runs.reads.map((item) => item.run))
    .toContain(otherRun);
  release();
  await page.clock.runFor(500);
  // The other run has no transactions: no heading, and nothing of the first run.
  await expect(section(page)).toHaveCount(0);
  await expect(page.getByText("Transaction move[3]")).toHaveCount(0);
});

test("a run with more than 50 transactions shows the first 50 and reads more on request", async ({
  page,
}) => {
  const runs = await platform(page);
  runs.status = "succeeded";
  const many = Array.from({ length: 50 }, (_, index) => ({
    ...(index === 6 ? inDoubt : completed),
    instance_key: `move[${index + 1}]`,
    iteration: [index + 1],
  }));
  runs.answer = (_run, route, cursor) =>
    route.fulfill({
      json: cursor
        ? {
            items: [{ ...inDoubt, instance_key: "move[51]" }],
            next_cursor: null,
          }
        : { items: many, next_cursor: "djI6bW92ZVs1MF0" },
    });
  await openRun(page);
  await expect(section(page).locator(".txn-summary")).toHaveText(
    "The first 50 of this run's transactions: 1 in doubt, 49 completed",
  );
  await expect(section(page).getByRole("article")).toHaveCount(50);
  // Its "1 in doubt" moves focus to that transaction, as the whole summary's does.
  await section(page).getByRole("button", { name: "1 in doubt" }).click();
  await expect(block(page, "move[7]").getByRole("heading")).toBeFocused();
  await section(page)
    .getByRole("button", { name: "Show more transactions" })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: `${captures}/run-transactions-more-than-50.png`,
  });
  await section(page)
    .getByRole("button", { name: "Show more transactions" })
    .click();
  await expect(section(page).getByRole("article")).toHaveCount(51);
  expect(runs.reads.at(-1)).toEqual({
    run: loopRunId,
    cursor: "djI6bW92ZVs1MF0",
  });
  await expect(section(page).locator(".txn-summary")).toHaveText(
    "51 transactions: 2 in doubt, 49 completed",
  );
  await expect(
    section(page).getByRole("button", { name: "Show more transactions" }),
  ).toHaveCount(0);
});

test("a workflow Studio can't draw is named, and no other workflow's graph stands for it", async ({
  page,
}) => {
  const version = "6d7e8f90-a1b2-4c3d-8e4f-5a6b7c8d9e03";
  await platform(page);
  await page.route(`${environment}/runs/${loopRunId}`, (route) =>
    route.fulfill({
      json: {
        id: loopRunId,
        activation: {
          id: "act",
          name: "move-each",
          revision: 1,
          request: { version_id: version },
        },
        state: { status: "suspended", active: [], input: {} },
      },
    }),
  );
  // A step kind of a newer platform.
  await page.route(`**/projects/project/workflows/${version}/export`, (route) =>
    route.fulfill({
      json: {
        id: version,
        document: {
          apiVersion: "weave/v1alpha1",
          kind: "Workflow",
          metadata: { name: "move-each", version: "1.0.0" },
          spec: {
            inputSchema: {},
            outputSchema: {},
            steps: [{ id: "later", kind: "fromALaterPlatform" }],
            output: { literal: {} },
          },
        },
      },
    }),
  );
  await openRun(page);
  const graph = page.getByRole("region", { name: "Workflow move-each 1.0.0" });
  await expect(graph).toContainText("Studio can't draw this workflow.");
  await expect(page.locator(".run-graph")).toHaveCount(0);
  await expect(page.getByText("untitled-workflow")).toHaveCount(0);
  // The rest of the run is there.
  await expect(section(page).locator(".txn-summary")).toBeVisible();
});

test("a run without transactions shows no Transactions section", async ({
  page,
}) => {
  await platform(page, { [loopRunId]: [] });
  await openRun(page);
  await expect(page.getByRole("heading", { name: "Events" })).toBeVisible();
  await page.clock.runFor(500);
  await expect(section(page)).toHaveCount(0);
});

test("a paused rollback counts its undone steps, and a cancel waits for the step that can't be undone", async ({
  page,
}) => {
  const undoing: RunTransactionView = {
    ...inDoubt,
    state: "compensating",
    incident_id: undefined,
    participants: [
      { ...inDoubt.participants[0], status: "cancelling" },
      { ...inDoubt.participants[1], status: "undone" },
      { ...inDoubt.participants[2], status: "failed" },
    ],
  };
  const deciding: RunTransactionView = {
    ...completed,
    instance_key: "move[6]",
    state: "deciding",
    ended_at: undefined,
    participants: [
      { ...completed.participants[0], status: "done" },
      { ...completed.participants[1], status: "done" },
      { ...completed.participants[2], status: "dispatched" },
    ],
  };
  await platform(page, { [loopRunId]: [undoing, deciding] });
  await openRun(page);
  await expect(block(page, "move[3]").locator(".status-pill")).toHaveText(
    "Undoing · 1 of 2",
  );
  await expect(block(page, "move[6]").locator(".status-pill")).toHaveText(
    "Waiting for submit[1], which can't be undone",
  );
  await page.screenshot({
    path: `${captures}/run-transactions-j16.png`,
    fullPage: true,
  });
});

test("a step with no undo or confirm yet claims nothing about one, whatever its status", async ({
  page,
}) => {
  const [hold, add, submit] = inDoubt.participants;
  const status = (value: string) =>
    value as (typeof inDoubt.participants)[number]["status"];
  await platform(page, {
    [loopRunId]: [
      {
        ...inDoubt,
        participants: [
          { ...hold, status: "dispatched" },
          { ...add, status: status("held_open"), attempts: undefined },
          { ...submit, status: "unknown" },
        ],
      },
    ],
  });
  await openRun(page);
  await expect(record(page, "move[3]")).toMatchAriaSnapshot(`
    - table "Steps of transaction move[3]":
      - row "Step How it's undone Forward Undo or confirm Operation key"
      - row:
        - cell "hold[3]"
        - cell "Reserved"
        - cell "Running"
        - cell "None"
        - cell /hold/
      - row:
        - cell /^add/
        - cell "Reversible"
        - cell "Held open"
        - cell "None"
        - cell /add/
      - row:
        - cell "submit[3]"
        - cell "Can't be undone"
        - cell "Unknown"
        - cell "None"
        - cell /submit/
  `);
});

test("a transaction in doubt after going ahead keeps Done with its check on every done step", async ({
  page,
}) => {
  const ahead: RunTransactionView = {
    ...inDoubt,
    participants: [
      { ...inDoubt.participants[0], status: "confirm_stuck", attempts: 10 },
      { ...inDoubt.participants[1], status: "done", attempts: undefined },
      { ...inDoubt.participants[2], status: "done" },
    ],
  };
  await platform(page, { [loopRunId]: [ahead] });
  await openRun(page);
  for (const step of ["add[3]", "submit[3]"]) {
    const row = stepRow(page, "move[3]", step);
    await expect(row.locator(".c-forward")).toContainText("Done");
    await expect(
      row.locator('.c-forward weave-icon[data-tone="success"]'),
    ).toHaveCount(1);
  }
  await expect(
    stepRow(page, "move[3]", "hold[3]").locator(".c-undo"),
  ).toContainText("Confirm failed");
  await expect(block(page, "move[3]").locator(".txn-cause")).toHaveText(
    "In doubt: A reservation couldn't be confirmed after the transaction went ahead.",
  );
  await page.screenshot({
    path: `${captures}/run-transactions-went-ahead.png`,
  });
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 360, height: 640 } });

  test("the run detail's transactions are one step row per step, and nothing is wider than the screen", async ({
    page,
  }) => {
    await platform(page);
    await openRun(page);
    await expect(record(page, "move[3]")).toBeVisible();
    const add = stepRow(page, "move[3]", "add[3]");
    expect(
      await add.evaluate(
        (element) =>
          getComputedStyle(element).gridTemplateColumns.split(" ").length,
      ),
    ).toBe(2);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth,
      ),
    ).toBeLessThanOrEqual(0);
    // A step with nothing to undo shows no Undo or confirm line.
    await expect(
      stepRow(page, "move[3]", "submit[3]").locator(".c-undo"),
    ).toBeHidden();
    await expect(add.locator(".c-undo")).toBeVisible();
    await section(page).scrollIntoViewIfNeeded();
    await page.screenshot({
      path: `${captures}/run-transactions-phone-360.png`,
    });
  });
});
