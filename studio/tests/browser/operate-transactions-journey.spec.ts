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
// Four transactions in doubt, from the Runs list that finds them to an
// Overview with none left: the record of one, the inbox's case, the called
// run's incident retried and then its caller's wait, by someone who can only
// retry, and the other two decided by a reconciler with evidence. The words
// are the shared corpus's where it has them. After each step, no business key
// is in an address, the page's title, a notice, a downloaded file's name, the
// console or a request to Weave AI. The platform is mocked with its own shapes.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";
import type { RunTransactionView } from "../../src/app/operate/transactions/transaction-contracts";
import { ids, NOW, runs } from "../transaction-incidents-fixture";
import { environment } from "./incidents-po";
import {
  ended,
  operator,
  reconciler,
  transactionsPlatform,
  type TransactionsPlatform,
} from "./operate-transactions-po";

interface Corpus {
  states: Record<string, { badge: string }>;
  stateWords: Record<string, string>;
  statuses: Record<string, { word: string } | null>;
  codes: Record<string, string>;
  resolutions: Record<string, { label: string; help: string }>;
}
const corpus = JSON.parse(
  readFileSync(resolve("tests/fixtures/transaction-corpus.json"), "utf8"),
) as Corpus;
const short = (id: string) => id.slice(0, 8);
/** The business keys of this moment's runs. */
const KEY = /T-10\d\d/;

// --------------------------------------------------------------- the key scan

