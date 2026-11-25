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
// The drawer of an incident that holds a transaction in doubt: its sentence,
// the related incidents and how they open, its facts with the business key,
// the record as one row per step at every drawer width, and the decision a
// person or a workflow recorded.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page, type Route } from "@playwright/test";
import type { IncidentView } from "../../src/app/operate/incidents/incidents-model";
import {
  called,
  caller,
  held,
  ids,
  loop,
  others,
  runs,
  unknown,
} from "../transaction-incidents-fixture";
import { environment, incidentPlatform } from "./incidents-po";

const captures = resolve(
  process.env["STUDIO_OPERATE_CAPTURES"]?.trim() || "test-results/operate",
);
mkdirSync(captures, { recursive: true });

const drawer = (page: Page) => page.locator("weave-incident-drawer");
const title = (page: Page) => drawer(page).locator("#incident-detail-title");
const links = (page: Page, name: string) =>
  drawer(page).getByRole("region", { name });
const record = (page: Page, key: string) =>
  drawer(page).getByRole("table", { name: `Steps of transaction ${key}` });
const step = (page: Page, key: string, name: string) =>
  record(page, key)
    .getByRole("row")
    .filter({ has: page.locator(".c-step", { hasText: name }) });
const open = (page: Page, id: string) =>
  page.goto(`/operate/incidents?incident=${id}`);
/** How many columns a step's row has: two is one row per step. */
const columns = (page: Page, key: string, name: string) =>
  step(page, key, name).evaluate(
    (row) => getComputedStyle(row).gridTemplateColumns.split(" ").length,
  );
/** An incident once decided: no links are left, and its transaction moved on. */
const decided = (
  { waits_on: _waitsOn, held_by: _heldBy, ...incident }: IncidentView,
  resolution: Partial<NonNullable<IncidentView["resolution"]>>,
): IncidentView => ({
  ...incident,
  ...(incident.transaction
    ? { transaction: { ...incident.transaction, state: "reconciled" } }
    : {}),
  status: "resolved",
  revision: 2,
  resolved_at: "2026-11-22T10:52:00Z",
  resolution: {
    receipt_id: "93333333-3333-4333-8333-333333333333",
    kind: "retry_safe",
    reason: "The target system is back",
    output: null,
    evidence_reference: null,
    ...resolution,
  },
});

