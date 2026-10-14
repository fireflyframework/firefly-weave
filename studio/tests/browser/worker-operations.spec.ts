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
// Operate › Workers: presence, claims, load and last contact for every
// worker, filters kept in the address, a worker's own address, and Drain and
// Resume with their error paths. The server checks every command.
import { test, expect, Page, Route } from "@playwright/test";
import { connected } from "./support";
import { acmePlatform, platformHost, type PlatformHost } from "./platform-host";

const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";
const online = "11111111-1111-4111-8111-111111111111";
const stale = "12222222-2222-4222-8222-222222222222";
const fresh = "13333333-3333-4333-8333-333333333333";
const revoked = "14444444-4444-4444-8444-444444444444";
const at = (seconds: number) =>
  new Date(Date.now() + seconds * 1000).toISOString();
const worker = (id: string, overrides: Record<string, unknown> = {}) => ({
  id,
  principal_id: "22222222-2222-4222-8222-222222222222",
  release_id: "33333333-3333-4333-8333-333333333333",
  task_types: ["crm-lookup@1.0.0"],
  capacity: 8,
  revoked: false,
  revision: 2,
  draining: false,
  presence: "recent",
  last_seen_at: at(-5),
  presence_expires_at: at(55),
  presence_ttl_seconds: 60,
  observed_at: at(0),
  active_leases: 2,
  available_capacity: 6,
  ...overrides,
});

async function workers(page: Page, capabilities: string[]) {
  await connected(page, { capabilities });
  const items = [
    worker(online),
    worker(stale, {
      presence: "stale",
      last_seen_at: at(-600),
      presence_expires_at: at(-540),
      release_id: "34444444-4444-4444-8444-444444444444",
      active_leases: 0,
      available_capacity: null,
    }),
    worker(fresh, {
      presence: "unknown",
      last_seen_at: null,
      presence_expires_at: null,
      registered_at: at(-120),
      task_types: ["email-send@1.0.0", "crm-lookup@1.0.0"],
      active_leases: 9,
    }),
    worker(revoked, { revoked: true }),
  ];
  let lists = 0;
  const queries: URLSearchParams[] = [];
  await page.route(`${environment}/workers?*`, (r) => {
    lists++;
    const query = new URL(r.request().url()).searchParams;
    queries.push(query);
    // As the server does: without state=all only current workers, not revoked
    // and seen (or registered) in the last hour; then every filter.
    const listed = (
      query.get("state") === "all" ? items : items.filter(isCurrent)
    ).filter((item) => serverMatches(item, query));
    return r.fulfill({ json: { items: listed, next_cursor: null } });
  });
  for (const item of items)
    await page.route(`${environment}/workers/${item.id}`, (r) =>
      r.fulfill({ json: items.find((w) => w.id === item.id) }),
    );
  return { items, lists: () => lists, queries: () => queries };
}
const isCurrent = (item: Record<string, unknown>) => {
  const contact = (item.last_seen_at ?? item.registered_at) as string | null;
  return (
    !item.revoked &&
    contact !== null &&
    contact !== undefined &&
    Date.parse(contact) > Date.now() - 3600_000
  );
};
/** The platform's own filters, as `workers.list` applies them. */
function serverMatches(
  item: Record<string, unknown>,
  query: URLSearchParams,
): boolean {
  const presence = query.get("presence");
  if (presence && item.presence !== presence) return false;
  const draining = query.get("draining");
  if (draining !== null && String(item.draining) !== draining) return false;
  const release = query.get("release_id");
  if (release && item.release_id !== release) return false;
  const type = query.get("task_type");
  if (type && !(item.task_types as string[]).includes(type)) return false;
  const target = query.get("target_id");
  const placement = item.placement as { target_id?: string } | undefined;
  if (target && placement?.target_id !== target) return false;
  const host = query.get("host_prefix");
  const identity = item.identity as { host?: string } | undefined;
  if (host && !identity?.host?.startsWith(host)) return false;
  return true;
}
/** Narrow screens fold the filters behind "Filters (n)": open them. */
async function openFilters(page: Page) {
  await page.locator(".operate-filters").waitFor({ state: "attached" });
  const toggle = page.locator(".filters-toggle");
  if (await toggle.isVisible()) await toggle.click();
}
const rows = (page: Page) =>
  page.getByRole("table", { name: "Workers" }).locator(".resource-row");

test("a filter asks the platform for its first page, so a match beyond the loaded pages shows", async ({
  page,
}) => {
  await workers(page, ["status.read"]);
  const queries: URLSearchParams[] = [];
  // Unfiltered, the offline worker sits on the third page of 50.
  await page.route(`${environment}/workers?*`, (r) => {
    const query = new URL(r.request().url()).searchParams;
    queries.push(query);
    if (query.get("presence") === "stale")
      return r.fulfill({
        json: {
          items: [worker(stale, { presence: "stale" })],
          next_cursor: null,
        },
      });
    const page = Number(query.get("cursor") ?? "0");
    return r.fulfill({
      json: {
        items:
          page === 2
            ? [worker(stale, { presence: "stale" })]
            : Array.from({ length: 50 }, (_, i) =>
                worker(
                  "18888888-8888-4888-8888-" +
                    String(page * 50 + i).padStart(12, "0"),
                ),
              ),
        next_cursor: page < 2 ? String(page + 1) : null,
      },
    });
  });
  await page.goto("/operate/workers");
  await expect(rows(page)).toHaveCount(50);
  await expect(page.getByText("50 workers loaded")).toBeVisible();
  const before = queries.length;
  await page.getByLabel("Status").selectOption("offline");
  await expect(rows(page)).toHaveCount(1);
  await expect(rows(page)).toContainText("Offline");
  const filtered = queries.slice(before);
  expect(filtered.length).toBeGreaterThan(0);
  for (const query of filtered) {
    expect(query.get("presence")).toBe("stale");
    expect(query.get("state")).toBe("current");
    expect(query.get("order")).toBe("last_seen_desc");
    expect(query.has("cursor")).toBe(false);
  }
});

