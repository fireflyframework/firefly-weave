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
      task_types: ["email-send@1.0.0", "crm-lookup@1.0.0"],
      active_leases: 9,
    }),
    worker(revoked, { revoked: true }),
  ];
  let lists = 0;
  await page.route(`${environment}/workers?*`, (r) => {
    lists++;
    return r.fulfill({ json: { items, next_cursor: null } });
  });
  for (const item of items)
    await page.route(`${environment}/workers/${item.id}`, (r) =>
      r.fulfill({ json: items.find((w) => w.id === item.id) }),
    );
  return { items, lists: () => lists };
}
const rows = (page: Page) =>
  page.getByRole("table", { name: "Workers" }).locator(".resource-row");

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("lists presence, claims and load, and hides revoked workers", async ({
      page,
    }) => {
      await workers(page, ["status.read"]);
      await page.getByRole("button", { name: "Workers", exact: true }).click();
      await expect(page).toHaveURL(/\/operate\/workers$/);
      await expect(rows(page)).toHaveCount(3);
      await expect(rows(page).nth(0)).toContainText("Online");
      await expect(rows(page).nth(0)).toContainText("2 of 8");
      await expect(rows(page).nth(1)).toContainText("Offline");
      await expect(rows(page).nth(2)).toContainText("Not seen yet");
      await expect(rows(page).nth(2).locator(".meter")).toHaveClass(/over/);
      await expect(page.getByText("3 workers", { exact: true })).toBeVisible();
      await page.getByLabel("Show revoked workers").check();
      await expect(rows(page)).toHaveCount(4);
      await expect(rows(page).nth(3)).toContainText("Revoked");
    });

    test("filters narrow the list and stay in the address", async ({
      page,
    }) => {
      await workers(page, ["status.read"]);
      await page.goto("/operate/workers?presence=offline");
      await expect(page.getByLabel("Status")).toHaveValue("offline");
      await expect(rows(page)).toHaveCount(1);
      await expect(page.getByText("1 of 3 workers")).toBeVisible();
      await page.getByLabel("Status").selectOption("");
      await page.getByLabel("Task type").selectOption("email-send@1.0.0");
      await expect(page).toHaveURL(
        /\/operate\/workers\?task_type=email-send(@|%40)1\.0\.0$/,
      );
      await expect(rows(page)).toHaveCount(1);
      await page.getByLabel("Draining only").check();
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
        page.getByRole("heading", { name: "No workers yet" }),
      ).toBeVisible();
    });
  });
