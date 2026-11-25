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
// The notice of a run a transaction stopped: it always leads to the incident,
// to the transaction in doubt or to the incidents of its code, whatever could
// be read; and a run held whole offers no cancel the platform would refuse.
import { expect, test, type Page, type Route } from "@playwright/test";
import type { RunTransactionView } from "../../src/app/operate/transactions/transaction-contracts";
import { completed, inDoubt } from "../run-transactions-fixture";
import { held, ids, others, runs } from "../transaction-incidents-fixture";
import { environment } from "./incidents-po";
import {
  operator,
  reconciler,
  transactionsPlatform,
  type TransactionsPlatform,
} from "./operate-transactions-po";

const notice = (page: Page) => page.locator(".notice.incident");
const step = (page: Page, name: string) =>
  notice(page).getByRole("button", { name, exact: true });
const controls = (page: Page) => page.locator(".run-controls");
const section = (page: Page) =>
  page.getByRole("region", { name: "Transactions" });
const heading = (page: Page, key: string) =>
  section(page)
    .getByRole("article", { name: `Transaction ${key}` })
    .getByRole("heading");
const inbox = async (page: Page, query: string) => {
  await expect(
    page.getByRole("heading", { level: 1, name: "Incidents" }),
  ).toBeVisible();
  await expect(page).toHaveURL(
    (url) => url.pathname + url.search === `/operate/incidents?${query}`,
  );
};
/** Opens a run; `answer` answers its transactions, or the platform does. */
async function openRun(
  page: Page,
  id: string,
  options: {
    capabilities?: readonly string[];
    answer?: (route: Route) => Promise<void> | void;
  } = {},
): Promise<TransactionsPlatform> {
  const platform = await transactionsPlatform(page, {
    capabilities: options.capabilities ?? reconciler,
  });
  if (options.answer)
    await page.route(
      `${environment}/runs/${id}/transactions?*`,
      options.answer,
    );
  await page.goto(`/operate/runs/${id}`);
  await expect(notice(page)).toBeVisible();
  return platform;
}