test.use({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC" });

test("a called run's stuck undo: its sentence, what holds it, its facts and its record, in that order", async ({
  page,
}) => {
  await incidentPlatform(page);
  await open(page, ids.called);
  await expect(title(page)).toHaveText("Incident WV-TXN-UNDO-STUCK");
  await expect(drawer(page).locator(".badges .status-pill").first()).toHaveText(
    "Active",
  );
  const sentence = drawer(page).locator('.notice[data-tone="danger"]');
  await expect(sentence).toHaveText(
    "A step's undo couldn't finish, so the transaction is in doubt.",
  );
  const section = links(page, "Held by");
  await expect(section).toContainText("WV-TXN-SETTLE-TIMEOUT");
  await expect(section).toContainText("move in run 6d18b3f0");
  await expect(section).toContainText(
    "That run waits for this one: resolve this incident first.",
  );
  await expect(section.locator(".status-pill")).toHaveText("Active");
  await expect(links(page, "Waits on")).toHaveCount(0);
  const facts = drawer(page).locator("dl.txn-facts");
  await expect(facts.locator("dt")).toHaveText([
    "Opened",
    "Workflow",
    "Step",
    "Business key",
    "Run",
  ]);
  await expect(facts.locator("dd").nth(0)).toHaveText("21 min ago");
  await expect(facts.locator("dd").nth(1)).toHaveText("add-quantity 2.0.0");
  await expect(facts.locator("dd").nth(2)).toHaveText("add");
  await expect(facts.locator("dd").nth(3)).toHaveText("T-1029");
  await expect(facts.locator("dd").nth(4)).toContainText("On hold");
  await expect(
    drawer(page).getByRole("heading", { level: 3, name: "Transaction apply" }),
  ).toBeVisible();
  await expect(drawer(page).locator(".txn-panel .record-title")).toContainText(
    "In doubt · action needed",
  );
  const order = await drawer(page).evaluate((element) =>
    [
      "#incident-detail-title",
      ".badges",
      '.notice[data-tone="danger"]',
      ".txn-links",
      ".txn-facts",
      ".txn-record",
      ".recent-events",
    ].map((selector) =>
      Math.round(element.querySelector(selector)!.getBoundingClientRect().top),
    ),
  );
  expect(order).toEqual([...order].sort((a, b) => a - b));
  // The step's own failure and its keys stay with the technical details.
  await expect(drawer(page).locator("details")).toContainText("UNAVAILABLE");
  await expect(drawer(page).locator("details")).toContainText("add#undo");
  await page.screenshot({ path: `${captures}/drawer-undo-stuck-1440.png` });
});

test("the record: what stays applied has no check, and each key is the whole operation key", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await incidentPlatform(page);
  await open(page, ids.called);
  const hold = step(page, "apply", "hold");
  await expect(hold.locator(".c-how")).toHaveText("Reserved");
  await expect(hold.locator(".c-undo")).toContainText("Still reserved");
  await expect(hold.locator(".c-forward weave-icon")).toHaveCount(0);
  const add = step(page, "apply", "add");
  await expect(add.locator(".c-forward")).toContainText("Done");
  await expect(add.locator(".c-forward weave-icon")).toHaveCount(0);
  await expect(add.locator(".c-undo")).toContainText("Undo failed");
  await expect(add.locator(".c-undo")).toContainText("3 tries");
  await expect(add).toHaveClass(/stuck/);
  const key = `${runs.called}:add#undo`;
  await expect(add.locator(".c-key code")).toHaveText(key);
  await add.getByRole("button", { name: `Copy operation key ${key}` }).click();
  await expect(page.getByText("Copied the operation key.")).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(key);
  await drawer(page).locator(".recent-events").scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${captures}/drawer-record-1440.png` });
});

test("a settle timeout waits on the called run's incident; its record names the child run and the pending step", async ({
  page,
}) => {
  await incidentPlatform(page);
  await open(page, ids.caller);
  await expect(drawer(page).locator('.notice[data-tone="danger"]')).toHaveText(
    "Steps still running didn't finish in time, so nothing can be undone safely yet.",
  );
  const section = links(page, "Waits on");
  await expect(section).toContainText("WV-TXN-UNDO-STUCK");
  await expect(section).toContainText("add in run 9a01c7d2");
  await expect(section).not.toContainText("add#undo");
  await expect(section).toContainText(
    "Resolve these first: this run waits for them.",
  );
  await expect(step(page, "move", "hold").locator(".c-undo")).toContainText(
    "Still reserved",
  );
  const add = step(page, "move", "add");
  await expect(add.locator(".c-forward")).toContainText("Running");
  await expect(add.locator(".c-step")).toContainText("Pending");
  await expect(add.locator(".c-step")).toContainText("Child run 9a01c7d2");
  await page.screenshot({ path: `${captures}/drawer-settle-timeout-1440.png` });
  await page.route(`${environment}/runs/${runs.called}`, (route) =>
    route.fulfill({
      json: {
        id: runs.called,
        business_key: null,
        correlation_key: null,
        activation: { id: "act", name: "add-quantity", revision: 1 },
        state: { status: "suspended", active: [], input: {} },
      },
    }),
  );
  await add.getByRole("button", { name: "Open child run 9a01c7d2" }).click();
  await expect(page).toHaveURL(new RegExp(`/operate/runs/${runs.called}`));
});

test("the step that can't be undone reads Unknown, and what came before it stays applied", async ({
  page,
}) => {
  await incidentPlatform(page);
  await open(page, ids.unknown);
  await expect(step(page, "move", "add").locator(".c-undo")).toContainText(
    "Not undone",
  );
  await expect(
    step(page, "move", "submit").locator(".c-forward"),
  ).toContainText("Unknown");
  await expect(drawer(page).locator(".txn-links")).toHaveCount(0);
  await page.screenshot({ path: `${captures}/drawer-unknown-1440.png` });
});

test("links withheld say that called runs wait", async ({ page }) => {
  const { waits_on: _links, ...withheld } = caller;
  await incidentPlatform(page, {
    items: [{ ...withheld, waits_on_more: true }],
  });
  await open(page, ids.caller);
  const section = links(page, "Waits on");
  await expect(
    section.getByText("Called runs wait in doubt. Open the run to see them.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(section.getByRole("button")).toHaveCount(0);
  await expect(section).not.toContainText("Resolve these first");
  await page.screenshot({ path: `${captures}/drawer-withheld-1440.png` });
});

test("a run held whole shows its sentence and no record, tied to no step", async ({
  page,
}) => {
  await incidentPlatform(page, { items: [held] });
  await open(page, ids.held);
  await expect(drawer(page).locator('.notice[data-tone="danger"]')).toHaveText(
    "The run grew too large to undo its transactions, so they're held until someone force ends the run.",
  );
  await expect(drawer(page)).toContainText(
    "Its transactions are held as they are. Open the run to see each one.",
  );
  await expect(drawer(page).getByRole("table")).toHaveCount(0);
  const facts = drawer(page).locator("dl.txn-facts dd");
  await expect(facts.nth(2)).toHaveText("Not tied to a step");
  await expect(facts.nth(3)).toHaveText("None");
  await page.screenshot({ path: `${captures}/drawer-held-run-1440.png` });
});

for (const [incident, resolution, words] of [
  [caller, { kind: "retry_safe" }, "Wait again"],
  [loop, { kind: "retry_safe" }, "Retry the release"],
  [others[0], { kind: "retry_safe" }, "Retry the step"],
  [called, { kind: "terminate" }, "Force end"],
] as const)
  test(`a resolved ${incident.code} reads ${words}`, async ({ page }) => {
    await incidentPlatform(page, { items: [decided(incident, resolution)] });
    await open(page, incident.id);
    await expect(
      drawer(page).locator("dt:text-is('Decision') + dd"),
    ).toHaveText(words);
    // Decided, it no longer is in doubt.
    await expect(
      drawer(page).locator('.notice[data-tone="danger"]'),
    ).toHaveCount(0);
  });

test("a decision a workflow applied names its run and opens it; evidence stays text", async ({
  page,
}) => {
  await incidentPlatform(page, {
    items: [
      decided(called, {
        kind: "accept_reconciled_result",
        output: { as: "compensated", participants: [] },
        evidence_reference: "<b>ticket 4411</b>",
        workflow_run_id: runs.resolving,
      }),
    ],
  });
  await open(page, ids.called);
  await expect(drawer(page).locator("dt:text-is('Decision') + dd")).toHaveText(
    "Mark as reconciled as undone",
  );
  await expect(drawer(page).locator("dt:text-is('Evidence') + dd")).toHaveText(
    "<b>ticket 4411</b>",
  );
  await page.screenshot({ path: `${captures}/drawer-resolved-1440.png` });
  const by = drawer(page).locator("dt:text-is('By') + dd");
  await expect(by).toHaveText("Workflow run 5c90e1b7");
  await by.getByRole("button").click();
  await expect(page).toHaveURL(new RegExp(`/operate/runs/${runs.resolving}`));
});

test("an incident with no transaction keeps its drawer", async ({ page }) => {
  await incidentPlatform(page);
  await open(page, others[0].id);
  await expect(title(page)).toHaveText("Incident WV-TASK-AMBIGUOUS");
  await expect(
    drawer(page).locator("dl.facts").first().locator("dt"),
  ).toHaveText([
    "Code",
    "Opened",
    "Last changed",
    "Workflow",
    "Step",
    "Attempt",
    "Run",
  ]);
  await expect(drawer(page).locator("weave-transaction-panel")).toHaveCount(0);
  await expect(drawer(page).locator("details dt")).toHaveText([
    "Incident ID",
    "Incident key",
    "Revision",
  ]);
});

// ------------------------------------------------------- related incidents

test("Open on a loaded partner reads nothing and moves the focus to its title", async ({
  page,
}) => {
  const platform = await incidentPlatform(page);
  await page.goto("/operate/incidents");
  await page
    .getByRole("button", {
      name: "Open incident WV-TXN-SETTLE-TIMEOUT at move",
    })
    .click();
  await links(page, "Waits on")
    .getByRole("button", {
      name: "Open incident WV-TXN-UNDO-STUCK, add in run 9a01c7d2",
    })
    .click();
  await expect(title(page)).toHaveText("Incident WV-TXN-UNDO-STUCK");
  await expect(title(page)).toBeFocused();
  expect(platform.reads).toEqual([]);
  await expect(page).toHaveURL(
    new RegExp(`/operate/incidents\\?incident=${ids.called}$`),
  );
});

/** An incident no page lists. */
const elsewhere = "a2000000-0000-4000-8000-000000000002";
/** A drawer on the caller's incident whose partner only incidents.read holds. */
async function partnerElsewhere(page: Page, items = [caller, ...others]) {
  const platform = await incidentPlatform(page, { items });
  const reads: Route[] = [];
  await page.route(`${environment}/incidents/${ids.called}`, (route) => {
    reads.push(route);
  });
  await page.goto("/operate/incidents");
  await page
    .getByRole("button", {
      name: "Open incident WV-TXN-SETTLE-TIMEOUT at move",
    })
    .click();
  const openPartner = links(page, "Waits on").getByRole("button", {
    name: "Open incident WV-TXN-UNDO-STUCK, add in run 9a01c7d2",
  });
  return { platform, reads, openPartner };
}
const unavailable = {
  status: 503,
  json: { code: "WV-UNAVAILABLE", message: "Unavailable" },
};

test("Open on a partner no page lists reads it once and opens it", async ({
  page,
}) => {
  const { reads, openPartner } = await partnerElsewhere(page);
  // Two presses in one turn, as a double click sends them.
  await openPartner.evaluate((button: HTMLElement) => {
    button.click();
    button.click();
  });
  await expect.poll(() => reads.length).toBe(1);
  await expect(openPartner).toHaveAttribute("aria-busy", "true");
  // Nor does a press while it is on its way.
  await openPartner.evaluate((button: HTMLElement) => button.click());
  await expect(title(page)).toHaveText("Incident WV-TXN-SETTLE-TIMEOUT");
  await reads[0].fulfill({ json: called });
  await expect(title(page)).toHaveText("Incident WV-TXN-UNDO-STUCK");
  await expect(title(page)).toBeFocused();
  expect(reads).toHaveLength(1);
  await expect(page).toHaveURL(
    new RegExp(`/operate/incidents\\?incident=${ids.called}$`),
  );
});

test("a partner that can't be read waits 5 s, then 10 s, and Try now reads it at once", async ({
  page,
}) => {
  const { reads, openPartner } = await partnerElsewhere(page);
  await openPartner.click();
  await expect.poll(() => reads.length).toBe(1);
  await reads[0].fulfill(unavailable);
  const problem = links(page, "Waits on").getByRole("alert");
  await expect(problem).toContainText(
    "Studio couldn't read this incident. It tries again in 5 s.",
  );
  await expect(problem).toContainText("Support code: WV-UNAVAILABLE");
  await page.screenshot({ path: `${captures}/drawer-partner-failed-1440.png` });
  await page.clock.fastForward(4_000);
  expect(reads).toHaveLength(1);
  await page.clock.fastForward(1_500);
  await expect.poll(() => reads.length).toBe(2);
  await reads[1].fulfill(unavailable);
  await expect(problem).toContainText("It tries again in 10 s.");
  await page.clock.fastForward(6_000);
  expect(reads).toHaveLength(2);
  await problem.getByRole("button", { name: "Try now" }).click();
  await expect.poll(() => reads.length).toBe(3);
  await reads[2].fulfill({ json: called });
  await expect(title(page)).toHaveText("Incident WV-TXN-UNDO-STUCK");
  await expect(title(page)).toBeFocused();
});

test("Try now keeps the focus while it reads, and Open takes it when the answer is final", async ({
  page,
}) => {
  const { reads, openPartner } = await partnerElsewhere(page);
  await openPartner.click();
  await expect.poll(() => reads.length).toBe(1);
  await reads[0].fulfill(unavailable);
  const problem = links(page, "Waits on").getByRole("alert");
  const tryNow = problem.getByRole("button", { name: "Try now" });
  await tryNow.focus();
  await page.keyboard.press("Enter");
  await expect.poll(() => reads.length).toBe(2);
  await expect(tryNow).toBeFocused();
  await expect(tryNow).toHaveAttribute("aria-busy", "true");
  await expect(tryNow).toHaveAttribute("aria-disabled", "true");
  // Nor does a press while it is on its way read it once more.
  await page.keyboard.press("Enter");
  await reads[1].fulfill(unavailable);
  await expect(problem).toContainText("It tries again in 10 s.");
  expect(reads).toHaveLength(2);
  await expect(tryNow).toBeFocused();
  await expect(tryNow).not.toHaveAttribute("aria-busy");
  // Nor does its own retry take the focus away.
  await page.clock.fastForward(11_000);
  await expect.poll(() => reads.length).toBe(3);
  await expect(tryNow).toBeFocused();
  await reads[2].fulfill({
    status: 404,
    json: { code: "WV-NOT-FOUND", message: "Incident not found" },
  });
  await expect(problem).toContainText("This incident can't be found.");
  await expect(tryNow).toHaveCount(0);
  await expect(openPartner).toBeFocused();
});

test("leaving the incident drops its partner's read and its retries", async ({
  page,
}) => {
  const { reads, openPartner } = await partnerElsewhere(page);
  await openPartner.click();
  await expect.poll(() => reads.length).toBe(1);
  await page
    .getByRole("button", { name: "Open incident WV-TASK-FAILED at fetch" })
    .click();
  await reads[0].fulfill({ json: called });
  await expect(title(page)).toHaveText("Incident WV-TASK-FAILED");
  // Nor does a failure read again once another incident is open.
  await page
    .getByRole("button", {
      name: "Open incident WV-TXN-SETTLE-TIMEOUT at move",
    })
    .click();
  await openPartner.click();
  await expect.poll(() => reads.length).toBe(2);
  await reads[1].fulfill(unavailable);
  await expect(links(page, "Waits on").getByRole("alert")).toBeVisible();
  await page
    .getByRole("button", { name: "Open incident WV-TASK-FAILED at fetch" })
    .click();
  await page.clock.fastForward(6_000);
  await expect(title(page)).toHaveText("Incident WV-TASK-FAILED");
  expect(reads).toHaveLength(2);
});

for (const [how, ask] of [
  [
    "the list's Open",
    (page: Page) =>
      page
        .getByRole("button", {
          name: "Open incident WV-TXN-SETTLE-TIMEOUT, move in run 2f3e4d5c",
        })
        .click(),
  ],
  [
    "the address",
    (page: Page) =>
      page.evaluate((id) => {
        history.pushState(null, "", `/operate/incidents?incident=${id}`);
        dispatchEvent(new PopStateEvent("popstate"));
      }, elsewhere),
  ],
] as const)
  test(`a partner's retry never opens over an incident asked for after it from ${how}`, async ({
    page,
  }) => {
    // Another called run in doubt, whose caller no page lists.
    const second: IncidentView = {
      ...called,
      id: "a2000000-0000-4000-8000-000000000003",
      held_by: {
        ...called.held_by!,
        id: elsewhere,
        run_id: "2f3e4d5c-0000-4000-8000-000000000002",
      },
    };
    const { reads, openPartner } = await partnerElsewhere(page, [
      caller,
      second,
      ...others,
    ]);
    const later: Route[] = [];
    await page.route(`${environment}/incidents/${elsewhere}`, (route) => {
      later.push(route);
    });
    await openPartner.click();
    await expect.poll(() => reads.length).toBe(1);
    await reads[0].fulfill(unavailable);
    await expect(links(page, "Waits on").getByRole("alert")).toBeVisible();
    await ask(page);
    await expect.poll(() => later.length).toBe(1);
    // It is on its way: the partner asked for before it isn't read again.
    await expect(links(page, "Waits on").getByRole("alert")).toHaveCount(0);
    await page.clock.fastForward(6_000);
    expect(reads).toHaveLength(1);
    await later[0].fulfill({
      json: { ...caller, id: elsewhere, code: "WV-TXN-PIVOT-UNKNOWN" },
    });
    await expect(title(page)).toHaveText("Incident WV-TXN-PIVOT-UNKNOWN");
  });

