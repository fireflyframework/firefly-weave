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
// Operate › Runs › a run with transactions: Cancel run says Weave undoes
// first, and stops before sending while a transaction is in doubt; Cancel
// without undoing lists what stays done in other systems and asks for the
// run's short ID, and waits in the action row while a transaction is in
// doubt. The platform is mocked with runs.transactions and runs.cancel.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page, type Route } from "@playwright/test";
import type { RunTransactionView } from "../../src/app/operate/transactions/transaction-contracts";
import {
  completed,
  inDoubt,
  inDoubtIncident,
  loopRunId,
} from "../run-transactions-fixture";
import { connected } from "./support";

const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";
const captures = resolve(
  process.env["STUDIO_OPERATE_CAPTURES"]?.trim() || "test-results/operate",
);
mkdirSync(captures, { recursive: true });
const operator = ["run.read", "catalog.read", "incident.read", "run.cancel"];
const reconciler = [...operator, "transaction.reconcile"];
const short = loopRunId.slice(0, 8);

/** A transaction still going: hold reserved, add running, submit not started. */
const running: RunTransactionView = {
  ...completed,
  instance_key: "move[6]",
  state: "open",
  ended_at: undefined,
  decided_at: undefined,
  participants: [
    { ...completed.participants[0], instance_key: "hold[6]", status: "done" },
    {
      ...completed.participants[1],
      instance_key: "add[6]",
      status: "dispatched",
    },
    {
      ...completed.participants[2],
      instance_key: "submit[6]",
      status: "pending",
    },
  ],
};

interface Platform {
  transactions: RunTransactionView[];
  posts: unknown[];
  /** Answers runs.cancel; the default accepts it. */
  cancel: (route: Route) => Promise<void>;
}
async function platform(
  page: Page,
  capabilities: string[],
  transactions: RunTransactionView[],
): Promise<Platform> {
  await connected(page, { capabilities });
  const result: Platform = {
    transactions,
    posts: [],
    cancel: (route) => route.fulfill({ json: { id: loopRunId } }),
  };
  await page.route(`${environment}/runs/*`, (route) => {
    const id = new URL(route.request().url()).pathname.split("/").pop()!;
    return route.fulfill({
      json: {
        id,
        business_key: null,
        correlation_key: null,
        activation: { id: "act", name: "move-each", revision: 1 },
        state: { status: "running", active: [], input: {} },
      },
    });
  });
  await page.route(`${environment}/runs/*/lifecycle`, (route) =>
    route.fulfill({ json: { archived: false, purged: false, revision: 1 } }),
  );
  await page.route(`${environment}/runs/*/history?*`, (route) =>
    route.fulfill({ json: { events: [], next_cursor: null } }),
  );
  // Pages of 50, as Studio asks for them; the cursor is where the next starts.
  await page.route(`${environment}/runs/*/transactions?*`, (route) => {
    const url = new URL(route.request().url());
    const at = Number(url.searchParams.get("cursor") ?? 0);
    const end = at + 50;
    return route.fulfill({
      json: {
        items: result.transactions.slice(at, end),
        next_cursor: end < result.transactions.length ? String(end) : null,
      },
    });
  });
  await page.route(`${environment}/runs/*/cancel`, async (route) => {
    result.posts.push(route.request().postDataJSON());
    await result.cancel(route);
  });
  await page.goto(`/operate/runs/${loopRunId}`);
  return result;
}
const dialog = (page: Page, name: string) => page.getByRole("dialog", { name });
const actionRow = (page: Page) => page.locator(".run-controls");

test.use({ viewport: { width: 1440, height: 900 } });