test.use({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC" });

test("a run in doubt leads from its notice to the transaction and to its incident", async ({
  page,
}) => {
  await openRun(page, runs.called);
  await expect(notice(page).getByRole("button")).toHaveText([
    "Open incident",
    "Go to the transaction",
  ]);
  await step(page, "Go to the transaction").click();
  await expect(heading(page, "apply")).toBeFocused();
  await expect(heading(page, "apply")).toBeInViewport();
  // The line of the action row doesn't repeat what the notice offers.
  await expect(page.locator(".cancel-blocked")).toContainText(
    "Cancel without undoing: a transaction is in doubt. Resolve it from its incident first.",
  );
  await expect(page.locator(".cancel-blocked").getByRole("button")).toHaveCount(
    0,
  );
  await step(page, "Open incident").click();
  await inbox(page, `incident=${ids.called}`);
});

test("the notice is reached before the action row, and the action row before the transactions", async ({
  page,
}) => {
  await openRun(page, runs.called);
  await expect(step(page, "Open incident")).toBeVisible();
  const order = await page.evaluate(() =>
    [".notice.incident button", ".run-controls button", ".txn-block button"]
      .map((selector) => document.querySelector(selector)!)
      .map(
        (element, index, all) =>
          index === 0 ||
          !!(
            all[index - 1].compareDocumentPosition(element) &
            Node.DOCUMENT_POSITION_FOLLOWING
          ),
      ),
  );
  expect(order).toEqual([true, true, true]);
});

test("of several transactions in doubt, the notice leads to the first, and says so once all are listed", async ({
  page,
}) => {
  const second: RunTransactionView = {
    ...inDoubt,
    instance_key: "move[4]",
    iteration: [4],
    incident_id: ids.caller,
  };
  const items = [
    completed,
    { ...inDoubt, incident_id: ids.loop },
    second,
  ] as RunTransactionView[];
  let next: string | null = "more";
  await openRun(page, runs.loop, {
    answer: (route) => route.fulfill({ json: { items, next_cursor: next } }),
  });
  await expect(notice(page).getByRole("button")).toHaveText([
    "Open incident",
    "Go to the transaction",
  ]);
  next = null;
  await page.reload();
  await expect(notice(page).getByRole("button")).toHaveText([
    "Open first incident",
    "Go to the first transaction in doubt",
  ]);
  await step(page, "Go to the first transaction in doubt").click();
  await expect(heading(page, "move[3]")).toBeFocused();
  await step(page, "Open first incident").click();
  await inbox(page, `incident=${ids.loop}`);
});

for (const [what, answer] of [
  [
    "lies past the first 50",
    (route: Route) =>
      route.fulfill({
        json: {
          items: Array.from({ length: 50 }, (_, index) => ({
            ...completed,
            instance_key: `move[${index + 100}]`,
          })),
          next_cursor: "more",
        },
      }),
  ],
  [
    "couldn't be read",
    (route: Route) =>
      route.fulfill({
        status: 503,
        json: { code: "WV-UNAVAILABLE", message: "Down" },
      }),
  ],
  [
    "may not be shown",
    (route: Route) =>
      route.fulfill({
        status: 403,
        json: { code: "WV-FORBIDDEN", message: "Forbidden" },
      }),
  ],
] as const)
  test(`the notice leads to its code's incidents when the transaction in doubt ${what}`, async ({
    page,
  }) => {
    await openRun(page, runs.called, { answer });
    await expect(notice(page).getByRole("button")).toHaveText([
      "Show in Incidents",
    ]);
    await step(page, "Show in Incidents").click();
    await inbox(page, "code=WV-TXN-UNDO-STUCK");
    await expect(
      page.getByRole("button", {
        name: "Open incident WV-TXN-UNDO-STUCK at add",
      }),
    ).toBeVisible();
  });

test("the notice keeps its height while its transactions are read, and what it offers through a failed read", async ({
  page,
}) => {
  let release: ((route: Route) => void) | null = null;
  const waiting: Route[] = [];
  const answers = (route: Route) => {
    if (release) release(route);
    else waiting.push(route);
  };
  await openRun(page, runs.called, { answer: answers });
  await expect.poll(() => waiting.length).toBe(1);
  await expect(notice(page).getByRole("button")).toHaveCount(0);
  const before = (await notice(page).boundingBox())!.height;
  release = (route) => void route.fallback();
  await waiting[0].fallback();
  await expect(step(page, "Open incident")).toBeVisible();
  expect((await notice(page).boundingBox())!.height).toBe(before);
  // The run goes on: its transactions are read again, and that read fails.
  let failed = 0;
  release = (route) => {
    failed++;
    void route.fulfill({
      status: 503,
      json: { code: "WV-UNAVAILABLE", message: "Down" },
    });
  };
  await page.clock.runFor(2_500);
  await expect.poll(() => failed).toBeGreaterThan(0);
  await expect(section(page).getByRole("alert")).toBeVisible();
  await expect(notice(page).getByRole("button")).toHaveText([
    "Open incident",
    "Go to the transaction",
  ]);
});

test("a run held whole leads to its incident, and offers no cancel the platform would refuse", async ({
  page,
}) => {
  const platform = await transactionsPlatform(page, {
    capabilities: reconciler,
    items: [held, ...others],
  });
  platform.runs = [{ ...platform.runs[5], id: runs.held }, ...platform.runs];
  platform.transactions[runs.held] = [
    { ...completed, state: "open", ended_at: undefined, decided_at: undefined },
  ];
  const sent: string[] = [];
  await page.route(`${environment}/runs/*/cancel`, (route) => {
    sent.push(route.request().url());
    return route.abort();
  });
  await page.goto(`/operate/runs/${runs.held}`);
  await expect(notice(page)).toContainText("This run stopped.");
  await expect(notice(page).getByRole("button")).toHaveText([
    "Show in Incidents",
  ]);
  const why =
    "This run is held, so it can't be canceled. Force end it from its incident.";
  await expect(page.locator(".cancel-blocked")).toHaveText(why);
  for (const name of ["Cancel run", "Cancel without undoing"]) {
    const button = controls(page).getByRole("button", { name, exact: true });
    await expect(button).toHaveAttribute("aria-disabled", "true");
    await expect(button).toHaveAccessibleDescription(why);
    await button.focus();
    await expect(button).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toHaveCount(0);
  }
  expect(sent).toEqual([]);
  await step(page, "Show in Incidents").click();
  await inbox(page, "code=WV-TXN-RUN-OVERSIZED");
  await expect(
    page.getByRole("button", {
      name: "Open incident WV-TXN-RUN-OVERSIZED at the run",
    }),
  ).toBeVisible();
});

test("a held run says nothing of a cancel to a person who can't cancel it", async ({
  page,
}) => {
  const platform = await transactionsPlatform(page, {
    capabilities: operator.filter((capability) => capability !== "run.cancel"),
    items: [held, ...others],
  });
  platform.runs = [{ ...platform.runs[5], id: runs.held }, ...platform.runs];
  await page.goto(`/operate/runs/${runs.held}`);
  await expect(step(page, "Show in Incidents")).toBeVisible();
  await expect(page.locator(".cancel-blocked")).toHaveCount(0);
});

test("a run that ended keeps its notice's words, with nowhere to lead", async ({
  page,
}) => {
  const platform = await transactionsPlatform(page, {
    capabilities: reconciler,
    items: [...others],
  });
  platform.run(runs.called, { status: "failed", active_incidents: 0 });
  await page.route(`${environment}/runs/${runs.called}`, async (route) => {
    await route.fulfill({
      json: {
        id: runs.called,
        activation: { id: "act", name: "add-quantity", revision: 1 },
        state: {
          status: "failed",
          active: [],
          input: {},
          incident: "WV-TXN-ROLLED-BACK",
          incidents: {},
        },
      },
    });
  });
  await page.goto(`/operate/runs/${runs.called}`);
  await expect(notice(page)).toContainText(
    "The transaction was undone. Support code: WV-TXN-ROLLED-BACK.",
  );
  await expect(section(page)).toBeVisible();
  await expect(notice(page).getByRole("button")).toHaveCount(0);
});