test("a partner's retry never opens over its own incident, asked for again from the list's Open", async ({
  page,
}) => {
  // No page lists the drawer's incident: another called run's link names it.
  await incidentPlatform(page, {
    items: [
      { ...called, id: "a2000000-0000-4000-8000-000000000003" },
      ...others,
    ],
  });
  await page.route(`${environment}/incidents/${ids.caller}`, (route) =>
    route.fulfill({ json: caller }),
  );
  const reads: Route[] = [];
  await page.route(`${environment}/incidents/${ids.called}`, (route) => {
    reads.push(route);
  });
  const openPartner = links(page, "Waits on").getByRole("button", {
    name: "Open incident WV-TXN-UNDO-STUCK, add in run 9a01c7d2",
  });
  await open(page, ids.caller);
  await expect(title(page)).toHaveText("Incident WV-TXN-SETTLE-TIMEOUT");
  // The address names one that can't be found: the drawer stays.
  await page.evaluate((id) => {
    history.pushState(null, "", `/operate/incidents?incident=${id}`);
    dispatchEvent(new PopStateEvent("popstate"));
  }, elsewhere);
  await expect(
    page.getByRole("heading", { name: "This incident can't be found" }),
  ).toBeVisible();
  await openPartner.click();
  await expect.poll(() => reads.length).toBe(1);
  await reads[0].fulfill(unavailable);
  const alert = links(page, "Waits on").getByRole("alert");
  await expect(alert).toBeVisible();
  await page
    .locator(".not-listed")
    .getByRole("button", {
      name: "Open incident WV-TXN-SETTLE-TIMEOUT, move in run 6d18b3f0",
    })
    .click();
  await expect(page).toHaveURL(
    new RegExp(`/operate/incidents\\?incident=${ids.caller}$`),
  );
  await expect(alert).toHaveCount(0);
  await page.clock.fastForward(6_000);
  expect(reads).toHaveLength(1);
  await expect(title(page)).toHaveText("Incident WV-TXN-SETTLE-TIMEOUT");
});