test("Cancel run says Weave undoes an unfinished transaction first, then that it is undoing", async ({
  page,
}) => {
  const runs = await platform(page, operator, [running]);
  await expect(page.locator(".txn-summary")).toBeVisible();
  runs.cancel = (route) =>
    route.fulfill({ json: { id: loopRunId, pending_undo: true } });
  await actionRow(page).getByRole("button", { name: "Cancel run" }).click();
  const cancel = dialog(page, "Cancel this run?");
  await expect(cancel).toContainText(
    "This run has a transaction that isn't finished. Weave undoes what it did before the run ends. Give a reason for the audit log.",
  );
  await expect(
    cancel.getByRole("button", { name: "Keep running" }),
  ).toBeVisible();
  await page.screenshot({ path: `${captures}/run-cancel-undo-first.png` });
  await cancel.getByLabel("Reason").fill("The target system is down");
  await cancel.getByRole("button", { name: "Cancel run" }).click();
  await expect(page.locator(".toast")).toContainText(
    "Canceling: undoing what this run's transactions did.",
  );
  expect(runs.posts).toEqual([{ reason: "The target system is down" }]);
  // Without transaction.reconcile, Cancel without undoing isn't offered.
  await expect(
    actionRow(page).getByRole("button", { name: "Cancel without undoing" }),
  ).toHaveCount(0);
});

test("Cancel run with a transaction in doubt says so before sending, and sends nothing", async ({
  page,
}) => {
  const runs = await platform(page, operator, [running, inDoubt]);
  await expect(page.locator(".txn-summary")).toBeVisible();
  await actionRow(page).getByRole("button", { name: "Cancel run" }).click();
  const cancel = dialog(page, "Cancel this run?");
  const notice = cancel.locator(".notice");
  await expect(notice).toHaveAttribute("data-tone", "danger");
  await expect(notice).toContainText(
    "A transaction is in doubt. Resolve the transaction first.",
  );
  await expect(
    cancel.getByRole("button", { name: "Cancel run" }),
  ).toBeDisabled();
  await page.screenshot({ path: `${captures}/run-cancel-in-doubt.png` });
  await cancel.getByLabel("Reason").fill("Stop it");
  await cancel.getByLabel("Reason").press("Control+Enter");
  expect(runs.posts).toEqual([]);
  await notice.getByRole("button", { name: "Open incident" }).click();
  await expect(page).toHaveURL(
    new RegExp(`/operate/incidents\\?incident=${inDoubtIncident}$`),
  );
  expect(runs.posts).toEqual([]);
});

test("a cancel refused because a transaction went in doubt names it and offers its incident", async ({
  page,
}) => {
  const runs = await platform(page, operator, [running]);
  await expect(page.locator(".txn-summary")).toBeVisible();
  runs.cancel = async (route) => {
    runs.transactions = [inDoubt];
    await route.fulfill({
      status: 409,
      json: {
        code: "WV-TXN-IN-DOUBT",
        message: "Resolve the transaction first.",
      },
    });
  };
  await actionRow(page).getByRole("button", { name: "Cancel run" }).click();
  const cancel = dialog(page, "Cancel this run?");
  await cancel.getByLabel("Reason").fill("Stop it");
  await cancel.getByRole("button", { name: "Cancel run" }).click();
  const toast = page.locator(".toast").filter({ hasText: "WV-TXN-IN-DOUBT" });
  await expect(toast).toContainText(
    `Run ${short} was not canceled. Resolve the transaction first. Support code: WV-TXN-IN-DOUBT.`,
  );
  await page.screenshot({ path: `${captures}/run-cancel-refused.png` });
  await toast.getByRole("button", { name: "Open incident" }).click();
  await expect(page).toHaveURL(
    new RegExp(`/operate/incidents\\?incident=${inDoubtIncident}$`),
  );
});

test("Cancel without undoing waits in the action row while a transaction is in doubt", async ({
  page,
}) => {
  await platform(page, reconciler, [running, inDoubt]);
  const button = actionRow(page).getByRole("button", {
    name: "Cancel without undoing",
  });
  await expect(button).toHaveAttribute("aria-disabled", "true");
  await button.focus();
  await expect(button).toBeFocused();
  await expect(button).toHaveAccessibleDescription(
    "Cancel without undoing: a transaction is in doubt. Resolve it from its incident first.",
  );
  await page.screenshot({
    path: `${captures}/run-cancel-without-undoing-blocked.png`,
  });
  // Pressed from the keyboard, it opens nothing: a dialog's backdrop would stop the click below.
  await page.keyboard.press("Enter");
  await expect(dialog(page, "Cancel without undoing?")).toHaveCount(0);
  await page
    .locator(".cancel-blocked")
    .getByRole("button", { name: "Open incident" })
    .click();
  await expect(page).toHaveURL(
    new RegExp(`/operate/incidents\\?incident=${inDoubtIncident}$`),
  );
});