/** Everything that leaves a page for a history, a notice, a file or a service. */
class KeyScan {
  private seen: { where: string; text: string }[] = [];
  private checked = 0;
  static async on(page: Page): Promise<KeyScan> {
    const scan = new KeyScan();
    const note = (where: string, text: string) =>
      scan.seen.push({ where, text });
    page.on("framenavigated", (frame) => {
      if (frame === page.mainFrame()) note("address", frame.url());
    });
    page.on("request", (request) => {
      note("request", decodeURIComponent(request.url()));
      if (/\/lumi\//.test(request.url()))
        note("weave ai", request.postData() ?? "");
    });
    page.on("download", (download) =>
      note("file", download.suggestedFilename()),
    );
    page.on("console", (message) => note("console", message.text()));
    await page.exposeBinding("seenOnPage", (_source, where, text) =>
      note(String(where), String(text)),
    );
    await page.addInitScript(() => {
      const seen = (where: string, text: string) =>
        (
          window as unknown as {
            seenOnPage: (where: string, text: string) => void;
          }
        ).seenOnPage(where, text);
      for (const name of ["pushState", "replaceState"] as const) {
        const original = history[name].bind(history);
        history[name] = (state, unused, url) => {
          original(state, unused, url);
          seen("address", location.href);
        };
      }
      const Native = window.Notification;
      if (Native)
        window.Notification = new Proxy(Native, {
          construct(target, [title, options]) {
            seen(
              "notification",
              `${title} ${(options as NotificationOptions | undefined)?.body ?? ""}`,
            );
            return new target(title as string, options as NotificationOptions);
          },
        });
      const watch = () =>
        new MutationObserver(() => {
          seen("title", document.title);
          for (const notice of document.querySelectorAll(".toast-text"))
            seen("notice", notice.textContent ?? "");
        }).observe(document, {
          childList: true,
          subtree: true,
          characterData: true,
        });
      watch();
    });
    return scan;
  }
  /** No business key left the page since the last check. */
  clean(step: string) {
    const found = this.seen.filter(({ text }) => KEY.test(text));
    expect(found, `after ${step}`).toEqual([]);
    expect(this.seen.length, `${step} was watched`).toBeGreaterThan(
      this.checked,
    );
    this.checked = this.seen.length;
  }
  /** What was seen of a kind, so a watch that saw nothing can't pass. */
  of(where: string): string[] {
    return this.seen
      .filter((item) => item.where === where)
      .map((item) => item.text);
  }
}

// ---------------------------------------------------------------- the screens

const runRows = (page: Page) =>
  page.getByRole("table", { name: "Runs" }).locator(".run-row");
const transactionsOf = (page: Page) =>
  page.getByRole("region", { name: "Transactions" });
const block = (page: Page, key: string) =>
  transactionsOf(page).getByRole("article", { name: `Transaction ${key}` });
const stepOf = (scope: Locator, key: string, name: string) =>
  scope
    .getByRole("table", { name: `Steps of transaction ${key}` })
    .getByRole("row")
    .filter({ has: scope.page().locator(".c-step", { hasText: name }) });
const high = (page: Page) =>
  page.getByRole("region", { name: "High priority" });
const codes = (scope: Locator) =>
  scope.locator(".resource-row > .row-text:first-child > :first-child");
const caseOf = (page: Page) =>
  page.getByRole("rowgroup", {
    name: "Case: move is waiting for a called run in doubt",
  });
const drawer = (page: Page) => page.locator("weave-incident-drawer");
const offers = (page: Page) =>
  drawer(page).getByRole("region", { name: "Resolve this incident" });
const offer = (page: Page, name: string) =>
  offers(page).getByRole("button", { name, exact: true });
const dialog = (page: Page, name: string) =>
  page.getByRole("dialog", { name, exact: true });
const decision = (page: Page) =>
  drawer(page).locator("dt:text-is('Decision') + dd");
const overviewPanel = (page: Page) =>
  page.getByRole("article", { name: "Transactions", exact: true });
const go = (page: Page, name: string) =>
  page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("button", { name, exact: true })
    .click();
/** Opens a run of the list by its short ID. */
const openRun = (page: Page, id: string) =>
  runRows(page)
    .filter({ has: page.locator(".cell-run code", { hasText: short(id) }) })
    .locator(".row-open")
    .click();
/** Opens an incident of the list by its code and step. */
const openIncident = (page: Page, code: string, at: string) =>
  page
    .getByRole("button", {
      name: `Open incident ${code} at ${at}`,
      exact: true,
    })
    .click();
/** Opens a decision, fills in its reason and evidence, and sends it. */
async function decide(
  page: Page,
  name: string,
  said: { reason: string; evidence?: string },
  more?: (asked: Locator) => Promise<void>,
) {
  await offer(page, name).click();
  const asked = dialog(page, name);
  await expect(asked).toBeVisible();
  await more?.(asked);
  await asked.getByLabel("Reason").fill(said.reason);
  if (said.evidence)
    await asked.getByLabel("Evidence reference").fill(said.evidence);
  await asked.getByRole("button", { name, exact: true }).click();
  await expect(asked).toHaveCount(0);
}

/** What the platform does once each decision is recorded. */
function settled(platform: TransactionsPlatform, { id }: { id: string }) {
  const runOf = (run: string) => platform.runs.find((item) => item.id === run)!;
  const step = (
    view: RunTransactionView,
    statuses: Record<
      string,
      RunTransactionView["participants"][number]["status"]
    >,
  ) =>
    view.participants.map(({ attempts: _attempts, ...participant }) => ({
      ...participant,
      status: statuses[participant.instance_key] ?? participant.status,
    }));
  const resolution = (
    facts: Partial<NonNullable<RunTransactionView["resolution"]>>,
  ) => ({
    kind: "retry_safe" as const,
    reason: "",
    actor_id: "human",
    resolved_at: NOW,
    ...facts,
  });
  const only = (run: string) => platform.transactions[run][0];
  switch (id) {
    case ids.called:
      // The undo went through: the called run ends, and nothing waits on it.
      platform.transaction(runs.called, {
        state: "compensated",
        ended_at: NOW,
        participants: step(only(runs.called), {
          hold: "cancelled",
          add: "undone",
        }).map((item) =>
          item.instance_key === "add" ? { ...item, attempts: 4 } : item,
        ),
        resolution: resolution({ reason: "The target system is back" }),
      });
      platform.run(
        runs.called,
        ended(runOf(runs.called), "failed", "compensated"),
      );
      platform.incidents.items = platform.incidents.items.map((item) => {
        if (item.id !== ids.caller || "unavailable" in item) return item;
        const { waits_on: _resolved, ...caller } = item;
        return caller;
      });
      return;
    case ids.caller:
      // It waited again, the called step had ended, and the rest was undone.
      platform.transaction(runs.caller, {
        state: "compensated",
        ended_at: NOW,
        participants: step(only(runs.caller), {
          hold: "cancelled",
          add: "failed",
        }),
        resolution: resolution({ reason: "The called run ended" }),
      });
      platform.run(
        runs.caller,
        ended(runOf(runs.caller), "failed", "compensated"),
      );
      return;
    case ids.loop:
      platform.transactions[runs.loop] = platform.transactions[runs.loop].map(
        (view) =>
          view.instance_key === "move[3]"
            ? {
                ...view,
                state: "reconciled",
                ended_at: NOW,
                resolution: resolution({
                  kind: "accept_reconciled_result",
                  reason: "Released by hand in the source system",
                  evidence_reference: "OPS-2304",
                  reconciled_as: "compensated",
                }),
              }
            : view,
      );
      platform.run(runs.loop, {
        status: "running",
        active_incidents: 0,
        transaction_state: "compensated",
      });
      return;
    case ids.unknown:
      platform.transaction(runs.unknown, {
        state: "completed",
        decided_at: NOW,
        ended_at: NOW,
        participants: step(only(runs.unknown), {
          hold: "confirmed",
          submit: "done",
        }),
        resolution: resolution({
          kind: "accept_reconciled_result",
          reason: "Found the order in its system",
          evidence_reference: "Search at 10:52",
        }),
      });
      platform.run(
        runs.unknown,
        ended(runOf(runs.unknown), "succeeded", "completed"),
      );
  }
}

test.use({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC" });

test("from the runs that need attention to none in doubt, and no business key leaves the pages", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const scan = await KeyScan.on(page);
  const platform = await transactionsPlatform(page, {
    capabilities: [...operator, "lumi.use"],
    decided: settled,
  });
  await page.route("**/lumi/status", (route) =>
    route.fulfill({
      json: { configured: true, provider: "openai", model: "test" },
    }),
  );
  const asked: unknown[] = [];
  await page.route("**/lumi/ask", (route) => {
    asked.push(route.request().postDataJSON());
    return route.fulfill({
      json: { answer: "Noted.", proposals: [], followUps: [] },
    });
  });
  await page.route(`${environment}/runs/*/export?*`, (route) =>
    route.fulfill({ json: { events: [], bounded_prefix: false } }),
  );
  /** Asks Weave AI about the page, with the run when it may be included. */
  const askWeaveAi = async (run?: string) => {
    await page.getByRole("button", { name: "Ask Weave AI" }).click();
    const panel = page.getByRole("dialog", {
      name: "Ask Weave AI",
      exact: true,
    });
    if (run) await panel.getByLabel("Include selected run").check();
    await panel
      .getByLabel("Message to Weave AI", { exact: true })
      .fill("What is in doubt here?");
    await panel
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    await expect(panel).toContainText("Noted.");
    await page.keyboard.press("Escape");
    await expect(panel).toHaveCount(0);
    expect(asked.at(-1)).toMatchObject({
      attachments: run ? [{ kind: "run", id: run }] : [],
    });
  };

  // ---- Overview counts four, and its link opens the runs to look at.
  await go(page, "Overview");
  const counts = overviewPanel(page).locator("dd");
  await expect(counts).toHaveText(["4", "1"]);
  await expect(overviewPanel(page).locator("dt")).toHaveText([
    "Transactions in doubt Waiting for a person to resolve them",
    "Undoing now oldest 12 min",
  ]);
  await expect(
    page.getByText("Transaction in doubt: WV-TXN-UNDO-STUCK at add"),
  ).toBeVisible();
  scan.clean("Overview");

  // ---- Runs › Needs attention: the four runs in doubt, the called one too.
  await go(page, "Runs");
  await expect(runRows(page)).toHaveCount(11);
  await page.getByRole("checkbox", { name: "Needs attention" }).check();
  await expect(runRows(page)).toHaveCount(4);
  await expect(runRows(page).locator(".cell-run code")).toHaveText(
    [runs.unknown, runs.called, runs.caller, runs.loop].map(short),
  );
  await expect(
    runRows(page).locator(".cell-transaction .status-pill"),
  ).toHaveText(Array(4).fill(corpus.stateWords["in_doubt"]));
  await expect(page).toHaveURL(/\/operate\/runs\?transaction_state=in_doubt$/);
  scan.clean("Runs that need attention");

  // ---- The called run's record: what is still reserved, and the stuck undo.
  await openRun(page, runs.called);
  await expect(page).toHaveURL(new RegExp(`/operate/runs/${runs.called}`));
  await expect(page.locator(".notice.incident p")).toHaveText([
    "This run stopped at add.",
    `${corpus.codes["WV-TXN-UNDO-STUCK"]} Support code: WV-TXN-UNDO-STUCK.`,
  ]);
  await expect(transactionsOf(page).locator(".txn-summary")).toHaveText(
    "1 transaction: 1 in doubt",
  );
  const apply = block(page, "apply");
  await expect(apply.locator(".status-pill")).toHaveText(
    corpus.states["in_doubt"].badge,
  );
  await expect(apply.locator(".txn-meta")).toContainText("Business key T-1029");
  // In doubt reads from the record's stuck step, never from its cause's code.
  await expect(apply.locator(".txn-cause")).toHaveText(
    `In doubt: ${corpus.codes["WV-TXN-UNDO-STUCK"]}`,
  );
  const held = stepOf(apply, "apply", "hold");
  await expect(held.locator(".c-forward")).toContainText("Done");
  await expect(held.locator(".c-forward weave-icon")).toHaveCount(0);
  await expect(held.locator(".c-undo")).toContainText("Still reserved");
  const stuck = stepOf(apply, "apply", "add");
  await expect(stuck.locator(".c-undo")).toContainText(
    corpus.statuses["undo_stuck"]!.word,
  );
  await expect(stuck.locator(".c-undo")).toContainText("3 tries");
  await expect(stuck.locator(".c-key code")).toHaveText(
    `${runs.called}:add#undo`,
  );
  const exported = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export history" }).click();
  expect((await exported).suggestedFilename()).toBe(
    `run-${runs.called}-history.json`,
  );
  await askWeaveAi(runs.called);
  scan.clean("the called run's record");

  // ---- Open incident: the inbox's High priority band, and the case.
  await apply.getByRole("button", { name: "Open incident" }).click();
  await expect(page).toHaveURL(
    new RegExp(`/operate/incidents\\?incident=${ids.called}$`),
  );
  await expect(high(page).locator(".band-count")).toHaveText("4 incidents");
  await expect(high(page)).toContainText(
    "Transactions in doubt. Other systems may not match Weave until someone resolves them.",
  );
  await expect(caseOf(page).locator(".case-head")).toContainText(
    "Resolve first: WV-TXN-UNDO-STUCK in run 9a01c7d2",
  );
  await expect(codes(caseOf(page))).toHaveText([
    "WV-TXN-SETTLE-TIMEOUT",
    "WV-TXN-UNDO-STUCK",
  ]);
  await expect(drawer(page).locator('.notice[data-tone="danger"]')).toHaveText(
    corpus.codes["WV-TXN-UNDO-STUCK"],
  );
  const heldBy = drawer(page).getByRole("region", { name: "Held by" });
  await expect(heldBy).toContainText("move in run 6d18b3f0");
  await expect(heldBy).toContainText(
    "That run waits for this one: resolve this incident first.",
  );
  await askWeaveAi();
  scan.clean("the case");

  // ---- An operator may only retry; the other decisions say what they need.
  const { retry, mark_reconciled, force_end, wait_again, it_happened } =
    corpus.resolutions;
  await expect(offers(page).getByRole("button")).toHaveText([
    retry.label,
    mark_reconciled.label,
    force_end.label,
  ]);
  for (const name of [mark_reconciled.label, force_end.label])
    await expect(offer(page, name)).toBeDisabled();
  await expect(
    offers(page).getByText("Needs the transaction reconciler role."),
  ).toHaveCount(2);
  await decide(
    page,
    retry.label,
    { reason: "The target system is back" },
    (dialogOf) => expect(dialogOf).toContainText(retry.help),
  );
  await expect(page.locator(".toast")).toContainText("Incident resolved.");
  await expect(decision(page)).toHaveText(retry.label);
  await expect(high(page).locator(".band-count")).toHaveText("3 incidents");
  await expect(caseOf(page)).toHaveCount(0);
  scan.clean("Retry the undo");

  // ---- Its caller waits on nothing now: it waits again, and the rest is undone.
  await openIncident(page, "WV-TXN-SETTLE-TIMEOUT", "move");
  await expect(drawer(page).locator("#incident-detail-title")).toHaveText(
    "Incident WV-TXN-SETTLE-TIMEOUT",
  );
  await expect(
    drawer(page).getByRole("region", { name: "Waits on" }),
  ).toHaveCount(0);
  await decide(
    page,
    wait_again.label,
    { reason: "The called run ended" },
    (dialogOf) => expect(dialogOf).toContainText(wait_again.help),
  );
  await expect(decision(page)).toHaveText(wait_again.label);
  await expect(high(page).locator(".band-count")).toHaveText("2 incidents");
  scan.clean("Wait again");

  // ---- The record can't tell which retry it was, so the run says the step's.
  await go(page, "Runs");
  await openRun(page, runs.caller);
  const move = block(page, "move");
  await expect(move.locator(".status-pill")).toHaveText(
    corpus.states["compensated"].badge,
  );
  await expect(move.locator(".txn-cause")).toHaveText(
    corpus.codes["WV-TXN-HOLD-EXPIRED"],
  );
  await expect(stepOf(move, "move", "hold").locator(".c-undo")).toContainText(
    corpus.statuses["cancelled"]!.word,
  );
  await expect(move.locator(".txn-timeline")).toContainText(
    "Resolved: Retry the step",
  );
  await expect(move.locator(".txn-resolution dd")).toHaveText([
    "Retry the step",
    "You · 10:54:00",
    "The called run ended",
  ]);
  scan.clean("the caller's record");

  // ---- A reconciler decides the two that are left, with evidence.
  await platform.as([...reconciler, "lumi.use"]);
  await go(page, "Incidents");
  await openIncident(page, "WV-TXN-CANCEL-STUCK", "hold[3]");
  await decide(
    page,
    mark_reconciled.label,
    {
      reason: "Released by hand in the source system",
      evidence: "OPS-2304",
    },
    async (dialogOf) => {
      await expect(dialogOf).toContainText(mark_reconciled.help);
      await dialogOf.getByRole("radio", { name: /^Undone/ }).check();
      await dialogOf
        .getByRole("checkbox", {
          name: "I confirmed hold[3] was released in the other system.",
        })
        .check();
      await expect(dialogOf).toContainText(
        `Release failed after 10 tries · operation key ${runs.loop}:hold[3]#cancel`,
      );
    },
  );
  await expect(decision(page)).toHaveText("Mark as reconciled as undone");
  await expect(high(page).locator(".band-count")).toHaveText("1 incident");
  scan.clean("Mark as reconciled");

  await openIncident(page, "WV-TXN-PIVOT-UNKNOWN", "submit");
  await decide(
    page,
    it_happened.label,
    { reason: "Found the order in its system", evidence: "Search at 10:52" },
    async (dialogOf) => {
      await expect(dialogOf).toContainText(it_happened.help);
      await dialogOf
        .getByLabel("Result of the step (JSON)")
        .fill('{"order": "O-77310"}');
    },
  );
  await expect(decision(page)).toHaveText(it_happened.label);
  await expect(high(page)).toHaveCount(0);
  scan.clean("It happened");

  // ---- Nothing is in doubt: every page says so.
  await go(page, "Overview");
  await expect(counts).toHaveText(["0", "1"]);
  await expect(
    overviewPanel(page).getByRole("link", {
      name: "Show runs that need attention",
    }),
  ).toHaveCount(0);
  await go(page, "Runs");
  await page.getByRole("checkbox", { name: "Needs attention" }).check();
  await expect(
    page.getByRole("heading", { name: "No runs need attention." }),
  ).toBeVisible();
  scan.clean("Overview again");

  // What each decision sent, and that every kind of place was watched.
  expect(
    platform.sent.map(({ id, revision, body }) => [
      id,
      revision,
      body.kind,
      body.output,
    ]),
  ).toEqual([
    [ids.called, '"1"', "retry_safe", undefined],
    [ids.caller, '"1"', "retry_safe", undefined],
    [
      ids.loop,
      '"1"',
      "accept_reconciled_result",
      {
        as: "compensated",
        participants: [{ instance_key: "hold[3]", outcome: "undone" }],
      },
    ],
    [ids.unknown, '"1"', "accept_reconciled_result", { order: "O-77310" }],
  ]);
  expect(new Set(platform.sent.map(({ body }) => body.receipt_id)).size).toBe(
    4,
  );
  expect(scan.of("file")).toEqual([`run-${runs.called}-history.json`]);
  expect(scan.of("weave ai").filter(Boolean)).toHaveLength(2);
  expect(scan.of("notice")).toContain("Incident resolved.");
  for (const where of ["address", "title", "request"])
    expect(scan.of(where).length, where).toBeGreaterThan(5);
});

test("the scan sees a business key in each place it watches", async ({
  page,
}) => {
  const scan = await KeyScan.on(page);
  await transactionsPlatform(page);
  await page.route("**/lumi/**", (route) => route.fulfill({ json: {} }));
  scan.clean("the page opened");
  const seen = async (where: string, act: () => Promise<unknown>) => {
    await act();
    await expect
      .poll(() => scan.of(where).filter((text) => KEY.test(text)).length)
      .toBeGreaterThan(0);
  };
  await seen("address", () =>
    page.evaluate(() => history.replaceState(null, "", "?key=T-1029")),
  );
  await seen("title", () =>
    page.evaluate(() => (document.title = "Incident T-1029")),
  );
  await seen("notice", () =>
    page.evaluate(() => {
      const notice = document.createElement("span");
      notice.className = "toast-text";
      notice.textContent = "Resolved T-1029.";
      document.body.append(notice);
    }),
  );
  await seen("console", () =>
    page.evaluate(() => console.log("Resolving T-1029")),
  );
  await seen("request", () =>
    page.evaluate(() => fetch("/studio/api/api/v1/runs?key=T-1029")),
  );
  await seen("weave ai", () =>
    page.evaluate(() =>
      fetch("/lumi/ask", { method: "POST", body: "About T-1029" }),
    ),
  );
  await seen("file", () =>
    page.evaluate(() => {
      const link = document.createElement("a");
      link.href = URL.createObjectURL(new Blob(["{}"]));
      link.download = "run-T-1029.json";
      link.click();
    }),
  );
  expect(() => scan.clean("a key in every place")).toThrow(/T-1029/);
});