for (const [where, field, moves] of [
  ["the drawer", null, true],
  ["a filter", "Workflow", false],
] as const)
  test(`a partner a retry reads opens while the focus is in ${where}, which ${moves ? "goes to its title" : "keeps it"}`, async ({
    page,
  }) => {
    const { reads, openPartner } = await partnerElsewhere(page);
    await openPartner.click();
    await expect.poll(() => reads.length).toBe(1);
    await reads[0].fulfill(unavailable);
    await expect(links(page, "Waits on").getByRole("alert")).toBeVisible();
    if (field) await page.getByLabel(field, { exact: true }).focus();
    else await expect(openPartner).toBeFocused();
    await page.clock.fastForward(5_500);
    await expect.poll(() => reads.length).toBe(2);
    await reads[1].fulfill({ json: called });
    await expect(title(page)).toHaveText("Incident WV-TXN-UNDO-STUCK");
    // A title takes it on the next turn.
    await page.clock.runFor(100);
    await expect(
      field ? page.getByLabel(field, { exact: true }) : title(page),
    ).toBeFocused();
  });

test("a partner on its way never opens under a decision", async ({ page }) => {
  const { reads, openPartner } = await partnerElsewhere(page);
  await openPartner.click();
  await expect.poll(() => reads.length).toBe(1);
  const wait = { name: "Wait again", exact: true };
  await drawer(page).getByRole("button", wait).click();
  const dialog = page.getByRole("dialog", wait);
  await dialog.getByLabel("Reason").fill("The target system is back");
  const answered = page.waitForResponse((response) =>
    response.url().endsWith(`/incidents/${ids.called}`),
  );
  await reads[0].fulfill({ json: called });
  await answered;
  // Studio has handled the answer once it draws again.
  await page.evaluate(
    () =>
      new Promise<void>((done) =>
        requestAnimationFrame(() => requestAnimationFrame(() => done())),
      ),
  );
  await expect(dialog).toContainText("Transaction move in run 6d18b3f0");
  await expect(dialog.getByLabel("Reason")).toHaveValue(
    "The target system is back",
  );
  await expect(title(page)).toHaveText("Incident WV-TXN-SETTLE-TIMEOUT");
});

