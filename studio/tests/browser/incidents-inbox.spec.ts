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
// Operate › Incidents: active incidents first, a drawer with the run's last
// engine events, and the Resolve incident dialog with its error paths. The
// server checks incident.resolve, the revision and the receipt.
import { test, expect, Page, Route } from "@playwright/test";
import { connected } from "./support";

const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";
const active = "91111111-1111-4111-8111-111111111111";
const resolved = "92222222-2222-4222-8222-222222222222";
const runId = "5e6f7a8b-0000-4000-8000-000000000001";
const uuid =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const incident = (id: string, overrides: Record<string, unknown> = {}) => ({
  id,
  run_id: runId,
  incident_key: "charge:1",
  node_id: "charge",
  generation: 1,
  origin_code: "WV-TASK-AMBIGUOUS",
  code: "WV-TASK-AMBIGUOUS",
  status: "active",
  revision: 3,
  external_effects_may_continue: false,
  ...overrides,
});

async function inbox(page: Page, capabilities: string[]) {
  await connected(page, { capabilities });
  const items: Record<string, unknown>[] = [
    incident(active),
    incident(resolved, {
      status: "resolved",
      code: "WV-TASK-FAILED",
      origin_code: "WV-TASK-FAILED",
      node_id: "notify",
      revision: 4,
      resolved_at: new Date().toISOString(),
      resolution: {
        receipt_id: "93333333-3333-4333-8333-333333333333",
        kind: "terminate",
        reason: "Customer withdrew the order",
        output: null,
        evidence_reference: null,
      },
      external_effects_may_continue: true,
    }),
  ];
  let lists = 0;
  await page.route(`${environment}/incidents?*`, (r) => {
    lists++;
    return r.fulfill({ json: { items, next_cursor: null } });
  });
  await page.route(`${environment}/runs/${runId}/history?*`, (r) => {
    const cursor = new URL(r.request().url()).searchParams.get("cursor");
    const first = cursor ? 101 : 1;
    const count = cursor ? 25 : 100;
    return r.fulfill({
      json: {
        run_id: runId,
        events: Array.from({ length: count }, (_, i) => ({
          id: `event-${first + i}`,
          type: first + i === 125 ? "incident_opened" : "task_scheduled",
          sequence: first + i,
          timestamp: new Date().toISOString(),
          data: { secret: "never shown" },
        })),
        next_cursor: cursor ? null : "next",
        high_water_sequence: 125,
      },
    });
  });
  return { items, lists: () => lists };
}
const rows = (page: Page) =>
  page.getByRole("table", { name: "Incidents" }).locator(".resource-row");