test("workers coalesce repeated appends and refresh all loaded pages", async ({
  page,
}) => {
  await workers(page, ["status.read"]);
  let pending: Route | undefined;
  let appends = 0;
  let refreshed = false;
  await page.route(`${environment}/workers?*`, (r) => {
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
          items: cursor ? [worker(stale)] : [worker(online)],
          next_cursor: cursor ? null : "next",
        },
      })
      .then(() => {
        if (!cursor && pending) refreshed = true;
      });
  });
  await page.goto("/operate/workers");
  await expect(rows(page)).toHaveCount(1);
  // Dispatch two events in one turn, including when the first disables the control.
  await page
    .getByRole("button", { name: "Load more workers" })
    .evaluate((button) => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
  await expect.poll(() => appends).toBe(1);
  await expect(
    page.getByRole("button", { name: "Load more workers" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await pending!.fulfill({
    json: { items: [worker(stale)], next_cursor: null },
  });
  await expect.poll(() => refreshed).toBe(true);
  await expect(
    page.getByRole("button", { name: "Refresh", exact: true }),
  ).not.toHaveAttribute("aria-disabled", "true");
  await expect(rows(page)).toHaveCount(2);
  expect(appends).toBe(2);
});

test("workers keep an append requested during a delayed refresh", async ({
  page,
}) => {
  await workers(page, ["status.read"]);
  let refresh: Route | undefined;
  let reads = 0;
  await page.route(`${environment}/workers?*`, (r) => {
    const cursor = new URL(r.request().url()).searchParams.get("cursor");
    if (!cursor && ++reads === 2) {
      refresh = r;
      return;
    }
    return r.fulfill({
      json: {
        items: cursor ? [worker(stale)] : [worker(online)],
        next_cursor: cursor ? null : "next",
      },
    });
  });
  await page.goto("/operate/workers");
  await expect(rows(page)).toHaveCount(1);
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => !!refresh).toBe(true);
  await page.getByRole("button", { name: "Load more workers" }).click();
  await refresh!.fulfill({
    json: { items: [worker(online)], next_cursor: "next" },
  });
  await expect(rows(page)).toHaveCount(2);
});

test("workers stop at the page limit and explain remaining records", async ({
  page,
}) => {
  await workers(page, ["status.read"]);
  let reads = 0;
  await page.route(`${environment}/workers?*`, (r) => {
    reads++;
    return r.fulfill({
      json: {
        items: [
          worker("19999999-9999-4999-8999-" + String(reads).padStart(12, "0")),
        ],
        next_cursor: `page-${reads}`,
      },
    });
  });
  await page.goto("/operate/workers");
  for (let i = 1; i < 10; i++) {
    await expect(rows(page)).toHaveCount(i);
    await page.getByRole("button", { name: "Load more workers" }).click();
  }
  await expect(rows(page)).toHaveCount(10);
  await expect(
    page.getByRole("button", { name: "Load more workers" }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Showing the first 500 workers.", { exact: false }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => reads).toBe(20);
  await expect(rows(page)).toHaveCount(10);
});

test("workers discard an append queued after the page closes", async ({
  page,
}) => {
  await workers(page, ["status.read"]);
  let delayed: Route | undefined;
  let reads = 0;
  let appends = 0;
  await page.route(`${environment}/workers?*`, (r) => {
    const cursor = new URL(r.request().url()).searchParams.get("cursor");
    if (cursor) appends++;
    if (!cursor && ++reads === 2) {
      delayed = r;
      return;
    }
    return r.fulfill({
      json: { items: [worker(online)], next_cursor: "next" },
    });
  });
  await page.goto("/operate/workers");
  await expect(rows(page)).toHaveCount(1);
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect.poll(() => !!delayed).toBe(true);
  await page.getByRole("button", { name: "Load more workers" }).click();
  await page.getByRole("button", { name: "Home", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Home", exact: true }),
  ).toBeVisible();
  await delayed!.fulfill({
    json: { items: [worker(online)], next_cursor: "next" },
  });
  await page.goto("/operate/workers");
  await expect(rows(page)).toHaveCount(1);
  expect(appends).toBe(0);
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("lists presence, claims and load, and hides revoked workers", async ({
      page,
    }) => {
      const mocked = await workers(page, ["status.read"]);
      await page.getByRole("button", { name: "Workers", exact: true }).click();
      await expect(page).toHaveURL(/\/operate\/workers$/);
      await expect(rows(page)).toHaveCount(3);
      await expect(rows(page).nth(0)).toContainText("Online");
      await expect(rows(page).nth(0)).toContainText("2 of 8");
      await expect(rows(page).nth(1)).toContainText("Offline");
      await expect(rows(page).nth(2)).toContainText("Not seen yet");
      await expect(rows(page).nth(2).locator(".meter")).toHaveClass(/over/);
      await openFilters(page);
      await expect(page.getByText("3 workers", { exact: true })).toBeVisible();
      // The platform lists current workers, last contact first.
      expect(mocked.queries().length).toBeGreaterThan(0);
      for (const query of mocked.queries()) {
        expect(query.get("state")).toBe("current");
        expect(query.get("order")).toBe("last_seen_desc");
      }
      await page.getByLabel("Show retired workers").check();
      await expect(rows(page)).toHaveCount(4);
      await expect(rows(page).nth(3)).toContainText("Revoked");
      expect(mocked.queries().at(-1)!.get("state")).toBe("all");
      await expect(page).toHaveURL(/\/operate\/workers\?revoked=true$/);
    });

    test("filters narrow the list and stay in the address", async ({
      page,
    }) => {
      const mocked = await workers(page, ["status.read"]);
      await page.goto("/operate/workers?presence=offline");
      await openFilters(page);
      await expect(page.getByLabel("Status")).toHaveValue("offline");
      await expect(rows(page)).toHaveCount(1);
      await expect(page.getByText("1 worker", { exact: true })).toBeVisible();
      expect(mocked.queries().at(-1)!.get("presence")).toBe("stale");
      await page.getByLabel("Status").selectOption("");
      await page.getByLabel("Task type").selectOption("email-send@1.0.0");
      await expect(page).toHaveURL(
        /\/operate\/workers\?task_type=email-send(@|%40)1\.0\.0$/,
      );
      await expect(rows(page)).toHaveCount(1);
      expect(mocked.queries().at(-1)!.get("task_type")).toBe(
        "email-send@1.0.0",
      );
      await page.getByLabel("Draining", { exact: true }).selectOption("true");
      await expect(page).toHaveURL(/draining=true/);
      await expect(
        page.getByRole("heading", { name: "No workers match these filters" }),
      ).toBeVisible();
      await page.getByRole("button", { name: "Clear filters" }).first().click();
      await expect(page).toHaveURL(/\/operate\/workers$/);
      await expect(rows(page)).toHaveCount(3);
    });

    test("reloads every 10 seconds while the page is visible", async ({
      page,
    }) => {
      await page.clock.install();
      const { lists } = await workers(page, ["status.read"]);
      await page.goto("/operate/workers");
      await expect(rows(page)).toHaveCount(3);
      const before = lists();
      await page.clock.fastForward(10_500);
      await expect.poll(lists).toBe(before + 1);
      await expect(page.getByText(/^Updated /)).toBeVisible();
    });

    test("a worker's address opens its detail, and Drain asks first", async ({
      page,
    }) => {
      const { items } = await workers(page, ["status.read", "worker.drain"]);
      const commands: { body: unknown; key?: string }[] = [];
      await page.route(`${environment}/workers/${online}/drain`, (r) => {
        commands.push({
          body: r.request().postDataJSON(),
          key: r.request().headers()["idempotency-key"],
        });
        items[0] = { ...items[0], draining: true, revision: 3 };
        return r.fulfill({ json: items[0] });
      });
      await page.goto(`/operate/workers/${online}`);
      const detail = page.locator("weave-worker-detail");
      await expect(detail.locator("#worker-detail-title")).toHaveText(
        "Worker 11111111",
      );
      await expect(detail).toContainText("Accepting new tasks");
      await detail.getByRole("button", { name: "Drain", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: "Drain this worker?" });
      await expect(dialog).toContainText(
        "It finishes its 2 active tasks and takes no new ones. You can resume it at any time.",
      );
      await dialog.getByRole("button", { name: "Drain", exact: true }).click();
      await expect(detail.locator(".badges")).toContainText("Draining");
      await expect(
        detail.getByRole("button", { name: "Resume", exact: true }),
      ).toBeVisible();
      expect(commands).toHaveLength(1);
      expect(commands[0].body).toEqual({ expected_revision: 2 });
      expect(commands[0].key).toBeTruthy();
      await detail.getByRole("button", { name: "Close detail" }).click();
      await expect(page).toHaveURL(/\/operate\/workers$/);
    });

    test("Resume on a draining worker sends its revision and takes new tasks again", async ({
      page,
    }) => {
      const { items } = await workers(page, ["status.read", "worker.drain"]);
      items[0] = { ...items[0], draining: true, revision: 3 };
      const commands: { body: unknown; key?: string }[] = [];
      await page.route(`${environment}/workers/${online}/resume`, (r) => {
        commands.push({
          body: r.request().postDataJSON(),
          key: r.request().headers()["idempotency-key"],
        });
        items[0] = { ...items[0], draining: false, revision: 4 };
        return r.fulfill({ json: items[0] });
      });
      await page.goto(`/operate/workers/${online}`);
      const detail = page.locator("weave-worker-detail");
      await expect(detail.locator(".badges")).toContainText("Draining");
      await detail.getByRole("button", { name: "Resume", exact: true }).click();
      await expect(
        page.getByText("Worker 11111111 takes new tasks again."),
      ).toBeVisible();
      await expect(detail.locator(".badges")).not.toContainText("Draining");
      await expect(
        detail.getByRole("button", { name: "Drain", exact: true }),
      ).toBeVisible();
      expect(commands).toHaveLength(1);
      expect(commands[0].body).toEqual({ expected_revision: 3 });
      expect(commands[0].key).toBeTruthy();
    });

    test("closing a worker steps back, so Back leaves Workers and does not repeat the list", async ({
      page,
    }) => {
      await workers(page, ["status.read"]);
      await page.getByRole("button", { name: "Workers", exact: true }).click();
      await expect(page).toHaveURL(/\/operate\/workers$/);
      await expect(rows(page)).toHaveCount(3);
      const detail = page.locator("weave-worker-detail");
      for (let cycle = 0; cycle < 2; cycle++) {
        await page.getByRole("button", { name: "Worker 11111111" }).click();
        await expect(page).toHaveURL(new RegExp(`/operate/workers/${online}$`));
        await expect(detail.locator("#worker-detail-title")).toHaveText(
          "Worker 11111111",
        );
        await detail.getByRole("button", { name: "Close detail" }).click();
        await expect(page).toHaveURL(/\/operate\/workers$/);
        await expect(detail).toHaveCount(0);
      }
      // Two open-and-close cycles leave one Workers entry, not one per cycle.
      await page.goBack();
      await expect(page).toHaveURL(/\/home$/);
    });

    test("a changed worker explains WV-WORKER-REVISION and refreshes", async ({
      page,
    }) => {
      const { items } = await workers(page, ["status.read", "worker.drain"]);
      await page.route(`${environment}/workers/${online}/drain`, (r) => {
        items[0] = { ...items[0], revision: 5 };
        return r.fulfill({
          status: 409,
          json: {
            code: "WV-WORKER-REVISION",
            message: "Worker control state changed; refresh before retrying",
          },
        });
      });
      await page.goto("/operate/workers");
      await page.getByRole("button", { name: "Worker 11111111" }).click();
      const detail = page.locator("weave-worker-detail");
      await detail.getByRole("button", { name: "Drain", exact: true }).click();
      await page
        .getByRole("dialog", { name: "Drain this worker?" })
        .getByRole("button", { name: "Drain", exact: true })
        .click();
      await expect(detail.getByRole("alert")).toContainText(
        "This worker changed since you opened it. Refresh it, then try again.",
      );
      await expect(detail.getByRole("alert")).toContainText(
        "Support code: WV-WORKER-REVISION",
      );
      await detail.getByRole("button", { name: "Refresh worker" }).click();
      await expect(detail.locator("dd").filter({ hasText: /^5$/ })).toHaveCount(
        1,
      );
      // The explanation goes once the worker is read again.
      await expect(detail.getByRole("alert")).toHaveCount(0);
    });

    if (viewport.width > 1024)
      test("choosing Workers again closes the open worker and clears the filters", async ({
        page,
      }) => {
        await workers(page, ["status.read"]);
        await page.goto(`/operate/workers/${online}?presence=online`);
        await expect(page.locator("weave-worker-detail")).toBeVisible();
        await page
          .getByRole("button", { name: "Workers", exact: true })
          .click();
        await expect(page).toHaveURL(/\/operate\/workers$/);
        await expect(page.locator("weave-worker-detail")).toHaveCount(0);
        await expect(page.getByLabel("Status")).toHaveValue("");
        await expect(rows(page)).toHaveCount(3);
      });

    test("rows fit beside an open worker without clipping or sideways scrolling", async ({
      page,
    }) => {
      await workers(page, ["status.read"]);
      await page.goto(`/operate/workers/${online}`);
      await expect(page.locator("weave-worker-detail")).toBeVisible();
      await expect(rows(page)).toHaveCount(3);
      await expect(rows(page).nth(0).locator(".meter-text")).toBeVisible();
      const layout = await page.evaluate(() => {
        const table = document
          .querySelector(".worker-table")!
          .getBoundingClientRect();
        const cells = [
          ...document.querySelectorAll<HTMLElement>(
            ".worker-table [role=cell], .worker-table [role=columnheader]",
          ),
        ].filter((cell) => cell.offsetParent !== null);
        return {
          sideways:
            document.documentElement.scrollWidth >
            document.documentElement.clientWidth,
          clipped: cells
            .filter((cell) => {
              const box = cell.getBoundingClientRect();
              return (
                box.right > table.right + 0.5 ||
                box.left < table.left - 0.5 ||
                cell.scrollWidth > cell.clientWidth + 1
              );
            })
            .map((cell) => cell.textContent?.trim()),
        };
      });
      expect(layout).toEqual({ sideways: false, clipped: [] });
    });

    if (viewport.width > 1024) {
      // A command's answer belongs to the worker it was for, never to the
      // worker the person opened while it was on its way.
      const drainThenOpenAnother = async (
        page: Page,
        answer: (
          route: Route,
          items: Record<string, unknown>[],
        ) => Promise<void>,
      ) => {
        const { items } = await workers(page, ["status.read", "worker.drain"]);
        let finish!: () => void;
        const gate = new Promise<void>((resolve) => (finish = resolve));
        await page.route(
          `${environment}/workers/${online}/drain`,
          async (r) => {
            await gate;
            await answer(r, items);
          },
        );
        await page.goto(`/operate/workers/${online}`);
        const detail = page.locator("weave-worker-detail");
        await detail
          .getByRole("button", { name: "Drain", exact: true })
          .click();
        await page
          .getByRole("dialog", { name: "Drain this worker?" })
          .getByRole("button", { name: "Drain", exact: true })
          .click();
        await page.getByRole("button", { name: "Worker 12222222" }).click();
        await expect(detail.locator("#worker-detail-title")).toHaveText(
          "Worker 12222222",
        );
        return {
          detail,
          // Lets the first worker's answer arrive and the page handle it.
          answerArrives: async () => {
            const answered = page.waitForResponse((response) =>
              response.url().endsWith(`/workers/${online}/drain`),
            );
            finish();
            await answered;
            await page.evaluate(
              () =>
                new Promise<void>((resolve) =>
                  requestAnimationFrame(() => setTimeout(resolve, 100)),
                ),
            );
          },
        };
      };

      test("a Drain on its way for one worker does not hold up another worker's", async ({
        page,
      }) => {
        const { detail, answerArrives } = await drainThenOpenAnother(
          page,
          async (r, items) => r.fulfill({ json: items[0] }),
        );
        await expect(
          detail.getByRole("button", { name: "Drain", exact: true }),
        ).toBeEnabled();
        await answerArrives();
      });

      test("a finished Drain never changes the worker opened meanwhile", async ({
        page,
      }) => {
        const { detail, answerArrives } = await drainThenOpenAnother(
          page,
          async (r, items) => {
            items[0] = { ...items[0], draining: true, revision: 3 };
            await r.fulfill({ json: items[0] });
          },
        );
        await answerArrives();
        await expect(
          page.getByText("Worker 11111111 is draining."),
        ).toBeVisible();
        await expect(detail.locator("#worker-detail-title")).toHaveText(
          "Worker 12222222",
        );
        await expect(page).toHaveURL(new RegExp(`/operate/workers/${stale}$`));
        await expect(detail.locator(".badges")).toContainText("Offline");
      });

      test("a refused Drain never becomes the alert of the worker opened meanwhile", async ({
        page,
      }) => {
        const { detail, answerArrives } = await drainThenOpenAnother(
          page,
          async (r) => {
            await r.fulfill({
              status: 409,
              json: {
                code: "WV-WORKER-REVISION",
                message:
                  "Worker control state changed; refresh before retrying",
              },
            });
          },
        );
        await answerArrives();
        await expect(detail.getByRole("alert")).toHaveCount(0);
        await expect(detail.locator("#worker-detail-title")).toHaveText(
          "Worker 12222222",
        );
        // The person still learns what happened to the worker they asked about.
        await expect(
          page.getByText("Worker 11111111 was not drained."),
        ).toBeVisible();
      });
    }

    test("closing the detail while Drain runs leaves no warning and still says what happened", async ({
      page,
    }) => {
      const warnings: string[] = [];
      page.on("console", (message) => {
        if (/NG0\d+/.test(message.text())) warnings.push(message.text());
      });
      const { items } = await workers(page, ["status.read", "worker.drain"]);
      let finish!: () => void;
      const gate = new Promise<void>((resolve) => (finish = resolve));
      await page.route(`${environment}/workers/${online}/drain`, async (r) => {
        await gate;
        items[0] = { ...items[0], draining: true, revision: 3 };
        await r.fulfill({ json: items[0] });
      });
      await page.goto(`/operate/workers/${online}`);
      const detail = page.locator("weave-worker-detail");
      await detail.getByRole("button", { name: "Drain", exact: true }).click();
      await page
        .getByRole("dialog", { name: "Drain this worker?" })
        .getByRole("button", { name: "Drain", exact: true })
        .click();
      await detail.getByRole("button", { name: "Close detail" }).click();
      await expect(detail).toHaveCount(0);
      finish();
      await expect(
        page.getByText("Worker 11111111 is draining."),
      ).toBeVisible();
      expect(warnings).toEqual([]);
    });

    test("a deep link to a worker that can't be found says so and returns to the list", async ({
      page,
    }) => {
      await workers(page, ["status.read"]);
      const missing = "15555555-5555-4555-8555-555555555555";
      let answered!: () => void;
      const missingAnswered = new Promise<void>(
        (resolve) => (answered = resolve),
      );
      await page.route(`${environment}/workers/${missing}`, async (r) => {
        await r.fulfill({
          status: 404,
          json: { code: "WV-NOT-FOUND", message: "Worker not found" },
        });
        answered();
      });
      // The list answers after the worker read, as the next reload would.
      await page.route(`${environment}/workers?*`, async (r) => {
        await missingAnswered;
        await r.fallback();
      });
      await page.goto(`/operate/workers/${missing}`);
      await expect(rows(page)).toHaveCount(3);
      const problem = page.getByRole("alert");
      await expect(problem).toContainText("Worker not found");
      await expect(problem).toContainText("Support code: WV-NOT-FOUND");
      await expect(page).toHaveURL(/\/operate\/workers$/);
      await expect(page.locator("weave-worker-detail")).toHaveCount(0);
    });

    test("opening a worker that is gone shows why and puts the list address back", async ({
      page,
    }) => {
      await workers(page, ["status.read"]);
      await page.route(`${environment}/workers/${stale}`, (r) =>
        r.fulfill({
          status: 404,
          json: { code: "WV-NOT-FOUND", message: "Worker not found" },
        }),
      );
      await page.goto("/operate/workers");
      await page.getByRole("button", { name: "Worker 12222222" }).click();
      await expect(page.getByRole("alert")).toContainText(
        "Support code: WV-NOT-FOUND",
      );
      await expect(page).toHaveURL(/\/operate\/workers$/);
      await expect(page.locator("weave-worker-detail")).toHaveCount(0);
      // The list's address came back without a second list entry.
      await page.goBack();
      await expect(page).toHaveURL(/\/home$/);
    });

    test("a worker the person may not read shows the access state", async ({
      page,
    }) => {
      await workers(page, ["status.read"]);
      await page.route(`${environment}/workers/${stale}`, (r) =>
        r.fulfill({
          status: 403,
          json: { code: "WV-DENIED", message: "Denied" },
        }),
      );
      await page.goto(`/operate/workers/${stale}`);
      const state = page.locator("weave-operate-state");
      await expect(state).toContainText("You don't have access to this worker");
      await expect(state).toContainText(
        "You need status.read in this environment.",
      );
      // The workers they can read stay on the page.
      await expect(rows(page)).toHaveCount(3);
      await expect(page).toHaveURL(/\/operate\/workers$/);
    });

    test("a worker that can't be read for another reason keeps its address for Try again", async ({
      page,
    }) => {
      const { items } = await workers(page, ["status.read"]);
      let fail = true;
      await page.route(`${environment}/workers/${online}`, (r) =>
        fail
          ? r.fulfill({
              status: 503,
              json: { code: "WV-UNAVAILABLE", message: "Unavailable" },
            })
          : r.fulfill({ json: items[0] }),
      );
      await page.goto(`/operate/workers/${online}`);
      const problem = page.getByRole("alert");
      await expect(problem).toContainText("Support code: WV-UNAVAILABLE");
      await expect(page).toHaveURL(new RegExp(`/operate/workers/${online}$`));
      await expect(page.locator("weave-worker-detail")).toHaveCount(0);
      fail = false;
      await problem.getByRole("button", { name: "Try again" }).click();
      await expect(
        page.locator("weave-worker-detail #worker-detail-title"),
      ).toHaveText("Worker 11111111");
      await expect(page.getByRole("alert")).toHaveCount(0);
    });

    test("readers see presence without Drain, and lapsed contact hides free slots", async ({
      page,
    }) => {
      await workers(page, ["status.read"]);
      await page.goto(`/operate/workers/${stale}`);
      const detail = page.locator("weave-worker-detail");
      await expect(detail.locator(".badges")).toContainText("Offline");
      await expect(
        detail
          .locator("dt")
          .filter({ hasText: "Free task slots" })
          .locator("+ dd"),
      ).toHaveText("Unknown");
      await expect(
        detail.getByRole("button", { name: "Drain", exact: true }),
      ).toHaveCount(0);
    });

    test("a deep link without status.read shows what access is needed", async ({
      page,
    }) => {
      await connected(page, { capabilities: ["run.read"] });
      await page.goto("/operate/workers");
      await expect(page.locator("#main h1")).toHaveText("Workers");
      await expect(page.locator("weave-operate-state")).toContainText(
        "You need status.read in this environment.",
      );
    });

    test("a refused list shows the access it needs until the list answers", async ({
      page,
    }) => {
      await page.clock.install();
      await workers(page, ["status.read"]);
      let refuse = true;
      await page.route(`${environment}/workers?*`, (r) =>
        refuse
          ? r.fulfill({
              status: 403,
              json: { code: "WV-DENIED", message: "Denied" },
            })
          : r.fallback(),
      );
      await page.goto("/operate/workers");
      await expect(page.locator("weave-operate-state")).toContainText(
        "You need status.read in this environment.",
      );
      refuse = false;
      // After a failure the next reload waits twice as long: 20 s.
      await page.clock.fastForward(20_500);
      await expect(rows(page)).toHaveCount(3);
      await expect(page.locator("weave-operate-state")).toHaveCount(0);
    });

    test("a refused next page shows the access it needs", async ({ page }) => {
      await connected(page, { capabilities: ["status.read"] });
      await page.route(`${environment}/workers?*`, (r) => {
        const cursor = new URL(r.request().url()).searchParams.get("cursor");
        return cursor
          ? r.fulfill({
              status: 403,
              json: { code: "WV-DENIED", message: "Denied" },
            })
          : r.fulfill({
              json: { items: [worker(online)], next_cursor: "more" },
            });
      });
      await page.goto("/operate/workers");
      await expect(rows(page)).toHaveCount(1);
      await page.getByRole("button", { name: "Load more workers" }).click();
      await expect(page.locator("weave-operate-state")).toContainText(
        "You need status.read in this environment.",
      );
    });

    test("a failed list shows its code and Try again", async ({ page }) => {
      await connected(page, { capabilities: ["status.read"] });
      let fail = true;
      await page.route(`${environment}/workers?*`, (r) =>
        fail
          ? r.fulfill({
              status: 503,
              json: { code: "WV-UNAVAILABLE", message: "Unavailable" },
            })
          : r.fulfill({ json: { items: [], next_cursor: null } }),
      );
      await page.goto("/operate/workers");
      const problem = page.getByRole("alert");
      await expect(problem).toContainText("Support code: WV-UNAVAILABLE");
      fail = false;
      await problem.getByRole("button", { name: "Try again" }).click();
      await expect(
        page.getByRole("heading", { name: "No current workers" }),
      ).toBeVisible();
    });
  });

for (const size of [
  { name: "a mouse at 1440x900", width: 1440, height: 900, touch: false },
  { name: "touch at 1440x900", width: 1440, height: 900, touch: true },
  { name: "a phone at 360x640", width: 360, height: 640, touch: true },
])
  test.describe(`filter checkboxes with ${size.name}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
    });

    test("are targets as tall as a control, with the box centered on the label", async ({
      page,
    }) => {
      await workers(page, ["status.read"]);
      await page.goto("/operate/workers");
      await expect(rows(page).first()).toBeVisible();
      await openFilters(page);
      const labels = page.locator(".operate-filters .checkbox-field");
      await expect(labels.first()).toBeVisible();
      const figures = await labels.evaluateAll((nodes) =>
        nodes.map((node) => {
          const label = node.getBoundingClientRect();
          const box = node
            .querySelector("input[type=checkbox]")!
            .getBoundingClientRect();
          return {
            height: label.height,
            boxCenter: box.top + box.height / 2,
            labelCenter: label.top + label.height / 2,
          };
        }),
      );
      const coarse = await page.evaluate(
        () => matchMedia("(pointer: coarse)").matches,
      );
      const target = coarse || size.width < 768 ? 44 : 36;
      for (const figure of figures) {
        expect(figure.height).toBeGreaterThanOrEqual(target - 0.5);
        expect(
          Math.abs(figure.boxCenter - figure.labelCenter),
        ).toBeLessThanOrEqual(0.5);
      }
    });

    test("the count sits on the filter line, and Retire's threshold is a full target", async ({
      page,
    }) => {
      await workers(page, ["status.read", "release.retire"]);
      await page.route(`${environment}/workers/retire-stale`, (r) =>
        r.fulfill({ json: { matched: 2, revoked: 0, worker_ids: [] } }),
      );
      await page.goto("/operate/workers");
      await expect(rows(page).first()).toBeVisible();
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
        .getByRole("button", { name: "Retire offline workers…" })
        .click();
      const dialog = page.getByRole("dialog", {
        name: "Retire offline workers",
      });
      await expect(dialog.getByRole("status")).toContainText("2 workers");
      const select = await dialog
        .getByLabel("Offline for at least")
        .boundingBox();
      expect(select!.height).toBeGreaterThanOrEqual(target - 0.5);
    });
  });

test.describe("platform filters, where workers run, and retiring", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  const target = "55555555-5555-4555-8555-555555555555";
  const release = "33333333-3333-4333-8333-333333333333";
  /** Targets and releases offered as filter choices. */
  async function choices(page: Page) {
    await page.route(`${environment}/deployment-targets?*`, (r) =>
      r.fulfill({
        json: { items: [{ id: target, name: "eu-west" }], next_cursor: null },
      }),
    );
    await page.route(`${environment}/worker-releases?*`, (r) =>
      r.fulfill({
        json: {
          items: [
            {
              id: release,
              image_digest: "sha256:" + "ab".repeat(32),
              capabilities: [{ taskType: "crm-lookup", taskVersion: "1.0.0" }],
            },
          ],
          next_cursor: null,
        },
      }),
    );
  }

  test("every filter goes to the platform; the host prefix never enters the address", async ({
    page,
  }) => {
    const mocked = await workers(page, [
      "status.read",
      "deployment.read",
      "catalog.read",
    ]);
    await choices(page);
    await page.goto("/operate/workers");
    await expect(rows(page)).toHaveCount(3);
    await page.getByLabel("Status").selectOption("online");
    await page.getByLabel("Draining", { exact: true }).selectOption("false");
    await page.getByLabel("Release").selectOption(release);
    await page.getByLabel("Task type").selectOption("crm-lookup@1.0.0");
    await page.getByLabel("Target").selectOption(target);
    await page.getByLabel("Order").selectOption("registered_desc");
    await page.getByLabel("Host starts with").fill("worker-");
    await expect
      .poll(() => mocked.queries().at(-1)?.get("host_prefix"))
      .toBe("worker-");
    const query = mocked.queries().at(-1)!;
    expect(Object.fromEntries(query)).toEqual({
      limit: "50",
      state: "current",
      order: "registered_desc",
      presence: "recent",
      draining: "false",
      release_id: release,
      task_type: "crm-lookup@1.0.0",
      target_id: target,
      host_prefix: "worker-",
    });
    // Another filter after the host prefix writes the address again: still
    // without the prefix, which the next read keeps sending.
    await page.getByLabel("Status").selectOption("offline");
    await expect(page).toHaveURL(/presence=offline/);
    await expect
      .poll(() => mocked.queries().at(-1)?.get("presence"))
      .toBe("stale");
    expect(mocked.queries().at(-1)!.get("host_prefix")).toBe("worker-");
    expect(page.url()).not.toContain("worker-");
    await page.getByLabel("Status").selectOption("online");
    expect(Object.fromEntries(new URL(page.url()).searchParams)).toEqual({
      presence: "online",
      draining: "false",
      release,
      task_type: "crm-lookup@1.0.0",
      target,
      order: "registered_desc",
    });
    await page
      .getByRole("search", { name: "Filter workers" })
      .getByRole("button", { name: /^Clear filters/ })
      .click();
    await expect(page).toHaveURL(/\/operate\/workers$/);
    await expect(page.getByLabel("Host starts with")).toHaveValue("");
    expect(mocked.queries().at(-1)!.has("host_prefix")).toBe(false);
  });

  test("an answer for the filters before never replaces the list for the new ones", async ({
    page,
  }) => {
    await workers(page, ["status.read"]);
    let hold = false;
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => (release = resolve));
    let reached: () => void = () => undefined;
    const out = new Promise<void>((resolve) => (reached = resolve));
    await page.route(`${environment}/workers?*`, async (r) => {
      const query = new URL(r.request().url()).searchParams;
      if (query.get("presence") === "stale")
        return r.fulfill({
          json: {
            items: [worker(stale, { presence: "stale" })],
            next_cursor: null,
          },
        });
      if (hold) {
        hold = false;
        reached();
        await held;
      }
      await r
        .fulfill({
          json: { items: [worker(online), worker(fresh)], next_cursor: null },
        })
        .catch(() => undefined);
    });
    await page.goto("/operate/workers");
    await expect(rows(page)).toHaveCount(2);
    // A refresh for no filters is out when the person picks Offline.
    hold = true;
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await out;
    await page.getByLabel("Status").selectOption("offline");
    release();
    await expect(rows(page)).toHaveCount(1);
    await page.waitForTimeout(500);
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page)).toContainText("Offline");
  });

  test("a worker shows where it runs, as text, and Unknown for what nobody reported", async ({
    page,
  }) => {
    const { items } = await workers(page, ["status.read"]);
    items[0] = {
      ...items[0],
      registered_at: at(-7200),
      identity: {
        host: "worker-7f9c",
        runtime: "container",
        sdk_version: "0.1.0a15",
        language_version: "3.12.4",
        os: "Linux",
        arch: "x86_64",
        platform_hint: "kubernetes",
        namespace: "weave",
        node: "node-a",
        labels: { team: "billing" },
      },
      placement: {
        target_id: target,
        target_name: "eu-west",
        deployment_id: "66666666-6666-4666-8666-666666666666",
        component: "workers",
      },
    } as never;
    await page.goto("/operate/workers");
    const first = rows(page).first();
    await expect(first).toContainText("worker-7f9c");
    await expect(first).toContainText("eu-west › workers");
    await expect(first).toContainText("SDK 0.1.0a15 · Runtime 3.12.4");
    await expect(rows(page).nth(1)).toContainText("No host reported");
    await expect(rows(page).nth(1)).toContainText("Unknown");
    await page.getByRole("button", { name: "Worker 11111111" }).click();
    const detail = page.locator("weave-worker-detail");
    await expect(detail).toContainText("A container on Kubernetes");
    await expect(detail).toContainText("Linux · x86_64");
    await expect(detail).toContainText("Namespace weave, node node-a");
    await expect(detail).toContainText("team=billing");
    await expect(detail.locator("a")).toHaveCount(0);
    await page.getByRole("button", { name: "Worker 12222222" }).click();
    await expect(detail.locator("dt:text-is('Registered') + dd")).toHaveText(
      "Unknown",
    );
    await expect(detail).toContainText(
      "Nothing: this worker doesn't say where it runs.",
    );
  });

  test("Retire offline workers previews 24 hours, says what matches, and retires each batch with its own key", async ({
    page,
  }) => {
    await workers(page, ["status.read", "release.retire"]);
    const sent: { body: Record<string, unknown>; key: string }[] = [];
    let matched = 250;
    await page.route(`${environment}/workers/retire-stale`, (r) => {
      const body = r.request().postDataJSON();
      sent.push({ body, key: r.request().headers()["idempotency-key"] });
      const ids = Array.from(
        { length: body.dry_run ? 0 : Math.min(100, matched) },
        (_, i) => "1aaaaaaa-0000-4000-8000-" + String(i).padStart(12, "0"),
      );
      const answer = { matched, revoked: ids.length, worker_ids: ids };
      if (!body.dry_run) matched -= ids.length;
      return r.fulfill({ json: answer });
    });
    await page.goto("/operate/workers");
    await page.getByRole("button", { name: "Retire offline workers…" }).click();
    const dialog = page.getByRole("dialog", { name: "Retire offline workers" });
    await expect(dialog.getByRole("status")).toHaveText(
      "250 workers match. One retirement revokes 100, oldest first; retire again for the rest.",
    );
    expect(sent[0].body).toEqual({ older_than_seconds: 86400, dry_run: true });
    await dialog.getByRole("button", { name: "Retire 100 workers" }).click();
    await expect(dialog.getByRole("status")).toHaveText(
      "Revoked 100 workers. 150 workers still match.",
    );
    await expect(
      dialog.getByRole("list", { name: "Revoked workers" }).locator("li"),
    ).toHaveCount(100);
    expect(sent[1].body).toEqual({ older_than_seconds: 86400, dry_run: false });
    await dialog.getByRole("button", { name: "Retire the next 100" }).click();
    await expect(dialog.getByRole("status")).toHaveText(
      "Revoked 100 workers. 50 workers still match.",
    );
    const keys = sent.map((item) => item.key);
    expect(new Set(keys).size).toBe(keys.length);
    for (const key of keys) expect(key).toMatch(/^[0-9a-f-]{36}$/);
    // Nothing matches for 30 days: there is nothing to confirm.
    matched = 0;
    await dialog.getByLabel("Offline for at least").selectOption("2592000");
    await expect(dialog.getByRole("status")).toContainText("No worker matches");
    await expect(dialog.getByRole("button", { name: /^Retire/ })).toHaveCount(
      0,
    );
    expect(sent.at(-1)!.body).toEqual({
      older_than_seconds: 2592000,
      dry_run: true,
    });
  });

  test("a lost retirement answer sends the same key again; a refusal says why", async ({
    page,
  }) => {
    await workers(page, ["status.read", "release.retire"]);
    const keys: string[] = [];
    let attempt = 0;
    await page.route(`${environment}/workers/retire-stale`, (r) => {
      const body = r.request().postDataJSON();
      if (body.dry_run)
        return r.fulfill({
          json: { matched: 3, revoked: 0, worker_ids: [] },
        });
      keys.push(r.request().headers()["idempotency-key"]);
      attempt++;
      return attempt === 1
        ? r.fulfill({
            status: 503,
            json: { code: "WV-UNAVAILABLE", message: "Unavailable" },
          })
        : attempt === 2
          ? r.fulfill({
              status: 403,
              json: { code: "WV-FORBIDDEN", message: "Not allowed" },
            })
          : r.fulfill({ json: { matched: 3, revoked: 3, worker_ids: [] } });
    });
    await page.goto("/operate/workers");
    await page.getByRole("button", { name: "Retire offline workers…" }).click();
    const dialog = page.getByRole("dialog", { name: "Retire offline workers" });
    await dialog.getByRole("button", { name: "Retire 3 workers" }).click();
    await expect(dialog.getByRole("alert")).toContainText(
      "Studio didn't get an answer",
    );
    await dialog.getByRole("button", { name: "Send again" }).click();
    await expect(dialog.getByRole("alert")).toContainText("Not allowed");
    expect(keys[1]).toBe(keys[0]);
    await dialog.getByRole("button", { name: "Retire 3 workers" }).click();
    await expect(dialog.getByRole("status")).toHaveText("Revoked 3 workers.");
    expect(keys[2]).not.toBe(keys[0]);
  });

  test("without release.retire, Retire and Revoke don't show", async ({
    page,
  }) => {
    await workers(page, ["status.read", "worker.drain"]);
    await page.goto("/operate/workers");
    await expect(rows(page)).toHaveCount(3);
    await expect(
      page.getByRole("button", { name: "Retire offline workers…" }),
    ).toHaveCount(0);
    await rows(page).first().getByRole("checkbox").check();
    const bar = page.getByRole("region", { name: "Selected workers" });
    await expect(bar.getByRole("button", { name: "Drain" })).toBeVisible();
    await expect(bar.getByRole("button", { name: "Revoke" })).toHaveCount(0);
    await page.getByRole("button", { name: "Worker 11111111" }).click();
    await expect(
      page
        .locator("weave-worker-detail")
        .getByRole("button", { name: "Revoke" }),
    ).toHaveCount(0);
  });

  test("selected workers drain, resume and are revoked one by one, each with its revision and answer", async ({
    page,
  }) => {
    const { items } = await workers(page, [
      "status.read",
      "worker.drain",
      "release.retire",
    ]);
    items[1] = { ...items[1], draining: true, revision: 9 };
    const sent: { path: string; body: unknown; key?: string }[] = [];
    await page.route(`${environment}/workers/*/*`, (r) => {
      const path = new URL(r.request().url()).pathname;
      sent.push({
        path: path.split("/").slice(-2).join("/"),
        body: r.request().postDataJSON(),
        key: r.request().headers()["idempotency-key"],
      });
      if (path.endsWith(`${fresh}/drain`))
        return r.fulfill({
          status: 409,
          json: { code: "WV-WORKER-REVISION", message: "Changed" },
        });
      return r.fulfill({ json: { revoked: true } });
    });
    await page.goto("/operate/workers");
    await expect(rows(page)).toHaveCount(3);
    await page.getByLabel("Select every worker on this list, up to 50").check();
    const bar = page.getByRole("region", { name: "Selected workers" });
    await expect(bar).toContainText("3 selected");
    await bar.getByRole("button", { name: "Drain" }).click();
    await page
      .getByRole("dialog", { name: "Drain 3 workers?" })
      .getByRole("button", { name: "Drain" })
      .click();
    const results = page.getByRole("region", { name: "Drain workers" });
    await expect(results.locator(".bulk-result")).toHaveCount(3);
    await expect(results.locator(".bulk-result").nth(0)).toContainText(
      "Draining.",
    );
    await expect(results.locator(".bulk-result").nth(1)).toContainText(
      "Not sent: it is already draining.",
    );
    await expect(results.locator(".bulk-result").nth(2)).toContainText(
      "it changed since the list was read",
    );
    expect(sent).toEqual([
      {
        path: `${online}/drain`,
        body: { expected_revision: 2 },
        key: expect.stringMatching(/^[0-9a-f-]{36}$/),
      },
      {
        path: `${fresh}/drain`,
        body: { expected_revision: 2 },
        key: expect.stringMatching(/^[0-9a-f-]{36}$/),
      },
    ]);
    expect(sent[0].key).not.toBe(sent[1].key);
    sent.length = 0;
    await rows(page).nth(1).getByRole("checkbox").check();
    await bar.getByRole("button", { name: "Resume" }).click();
    await page
      .getByRole("dialog", { name: "Resume this worker?" })
      .getByRole("button", { name: "Resume" })
      .click();
    await expect(
      page.getByRole("region", { name: "Resume workers" }),
    ).toContainText("Takes new tasks again.");
    expect(sent).toEqual([
      {
        path: `${stale}/resume`,
        body: { expected_revision: 9 },
        key: expect.stringMatching(/^[0-9a-f-]{36}$/),
      },
    ]);
    sent.length = 0;
    await rows(page).first().getByRole("checkbox").check();
    await bar.getByRole("button", { name: "Revoke" }).click();
    await page
      .getByRole("dialog", { name: "Revoke this worker?" })
      .getByRole("button", { name: "Revoke worker" })
      .click();
    await expect(
      page.getByRole("region", { name: "Revoke workers" }),
    ).toContainText("Revoked.");
    expect(sent.map((item) => item.path)).toEqual([`${online}/revoke`]);
  });
});

test.describe("another person on the same environment", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  /**
   * Jane opens Workers while the platform's status can't be read; `switch`
   * then finds John signed in on the same environment, with the page open.
   * `person()` says who a request was sent for.
   */
  async function janeThenJohn(page: Page) {
    let person = "jane";
    let reachable = false;
    const identity = (host: PlatformHost) => {
      const base = host.identity();
      return {
        ...base,
        principal_id: `principal-${person}`,
        grants: base.grants.map((grant) => ({
          ...grant,
          capabilities: [
            ...grant.capabilities,
            "status.read",
            "worker.drain",
            "release.retire",
          ],
        })),
      };
    };
    const host = await platformHost(page, {
      platforms: [acmePlatform()],
      active: "Acme",
      test: (current) =>
        reachable
          ? {
              json: {
                session: current.session(),
                identity: identity(current),
                workspaces: current.current?.workspaces ?? [],
                truncated: false,
                workspace_revoked: false,
              },
            }
          : {
              status: 503,
              json: { code: "WV-STUDIO-UPSTREAM", message: "Unreachable" },
            },
    });
    await page.route("**/studio/api/api/v1/identity", (r) =>
      r.fulfill({ json: identity(host) }),
    );
    const lists: { person: string; query: URLSearchParams }[] = [];
    await page.route("**/workers?*", (r) => {
      lists.push({
        person,
        query: new URL(r.request().url()).searchParams,
      });
      return r.fulfill({
        json: {
          items: [worker(online), worker(fresh), worker(stale)],
          next_cursor: null,
        },
      });
    });
    await page.goto("/operate/workers");
    await expect(rows(page)).toHaveCount(3);
    return {
      lists,
      person: () => person,
      /** `behindDialog`: the change arrives while a dialog covers the page. */
      async switch(behindDialog = false) {
        person = "john";
        reachable = true;
        const again = page
          .locator(".platform-banner")
          .getByRole("button", { name: "Try again" });
        if (behindDialog) await again.dispatchEvent("click");
        else await again.click();
        await expect(page.locator(".platform-banner")).toHaveCount(0);
      },
    };
  }

  test("a bulk action stops when the person changes, sending nothing for the next one", async ({
    page,
  }) => {
    const people = await janeThenJohn(page);
    const sent: { person: string; path: string }[] = [];
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => (release = resolve));
    let reached: () => void = () => undefined;
    const out = new Promise<void>((resolve) => (reached = resolve));
    await page.route("**/workers/*/drain", async (r) => {
      sent.push({
        person: people.person(),
        path: new URL(r.request().url()).pathname
          .split("/")
          .slice(-2)
          .join("/"),
      });
      if (sent.length === 1) {
        reached();
        await held;
      }
      await r.fulfill({ json: { revoked: false } }).catch(() => undefined);
    });
    await page.getByLabel("Select every worker on this list, up to 50").check();
    await page
      .getByRole("region", { name: "Selected workers" })
      .getByRole("button", { name: "Drain" })
      .click();
    await page
      .getByRole("dialog", { name: "Drain 3 workers?" })
      .getByRole("button", { name: "Drain" })
      .click();
    // The first drain is on its way when John turns out to be signed in.
    await out;
    await people.switch();
    release();
    await expect
      .poll(() => people.lists.filter((item) => item.person === "john").length)
      .toBeGreaterThan(0);
    await page.waitForTimeout(500);
    expect(sent).toEqual([{ person: "jane", path: `${online}/drain` }]);
    await expect(
      page.getByRole("region", { name: "Drain workers" }),
    ).toHaveCount(0);
  });

  test("a bulk action confirmed after the person changes sends nothing", async ({
    page,
  }) => {
    const people = await janeThenJohn(page);
    const sent: string[] = [];
    await page.route("**/workers/*/drain", (r) => {
      sent.push(people.person());
      return r.fulfill({ json: { revoked: false } });
    });
    await page.getByLabel("Select every worker on this list, up to 50").check();
    await page
      .getByRole("region", { name: "Selected workers" })
      .getByRole("button", { name: "Drain" })
      .click();
    const dialog = page.getByRole("dialog", { name: "Drain 3 workers?" });
    await expect(dialog).toBeVisible();
    // John turns out to be signed in while Jane's confirmation is open.
    await people.switch(true);
    await expect
      .poll(() => people.lists.filter((item) => item.person === "john").length)
      .toBeGreaterThan(0);
    if (await dialog.isVisible())
      await dialog.getByRole("button", { name: "Drain" }).click();
    await page.waitForTimeout(500);
    expect(sent).toEqual([]);
    await expect(
      page.getByRole("region", { name: "Drain workers" }),
    ).toHaveCount(0);
  });

  test("a host prefix typed by one person is gone for the next", async ({
    page,
  }) => {
    const people = await janeThenJohn(page);
    await page.getByLabel("Host starts with").fill("jane-private-");
    await expect
      .poll(() => people.lists.at(-1)?.query.get("host_prefix"))
      .toBe("jane-private-");
    const page1 = await page.locator("weave-workers-page").elementHandle();
    await people.switch();
    await expect
      .poll(() => people.lists.filter((item) => item.person === "john").length)
      .toBeGreaterThan(0);
    await expect(page.getByLabel("Host starts with")).toHaveValue("");
    // The same page, not a new one, forgot it.
    expect(await page1!.evaluate((node) => node.isConnected)).toBe(true);
    await page.waitForTimeout(500);
    for (const item of people.lists.filter((entry) => entry.person === "john"))
      expect(item.query.has("host_prefix")).toBe(false);
    expect(page.url()).not.toContain("jane-private-");
  });

  for (const [name, answer, notice] of [
    [
      "a retirement",
      { json: { matched: 3, revoked: 3, worker_ids: [] } },
      "Retired 3 offline workers.",
    ],
    [
      "a lost retirement",
      { status: 503, json: { code: "WV-UNAVAILABLE", message: "Unavailable" } },
      "Studio didn't get an answer about retiring offline workers: workers may have been revoked. Open Retire offline workers… to see what still matches.",
    ],
  ] as const)
    test(`${name} answered after the person changes still says what happened`, async ({
      page,
    }) => {
      const people = await janeThenJohn(page);
      let release: () => void = () => undefined;
      const held = new Promise<void>((resolve) => (release = resolve));
      let reached: () => void = () => undefined;
      const out = new Promise<void>((resolve) => (reached = resolve));
      const sent: string[] = [];
      await page.route("**/workers/retire-stale", async (r) => {
        const body = r.request().postDataJSON();
        if (body.dry_run)
          return r.fulfill({
            json: { matched: 3, revoked: 0, worker_ids: [] },
          });
        sent.push(people.person());
        reached();
        await held;
        await r
          .fulfill({ status: answer.status ?? 200, json: answer.json })
          .catch(() => undefined);
      });
      await page
        .getByRole("button", { name: "Retire offline workers…" })
        .click();
      const dialog = page.getByRole("dialog", {
        name: "Retire offline workers",
      });
      await dialog.getByRole("button", { name: "Retire 3 workers" }).click();
      await out;
      await people.switch(true);
      await expect(dialog).toHaveCount(0);
      release();
      await expect(page.getByText(notice)).toBeVisible();
      // Nothing is sent again for the next person.
      expect(sent).toEqual(["jane"]);
    });
});