test("Cancel without undoing lists what stays done and needs the run's short ID", async ({
  page,
}) => {
  const runs = await platform(page, reconciler, [completed, running]);
  const button = actionRow(page).getByRole("button", {
    name: "Cancel without undoing",
  });
  await expect(button).not.toHaveAttribute("aria-disabled", "true");
  await button.click();
  const cancel = dialog(page, "Cancel without undoing?");
  await expect(cancel).toContainText(
    "Steps already done stay done in other systems: Weave won't undo them, and the run's transactions end Force ended.",
  );
  await expect(cancel.locator(".stays-done li")).toHaveText([
    /hold\[6\]\s*· Reserved: Weave won't release it/,
    /add\[6\]\s*· Reversible: still running and may finish; Weave won't undo it/,
  ]);
  const reason = cancel.getByLabel("Reason");
  // The fields share the title's left edge.
  const [title, field] = await Promise.all([
    cancel
      .getByRole("heading", { name: "Cancel without undoing?" })
      .boundingBox(),
    reason.boundingBox(),
  ]);
  expect(Math.abs(title!.x - field!.x)).toBeLessThanOrEqual(1);
  await expect(cancel.locator(".reason-count")).toHaveText("0 / 2,000");
  await reason.fill("The target system is down until tomorrow");
  await expect(cancel.locator(".reason-count")).toHaveText("40 / 2,000");
  const typed = cancel.getByLabel(`Type ${short} to confirm`);
  await expect(typed).toHaveValue("");
  const confirm = cancel.getByRole("button", {
    name: "Cancel without undoing",
  });
  await expect(confirm).toBeDisabled();
  for (const wrong of [short.slice(0, 7), loopRunId, "cancel"]) {
    await typed.fill(wrong);
    await expect(confirm).toBeDisabled();
  }
  await typed.press("Enter");
  expect(runs.posts).toEqual([]);
  await typed.fill(` ${short.toUpperCase().slice(0, 4)} ${short.slice(4)} `);
  await expect(confirm).toBeEnabled();
  await page.screenshot({ path: `${captures}/run-cancel-without-undoing.png` });
  await confirm.dblclick();
  await expect(page.locator(".toast")).toContainText(
    "Run canceled without undoing. Steps already done stay done in other systems.",
  );
  expect(runs.posts).toEqual([
    { reason: "The target system is down until tomorrow", skip_undo: true },
  ]);
});

test("Cancel without undoing is offered past the first 50 transactions, and lists what stays done there", async ({
  page,
}) => {
  const earlier = Array.from({ length: 50 }, (_, index) => ({
    ...completed,
    instance_key: `move[${index + 100}]`,
  }));
  await platform(page, reconciler, [...earlier, running]);
  await expect(page.locator(".txn-summary")).toBeVisible();
  const button = actionRow(page).getByRole("button", {
    name: "Cancel without undoing",
  });
  await expect(button).not.toHaveAttribute("aria-disabled", "true");
  await button.click();
  await expect(
    dialog(page, "Cancel without undoing?").locator(".stays-done li"),
  ).toHaveText([
    /hold\[6\]\s*· Reserved: Weave won't release it/,
    /add\[6\]\s*· Reversible: still running and may finish; Weave won't undo it/,
  ]);
});

test("Cancel without undoing isn't offered without an unfinished transaction", async ({
  page,
}) => {
  await platform(page, reconciler, [completed]);
  await expect(page.locator(".txn-summary")).toBeVisible();
  await expect(
    actionRow(page).getByRole("button", { name: "Cancel run" }),
  ).toBeVisible();
  await expect(
    actionRow(page).getByRole("button", { name: "Cancel without undoing" }),
  ).toHaveCount(0);
});
