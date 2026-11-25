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
// engine events, and the Resolve incident dialog with its error paths; and
// transactions in doubt first, as one case with the called runs they wait
// for. The server checks incident.resolve, the revision and the receipt.
import { mkdirSync } from "node:fs";
import { resolve as resolvePath } from "node:path";
import { test, expect, Page, Route } from "@playwright/test";
import type { IncidentView } from "../../src/app/operate/incidents/incidents-model";
import {
  called,
  caller,
  held,
  ids,
  loop,
  others,
  unknown,
} from "../transaction-incidents-fixture";
import {
  earlier,
  environment,
  incidentPlatform,
  type IncidentPlatform,
} from "./incidents-po";
import {
  acmePlatform,
  platformHost,
  signInWaiting,
  type PlatformHost,
} from "./platform-host";
import { connected, pauseClock } from "./support";

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
  const queries: URLSearchParams[] = [];
  await page.route(`${environment}/incidents?*`, (r) => {
    const query = new URL(r.request().url()).searchParams;
    queries.push(query);
    // One load reads incidents in doubt first, then every other one.
    const priority = query.get("priority");
    if (priority !== "high") lists++;
    // As the platform does: the status, code, test and priority filters apply here.
    const statuses = query.getAll("status");
    const listed = items.filter(
      (item) =>
        (!statuses.length || statuses.includes(String(item.status))) &&
        (!query.get("code") || item.code === query.get("code")) &&
        (query.get("include_test") === "true" || !item.test) &&
        (!priority || (item.priority ?? "normal") === priority),
    );
    return r.fulfill({ json: { items: listed, next_cursor: null } });
  });
  await page.route(`${environment}/incidents/*`, (r) => {
    const id = new URL(r.request().url()).pathname.split("/").pop();
    const found = items.find((item) => item.id === id);
    return found
      ? r.fulfill({ json: found })
      : r.fulfill({
          status: 404,
          json: { code: "WV-NOT-FOUND", message: "Incident not found" },
        });
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
  return { items, lists: () => lists, queries: () => queries };
}
/** Answers the read of incidents in doubt with none, as a platform without any; false for every other read. */
function noneInDoubt(r: Route): boolean {
  if (new URL(r.request().url()).searchParams.get("priority") !== "high")
    return false;
  void r.fulfill({ json: { items: [], next_cursor: null } });
  return true;
}
/** Narrow screens fold the filters behind "Filters (n)": open them. */
async function openFilters(page: Page) {
  await page.locator(".operate-filters").waitFor({ state: "attached" });
  const toggle = page.locator(".filters-toggle");
  if (await toggle.isVisible()) await toggle.click();
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

test("a filter asks the platform for its first page, so a match beyond the loaded pages shows", async ({
  page,
}) => {
  await inbox(page, ["incident.read"]);
  const queries: URLSearchParams[] = [];
  // Without a code filter, the failed incident sits on the third page.
  await page.route(`${environment}/incidents?*`, (r) => {
    if (noneInDoubt(r)) return;
    const query = new URL(r.request().url()).searchParams;
    queries.push(query);
    if (query.get("code") === "WV-TASK-FAILED")
      return r.fulfill({
        json: {
          items: [incident(resolved, { code: "WV-TASK-FAILED" })],
          next_cursor: null,
        },
      });
    const at = Number(query.get("cursor") ?? "0");
    return r.fulfill({
      json: {
        items:
          at === 2
            ? [incident(resolved, { code: "WV-TASK-FAILED" })]
            : Array.from({ length: 50 }, (_, i) =>
                incident(
                  "98888888-8888-4888-8888-" +
                    String(at * 50 + i).padStart(12, "0"),
                ),
              ),
        next_cursor: at < 2 ? String(at + 1) : null,
      },
    });
  });
  await page.goto("/operate/incidents");
  await expect(rows(page)).toHaveCount(50);
  await expect(page.getByText("50 incidents loaded")).toBeVisible();
  const before = queries.length;
  await page.getByLabel("Code", { exact: true }).fill("WV-TASK-FAILED");
  await page.getByLabel("Code", { exact: true }).press("Enter");
  await expect(rows(page)).toHaveCount(1);
  await expect(rows(page)).toContainText("WV-TASK-FAILED");
  const filtered = queries.slice(before);
  expect(filtered.length).toBeGreaterThan(0);
  for (const query of filtered) {
    expect(query.get("code")).toBe("WV-TASK-FAILED");
    expect(query.getAll("status")).toEqual(["active"]);
    expect(query.get("include_test")).toBe("false");
    expect(query.get("order")).toBe("opened_desc");
    expect(query.has("cursor")).toBe(false);
  }
  await expect(page).toHaveURL(/\/operate\/incidents\?code=WV-TASK-FAILED$/);
});

test("incidents coalesce repeated appends and refresh all loaded pages", async ({
  page,
}) => {
  await inbox(page, ["incident.read"]);
  let pending: Route | undefined;
  let appends = 0;
  let refreshed = false;
  await page.route(`${environment}/incidents?*`, (r) => {
    const cursor = new URL(r.request().url()).searchParams.get("cursor");
    if (cursor) {
      appends++;
      if (!pending) {
        pending = r;
        return;
      }
    }
    return r
      .fulfill({
        json: {
          items: cursor ? [incident(active)] : [incident(resolved)],
          next_cursor: cursor ? null : "next",
        },
      })
      .then(() => {
        if (!cursor && pending) refreshed = true;
      });
  });
  await page.goto("/operate/incidents?status=all");
  await expect(rows(page)).toHaveCount(1);
  // Dispatch two events in one turn, including when the first disables the control.
  await page
    .getByRole("button", { name: "Load more incidents" })
    .evaluate((button) => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
  await expect.poll(() => appends).toBe(1);
  await expect(
    page.getByRole("button", { name: "Load more incidents" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await pending!.fulfill({
    json: { items: [incident(active)], next_cursor: null },
  });
  await expect.poll(() => refreshed).toBe(true);
  await expect(
    page.getByRole("button", { name: "Refresh", exact: true }),
  ).not.toHaveAttribute("aria-disabled", "true");
  await expect(rows(page)).toHaveCount(2);
  expect(appends).toBe(2);
});

test("incidents keep an append requested during a delayed refresh", async ({
  page,
}) => {
  await inbox(page, ["incident.read"]);
  let refresh: Route | undefined;
  let reads = 0;
  await page.route(`${environment}/incidents?*`, (r) => {
    const cursor = new URL(r.request().url()).searchParams.get("cursor");
    if (!cursor && ++reads === 2) {
      refresh = r;
      return;
    }
    return r.fulfill({
      json: {
        items: cursor ? [incident(active)] : [incident(resolved)],
        next_cursor: cursor ? null : "next",
      },
    });
  });
  await page.goto("/operate/incidents?status=all");
  await expect(rows(page)).toHaveCount(1);
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => !!refresh).toBe(true);
  await page.getByRole("button", { name: "Load more incidents" }).click();
  await refresh!.fulfill({
    json: { items: [incident(resolved)], next_cursor: "next" },
  });
  await expect(rows(page)).toHaveCount(2);
});

test("incidents stop at the page limit and explain remaining records", async ({
  page,
}) => {
  await inbox(page, ["incident.read"]);
  let reads = 0;
  await page.route(`${environment}/incidents?*`, (r) => {
    if (noneInDoubt(r)) return;
    reads++;
    return r.fulfill({
      json: {
        items: [
          incident(
            "19999999-9999-4999-8999-" + String(reads).padStart(12, "0"),
          ),
        ],
        next_cursor: `page-${reads}`,
      },
    });
  });
  await page.goto("/operate/incidents");
  for (let i = 1; i < 10; i++) {
    await expect(rows(page)).toHaveCount(i);
    await page.getByRole("button", { name: "Load more incidents" }).click();
  }
  await expect(rows(page)).toHaveCount(10);
  await expect(
    page.getByRole("button", { name: "Load more incidents" }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Showing the first 500 incidents.", { exact: false }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => reads).toBe(20);
  await expect(rows(page)).toHaveCount(10);
});

test("incidents discard an append queued after the page closes", async ({
  page,
}) => {
  await inbox(page, ["incident.read"]);
  let delayed: Route | undefined;
  let reads = 0;
  let appends = 0;
  await page.route(`${environment}/incidents?*`, (r) => {
    if (noneInDoubt(r)) return;
    const cursor = new URL(r.request().url()).searchParams.get("cursor");
    if (cursor) appends++;
    if (!cursor && ++reads === 2) {
      delayed = r;
      return;
    }
    return r.fulfill({
      json: { items: [incident(active)], next_cursor: "next" },
    });
  });
  await page.goto("/operate/incidents");
  await expect(rows(page)).toHaveCount(1);
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => !!delayed).toBe(true);
  await page.getByRole("button", { name: "Load more incidents" }).click();
  await page.getByRole("button", { name: "Home", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Home", exact: true }),
  ).toBeVisible();
  await delayed!.fulfill({
    json: { items: [incident(active)], next_cursor: "next" },
  });
  await page.goto("/operate/incidents");
  await expect(rows(page)).toHaveCount(1);
  expect(appends).toBe(0);
});

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
      await openFilters(page);
      await page.getByLabel("Status").selectOption("all");
      await expect(page).toHaveURL(/\/operate\/incidents\?status=all$/);
      await expect(rows(page)).toHaveCount(2);
      await expect(rows(page).nth(1)).toContainText("Resolved");
      await expect(
        rows(page).nth(1).getByText("Effects may continue"),
      ).toBeAttached();
      await page.getByLabel("Code", { exact: true }).fill("WV-TASK-FAILED");
      await page.getByLabel("Code", { exact: true }).press("Enter");
      await expect(rows(page)).toHaveCount(1);
      await page.reload();
      await openFilters(page);
      await expect(page.getByLabel("Code", { exact: true })).toHaveValue(
        "WV-TASK-FAILED",
      );
      await expect(rows(page)).toHaveCount(1);
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
      // Only runFor moves a paused clock, and Refresh restarts the wait, so
      // the next reload is due exactly 15 s on: one every 20 s fails here.
      await pauseClock(page);
      const before = lists() + 1;
      await page.getByRole("button", { name: "Refresh" }).click();
      await expect.poll(lists).toBe(before);
      await page.clock.runFor(14_500);
      expect(lists()).toBe(before);
      await page.clock.runFor(1_000);
      await expect.poll(lists).toBe(before + 1);
      // The revision a decision is sent with never changes under the dialog.
      // Studio draws on its timers, so the clock runs while the dialog opens.
      await page.clock.resume();
      const dialog = await openDialog(page);
      await expect(dialog).toBeVisible();
      await pauseClock(page);
      await page.clock.runFor(31_000);
      expect(lists()).toBe(before + 1);
      await dialog.getByRole("button", { name: "Cancel" }).click();
      await page.clock.runFor(15_500);
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
        if (noneInDoubt(r)) return;
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

    test("Refresh incident that can't read the incident says why in the dialog", async ({
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
      await page.route(`${environment}/incidents/${active}`, (r) =>
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
        "Studio couldn't find this incident when it read it again.",
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

test.describe("platform filters, direct links and opening times", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  const hidden = "94444444-4444-4444-8444-444444444444";
  const facts = {
    instance_key: "charge#3",
    opened_at: "2026-10-07T10:00:00Z",
    updated_at: "2026-10-07T10:05:00Z",
    workflow: { name: "invoice-approval", version: "1.3.0" },
    run_status: "blocked",
  };

  test("every filter goes to the platform and stays in the address", async ({
    page,
  }) => {
    const { queries } = await inbox(page, ["incident.read"]);
    await page.goto("/operate/incidents");
    await expect(rows(page)).toHaveCount(1);
    await page.getByLabel("Status").selectOption("closed");
    for (const [label, value] of [
      ["Code", "WV-TASK-AMBIGUOUS"],
      ["Workflow", "invoice-approval"],
      ["Step", "charge"],
    ]) {
      await page.getByLabel(label, { exact: true }).fill(value);
      await page.getByLabel(label, { exact: true }).press("Enter");
    }
    await page.getByLabel("Opened after").fill("2026-10-01T08:00");
    await page.getByLabel("Opened after").press("Enter");
    await page.getByLabel("Opened before").fill("2026-10-08T08:00");
    await page.getByLabel("Opened before").press("Enter");
    await page.getByLabel("Order").selectOption("opened_asc");
    await page.getByLabel("Include test runs").check();
    await expect.poll(() => queries().at(-1)?.get("include_test")).toBe("true");
    const query = queries().at(-1)!;
    expect(query.getAll("status")).toEqual(["closed"]);
    expect(query.get("code")).toBe("WV-TASK-AMBIGUOUS");
    expect(query.get("workflow")).toBe("invoice-approval");
    expect(query.get("node_id")).toBe("charge");
    expect(Date.parse(query.get("opened_after")!)).toBe(
      new Date("2026-10-01T08:00").getTime(),
    );
    expect(Date.parse(query.get("opened_before")!)).toBe(
      new Date("2026-10-08T08:00").getTime(),
    );
    expect(query.get("order")).toBe("opened_asc");
    expect(query.has("cursor")).toBe(false);
    const address = new URL(page.url()).searchParams;
    expect(Object.fromEntries(address)).toMatchObject({
      status: "closed",
      code: "WV-TASK-AMBIGUOUS",
      workflow: "invoice-approval",
      node_id: "charge",
      include_test: "true",
      order: "opened_asc",
    });
    // A start after the end is refused before anything is sent.
    const sent = queries().length;
    await page.getByLabel("Opened after").fill("2026-10-09T08:00");
    await page.getByLabel("Opened after").press("Enter");
    await expect(page.getByRole("alert")).toContainText(
      "Choose a start before the end.",
    );
    expect(queries().length).toBe(sent);
    await page.getByRole("button", { name: /^Clear filters/ }).click();
    await expect(page).toHaveURL(/\/operate\/incidents$/);
    await expect.poll(() => queries().at(-1)?.get("priority")).toBe("normal");
    expect(Object.fromEntries(queries().at(-1)!)).toEqual({
      limit: "50",
      order: "opened_desc",
      include_test: "false",
      status: "active",
      priority: "normal",
    });
  });

  test("a direct link opens an incident no loaded page lists, and decides on its revision", async ({
    page,
  }) => {
    await inbox(page, ["incident.read", "incident.resolve", "run.read"]);
    // Only incidents.read finds it: no list page includes it.
    const lost = incident(hidden, {
      ...facts,
      revision: 7,
      code: "WV-TASK-LOST",
    });
    await page.route(`${environment}/incidents/${hidden}`, (r) =>
      r.fulfill({ json: lost }),
    );
    const reads: string[] = [];
    page.on("request", (request) => {
      if (request.url().includes(`/incidents/${hidden}`))
        reads.push(request.url());
    });
    let ifMatch = "";
    await page.route(`${environment}/incidents/${hidden}/resolve`, (r) => {
      ifMatch = r.request().headers()["if-match"] ?? "";
      return r.fulfill({
        json: { ...lost, status: "resolved", revision: 8 },
      });
    });
    await page.goto(`/operate/incidents?incident=${hidden}`);
    const drawer = page.locator("weave-incident-drawer");
    await expect(drawer.locator("#incident-detail-title")).toHaveText(
      "Incident WV-TASK-LOST",
    );
    await expect(rows(page)).toHaveCount(1);
    expect(reads.some((url) => url.endsWith(`/incidents/${hidden}`))).toBe(
      true,
    );
    await drawer
      .getByRole("button", { name: "Resolve incident", exact: true })
      .click();
    await endTheRun(page);
    await expect(page.getByText("Incident resolved.")).toBeVisible();
    expect(ifMatch).toBe('"7"');
    await drawer.getByRole("button", { name: "Close detail" }).click();
    await expect(page).toHaveURL(/\/operate\/incidents$/);
  });

  test("a direct link to an incident that is missing or refused says so", async ({
    page,
  }) => {
    await inbox(page, ["incident.read"]);
    await page.goto(`/operate/incidents?incident=${hidden}`);
    await expect(
      page.getByRole("heading", { name: "This incident can't be found" }),
    ).toBeVisible();
    await expect(rows(page)).toHaveCount(1);
    await page.route(`${environment}/incidents/${hidden}`, (r) =>
      r.fulfill({
        status: 403,
        json: { code: "WV-FORBIDDEN", message: "Not allowed" },
      }),
    );
    await page.goto(`/operate/incidents?incident=${hidden}`);
    await expect(
      page.getByRole("heading", {
        name: "You don't have access to this incident",
      }),
    ).toBeVisible();
    await expect(page.locator("weave-incident-drawer")).toHaveCount(0);
  });

  for (const [name, refused, heading] of [
    [
      "missing",
      { status: 404, code: "WV-NOT-FOUND" },
      "This incident can't be found",
    ],
    [
      "refused",
      { status: 403, code: "WV-FORBIDDEN" },
      "You don't have access to this incident",
    ],
  ] as const)
    test(`Try again reads an incident that was ${name}, keeps the focus, and opens it once it answers`, async ({
      page,
    }) => {
      await inbox(page, ["incident.read"]);
      const reads: Route[] = [];
      await page.route(`${environment}/incidents/${hidden}`, (route) => {
        reads.push(route);
      });
      const answer = {
        status: refused.status,
        json: { code: refused.code, message: "No" },
      };
      await page.goto(`/operate/incidents?incident=${hidden}`);
      await expect.poll(() => reads.length).toBe(1);
      await reads[0].fulfill(answer);
      const problem = page.locator(".empty-state").filter({
        has: page.getByRole("heading", { name: heading }),
      });
      const again = problem.getByRole("button", { name: "Try again" });
      await expect(again).toBeVisible();
      await page.screenshot({ path: `${captures}/incident-${name}.png` });
      await again.focus();
      await page.keyboard.press("Enter");
      await expect.poll(() => reads.length).toBe(2);
      // A second press while it is on its way reads nothing.
      await page.keyboard.press("Enter");
      await expect(again).toBeFocused();
      await reads[1].fulfill(answer);
      await page.evaluate(
        () => new Promise((done) => requestAnimationFrame(done)),
      );
      await expect(again).toBeFocused();
      expect(reads).toHaveLength(2);
      await page.keyboard.press("Enter");
      await expect.poll(() => reads.length).toBe(3);
      await reads[2].fulfill({ json: incident(hidden, facts) });
      const title = page.locator("#incident-detail-title");
      await expect(title).toHaveText("Incident WV-TASK-AMBIGUOUS");
      await expect(problem).toHaveCount(0);
      await expect(title).toBeFocused();
    });

  test("Try again keeps the focus as an incident fails in another way, and leaves it where the person moved it", async ({
    page,
  }) => {
    await inbox(page, ["incident.read"]);
    const reads: Route[] = [];
    await page.route(`${environment}/incidents/${hidden}`, (route) => {
      reads.push(route);
    });
    const missing = {
      status: 404,
      json: { code: "WV-NOT-FOUND", message: "Incident not found" },
    };
    const refused = {
      status: 403,
      json: { code: "WV-FORBIDDEN", message: "Not allowed" },
    };
    const unavailable = {
      status: 503,
      json: { code: "WV-UNAVAILABLE", message: "Unavailable" },
    };
    await page.goto(`/operate/incidents?incident=${hidden}`);
    const again = page.getByRole("button", { name: "Try again" });
    const ways = [
      [missing, page.getByText("This incident can't be found")],
      [unavailable, page.getByText("Support code: WV-UNAVAILABLE")],
      [refused, page.getByText("You don't have access to this incident")],
      [
        { json: { id: hidden, unavailable: true } },
        page.getByText("Studio can't show this incident"),
      ],
      [missing, page.getByText("This incident can't be found")],
    ] as const;
    for (const [index, [answer, words]] of ways.entries()) {
      await expect.poll(() => reads.length).toBe(index + 1);
      await reads[index].fulfill(answer);
      await expect(words).toBeVisible();
      if (index) await expect(again).toBeFocused();
      else await again.focus();
      if (index < ways.length - 1) await page.keyboard.press("Enter");
    }
    await page.keyboard.press("Enter");
    await expect.poll(() => reads.length).toBe(ways.length + 1);
    const workflow = page.getByLabel("Workflow", { exact: true });
    await workflow.focus();
    await reads[ways.length].fulfill(unavailable);
    await expect(ways[1][1]).toBeVisible();
    await expect(workflow).toBeFocused();
  });

  for (const [name, to] of [
    ["no incident", ""],
    ["the incident that is open", `?incident=${active}`],
  ])
    test(`an incident being read never opens once the address names ${name}`, async ({
      page,
    }) => {
      await inbox(page, ["incident.read"]);
      const reads: Route[] = [];
      await page.route(`${environment}/incidents/${hidden}`, (route) => {
        reads.push(route);
      });
      const move = (query: string) =>
        page.evaluate((to) => {
          history.pushState(null, "", `/operate/incidents${to}`);
          dispatchEvent(new PopStateEvent("popstate"));
        }, query);
      const title = page.locator("#incident-detail-title");
      await page.goto(`/operate/incidents${to}`);
      await expect(title).toHaveCount(to ? 1 : 0);
      await expect(rows(page)).toHaveCount(1);
      await move(`?incident=${hidden}`);
      await expect.poll(() => reads.length).toBe(1);
      if (to) await move(to);
      else {
        // A filter leaves it out of the address, as a link to the list does.
        await page.getByLabel("Include test runs").check();
        await expect(page).toHaveURL(
          /\/operate\/incidents\?include_test=true$/,
        );
      }
      await settle(page, `/incidents/${hidden}`, () => {
        void reads[0].fulfill({
          json: incident(hidden, { code: "WV-TASK-LOST" }),
        });
      });
      if (to) await expect(title).toHaveText("Incident WV-TASK-AMBIGUOUS");
      else await expect(title).toHaveCount(0);
      // Named again, it is read again.
      await move(`?incident=${hidden}`);
      await expect.poll(() => reads.length).toBe(2);
      await reads[1].fulfill({
        json: incident(hidden, { code: "WV-TASK-LOST" }),
      });
      await expect(title).toHaveText("Incident WV-TASK-LOST");
    });

  test("an incident that couldn't be opened is forgotten once the address no longer names it", async ({
    page,
  }) => {
    await inbox(page, ["incident.read"]);
    await page.goto(`/operate/incidents?incident=${hidden}`);
    const problem = page.getByRole("heading", {
      name: "This incident can't be found",
    });
    await expect(problem).toBeVisible();
    await page.getByLabel("Include test runs").check();
    await expect(page).toHaveURL(/\/operate\/incidents\?include_test=true$/);
    await expect(problem).toHaveCount(0);
    await expect(rows(page)).toHaveCount(1);
    // Nor after Back, or a link to the list.
    const move = (query: string) =>
      page.evaluate((to) => {
        history.pushState(null, "", `/operate/incidents${to}`);
        dispatchEvent(new PopStateEvent("popstate"));
      }, query);
    await move(`?incident=${hidden}`);
    await expect(problem).toBeVisible();
    await move("");
    await expect(problem).toHaveCount(0);
    // Nor once it names the incident that is open again.
    await move(`?incident=${active}`);
    const title = page.locator("#incident-detail-title");
    await expect(title).toHaveText("Incident WV-TASK-AMBIGUOUS");
    await move(`?incident=${hidden}`);
    await expect(problem).toBeVisible();
    await move(`?incident=${active}`);
    await expect(problem).toHaveCount(0);
    await expect(title).toHaveText("Incident WV-TASK-AMBIGUOUS");
  });

  for (const [name, extra, expected, hint] of [
    ["estimated", { opened_at_estimated: true }, /\(estimated\)/, true],
    ["exact", { opened_at_estimated: false }, /ago$/, false],
    ["exact when the platform leaves the flag out", {}, /ago$/, false],
  ] as const)
    test(`the drawer shows an opening time that is ${name}`, async ({
      page,
    }) => {
      const { items } = await inbox(page, ["incident.read", "run.read"]);
      items[0] = { ...items[0], ...facts, ...extra };
      await page.goto(`/operate/incidents?incident=${active}`);
      const drawer = page.locator("weave-incident-drawer");
      const opened = drawer.locator("dd.opened");
      await expect(opened.locator("time")).toHaveText(expected);
      await expect(opened.locator("time")).toHaveAttribute(
        "datetime",
        "2026-10-07T10:00:00.000Z",
      );
      await expect(opened.locator("small")).toHaveCount(hint ? 1 : 0);
      await expect(drawer).toContainText("invoice-approval 1.3.0");
      await expect(drawer).toContainText("charge#3");
      await expect(drawer.locator(".run-fact .status-pill")).toHaveText(
        "Blocked",
      );
      // Types only: the events' data never reaches the page.
      await expect(drawer.locator(".event-lines li")).toHaveCount(20);
      await expect(drawer).not.toContainText("never shown");
    });

  test("an incident without an opening time says Unknown, as an earlier platform's does", async ({
    page,
  }) => {
    const { items } = await inbox(page, ["incident.read", "run.read"]);
    await page.goto(`/operate/incidents?incident=${items[0].id}`);
    const drawer = page.locator("weave-incident-drawer");
    await expect(drawer.locator("dd.opened")).toHaveText("Unknown");
    await expect(drawer.locator("dt:text-is('Workflow') + dd")).toHaveText(
      "Unknown",
    );
    await expect(rows(page).first()).toContainText("Unknown");
  });
});

for (const size of [
  { name: "a mouse at 1440x900", width: 1440, height: 900, touch: false },
  { name: "touch at 1440x900", width: 1440, height: 900, touch: true },
  { name: "a window at 600x500", width: 600, height: 500, touch: false },
  { name: "a phone at 360x640", width: 360, height: 640, touch: true },
])
  test.describe(`alignment with ${size.name}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
    });

    test("the count sits on the filter line, and the drawer's Run button on its status pill, as full targets", async ({
      page,
    }) => {
      const { items } = await inbox(page, ["incident.read", "run.read"]);
      items[0] = { ...items[0], run_status: "blocked" };
      await page.goto("/operate/incidents");
      await expect(rows(page)).toHaveCount(1);
      await openFilters(page);
      const coarse = await page.evaluate(
        () => matchMedia("(pointer: coarse)").matches,
      );
      const target = coarse || size.width < 768 ? 44 : 36;
      const line = await page
        .locator(".operate-filters")
        .evaluate((filters) => {
          const middle = (node: Element) => {
            const box = node.getBoundingClientRect();
            return { top: box.top, center: box.top + box.height / 2 };
          };
          return {
            count: middle(filters.querySelector(".operate-count")!),
            checkbox: middle(filters.querySelector(".checkbox-field")!),
          };
        });
      // Beside the checkbox (a wrapped line has its own row), on its center.
      if (Math.abs(line.count.top - line.checkbox.top) < target)
        expect(
          Math.abs(line.count.center - line.checkbox.center),
        ).toBeLessThanOrEqual(0.5);
      await page
        .getByRole("button", {
          name: "Open incident WV-TASK-AMBIGUOUS at charge",
        })
        .click();
      const fact = page.locator("weave-incident-drawer dd.run-fact");
      await expect(fact.locator(".status-pill")).toHaveText("Blocked");
      const run = await fact.evaluate((node) => {
        const button = node.querySelector("button")!.getBoundingClientRect();
        const pill = node
          .querySelector(".status-pill")!
          .getBoundingClientRect();
        return {
          height: button.height,
          button: button.top + button.height / 2,
          pill: pill.top + pill.height / 2,
        };
      });
      expect(Math.abs(run.button - run.pill)).toBeLessThanOrEqual(0.5);
      if (target === 44) expect(run.height).toBeGreaterThanOrEqual(43.5);
    });
  });

// ------------------------------------------------- transactions in doubt

const band = (page: Page, name: string) => page.getByRole("region", { name });
const high = (page: Page) => band(page, "High priority");
const other = (page: Page) => band(page, "Other incidents");
/** The codes a table's rows name, in order. */
const codes = (scope: ReturnType<typeof band>) =>
  scope.locator(".resource-row > .row-text:first-child > :first-child");
const caseOf = (page: Page) =>
  page.getByRole("rowgroup", {
    name: "Case: move is waiting for a called run in doubt",
  });
const captures = resolvePath(
  process.env["STUDIO_OPERATE_CAPTURES"]?.trim() || "test-results/operate",
);
mkdirSync(captures, { recursive: true });

test.describe("transactions in doubt", () => {
  test.use({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC" });

  test("come first, in a High priority band above the other incidents, each read by its priority", async ({
    page,
  }) => {
    const platform = await incidentPlatform(page);
    await page.goto("/operate/incidents");
    const heading = high(page).getByRole("heading", { level: 2 });
    await expect(heading).toHaveText("High priority");
    await expect(heading.locator("weave-icon")).toHaveAttribute(
      "name",
      "critical",
    );
    await expect(high(page).locator(".band-count")).toHaveText("4 incidents");
    await expect(high(page)).toContainText(
      "Transactions in doubt. Other systems may not match Weave until someone resolves them.",
    );
    await expect(
      other(page).getByRole("heading", { level: 2, name: "Other incidents" }),
    ).toBeVisible();
    await expect(other(page).locator(".band-count")).toHaveText("4 incidents");
    await expect(codes(other(page))).toHaveText([
      "WV-TASK-AMBIGUOUS",
      "WV-TASK-FAILED",
      "WV-TASK-LOST",
      "WV-TASK-AMBIGUOUS",
    ]);
    expect(
      (await high(page).boundingBox())!.y +
        (await high(page).boundingBox())!.height,
    ).toBeLessThanOrEqual((await other(page).boundingBox())!.y);
    // The first load reads the band in doubt, then the rest, with the same filters.
    expect(platform.queries.map((query) => query.get("priority"))).toEqual([
      "high",
      "normal",
    ]);
    const [first, second] = platform.queries.map((query) => {
      const rest = new URLSearchParams(query);
      rest.delete("priority");
      return rest.toString();
    });
    expect(first).toBe(second);
    await page.screenshot({ path: `${captures}/inbox-bands-1440.png` });
  });

  test("a row in the High priority band shows its business key, and neither the pill nor the words the band says", async ({
    page,
  }) => {
    await incidentPlatform(page);
    await page.goto("/operate/incidents");
    const row = high(page)
      .locator(".resource-row")
      .filter({ hasText: "WV-TXN-PIVOT-UNKNOWN" });
    await expect(row.locator(".row-text").first()).toContainText(
      "Business key T-1034",
    );
    await expect(row.locator(".status-pill")).toHaveText(["Active"]);
    await expect(row).not.toContainText("In doubt");
    await expect(row).not.toContainText("High priority");
    // The key stays whole: it never breaks.
    await expect(row.locator(".business-key")).toHaveCSS(
      "white-space",
      "nowrap",
    );
  });

  test("a loop's step names its iteration, in its row, in its drawer and where another row waits for it", async ({
    page,
  }) => {
    await incidentPlatform(page, {
      items: [
        loop,
        {
          ...called,
          held_by: { ...called.held_by!, instance_key: "move[3]" },
        },
        ...others,
      ],
    });
    await page.goto("/operate/incidents");
    const row = high(page)
      .locator(".resource-row")
      .filter({ hasText: "WV-TXN-CANCEL-STUCK" });
    await expect(row.getByRole("cell").nth(3)).toHaveText("hold[3]");
    const waits = high(page).locator(".not-listed");
    await expect(waits.getByRole("cell").nth(3)).toHaveText("move[3]");
    await expect(waits).toContainText(
      "Not in this list · move[3] in run 6d18b3f0",
    );
    await expect(high(page).locator(".case-head")).toContainText(
      "Case: move[3] is waiting for a called run in doubt",
    );
    await row
      .getByRole("button", {
        name: "Open incident WV-TXN-CANCEL-STUCK at hold[3]",
        exact: true,
      })
      .click();
    await expect(
      page.locator("dl.txn-facts dt:text-is('Step') + dd"),
    ).toHaveText("hold[3]");
  });

  test("a run held whole is in the band too, tied to no step", async ({
    page,
  }) => {
    await incidentPlatform(page, { items: [held] });
    await page.goto("/operate/incidents");
    await expect(
      high(page).getByRole("button", {
        name: "Open incident WV-TXN-RUN-OVERSIZED at the run",
      }),
    ).toBeVisible();
    // Its name, then no step: neither the platform's own step nor its code.
    const row = high(page).locator(".resource-row").first();
    await expect(row.locator("small").first()).toHaveText("Not tied to a step");
    await expect(row.getByRole("cell").nth(3)).toHaveText("Not tied to a step");
    await expect(high(page)).not.toContainText("@transactions");
    // Nothing else is open: the band stands alone.
    await expect(other(page)).toHaveCount(0);
  });

  for (const [address, sent] of [
    ["?code=WV-TXN-PIVOT-UNKNOWN", { code: "WV-TXN-PIVOT-UNKNOWN" }],
    ["?status=all", {}],
  ] as const)
    test(`one list at ${address} says High priority and In doubt on each row in doubt`, async ({
      page,
    }) => {
      const platform = await incidentPlatform(page);
      await page.goto(`/operate/incidents${address}`);
      const row = rows(page).filter({ hasText: "WV-TXN-PIVOT-UNKNOWN" });
      await expect(row.locator(".row-text").first()).toContainText(
        "In doubt · Business key T-1034",
      );
      await expect(row.locator(".status-pill")).toHaveText([
        "Active",
        "High priority",
      ]);
      await expect(high(page)).toHaveCount(0);
      await expect(page.getByRole("heading", { level: 2 })).toHaveCount(0);
      expect(platform.queries).toHaveLength(1);
      expect(platform.queries[0].has("priority")).toBe(false);
      for (const [key, value] of Object.entries(sent))
        expect(platform.queries[0].get(key)).toBe(value);
      await page.screenshot({
        path: `${captures}/inbox-one-list-${"code" in sent ? "code" : "status"}-1440.png`,
      });
    });

  test("a decided incident is no longer in doubt: one list shows it as any other", async ({
    page,
  }) => {
    await incidentPlatform(page, {
      items: [{ ...unknown, status: "resolved" }],
    });
    await page.goto("/operate/incidents?status=resolved");
    const row = rows(page).first();
    await expect(row).toContainText("WV-TXN-PIVOT-UNKNOWN");
    await expect(row.locator(".status-pill")).toHaveText(["Resolved"]);
    await expect(row.locator(".row-text").first()).toContainText("submit");
    await expect(row).not.toContainText("In doubt");
  });

  test("a caller's settle timeout and its called run's incident are one case, aligned with the columns", async ({
    page,
  }) => {
    await incidentPlatform(page);
    await page.goto("/operate/incidents");
    const group = caseOf(page);
    await expect(group).toBeVisible();
    await expect(group.locator(".case-head")).toContainText(
      "Resolve first: WV-TXN-UNDO-STUCK in run 9a01c7d2",
    );
    await expect(codes(group)).toHaveText([
      "WV-TXN-SETTLE-TIMEOUT",
      "WV-TXN-UNDO-STUCK",
    ]);
    // Only the one to resolve first says so.
    await expect(
      group.locator(".resource-row").nth(0).locator(".tag"),
    ).toHaveCount(0);
    await expect(
      group.locator(".resource-row").nth(1).locator(".tag"),
    ).toHaveText("Resolve first");
    // The case sits between the rows before and after it, and nothing shows twice.
    await expect(codes(high(page))).toHaveText([
      "WV-TXN-PIVOT-UNKNOWN",
      "WV-TXN-SETTLE-TIMEOUT",
      "WV-TXN-UNDO-STUCK",
      "WV-TXN-CANCEL-STUCK",
    ]);
    const edges = await high(page)
      .getByRole("table")
      .evaluate((table) => {
        const lefts = (row: Element) =>
          [...row.children].map((cell) => cell.getBoundingClientRect().left);
        return {
          head: lefts(table.querySelector(".table-head")!),
          members: [...table.querySelectorAll(".case .resource-row")].map(
            lefts,
          ),
        };
      });
    expect(edges.members).toHaveLength(2);
    for (const member of edges.members)
      for (const [column, left] of member.entries())
        expect(
          Math.abs(left - edges.head[column]),
          `column ${column}`,
        ).toBeLessThanOrEqual(1);
    // The incident's words sit indented inside its own column.
    const indent = await group
      .locator(".resource-row .row-open")
      .first()
      .evaluate(
        (button) =>
          button.getBoundingClientRect().left -
          button.closest("[role=cell]")!.getBoundingClientRect().left,
      );
    expect(indent).toBeCloseTo(22, 0);
  });

  test("a case member that isn't in the list says where it is, and opens", async ({
    page,
  }) => {
    const platform = await incidentPlatform(page, {
      items: [called, ...others],
    });
    await page.goto("/operate/incidents");
    const group = caseOf(page);
    await expect(codes(group)).toHaveText([
      "WV-TXN-UNDO-STUCK",
      "WV-TXN-SETTLE-TIMEOUT",
    ]);
    const missing = group.locator(".resource-row").nth(1);
    await expect(missing).toContainText(
      "Not in this list · move in run 6d18b3f0",
    );
    await expect(missing.locator(".tag")).toHaveCount(0);
    await expect(
      group.locator(".resource-row").nth(0).locator(".tag"),
    ).toHaveText("Resolve first");
    await page.screenshot({ path: `${captures}/inbox-case-missing-1440.png` });
    // The platform holds it, on another page: Open reads it.
    platform.items.push(caller);
    await missing
      .getByRole("button", {
        name: "Open incident WV-TXN-SETTLE-TIMEOUT, move in run 6d18b3f0",
      })
      .click();
    await expect(
      page.locator("weave-incident-drawer #incident-detail-title"),
    ).toHaveText("Incident WV-TXN-SETTLE-TIMEOUT");
    expect(platform.reads).toEqual([ids.caller]);
    await expect(page).toHaveURL(
      new RegExp(`/operate/incidents\\?incident=${ids.caller}$`),
    );
  });

  test("Open on a case member that couldn't be read reads it again, once at a time", async ({
    page,
  }) => {
    await incidentPlatform(page, { items: [called, ...others] });
    const reads: Route[] = [];
    await page.route(`${environment}/incidents/${ids.caller}`, (route) => {
      reads.push(route);
    });
    await page.goto("/operate/incidents");
    const open = caseOf(page).getByRole("button", {
      name: "Open incident WV-TXN-SETTLE-TIMEOUT, move in run 6d18b3f0",
    });
    await open.click();
    await expect.poll(() => reads.length).toBe(1);
    await reads[0].fulfill({
      status: 404,
      json: { code: "WV-NOT-FOUND", message: "Incident not found" },
    });
    const missing = page.getByRole("heading", {
      name: "This incident can't be found",
    });
    await expect(missing).toBeVisible();
    // The address names it already: Open reads it all the same.
    await open.click();
    await expect.poll(() => reads.length).toBe(2);
    // Nor does a press while it is on its way read it once more.
    await open.click();
    await page.evaluate(
      () => new Promise((done) => requestAnimationFrame(done)),
    );
    expect(reads).toHaveLength(2);
    await reads[1].fulfill({ json: caller });
    await expect(
      page.locator("weave-incident-drawer #incident-detail-title"),
    ).toHaveText("Incident WV-TXN-SETTLE-TIMEOUT");
    await expect(missing).toHaveCount(0);
    await expect(open).toBeFocused();
  });

  test("each band loads more with its own cursor, and a refresh reads each band's own pages", async ({
    page,
  }) => {
    const platform = await incidentPlatform(page, { pageSize: 3 });
    await page.goto("/operate/incidents");
    // Three of four in each band: the case's called run is on the first page.
    await expect(high(page).locator(".band-count")).toHaveText("3 shown");
    await expect(other(page).locator(".band-count")).toHaveText("3 shown");
    await expect(page.getByText("6 incidents loaded")).toBeVisible();
    await other(page)
      .getByRole("button", { name: "Load more incidents" })
      .click();
    await expect(codes(other(page))).toHaveCount(4);
    await expect(other(page).locator(".band-count")).toHaveText("4 incidents");
    await expect(
      other(page).getByRole("button", { name: "Load more incidents" }),
    ).toHaveCount(0);
    await expect(codes(high(page))).toHaveCount(3);
    await expect(high(page).locator(".band-count")).toHaveText("3 shown");
    const asked = () =>
      platform.queries.map(
        (query) =>
          `${query.get("priority")} ${query.get("cursor") ?? "from the start"}`,
      );
    expect(asked()).toEqual([
      "high from the start",
      "normal from the start",
      "normal normal:3",
    ]);
    const before = platform.queries.length;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect
      .poll(() => asked().slice(before))
      .toEqual([
        "high from the start",
        "normal from the start",
        "normal normal:3",
      ]);
    await high(page)
      .getByRole("button", { name: "Load more incidents" })
      .click();
    await expect(codes(high(page))).toHaveCount(4);
    expect(asked().at(-1)).toBe("high high:3");
    await expect(codes(other(page))).toHaveCount(4);
    await expect(page.getByText("8 incidents", { exact: true })).toBeVisible();
  });

  test("a code turns the bands into one list that loads more without a priority, and clearing it brings them back", async ({
    page,
  }) => {
    const platform = await incidentPlatform(page, {
      items: [unknown, { ...unknown, id: ids.loop }, caller, ...others],
      pageSize: 1,
    });
    await page.goto("/operate/incidents");
    await expect(codes(high(page))).toHaveCount(1);
    await page.getByLabel("Code", { exact: true }).fill("WV-TXN-PIVOT-UNKNOWN");
    await page.getByLabel("Code", { exact: true }).press("Enter");
    await expect(high(page)).toHaveCount(0);
    await expect(rows(page)).toHaveCount(1);
    await page.getByRole("button", { name: "Load more incidents" }).click();
    await expect(rows(page)).toHaveCount(2);
    const last = platform.queries.at(-1)!;
    expect(last.get("cursor")).toBe("every:1");
    expect(last.has("priority")).toBe(false);
    await page.getByRole("button", { name: /^Clear filters/ }).click();
    await expect(codes(high(page))).toHaveCount(1);
    await expect(codes(other(page))).toHaveCount(1);
  });

  test("nothing in doubt: no band and no Other incidents, the list as it was", async ({
    page,
  }) => {
    await incidentPlatform(page, { items: others });
    await page.goto("/operate/incidents");
    await expect(rows(page)).toHaveCount(4);
    await expect(page.getByRole("heading", { level: 2 })).toHaveCount(0);
    await expect(page.locator(".band-note")).toHaveCount(0);
  });

  test("the address keeps the incident and never its business key or the band", async ({
    page,
  }) => {
    await incidentPlatform(page);
    await page.goto("/operate/incidents");
    const title = await page.title();
    await high(page)
      .getByRole("button", {
        name: "Open incident WV-TXN-PIVOT-UNKNOWN at submit",
      })
      .click();
    await expect(page).toHaveURL(
      new RegExp(`/operate/incidents\\?incident=${ids.unknown}$`),
    );
    await page.getByLabel("Include test runs").check();
    await expect(page).toHaveURL(/include_test=true/);
    expect(page.url()).not.toContain("T-1034");
    expect(page.url()).not.toContain("priority");
    expect(await page.title()).toBe(title);
  });

  test("a refresh keeps the open drawer on its incident, whichever band holds it", async ({
    page,
  }) => {
    const platform = await incidentPlatform(page);
    await page.goto("/operate/incidents");
    await caseOf(page)
      .getByRole("button", { name: "Open incident WV-TXN-UNDO-STUCK at add" })
      .click();
    const drawer = page.locator("weave-incident-drawer");
    await expect(drawer.locator("#incident-detail-title")).toHaveText(
      "Incident WV-TXN-UNDO-STUCK",
    );
    platform.items = platform.items.map((item) =>
      item.id === ids.called ? { ...called, revision: 9 } : item,
    );
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(drawer.locator("details dd").last()).toHaveText("9");
    await expect(drawer.locator("#incident-detail-title")).toHaveText(
      "Incident WV-TXN-UNDO-STUCK",
    );
    await expect(caseOf(page).locator(".resource-row.selected")).toHaveCount(1);
    await page.screenshot({ path: `${captures}/inbox-case-open-1440.png` });
  });

  test("a decided incident leaves its band at once", async ({ page }) => {
    const platform = await incidentPlatform(page, {
      items: [loop, unknown, ...others],
    });
    let hold = false;
    await page.route(`${environment}/incidents?*`, (route) =>
      hold ? undefined : route.fallback(),
    );
    await page.route(
      `${environment}/incidents/${ids.loop}/resolve`,
      (route) => {
        hold = true;
        platform.items = platform.items.filter((item) => item.id !== ids.loop);
        return route.fulfill({
          json: { ...loop, status: "resolved", revision: 2 },
        });
      },
    );
    await page.goto(`/operate/incidents?incident=${ids.loop}`);
    await expect(codes(high(page))).toHaveCount(2);
    await page
      .locator("weave-incident-drawer")
      .getByRole("button", { name: "Resolve incident", exact: true })
      .click();
    await endTheRun(page);
    // The reload after it is still on its way: the band already left it out.
    await expect(codes(high(page))).toHaveText(["WV-TXN-PIVOT-UNKNOWN"]);
    await expect(codes(other(page))).toHaveCount(4);
  });

  test("a failed band keeps what was on screen and says so", async ({
    page,
  }) => {
    await incidentPlatform(page);
    await page.goto("/operate/incidents");
    await expect(codes(high(page))).toHaveCount(4);
    await page.route(`${environment}/incidents?*priority=normal*`, (route) =>
      route.fulfill({
        status: 503,
        json: { code: "WV-UNAVAILABLE", message: "Unavailable" },
      }),
    );
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(page.getByText("Support code: WV-UNAVAILABLE")).toBeVisible();
    await expect(codes(high(page))).toHaveCount(4);
    await expect(codes(other(page))).toHaveCount(4);
  });

  test("an incident Studio can't show keeps the band the platform lists it in", async ({
    page,
  }) => {
    const hidden = "c1000000-0000-4000-8000-000000000001";
    const platform = await incidentPlatform(page, {
      items: [unknown, { id: hidden, unavailable: true }, ...others],
      unavailableInDoubt: [hidden],
    });
    await page.goto("/operate/incidents");
    await expect(codes(high(page))).toHaveText([
      "WV-TXN-PIVOT-UNKNOWN",
      "Unavailable incident",
    ]);
    await expect(high(page).locator(".band-count")).toHaveText("2 incidents");
    const row = high(page).locator(".resource-row").nth(1);
    await expect(row).toContainText("Studio can't show this incident.");
    await expect(row.getByRole("button")).toHaveCount(0);
    await expect(codes(other(page))).toHaveCount(4);
    // Alone there, it still keeps the band.
    platform.items = platform.items.filter((item) => item.id !== ids.unknown);
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(codes(high(page))).toHaveText(["Unavailable incident"]);
    await expect(codes(other(page))).toHaveCount(4);
  });
});

test.describe("a refresh keeps the keyboard focus on its incident", () => {
  test.use({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC" });

  /** An incident that opens after every other: the first row of its band. */
  const newest = (incident: IncidentView): IncidentView => ({
    ...incident,
    id: "d1000000-0000-4000-8000-000000000001",
    code: incident.priority ? "WV-TXN-AFTER-PIVOT-STUCK" : "WV-TASK-REFUSED",
    opened_at: "2026-11-22T10:53:30Z",
  });
  const row = (page: Page, name: string) =>
    page.getByRole("button", { name: `Open incident ${name}`, exact: true });
  /** The page's own refresh, not a press that would move the focus. */
  const refresh = (page: Page) => page.clock.fastForward(16_000);

  test("in one list", async ({ page }) => {
    const platform = await incidentPlatform(page, { items: others });
    await page.goto("/operate/incidents");
    await row(page, "WV-TASK-FAILED at fetch").focus();
    platform.items = [newest(others[0]), ...others];
    await refresh(page);
    await expect(codes(page.locator(".incident-list"))).toHaveCount(5);
    await expect(row(page, "WV-TASK-FAILED at fetch")).toBeFocused();
  });

  for (const [name, kept, added, first, scope] of [
    [
      "in the High priority band",
      "WV-TXN-PIVOT-UNKNOWN at submit",
      unknown,
      "WV-TXN-AFTER-PIVOT-STUCK",
      high,
    ],
    [
      "in Other incidents",
      "WV-TASK-FAILED at fetch",
      others[0],
      "WV-TASK-REFUSED",
      other,
    ],
  ] as const)
    test(`${name}, as another incident takes its first row`, async ({
      page,
    }) => {
      const platform = await incidentPlatform(page);
      await page.goto("/operate/incidents");
      await row(page, kept).focus();
      platform.items = [newest(added), ...platform.items];
      await refresh(page);
      await expect(codes(scope(page)).first()).toHaveText(first);
      await expect(row(page, kept)).toBeFocused();
    });

  test("beside a case, as an incident joins it, and as another case comes between", async ({
    page,
  }) => {
    const kept = "WV-TXN-CANCEL-STUCK at hold[3]";
    const platform = await incidentPlatform(page);
    await page.goto("/operate/incidents");
    await row(page, kept).focus();
    // Between the case and its row.
    platform.items = platform.items.flatMap((item) =>
      item.id === ids.loop ? [newest(unknown), item] : [item],
    );
    await refresh(page);
    await expect(codes(high(page))).toHaveText([
      "WV-TXN-PIVOT-UNKNOWN",
      "WV-TXN-SETTLE-TIMEOUT",
      "WV-TXN-UNDO-STUCK",
      "WV-TXN-AFTER-PIVOT-STUCK",
      "WV-TXN-CANCEL-STUCK",
    ]);
    await expect(row(page, kept)).toBeFocused();
    // Another case, right before its row: the row belongs to the rows after it.
    const second: IncidentView = {
      ...called,
      id: "d1000000-0000-4000-8000-000000000002",
      held_by: {
        ...called.held_by!,
        id: "d1000000-0000-4000-8000-00000000000f",
      },
    };
    platform.items = platform.items.flatMap((item) =>
      item.id === ids.loop ? [second, item] : [item],
    );
    await refresh(page);
    await expect(page.locator(".case")).toHaveCount(2);
    await expect(row(page, kept)).toBeFocused();
  });

  test("inside a case, as the incident that waits for it leads, or leaves", async ({
    page,
  }) => {
    const kept = "WV-TXN-UNDO-STUCK at add";
    const platform = await incidentPlatform(page, {
      items: [unknown, called, ...others],
    });
    await page.goto("/operate/incidents");
    await row(page, kept).focus();
    platform.items = [unknown, caller, called, ...others];
    await refresh(page);
    await expect(codes(caseOf(page))).toHaveText([
      "WV-TXN-SETTLE-TIMEOUT",
      "WV-TXN-UNDO-STUCK",
    ]);
    await expect(row(page, kept)).toBeFocused();
    platform.items = [unknown, called, ...others];
    await refresh(page);
    await expect(codes(caseOf(page))).toHaveText([
      "WV-TXN-UNDO-STUCK",
      "WV-TXN-SETTLE-TIMEOUT",
    ]);
    await expect(row(page, kept)).toBeFocused();
  });

  test("on the Open of an incident that isn't in the list, as a page lists it", async ({
    page,
  }) => {
    const platform = await incidentPlatform(page, { items: [called] });
    await page.goto("/operate/incidents");
    await row(page, "WV-TXN-SETTLE-TIMEOUT, move in run 6d18b3f0").focus();
    platform.items = [caller, called];
    await refresh(page);
    await expect(page.locator(".not-listed")).toHaveCount(0);
    await expect(row(page, "WV-TXN-SETTLE-TIMEOUT at move")).toBeFocused();
  });

  test("as the first incident in doubt turns one list into bands, and as the last one leaves", async ({
    page,
  }) => {
    const platform = await incidentPlatform(page, { items: others });
    await page.goto("/operate/incidents");
    await row(page, "WV-TASK-FAILED at fetch").focus();
    platform.items = [unknown, ...others];
    await refresh(page);
    await expect(codes(high(page))).toHaveCount(1);
    await expect(row(page, "WV-TASK-FAILED at fetch")).toBeFocused();
    platform.items = others;
    await refresh(page);
    await expect(high(page)).toHaveCount(0);
    await expect(row(page, "WV-TASK-FAILED at fetch")).toBeFocused();
  });

  const band = (page: Page, name: string) =>
    page.getByRole("heading", { level: 2, name });
  /** Incidents decided elsewhere, as the next refresh finds them gone. */
  async function decidedElsewhere(
    page: Page,
    platform: IncidentPlatform,
    ...gone: IncidentView[]
  ) {
    const out = new Set(gone.map((item) => item.id));
    platform.items = platform.items.filter((item) => !out.has(item.id));
    await refresh(page);
    for (const id of out)
      await expect(
        page.locator(`.incident-list .row-open[data-incident="${id}"]`),
      ).toHaveCount(0);
  }
  for (const [name, items, from, gone, to] of [
    [
      "one in a list gives it to the next row",
      others,
      "WV-TASK-FAILED at fetch",
      [others[1]],
      (page: Page) => row(page, "WV-TASK-LOST at send"),
    ],
    [
      "one in a list gives it past the rows that go with it",
      others,
      "WV-TASK-FAILED at fetch",
      [others[1], others[2]],
      (page: Page) => row(page, "WV-TASK-AMBIGUOUS at write"),
    ],
    [
      "the last of a list gives it to the row before it",
      others,
      "WV-TASK-AMBIGUOUS at write",
      [others[3]],
      (page: Page) => row(page, "WV-TASK-LOST at send"),
    ],
    [
      "the only one gives it to the page's title",
      [others[1]],
      "WV-TASK-FAILED at fetch",
      [others[1]],
      (page: Page) => page.getByRole("heading", { level: 1 }),
    ],
    [
      "one of a case gives it to the next of its band",
      [unknown, caller, called, loop, ...others],
      "WV-TXN-UNDO-STUCK at add",
      [caller, called],
      (page: Page) => row(page, "WV-TXN-CANCEL-STUCK at hold[3]"),
    ],
    [
      "the last of a band gives it to the band's heading",
      [unknown, loop, ...others],
      "WV-TXN-CANCEL-STUCK at hold[3]",
      [loop],
      (page: Page) => band(page, "High priority"),
    ],
    [
      "the last of Other incidents gives it to that heading",
      [unknown, ...others],
      "WV-TASK-AMBIGUOUS at write",
      [others[3]],
      (page: Page) => band(page, "Other incidents"),
    ],
    [
      "the only one of a band gives it to the row after it",
      [unknown, ...others],
      "WV-TXN-PIVOT-UNKNOWN at submit",
      [unknown],
      (page: Page) => row(page, "WV-TASK-AMBIGUOUS at send"),
    ],
  ] as const)
    test(`as incidents leave: ${name}`, async ({ page }) => {
      const platform = await incidentPlatform(page, { items: [...items] });
      await page.goto("/operate/incidents");
      await row(page, from).focus();
      await decidedElsewhere(page, platform, ...gone);
      await expect(to(page)).toBeFocused();
      // It is a place in the list like any other: the next refresh keeps it.
      platform.items = [...platform.items, newest(others[0])];
      await refresh(page);
      await expect(codes(page.locator(".incident-list")).last()).toHaveText(
        "WV-TASK-REFUSED",
      );
      await expect(to(page)).toBeFocused();
    });

  test("and leaves it alone once the person moved it", async ({ page }) => {
    const platform = await incidentPlatform(page, { items: others });
    await page.goto("/operate/incidents");
    await row(page, "WV-TASK-FAILED at fetch").focus();
    await page.getByLabel("Workflow", { exact: true }).focus();
    platform.items = [unknown, ...others];
    await refresh(page);
    await expect(codes(high(page))).toHaveCount(1);
    await expect(page.getByLabel("Workflow", { exact: true })).toBeFocused();
    // Nor does a focus nobody has go to a row.
    await page.getByLabel("Workflow", { exact: true }).blur();
    platform.items = others;
    await refresh(page);
    await expect(high(page)).toHaveCount(0);
    await expect(page.locator("body")).toBeFocused();
  });

  test.describe("behind the drawer at 800x900", () => {
    test.use({ viewport: { width: 800, height: 900 } });

    for (const [name, before, added] of [
      ["another incident in doubt opens", [unknown, ...others], newest(loop)],
      ["the first incident in doubt opens", others, unknown],
    ] as const)
      test(`Escape returns to the row that opened it after ${name}`, async ({
        page,
      }) => {
        const opener = row(
          page,
          before[0].priority
            ? "WV-TXN-PIVOT-UNKNOWN at submit"
            : "WV-TASK-FAILED at fetch",
        );
        const platform = await incidentPlatform(page, { items: [...before] });
        await page.goto("/operate/incidents");
        await opener.focus();
        await page.keyboard.press("Enter");
        const drawer = page.locator("weave-incident-drawer");
        await expect(drawer.locator("#incident-detail-title")).toBeFocused();
        platform.items = [added, ...before];
        await refresh(page);
        await expect(
          page.locator(`.incident-band.high .row-open`).first(),
        ).toHaveText(added.code);
        // Reading the list never takes the focus out of the drawer.
        await expect(drawer.locator("#incident-detail-title")).toBeFocused();
        await page.keyboard.press("Escape");
        await expect(drawer).toHaveCount(0);
        await expect(opener).toBeFocused();
      });

    for (const [name, leave] of [
      ["decided elsewhere", decidedElsewhere],
      [
        "decided in the sheet",
        async (page: Page, platform: IncidentPlatform, gone: IncidentView) => {
          await page.route(
            `${environment}/incidents/${gone.id}/resolve`,
            (route) => {
              platform.items = platform.items.filter(
                (item) => item.id !== gone.id,
              );
              return route.fulfill({
                json: { ...gone, status: "resolved", revision: 2 },
              });
            },
          );
          await page
            .locator("weave-incident-drawer")
            .getByRole("button", { name: "Resolve incident", exact: true })
            .click();
          await endTheRun(page);
          await expect(
            page.getByRole("dialog", { name: "Resolve incident" }),
          ).toHaveCount(0);
          await expect(
            page.locator(`.incident-list [data-incident="${gone.id}"]`),
          ).toHaveCount(0);
          await page.clock.runFor(100);
        },
      ],
    ] as const)
      test(`Escape returns to the next row once the incident is ${name}`, async ({
        page,
      }) => {
        const platform = await incidentPlatform(page, { items: [...others] });
        await page.goto("/operate/incidents");
        await row(page, "WV-TASK-FAILED at fetch").focus();
        await page.keyboard.press("Enter");
        const title = page.locator("#incident-detail-title");
        await expect(title).toBeFocused();
        await leave(page, platform, others[1]);
        await expect(title).toBeFocused();
        // A later refresh, without it too.
        await refresh(page);
        await page.keyboard.press("Escape");
        await expect(page.locator("weave-incident-drawer")).toHaveCount(0);
        await expect(row(page, "WV-TASK-LOST at send")).toBeFocused();
      });
  });
});

test.describe("a platform of an earlier release", () => {
  test.use({ viewport: { width: 1440, height: 900 }, timezoneId: "UTC" });

  const problem = (page: Page) => page.locator(".operate-problem");
  const asked = (platform: IncidentPlatform) =>
    platform.queries.map(
      (query) =>
        `${query.get("priority") ?? "every"} ${query.get("cursor") ?? "from the start"}`,
    );

  test("that refuses the priority shows one list, and Studio doesn't ask for it again", async ({
    page,
  }) => {
    const platform = await incidentPlatform(page, {
      items: [unknown, ...others].map(earlier),
      pageSize: 3,
      priority: "refuses",
    });
    await page.goto("/operate/incidents");
    await expect(rows(page)).toHaveCount(3);
    await expect(problem(page)).toHaveCount(0);
    await expect(page.getByRole("heading", { level: 2 })).toHaveCount(0);
    expect(asked(platform)).toEqual([
      "high from the start",
      "every from the start",
    ]);
    await page.getByRole("button", { name: "Load more incidents" }).click();
    await expect(rows(page)).toHaveCount(5);
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect.poll(() => platform.queries.length).toBe(5);
    await expect(rows(page)).toHaveCount(5);
    await page
      .getByLabel("Workflow", { exact: true })
      .fill("move-between-systems");
    await page.getByLabel("Workflow", { exact: true }).press("Enter");
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page)).not.toContainText("In doubt");
    expect(asked(platform)).toEqual([
      "high from the start",
      "every from the start",
      "every every:3",
      "every from the start",
      "every every:3",
      "every from the start",
    ]);
  });

  test("that refuses the priority is asked for it again once another person signs in", async ({
    page,
  }) => {
    let person = "jane";
    const identity = (host: PlatformHost) => ({
      ...host.identity(),
      principal_id: `principal-${person}`,
      grants: host.identity().grants.map((grant) => ({
        ...grant,
        capabilities: ["incident.read"],
      })),
    });
    const host = await platformHost(page, {
      platforms: [acmePlatform()],
      active: "Acme",
      test: (current) => ({
        json: {
          session: current.session(),
          identity: identity(current),
          workspaces: current.current?.workspaces ?? [],
          truncated: false,
          workspace_revoked: false,
        },
      }),
    });
    await page.route("**/studio/api/api/v1/identity", (route) =>
      route.fulfill({ json: identity(host) }),
    );
    const asked: string[] = [];
    // The platform is upgraded before John signs in.
    await page.route("**/environments/*/incidents?*", (route) => {
      const priority = new URL(route.request().url()).searchParams.get(
        "priority",
      );
      asked.push(`${person} ${priority ?? "every"}`);
      if (person === "jane" && priority)
        return route.fulfill({
          status: 422,
          json: { code: "WV-VALIDATION", message: "Unknown query parameter" },
        });
      return route.fulfill({
        json: {
          items: priority === "high" ? [unknown] : others,
          next_cursor: null,
        },
      });
    });
    await page.goto("/operate/incidents");
    await expect(rows(page)).toHaveCount(4);
    const finish = await signInWaiting(page, host, () =>
      page.getByRole("button", { name: "Incidents", exact: true }).click(),
    );
    await expect(rows(page)).toHaveCount(4);
    await expect(page.getByRole("heading", { level: 2 })).toHaveCount(0);
    person = "john";
    await finish();
    await expect(codes(high(page))).toHaveText(["WV-TXN-PIVOT-UNKNOWN"]);
    await expect(codes(other(page))).toHaveCount(4);
    expect(asked.slice(-2)).toEqual(["john high", "john normal"]);
  });

  test("that ignores the priority shows one list: no incident but a transaction's reads as in doubt", async ({
    page,
  }) => {
    const platform = await incidentPlatform(page, {
      items: [unknown, ...others].map(earlier),
      pageSize: 4,
      priority: "ignores",
    });
    await page.goto("/operate/incidents");
    await expect(rows(page)).toHaveCount(4);
    await expect(page.getByRole("heading", { level: 2 })).toHaveCount(0);
    await expect(page.locator(".band-note")).toHaveCount(0);
    await expect(page.getByText("4 incidents loaded")).toBeVisible();
    await page.getByRole("button", { name: "Load more incidents" }).click();
    await expect(codes(page.locator(".incident-list"))).toHaveText([
      "WV-TXN-PIVOT-UNKNOWN",
      "WV-TASK-AMBIGUOUS",
      "WV-TASK-FAILED",
      "WV-TASK-LOST",
      "WV-TASK-AMBIGUOUS",
    ]);
    await expect(page.getByText("5 incidents", { exact: true })).toBeVisible();
    const before = platform.queries.length;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    // The band in doubt, then the list's own two pages.
    await expect.poll(() => platform.queries.length).toBe(before + 3);
    await expect(rows(page)).toHaveCount(5);
    await expect(page.getByRole("heading", { level: 2 })).toHaveCount(0);
  });

  test("that ignores the priority and can't show an incident shows it once, in no band", async ({
    page,
  }) => {
    await incidentPlatform(page, {
      items: [
        { id: "c1000000-0000-4000-8000-000000000001", unavailable: true },
      ],
      priority: "ignores",
    });
    await page.goto("/operate/incidents");
    await expect(rows(page)).toHaveText(/Unavailable incident/);
    await expect(page.getByRole("heading", { level: 2 })).toHaveCount(0);
    await expect(page.getByText("1 incident", { exact: true })).toBeVisible();
  });

  test("a filter the platform refuses is an error, and clearing it brings the bands back", async ({
    page,
  }) => {
    const platform = await incidentPlatform(page);
    const refused = {
      status: 422,
      json: { code: "WV-VALIDATION", message: "Invalid workflow" },
    };
    await page.route(`${environment}/incidents?*workflow=*`, (route) =>
      route.fulfill(refused),
    );
    await page.goto("/operate/incidents?workflow=not%20a%20name");
    await expect(page.getByText("Support code: WV-VALIDATION")).toBeVisible();
    await page.unroute(`${environment}/incidents?*workflow=*`);
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(
      page.getByRole("button", { name: /^Clear filters/ }),
    ).toBeVisible();
    await page.getByRole("button", { name: /^Clear filters/ }).click();
    await expect(codes(high(page))).toHaveCount(4);
    expect(asked(platform).slice(-2)).toEqual([
      "high from the start",
      "normal from the start",
    ]);
  });

  test("a cursor the platform refuses is an error: the bands stay, and Studio keeps asking for them", async ({
    page,
  }) => {
    const platform = await incidentPlatform(page, { pageSize: 3 });
    await page.goto("/operate/incidents");
    await high(page)
      .getByRole("button", { name: "Load more incidents" })
      .click();
    await expect(codes(high(page))).toHaveCount(4);
    let held = 0;
    await page.route(`${environment}/incidents?*cursor=high*`, (route) => {
      held++;
      return route.fulfill({
        status: 422,
        json: { code: "WV-VALIDATION", message: "Invalid scope-bound cursor" },
      });
    });
    const before = platform.queries.length;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(page.getByText("Support code: WV-VALIDATION")).toBeVisible();
    expect(held).toBe(1);
    await expect(codes(high(page))).toHaveCount(4);
    await expect(codes(other(page))).toHaveCount(3);
    // Neither one list nor the other band was read after it.
    expect(asked(platform).slice(before)).toEqual(["high from the start"]);
    await page.unroute(`${environment}/incidents?*cursor=high*`);
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(problem(page)).toHaveCount(0);
    await expect(codes(high(page))).toHaveCount(4);
    expect(asked(platform).slice(before + 1)).toEqual([
      "high from the start",
      "high high:3",
      "normal from the start",
    ]);
  });
});

for (const size of [
  { name: "a phone at 360x640", width: 360, height: 640 },
  { name: "a window at 600x500", width: 600, height: 500 },
])
  test.describe(`transactions in doubt on ${size.name}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.width < 400,
      timezoneId: "UTC",
    });

    test("one column of bands, the case and its missing member's Open within the screen", async ({
      page,
    }) => {
      await incidentPlatform(page, { items: [unknown, called, ...others] });
      await page.goto("/operate/incidents");
      const group = caseOf(page);
      await expect(group).toBeVisible();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth - window.innerWidth,
        ),
      ).toBeLessThanOrEqual(0);
      const open = group.getByRole("button", {
        name: "Open incident WV-TXN-SETTLE-TIMEOUT, move in run 6d18b3f0",
      });
      await expect(open).toBeVisible();
      const box = (await open.boundingBox())!;
      expect(box.height).toBeGreaterThanOrEqual(43.5);
      expect(box.x + box.width).toBeLessThanOrEqual(size.width);
      // No code or key is cut: every cell's text fits its cell.
      const cut = await high(page).evaluate((section) =>
        [
          ...section.querySelectorAll<HTMLElement>(
            ".row-open, .row-text small, .business-key, .case-head strong, .tag, .status-pill",
          ),
        ]
          .filter((node) => node.scrollWidth > node.clientWidth + 1)
          .map((node) => node.textContent),
      );
      expect(cut).toEqual([]);
      await page.screenshot({
        path: `${captures}/inbox-bands-${size.width}.png`,
      });
      await open.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: `${captures}/inbox-case-missing-${size.width}.png`,
      });
    });
  });