for (const [name, answer, words] of [
  [
    "missing",
    {
      status: 404,
      json: { code: "WV-NOT-FOUND", message: "Incident not found" },
    },
    "This incident can't be found. It may belong to another environment, or its run's data was deleted.",
  ],
  [
    "refused",
    { status: 403, json: { code: "WV-FORBIDDEN", message: "Not allowed" } },
    "You need incident.read in this environment. Ask an administrator in Settings › People and access.",
  ],
  [
    "withheld",
    { json: { id: ids.called, unavailable: true } },
    "Studio can't show this incident: its run can't be shown here.",
  ],
] as const)
  test(`a partner that is ${name} says so, and isn't read again`, async ({
    page,
  }) => {
    const { reads, openPartner } = await partnerElsewhere(page);
    await openPartner.click();
    await expect.poll(() => reads.length).toBe(1);
    await reads[0].fulfill(answer);
    await expect(links(page, "Waits on").getByRole("alert")).toContainText(
      words,
    );
    await page.clock.fastForward(70_000);
    expect(reads).toHaveLength(1);
    await expect(title(page)).toHaveText("Incident WV-TXN-SETTLE-TIMEOUT");
  });

test("a direct link opens the drawer, and the business key never enters the address", async ({
  page,
}) => {
  await incidentPlatform(page, { items: [called, caller, ...others] });
  await open(page, ids.called);
  await expect(title(page)).toHaveText("Incident WV-TXN-UNDO-STUCK");
  await links(page, "Held by")
    .getByRole("button", {
      name: "Open incident WV-TXN-SETTLE-TIMEOUT, move in run 6d18b3f0",
    })
    .click();
  await expect(title(page)).toHaveText("Incident WV-TXN-SETTLE-TIMEOUT");
  expect(page.url()).toContain(`incident=${ids.caller}`);
  expect(page.url()).not.toContain("T-1029");
});