/** Opens the active incident's drawer and its Resolve incident dialog. */
async function openDialog(page: Page) {
  await page
    .getByRole("button", {
      name: "Open incident WV-TASK-AMBIGUOUS at charge",
    })
    .click();
  await page
    .getByRole("button", { name: "Resolve incident", exact: true })
    .click();
  return page.getByRole("dialog", { name: "Resolve incident" });
}
/** Chooses End the run with a reason and sends it. */
async function endTheRun(page: Page, reason = "Order canceled upstream") {
  const dialog = page.getByRole("dialog", { name: "Resolve incident" });
  await dialog.getByLabel(/End the run/).check();
  await dialog.getByLabel("Reason").fill(reason);
  await dialog
    .getByRole("button", { name: "Resolve incident", exact: true })
    .click();
}
/** Lets an answer held back by a test arrive, and the page handle it. */
async function settle(page: Page, url: string, release: () => void) {
  const answered = page.waitForResponse((response) =>
    response.url().endsWith(url),
  );
  release();
  await answered;
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => setTimeout(resolve, 100)),
      ),
  );
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("shows active incidents first, with every status a filter away", async ({
      page,
    }) => {
      await inbox(page, ["incident.read"]);
      await page
        .getByRole("button", { name: "Incidents", exact: true })
        .click();
      await expect(page).toHaveURL(/\/operate\/incidents$/);
      await expect(rows(page)).toHaveCount(1);
      await expect(rows(page).first()).toContainText("WV-TASK-AMBIGUOUS");
      await expect(rows(page).first()).toContainText("Active");
      await page.getByLabel("Status").selectOption("all");
      await expect(page).toHaveURL(/\/operate\/incidents\?status=all$/);
      await expect(rows(page)).toHaveCount(2);
      await expect(rows(page).nth(1)).toContainText("Resolved");
      await expect(
        rows(page).nth(1).getByText("Effects may continue"),
      ).toBeAttached();
      await page.getByLabel("Code").selectOption("WV-TASK-FAILED");
      await expect(rows(page)).toHaveCount(1);
      await page.reload();
      await expect(page.getByLabel("Code")).toHaveValue("WV-TASK-FAILED");
    });

    test("the drawer shows the run's last 20 engine events, types only", async ({
      page,
    }) => {
      await inbox(page, ["incident.read", "run.read"]);
      await page.goto("/operate/incidents");
      await page
        .getByRole("button", {
          name: "Open incident WV-TASK-AMBIGUOUS at charge",
        })
        .click();
      const drawer = page.locator("weave-incident-drawer");
      const events = drawer.locator(".event-lines li");
      await expect(events).toHaveCount(20);
      await expect(events.first()).toContainText("#106");
      await expect(events.last()).toContainText("#125");
      await expect(events.last()).toContainText("incident_opened");
      await expect(drawer).not.toContainText("never shown");
      // A reader without incident.resolve gets no Resolve incident.
      await expect(
        drawer.getByRole("button", { name: "Resolve incident", exact: true }),
      ).toHaveCount(0);
    });

    test("Retry the step sends the revision and a new receipt", async ({
      page,
    }) => {
      const { items } = await inbox(page, [
        "incident.read",
        "incident.resolve",
        "run.read",
      ]);
      const sent: { body: Record<string, unknown>; ifMatch?: string }[] = [];
      await page.route(`${environment}/incidents/${active}/resolve`, (r) => {
        const body = r.request().postDataJSON();
        sent.push({ body, ifMatch: r.request().headers()["if-match"] });
        items[0] = {
          ...items[0],
          status: "resolved",
          revision: 4,
          resolution: { ...body, output: null, evidence_reference: null },
          resolved_at: new Date().toISOString(),
        };
        return r.fulfill({ json: items[0] });
      });
      await page.goto("/operate/incidents");
      await page
        .getByRole("button", {
          name: "Open incident WV-TASK-AMBIGUOUS at charge",
        })
        .click();
      await page
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      const dialog = page.getByRole("dialog", { name: "Resolve incident" });
      await dialog
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      await expect(dialog.getByText("Choose a decision.")).toBeVisible();
      await expect(
        dialog.getByText("Enter a reason for the audit log."),
      ).toBeVisible();
      await dialog.getByLabel(/Retry the step/).check();
      await dialog.getByLabel("Reason").fill("Provider confirmed nothing ran");
      await dialog
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      await expect(page.getByText("Incident resolved.")).toBeVisible();
      await expect(dialog).toHaveCount(0);
      expect(sent).toHaveLength(1);
      expect(sent[0].ifMatch).toBe('"3"');
      expect(sent[0].body).toMatchObject({
        kind: "retry_safe",
        reason: "Provider confirmed nothing ran",
      });
      expect(String(sent[0].body["receipt_id"])).toMatch(uuid);
      expect(sent[0].body).not.toHaveProperty("output");
      const drawer = page.locator("weave-incident-drawer");
      await expect(drawer.locator(".badges")).toContainText("Resolved");
      await expect(drawer).toContainText("Retry the step");
      // The dialog's button is gone: the incident's title has the focus.
      await expect(drawer.locator("#incident-detail-title")).toBeFocused();
    });

    test("Accept a verified result needs evidence and a JSON result", async ({
      page,
    }) => {
      await inbox(page, ["incident.read", "incident.resolve"]);
      const bodies: Record<string, unknown>[] = [];
      let refuse = true;
      await page.route(`${environment}/incidents/${active}/resolve`, (r) => {
        bodies.push(r.request().postDataJSON());
        if (refuse) {
          refuse = false;
          return r.fulfill({
            status: 422,
            json: {
              code: "WV-RUNTIME-OUTPUT",
              message: "Reconciled output violates the pinned schema",
            },
          });
        }
        return r.fulfill({
          json: incident(active, { status: "resolved", revision: 4 }),
        });
      });
      await page.goto("/operate/incidents");
      await page
        .getByRole("button", {
          name: "Open incident WV-TASK-AMBIGUOUS at charge",
        })
        .click();
      await page
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      const dialog = page.getByRole("dialog", { name: "Resolve incident" });
      await dialog.getByLabel(/Accept a verified result/).check();
      await dialog.getByLabel("Reason").fill("Provider receipt shows success");
      await dialog.getByLabel("Verified result (JSON)").fill("{oops");
      await dialog
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      await expect(
        dialog.getByText("Enter where the verified result can be checked."),
      ).toBeVisible();
      await expect(
        dialog.getByText("Enter the verified result as JSON"),
      ).toBeVisible();
      expect(bodies).toHaveLength(0);
      await dialog
        .getByLabel("Evidence reference")
        .fill("provider:receipt:123");
      await dialog.getByLabel("Verified result (JSON)").fill('{"charged": 42}');
      await dialog
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      await expect(
        dialog.getByText("Reconciled output violates the pinned schema"),
      ).toBeVisible();
      await dialog
        .getByLabel("Verified result (JSON)")
        .fill('{"charged": true}');
      await dialog
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      await expect(page.getByText("Incident resolved.")).toBeVisible();
      expect(bodies[1]).toMatchObject({
        kind: "accept_reconciled_result",
        evidence_reference: "provider:receipt:123",
        output: { charged: true },
      });
      // A changed decision is a new decision: it gets its own receipt.
      expect(bodies[1]["receipt_id"]).not.toBe(bodies[0]["receipt_id"]);
    });

    test("the decisions fit the dialog without scrolling sideways", async ({
      page,
    }) => {
      await inbox(page, ["incident.read", "incident.resolve"]);
      await page.goto("/operate/incidents");
      const dialog = await openDialog(page);
      await dialog.getByLabel(/Accept a verified result/).check();
      await expect(dialog.getByLabel("Verified result (JSON)")).toBeVisible();
      const fit = await dialog.evaluate((panel) => {
        const radio = panel.querySelector<HTMLElement>(
          ".resolve-choice input",
        )!;
        return {
          sideways: panel.scrollWidth - panel.clientWidth,
          radio: radio.getBoundingClientRect().width,
        };
      });
      expect(fit.sideways).toBeLessThanOrEqual(1);
      // The radio is as wide as itself, leaving the room to its words.
      expect(fit.radio).toBeLessThan(30);
    });

    test("a changed incident explains WV-INCIDENT-REVISION and decides again", async ({
      page,
    }) => {
      const { items } = await inbox(page, [
        "incident.read",
        "incident.resolve",
      ]);
      const matches: string[] = [];
      await page.route(`${environment}/incidents/${active}/resolve`, (r) => {
        const match = r.request().headers()["if-match"];
        matches.push(match);
        if (match === '"3"') {
          items[0] = { ...items[0], revision: 5 };
          return r.fulfill({
            status: 409,
            json: {
              code: "WV-INCIDENT-REVISION",
              message: "Incident revision changed",
            },
          });
        }
        return r.fulfill({
          json: incident(active, { status: "resolved", revision: 6 }),
        });
      });
      await page.goto("/operate/incidents");
      await page
        .getByRole("button", {
          name: "Open incident WV-TASK-AMBIGUOUS at charge",
        })
        .click();
      await page
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      const dialog = page.getByRole("dialog", { name: "Resolve incident" });
      await dialog.getByLabel(/End the run/).check();
      await dialog.getByLabel("Reason").fill("Order canceled upstream");
      await dialog
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      await expect(dialog.getByRole("alert")).toContainText(
        "Someone changed this incident since you opened it.",
      );
      await expect(dialog.getByRole("alert")).toContainText(
        "Support code: WV-INCIDENT-REVISION",
      );
      await dialog.getByRole("button", { name: "Refresh incident" }).click();
      await expect
        .poll(() =>
          page
            .locator("weave-incident-drawer")
            .locator("dd")
            .filter({ hasText: /^5$/ })
            .count(),
        )
        .toBe(1);
      await dialog
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      await expect(page.getByText("Incident resolved.")).toBeVisible();
      expect(matches).toEqual(['"3"', '"5"']);
    });

    test("an inactive incident explains WV-RUNTIME-STATE", async ({ page }) => {
      await inbox(page, ["incident.read", "incident.resolve"]);
      await page.route(`${environment}/incidents/${active}/resolve`, (r) =>
        r.fulfill({
          status: 409,
          json: {
            code: "WV-RUNTIME-STATE",
            message: "Incident is no longer active",
          },
        }),
      );
      await page.goto("/operate/incidents");
      await page
        .getByRole("button", {
          name: "Open incident WV-TASK-AMBIGUOUS at charge",
        })
        .click();
      await page
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      const dialog = page.getByRole("dialog", { name: "Resolve incident" });
      await dialog.getByLabel(/End the run/).check();
      await dialog.getByLabel("Reason").fill("Order canceled upstream");
      await dialog
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      await expect(dialog.getByRole("alert")).toContainText(
        "Incident is no longer active",
      );
      await expect(dialog.getByRole("alert")).toContainText(
        "Support code: WV-RUNTIME-STATE",
      );
    });

    test("a lost answer is sent again with the same receipt", async ({
      page,
    }) => {
      await inbox(page, ["incident.read", "incident.resolve"]);
      const receipts: unknown[] = [];
      let lose = true;
      await page.route(`${environment}/incidents/${active}/resolve`, (r) => {
        receipts.push(r.request().postDataJSON()["receipt_id"]);
        if (lose) {
          lose = false;
          return r.abort("connectionreset");
        }
        return r.fulfill({
          json: incident(active, { status: "resolved", revision: 4 }),
        });
      });
      await page.goto("/operate/incidents");
      await page
        .getByRole("button", {
          name: "Open incident WV-TASK-AMBIGUOUS at charge",
        })
        .click();
      await page
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      const dialog = page.getByRole("dialog", { name: "Resolve incident" });
      await dialog.getByLabel(/End the run/).check();
      await dialog.getByLabel("Reason").fill("Order canceled upstream");
      await dialog
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      await expect(dialog.getByRole("alert")).toContainText(
        "Studio didn't get an answer",
      );
      await dialog
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      await expect(page.getByText("Incident resolved.")).toBeVisible();
      expect(receipts).toHaveLength(2);
      expect(receipts[1]).toBe(receipts[0]);
    });

    test("without incident.read the entry hides and the page names the access", async ({
      page,
    }) => {
      await connected(page, { capabilities: ["run.read"] });
      await expect(
        page.getByRole("button", { name: "Incidents", exact: true }),
      ).toHaveCount(0);
      await page.goto("/operate/incidents");
      await expect(page.locator("weave-operate-state")).toContainText(
        "You need incident.read in this environment.",
      );
    });
    test("a changed decision after a lost answer gets its own receipt", async ({
      page,
    }) => {
      await inbox(page, ["incident.read", "incident.resolve"]);
      const receipts: unknown[] = [];
      let lose = true;
      await page.route(`${environment}/incidents/${active}/resolve`, (r) => {
        receipts.push(r.request().postDataJSON()["receipt_id"]);
        if (lose) {
          lose = false;
          return r.abort("connectionreset");
        }
        return r.fulfill({
          json: incident(active, { status: "resolved", revision: 4 }),
        });
      });
      await page.goto("/operate/incidents");
      const dialog = await openDialog(page);
      await endTheRun(page);
      await expect(dialog.getByRole("alert")).toContainText(
        "Studio didn't get an answer",
      );
      // A different reason is a different decision: reusing the receipt
      // would make the platform answer WV-INCIDENT-RECEIPT-CONFLICT.
      await endTheRun(page, "Order canceled by the customer");
      await expect(page.getByText("Incident resolved.")).toBeVisible();
      expect(receipts).toHaveLength(2);
      expect(String(receipts[1])).toMatch(uuid);
      expect(receipts[1]).not.toBe(receipts[0]);
    });

    test("a lost answer keeps its receipt when the dialog opens again", async ({
      page,
    }) => {
      await inbox(page, ["incident.read", "incident.resolve"]);
      const receipts: unknown[] = [];
      let lose = true;
      await page.route(`${environment}/incidents/${active}/resolve`, (r) => {
        receipts.push(r.request().postDataJSON()["receipt_id"]);
        if (lose) {
          lose = false;
          return r.abort("connectionreset");
        }
        return r.fulfill({
          json: incident(active, { status: "resolved", revision: 4 }),
        });
      });
      await page.goto("/operate/incidents");
      const dialog = await openDialog(page);
      await endTheRun(page);
      await expect(dialog.getByRole("alert")).toContainText(
        "Studio didn't get an answer",
      );
      await dialog.getByRole("button", { name: "Cancel" }).click();
      await expect(dialog).toHaveCount(0);
      await page
        .locator("weave-incident-drawer")
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      await endTheRun(page);
      await expect(page.getByText("Incident resolved.")).toBeVisible();
      expect(receipts).toHaveLength(2);
      expect(receipts[1]).toBe(receipts[0]);
    });

    test("reloads every 15 seconds, but not under an open Resolve incident", async ({
      page,
    }) => {
      await page.clock.install();
      const { lists } = await inbox(page, [
        "incident.read",
        "incident.resolve",
      ]);
      await page.goto("/operate/incidents");
      await expect(rows(page)).toHaveCount(1);
      const before = lists();
      await page.clock.fastForward(15_500);
      await expect.poll(lists).toBe(before + 1);
      // The revision a decision is sent with never changes under the dialog.
      const dialog = await openDialog(page);
      await page.clock.fastForward(31_000);
      expect(lists()).toBe(before + 1);
      await dialog.getByRole("button", { name: "Cancel" }).click();
      await page.clock.fastForward(15_500);
      await expect.poll(lists).toBe(before + 2);
    });

    test("a reload already on its way when Resolve incident opens never changes the incident under it", async ({
      page,
    }) => {
      await page.clock.install();
      const { items, lists } = await inbox(page, [
        "incident.read",
        "incident.resolve",
      ]);
      const matches: string[] = [];
      await page.route(`${environment}/incidents/${active}/resolve`, (r) => {
        matches.push(r.request().headers()["if-match"]);
        return r.fulfill({
          json: incident(active, { status: "resolved", revision: 8 }),
        });
      });
      let hold: Promise<void> | null = null;
      await page.route(`${environment}/incidents?*`, async (r) => {
        await hold;
        await r.fallback();
      });
      await page.goto("/operate/incidents");
      await expect(rows(page)).toHaveCount(1);
      await page
        .getByRole("button", {
          name: "Open incident WV-TASK-AMBIGUOUS at charge",
        })
        .click();
      let release!: () => void;
      hold = new Promise<void>((resolve) => (release = resolve));
      const before = lists();
      const requested = page.waitForRequest((request) =>
        /\/incidents\?/.test(request.url()),
      );
      await page.clock.fastForward(15_500);
      await requested;
      // The reload is on its way when the person opens Resolve incident.
      await page
        .getByRole("button", { name: "Resolve incident", exact: true })
        .click();
      const dialog = page.getByRole("dialog", { name: "Resolve incident" });
      await expect(dialog).toBeVisible();
      items[0] = { ...items[0], revision: 7 };
      hold = null;
      release();
      await expect.poll(lists).toBe(before + 1);
      // The decision is sent with the revision the person looked at.
      await endTheRun(page);
      await expect(page.getByText("Incident resolved.")).toBeVisible();
      expect(matches).toEqual(['"3"']);
    });

    test("a refused list shows the access it needs until the list answers", async ({
      page,
    }) => {
      await page.clock.install();
      await inbox(page, ["incident.read"]);
      let refuse = true;
      await page.route(`${environment}/incidents?*`, (r) =>
        refuse
          ? r.fulfill({
              status: 403,
              json: { code: "WV-DENIED", message: "Denied" },
            })
          : r.fallback(),
      );
      await page.goto("/operate/incidents");
      await expect(page.locator("weave-operate-state")).toContainText(
        "You need incident.read in this environment.",
      );
      refuse = false;
      // After a failure the next reload waits twice as long: 30 s.
      await page.clock.fastForward(30_500);
      await expect(rows(page)).toHaveCount(1);
      await expect(page.locator("weave-operate-state")).toHaveCount(0);
    });

    test("Load more incidents adds the next page", async ({ page }) => {
      await connected(page, { capabilities: ["incident.read"] });
      await page.route(`${environment}/incidents?*`, (r) => {
        const cursor = new URL(r.request().url()).searchParams.get("cursor");
        return r.fulfill({
          json: cursor
            ? { items: [incident(resolved)], next_cursor: null }
            : { items: [incident(active)], next_cursor: "more" },
        });
      });
      await page.goto("/operate/incidents?status=all");
      await expect(rows(page)).toHaveCount(1);
      await page.getByRole("button", { name: "Load more incidents" }).click();
      await expect(rows(page)).toHaveCount(2);
      await expect(
        page.getByRole("button", { name: "Load more incidents" }),
      ).toHaveCount(0);
    });

    test("a refused next page shows the access it needs", async ({ page }) => {
      await connected(page, { capabilities: ["incident.read"] });
      await page.route(`${environment}/incidents?*`, (r) => {
        const cursor = new URL(r.request().url()).searchParams.get("cursor");
        return cursor
          ? r.fulfill({
              status: 403,
              json: { code: "WV-DENIED", message: "Denied" },
            })
          : r.fulfill({
              json: { items: [incident(active)], next_cursor: "more" },
            });
      });
      await page.goto("/operate/incidents");
      await page.getByRole("button", { name: "Load more incidents" }).click();
      await expect(page.locator("weave-operate-state")).toContainText(
        "You need incident.read in this environment.",
      );
    });

    test("a failed list shows its code and Try again", async ({ page }) => {
      await connected(page, { capabilities: ["incident.read"] });
      let fail = true;
      await page.route(`${environment}/incidents?*`, (r) =>
        fail
          ? r.fulfill({
              status: 503,
              json: { code: "WV-UNAVAILABLE", message: "Unavailable" },
            })
          : r.fulfill({ json: { items: [], next_cursor: null } }),
      );
      await page.goto("/operate/incidents");
      const problem = page.getByRole("alert");
      await expect(problem).toContainText("Support code: WV-UNAVAILABLE");
      fail = false;
      await problem.getByRole("button", { name: "Try again" }).click();
      await expect(
        page.getByRole("heading", { name: "No active incidents" }),
      ).toBeVisible();
    });

    test("run events the person may not read show the access they need", async ({
      page,
    }) => {
      await inbox(page, ["incident.read"]);
      await page.route(`${environment}/runs/${runId}/history?*`, (r) =>
        r.fulfill({
          status: 403,
          json: { code: "WV-DENIED", message: "Denied" },
        }),
      );
      await page.goto("/operate/incidents");
      await page
        .getByRole("button", {
          name: "Open incident WV-TASK-AMBIGUOUS at charge",
        })
        .click();
      const state = page.locator("weave-incident-drawer weave-operate-state");
      await expect(state).toContainText("Run events need run access");
      await expect(state).toContainText(
        "You need run.read in this environment.",
      );
    });

    test("run events that can't be read show their code and Try again", async ({
      page,
    }) => {
      await inbox(page, ["incident.read", "run.read"]);
      let fail = true;
      await page.route(`${environment}/runs/${runId}/history?*`, (r) =>
        fail
          ? r.fulfill({
              status: 404,
              json: { code: "WV-NOT-FOUND", message: "Run not found" },
            })
          : r.fallback(),
      );
      await page.goto("/operate/incidents");
      await page
        .getByRole("button", {
          name: "Open incident WV-TASK-AMBIGUOUS at charge",
        })
        .click();
      const drawer = page.locator("weave-incident-drawer");
      const problem = drawer.getByRole("alert");
      await expect(problem).toContainText("Run not found");
      await expect(problem).toContainText("Support code: WV-NOT-FOUND");
      fail = false;
      await problem.getByRole("button", { name: "Try again" }).click();
      await expect(drawer.locator(".event-lines li")).toHaveCount(20);
      await expect(drawer.getByRole("alert")).toHaveCount(0);
    });

    test("Refresh incident that can't read the incidents says why in the dialog", async ({
      page,
    }) => {
      await inbox(page, ["incident.read", "incident.resolve"]);
      await page.route(`${environment}/incidents/${active}/resolve`, (r) =>
        r.fulfill({
          status: 409,
          json: {
            code: "WV-INCIDENT-REVISION",
            message: "Incident revision changed",
          },
        }),
      );
      let failList = false;
      await page.route(`${environment}/incidents?*`, (r) =>
        failList
          ? r.fulfill({
              status: 503,
              json: { code: "WV-UNAVAILABLE", message: "Platform unavailable" },
            })
          : r.fallback(),
      );
      await page.goto("/operate/incidents");
      const dialog = await openDialog(page);
      await endTheRun(page);
      await expect(dialog.getByRole("alert")).toContainText(
        "Support code: WV-INCIDENT-REVISION",
      );
      failList = true;
      await dialog.getByRole("button", { name: "Refresh incident" }).click();
      await expect(dialog.getByRole("alert")).toContainText(
        "Platform unavailable",
      );
      await expect(dialog.getByRole("alert")).toContainText(
        "Support code: WV-UNAVAILABLE",
      );
    });

    test("Refresh incident that no longer finds the incident says so", async ({
      page,
    }) => {
      const { items } = await inbox(page, [
        "incident.read",
        "incident.resolve",
      ]);
      await page.route(`${environment}/incidents/${active}/resolve`, (r) => {
        items.splice(0, 1);
        return r.fulfill({
          status: 409,
          json: {
            code: "WV-INCIDENT-REVISION",
            message: "Incident revision changed",
          },
        });
      });
      await page.goto("/operate/incidents");
      const dialog = await openDialog(page);
      await endTheRun(page);
      await dialog.getByRole("button", { name: "Refresh incident" }).click();
      await expect(dialog.getByRole("alert")).toContainText(
        "Studio couldn't find this incident when it read the list again.",
      );
      await expect(
        dialog.getByRole("button", { name: "Refresh incident" }),
      ).toHaveCount(0);
    });

    test("leaving Incidents while a decision is on its way leaves no warning and still says what happened", async ({
      page,
    }) => {
      const warnings: string[] = [];
      page.on("console", (message) => {
        if (/NG0\d+/.test(message.text())) warnings.push(message.text());
      });
      const { items } = await inbox(page, [
        "incident.read",
        "incident.resolve",
        "run.read",
      ]);
      let finish!: () => void;
      const gate = new Promise<void>((resolve) => (finish = resolve));
      await page.route(
        `${environment}/incidents/${active}/resolve`,
        async (r) => {
          await gate;
          items[0] = { ...items[0], status: "resolved", revision: 4 };
          await r.fulfill({ json: items[0] });
        },
      );
      await page.goto("/operate/incidents");
      const dialog = await openDialog(page);
      await endTheRun(page);
      await expect(
        dialog.getByRole("button", { name: "Resolving…" }),
      ).toBeVisible();
      await dialog.getByRole("button", { name: "Cancel" }).click();
      await page
        .locator("weave-incident-drawer")
        .getByRole("button", { name: "Close detail" })
        .click();
      await page.getByRole("button", { name: "Runs", exact: true }).click();
      await expect(page.locator("weave-incidents-page")).toHaveCount(0);
      await settle(page, `/incidents/${active}/resolve`, finish);
      await expect(
        page.getByText("Incident WV-TASK-AMBIGUOUS at charge is resolved."),
      ).toBeVisible();
      expect(warnings).toEqual([]);
    });

    // An answer belongs to the incident it was for: once the person has
    // closed its drawer, it opens nothing and a short notice says what happened.
    for (const [name, refusal, notice] of [
      [
        "a finished decision",
        null,
        "Incident WV-TASK-AMBIGUOUS at charge is resolved.",
      ],
      [
        "a refused decision",
        {
          status: 409,
          json: {
            code: "WV-RUNTIME-STATE",
            message: "Incident is no longer active",
          },
        },
        "Incident WV-TASK-AMBIGUOUS at charge was not resolved. Incident is no longer active",
      ],
    ] as const)
      test(`${name} for a closed drawer opens nothing and says what happened`, async ({
        page,
      }) => {
        const { items } = await inbox(page, [
          "incident.read",
          "incident.resolve",
          "run.read",
        ]);
        let finish!: () => void;
        const gate = new Promise<void>((resolve) => (finish = resolve));
        await page.route(
          `${environment}/incidents/${active}/resolve`,
          async (r) => {
            await gate;
            if (refusal) return r.fulfill(refusal);
            items[0] = { ...items[0], status: "resolved", revision: 4 };
            return r.fulfill({ json: items[0] });
          },
        );
        await page.goto("/operate/incidents");
        const dialog = await openDialog(page);
        await endTheRun(page);
        await expect(
          dialog.getByRole("button", { name: "Resolving…" }),
        ).toBeVisible();
        await dialog.getByRole("button", { name: "Cancel" }).click();
        await page
          .locator("weave-incident-drawer")
          .getByRole("button", { name: "Close detail" })
          .click();
        await expect(page.locator("weave-incident-drawer")).toHaveCount(0);
        await settle(page, `/incidents/${active}/resolve`, finish);
        await expect(page.getByText(notice)).toBeVisible();
        await expect(page.locator("weave-incident-drawer")).toHaveCount(0);
        await expect(page.getByRole("dialog")).toHaveCount(0);
      });

    test("choosing Incidents again clears the filters", async ({ page }) => {
      await inbox(page, ["incident.read"]);
      await page.goto("/operate/incidents?status=all&code=WV-TASK-FAILED");
      await expect(rows(page)).toHaveCount(1);
      await expect(rows(page).first()).toContainText("Resolved");
      await page
        .getByRole("button", { name: "Incidents", exact: true })
        .click();
      await expect(page).toHaveURL(/\/operate\/incidents$/);
      await expect(page.getByLabel("Status")).toHaveValue("active");
      await expect(page.getByLabel("Code")).toHaveValue("");
      await expect(rows(page)).toHaveCount(1);
      await expect(rows(page).first()).toContainText("Active");
    });

    if (viewport.width > 1024) {
      // A decision's answer belongs to the incident it was for, never to the
      // incident the person opened while it was on its way.
      const other = "94444444-4444-4444-8444-444444444444";
      const decideThenOpenAnother = async (
        page: Page,
        answer: (
          route: Route,
          items: Record<string, unknown>[],
        ) => Promise<void>,
      ) => {
        const { items, lists } = await inbox(page, [
          "incident.read",
          "incident.resolve",
          "run.read",
        ]);
        items.push(
          incident(other, {
            incident_key: "ship:1",
            node_id: "ship",
            code: "WV-TASK-FAILED",
            origin_code: "WV-TASK-FAILED",
          }),
        );
        let finish!: () => void;
        const gate = new Promise<void>((resolve) => (finish = resolve));
        await page.route(
          `${environment}/incidents/${active}/resolve`,
          async (r) => {
            await gate;
            await answer(r, items);
          },
        );
        await page.goto("/operate/incidents");
        const dialog = await openDialog(page);
        await endTheRun(page);
        await expect(
          dialog.getByRole("button", { name: "Resolving…" }),
        ).toBeVisible();
        await dialog.getByRole("button", { name: "Cancel" }).click();
        await page
          .getByRole("button", { name: "Open incident WV-TASK-FAILED at ship" })
          .click();
        const drawer = page.locator("weave-incident-drawer");
        await expect(drawer.locator("#incident-detail-title")).toHaveText(
          "Incident WV-TASK-FAILED",
        );
        await drawer
          .getByRole("button", { name: "Resolve incident", exact: true })
          .click();
        // The first decision on its way never holds up this one.
        await expect(
          dialog.getByRole("button", { name: "Resolve incident", exact: true }),
        ).toBeVisible();
        return {
          items,
          lists,
          drawer,
          dialog,
          answerArrives: () =>
            settle(page, `/incidents/${active}/resolve`, finish),
        };
      };

      test("a finished decision never changes the incident opened meanwhile", async ({
        page,
      }) => {
        const { drawer, dialog, answerArrives } = await decideThenOpenAnother(
          page,
          async (r, items) => {
            items[0] = { ...items[0], status: "resolved", revision: 4 };
            await r.fulfill({ json: items[0] });
          },
        );
        await answerArrives();
        await expect(
          page.getByText("Incident WV-TASK-AMBIGUOUS at charge is resolved."),
        ).toBeVisible();
        await expect(drawer.locator("#incident-detail-title")).toHaveText(
          "Incident WV-TASK-FAILED",
        );
        await expect(dialog).toBeVisible();
        await expect(dialog).toContainText("WV-TASK-FAILED at ship");
        await expect(dialog.getByRole("alert")).toHaveCount(0);
        // The incident that was resolved leaves the Active list.
        await expect(rows(page)).toHaveCount(1);
        await expect(rows(page).first()).toContainText("WV-TASK-FAILED");
      });

      test("a refused decision never becomes the alert of the incident opened meanwhile", async ({
        page,
      }) => {
        const { drawer, dialog, answerArrives } = await decideThenOpenAnother(
          page,
          (r) =>
            r.fulfill({
              status: 409,
              json: {
                code: "WV-RUNTIME-STATE",
                message: "Incident is no longer active",
              },
            }),
        );
        await answerArrives();
        await expect(dialog.getByRole("alert")).toHaveCount(0);
        await expect(drawer.locator("#incident-detail-title")).toHaveText(
          "Incident WV-TASK-FAILED",
        );
        // The person still learns what happened to the incident they decided.
        await expect(
          page.getByText(
            "Incident WV-TASK-AMBIGUOUS at charge was not resolved. Incident is no longer active",
          ),
        ).toBeVisible();
      });

      test("a finished decision doesn't reload the incident under another open Resolve incident", async ({
        page,
      }) => {
        const { items, lists, dialog, answerArrives } =
          await decideThenOpenAnother(page, async (r, items) => {
            items[0] = { ...items[0], status: "resolved", revision: 4 };
            // Someone else changed the other incident in the meantime.
            items[1] = { ...items[1], revision: 9 };
            await r.fulfill({ json: items[0] });
          });
        const matches: string[] = [];
        await page.route(`${environment}/incidents/${other}/resolve`, (r) => {
          matches.push(r.request().headers()["if-match"]);
          return r.fulfill({
            json: { ...items[1], status: "resolved", revision: 10 },
          });
        });
        const before = lists();
        await answerArrives();
        expect(lists()).toBe(before);
        // The decision is sent with the revision the person looked at.
        await endTheRun(page);
        await expect(page.getByText("Incident resolved.")).toBeVisible();
        await expect(dialog).toHaveCount(0);
        expect(matches).toEqual(['"3"']);
      });
    }
  });