// ---------------------------------------------------------------- widths

test("the record is one row per step in the drawer beside the list", async ({
  page,
}) => {
  await incidentPlatform(page);
  await open(page, ids.called);
  await expect(record(page, "apply")).toBeVisible();
  const box = (await page.locator(".record-detail").boundingBox())!;
  expect(box.width).toBeCloseTo(420, 0);
  expect(await columns(page, "apply", "add")).toBe(2);
  // The step's name left, how it's undone at the right edge; then its words
  // side by side from the left, and the key under them.
  const at = await step(page, "apply", "hold").evaluate((row) => {
    const box = (selector: string) =>
      row.querySelector(selector)!.getBoundingClientRect();
    const edges = row.getBoundingClientRect();
    return {
      how: edges.right - box(".c-how").right,
      words: box(".c-undo").left - box(".c-forward").right,
      sameLine: box(".c-undo").top - box(".c-forward").top,
      key: box(".c-key").top - box(".c-forward").bottom,
    };
  });
  expect(at.how).toBeCloseTo(0, 0);
  expect(at.words).toBeGreaterThan(0);
  expect(at.words).toBeLessThanOrEqual(24);
  expect(Math.abs(at.sameLine)).toBeLessThanOrEqual(1);
  expect(at.key).toBeGreaterThanOrEqual(0);
  // Two pairs of facts a row, Run across.
  expect(
    await drawer(page)
      .locator(".txn-facts")
      .evaluate(
        (list) => getComputedStyle(list).gridTemplateColumns.split(" ").length,
      ),
  ).toBe(4);
});

for (const size of [
  { width: 767, height: 900, touch: false },
  { width: 600, height: 500, touch: false },
  { width: 360, height: 640, touch: true },
])
  test.describe(`in a window ${size.width}x${size.height}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
    });

    test("the drawer is a full-screen sheet, its record one row per step and nothing wider than the screen", async ({
      page,
    }) => {
      await incidentPlatform(page);
      await open(page, ids.called);
      await expect(record(page, "apply")).toBeVisible();
      const sheet = (await page.locator(".record-detail").boundingBox())!;
      expect(sheet).toEqual({
        x: 0,
        y: 0,
        width: size.width,
        height: size.height,
      });
      expect(await columns(page, "apply", "add")).toBe(2);
      expect(
        await page
          .locator(".record-detail")
          .evaluate((element) => element.scrollWidth - element.clientWidth),
      ).toBeLessThanOrEqual(0);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth - window.innerWidth,
        ),
      ).toBeLessThanOrEqual(0);
      // What a person must see first: the sentence and what holds it.
      if (size.width === 360) {
        const heldBy = (await links(page, "Held by").boundingBox())!;
        expect(heldBy.y + heldBy.height).toBeLessThanOrEqual(size.height);
      }
      for (const button of [
        links(page, "Held by").getByRole("button"),
        step(page, "apply", "add").getByRole("button", { name: /^Copy/ }),
      ])
        expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(
          43.5,
        );
      await page.screenshot({
        path: `${captures}/drawer-undo-stuck-${size.width}.png`,
      });
      await record(page, "apply").scrollIntoViewIfNeeded();
      await page.screenshot({
        path: `${captures}/drawer-record-${size.width}.png`,
      });
      // Escape closes the sheet and the list is back.
      await page.keyboard.press("Escape");
      await expect(drawer(page)).toHaveCount(0);
    });
  });

test.describe("in the narrow drawer", () => {
  test.use({ viewport: { width: 1200, height: 900 } });

  test("the record and its keys stay inside, and the facts follow the drawer's width", async ({
    page,
  }) => {
    await incidentPlatform(page);
    await open(page, ids.called);
    await expect(record(page, "apply")).toBeVisible();
    const box = (await page.locator(".record-detail").boundingBox())!;
    expect(box.width).toBeCloseTo(320, 0);
    expect(await columns(page, "apply", "add")).toBe(2);
    // Too narrow for both words: Undo or confirm goes under Forward.
    const forward = (await step(page, "apply", "add")
      .locator(".c-forward")
      .boundingBox())!;
    const undo = (await step(page, "apply", "add")
      .locator(".c-undo")
      .boundingBox())!;
    expect(undo.y).toBeGreaterThanOrEqual(forward.y + forward.height);
    expect(undo.x).toBeCloseTo(forward.x, 0);
    expect(
      await page
        .locator(".record-detail")
        .evaluate((element) => element.scrollWidth - element.clientWidth),
    ).toBeLessThanOrEqual(0);
    expect(
      await drawer(page)
        .locator(".txn-facts")
        .evaluate(
          (list) =>
            getComputedStyle(list).gridTemplateColumns.split(" ").length,
        ),
    ).toBe(2);
    await page.screenshot({ path: `${captures}/drawer-undo-stuck-1200.png` });
  });
});
