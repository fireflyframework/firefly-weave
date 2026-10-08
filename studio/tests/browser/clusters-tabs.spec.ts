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
// Clusters tabs: Targets with runner presence, Jobs, and every runner with
// Revoke. The server authorizes each read and command; Studio only leaves
// out the buttons it would refuse.
import { test, expect, Page } from "@playwright/test";
import { connected } from "./support";

const scope = {
  tenant_id: "tenant",
  project_id: "project",
  environment_id: "development",
};
const targetId = "11111111-1111-4111-8111-111111111111";
const otherTargetId = "22222222-2222-4222-8222-222222222222";
const target = (id: string, name: string, adapter: string) => ({
  id,
  name,
  adapter,
  external_identity: `${name}-identity`,
  boundary: `${name}-boundary`,
  runner_principal_id: "44444444-4444-4444-8444-444444444444",
  capabilities: ["observe", "update"],
  scope,
  revision: 1,
  disabled: false,
  created_at: "2026-10-03T10:00:00Z",
});
const at = (seconds: number) =>
  new Date(Date.now() + seconds * 1000).toISOString();
const runner = (
  id: string,
  target_id: string,
  options: { lastSeen: number; expires: number; revoked?: boolean },
) => ({
  id,
  principal_id: "55555555-5555-4555-8555-555555555555",
  target_id,
  adapter: target_id === targetId ? "docker-compose" : "kubernetes",
  adapter_version: "1",
  capabilities: ["observe", "update"],
  last_seen: at(options.lastSeen),
  expires_at: at(options.expires),
  revoked: options.revoked ?? false,
});
const online = "71111111-7777-4777-8777-777777777777";
const offline = "72222222-7777-4777-8777-777777777777";
const revoked = "73333333-7777-4777-8777-777777777777";

async function clusters(page: Page, capabilities: string[]) {
  await connected(page, { capabilities });
  const runners = [
    runner(online, targetId, { lastSeen: -10, expires: 80 }),
    runner(offline, otherTargetId, { lastSeen: -240, expires: -150 }),
    runner(revoked, targetId, { lastSeen: -900, expires: -810, revoked: true }),
  ];
  const requests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/deployment-")) requests.push(request.url());
  });
  await page.route("**/deployment-targets?*", (r) =>
    r.fulfill({
      json: {
        items: [
          target(targetId, "local-docker", "docker-compose"),
          target(otherTargetId, "kind-weave-test", "kubernetes"),
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/deployment-runners?*", (r) =>
    r.fulfill({ json: { items: runners, next_cursor: null } }),
  );
  const job = {
    id: "88888888-8888-4888-8888-888888888888",
    scope,
    target_id: targetId,
    target_revision: 1,
    kind: "observe",
    plan_id: null,
    plan_digest: null,
    state: "succeeded",
    revision: 2,
    operation_key: "op",
    created_at: "2026-10-08T09:00:00Z",
    deadline: at(600),
    receipt: null,
    observation_id: null,
    reconciliation_started_at: null,
  };
  await page.route("**/deployment-jobs?*", (r) =>
    r.fulfill({ json: { items: [job], next_cursor: null } }),
  );
  await page.route(`**/deployment-jobs/${job.id}`, (r) =>
    r.fulfill({ json: job }),
  );
  await page.getByRole("button", { name: "Clusters", exact: true }).click();
  return { runners, requests };
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("targets show their adapter and runner presence", async ({ page }) => {
      await clusters(page, ["deployment.read"]);
      const tabs = page.getByRole("tablist", { name: "Clusters sections" });
      await expect(tabs.getByRole("tab")).toHaveText([
        "Targets",
        "Jobs",
        "Runners",
      ]);
      await expect(tabs.getByRole("tab", { name: "Targets" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      const cards = page.locator(".resource-cards li");
      await expect(cards.filter({ hasText: "local-docker" })).toContainText(
        "Runner online",
      );
      await expect(cards.filter({ hasText: "kind-weave-test" })).toContainText(
        "Runner offline · last contact 4 min ago",
      );
      await expect(
        cards.filter({ hasText: "local-docker" }).locator("weave-icon"),
      ).toHaveAttribute("data-icon", "compose");
      await expect(page.getByText("Updated just now")).toBeVisible();
    });

    test("tabs keep their place in the address and move with the arrow keys", async ({
      page,
    }) => {
      await clusters(page, ["deployment.read"]);
      await page.getByRole("tab", { name: "Jobs" }).click();
      await expect(page).toHaveURL(/\/operate\/clusters\?tab=jobs$/);
      await expect(
        page.getByRole("button", { name: "observe · 2026-10-08T09:00:00Z" }),
      ).toBeVisible();
      await page.keyboard.press("ArrowRight");
      await expect(page.getByRole("tab", { name: "Runners" })).toBeFocused();
      await expect(page).toHaveURL(/\/operate\/clusters\?tab=runners$/);
      await page.reload();
      await expect(page.getByRole("tab", { name: "Runners" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await page.getByRole("tab", { name: "Jobs" }).click();
      await page
        .getByRole("button", { name: "observe · 2026-10-08T09:00:00Z" })
        .click();
      await page.getByRole("button", { name: "Back to Clusters" }).click();
      await expect(page).toHaveURL(/\/operate\/clusters\?tab=jobs$/);
    });

    test("every runner shows its presence, and readers cannot revoke", async ({
      page,
    }) => {
      await clusters(page, ["deployment.read"]);
      await page.getByRole("tab", { name: "Runners" }).click();
      const table = page.getByRole("table", { name: "Runners" });
      const rows = table.locator(".resource-row");
      await expect(rows).toHaveCount(3);
      await expect(rows.nth(0)).toContainText("Online");
      await expect(rows.nth(0)).toContainText("local-docker");
      await expect(rows.nth(1)).toContainText("Offline");
      await expect(rows.nth(1)).toContainText("kind-weave-test");
      await expect(rows.nth(2)).toContainText("Revoked");
      await expect(table.getByRole("button", { name: /^Revoke/ })).toHaveCount(
        0,
      );
    });

    test("a target manager revokes a runner after confirming", async ({
      page,
    }) => {
      const { runners } = await clusters(page, [
        "deployment.read",
        "target.manage",
      ]);
      const revokes: { body: string | null; key?: string }[] = [];
      await page.route(`**/deployment-runners/${online}/revoke`, (r) => {
        revokes.push({
          body: r.request().postData(),
          key: r.request().headers()["idempotency-key"],
        });
        runners[0] = { ...runners[0], revoked: true };
        return r.fulfill({ json: runners[0] });
      });
      await page.getByRole("tab", { name: "Runners" }).click();
      const table = page.getByRole("table", { name: "Runners" });
      // Only live registrations offer Revoke.
      await expect(table.getByRole("button", { name: /^Revoke/ })).toHaveCount(
        2,
      );
      await table
        .getByRole("button", { name: `Revoke runner ${online.slice(0, 8)}` })
        .click();
      const dialog = page.getByRole("dialog", { name: "Revoke this runner?" });
      await expect(dialog).toContainText(
        "The runner for local-docker can't claim jobs any more",
      );
      await dialog.getByRole("button", { name: "Revoke runner" }).click();
      await expect(page.getByText("Runner revoked.")).toBeVisible();
      expect(revokes).toHaveLength(1);
      expect(revokes[0].body).toBeNull();
      expect(revokes[0].key).toBeTruthy();
      await expect(table.locator(".resource-row").nth(0)).toContainText(
        "Revoked",
      );
    });

    test("a refused revoke explains itself and changes nothing", async ({
      page,
    }) => {
      await clusters(page, ["deployment.read", "target.manage"]);
      await page.route(`**/deployment-runners/${online}/revoke`, (r) =>
        r.fulfill({
          status: 403,
          json: { code: "WV-DENIED", message: "Denied" },
        }),
      );
      await page.getByRole("tab", { name: "Runners" }).click();
      await page
        .getByRole("button", { name: `Revoke runner ${online.slice(0, 8)}` })
        .click();
      await page
        .getByRole("dialog", { name: "Revoke this runner?" })
        .getByRole("button", { name: "Revoke runner" })
        .click();
      await expect(page.getByRole("alert")).toContainText(
        "Your account does not have permission for this action",
      );
      await expect(
        page
          .getByRole("table", { name: "Runners" })
          .locator(".resource-row")
          .nth(0),
      ).toContainText("Online");
    });

    test("Refresh reloads the open tab", async ({ page }) => {
      const { requests } = await clusters(page, ["deployment.read"]);
      await page.getByRole("tab", { name: "Runners" }).click();
      await expect(page.getByRole("table", { name: "Runners" })).toBeVisible();
      const before = requests.filter((url) =>
        url.includes("/deployment-runners?"),
      ).length;
      await page.getByRole("button", { name: "Refresh", exact: true }).click();
      await expect
        .poll(
          () =>
            requests.filter((url) => url.includes("/deployment-runners?"))
              .length,
        )
        .toBe(before + 1);
    });
  });

// How the page keeps itself current: the 15 s and 2 s reloads, never while a
// form is open, what "Updated" claims, and what a card says before the runner
// read has answered. Time is driven by the page clock; reads wait on gates.
const jobId = "88888888-8888-4888-8888-888888888888";
const jobButton = "observe · 2026-10-08T09:00:00Z";
interface Gate {
  promise: Promise<void>;
  release: () => void;
}
const gate = (): Gate => {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => (release = resolve));
  return { promise, release };
};
interface Behavior {
  /** Holds every runners read until released; the answer is fixed first. */
  hold: Promise<void> | null;
  runnersFail: { status: number; json: unknown } | null;
  jobState: string;
  runners: ReturnType<typeof runner>[];
}

async function polling(
  page: Page,
  capabilities: string[],
  init: Partial<Behavior> = {},
) {
  await page.clock.install();
  await connected(page, { capabilities });
  const behavior: Behavior = {
    hold: null,
    runnersFail: null,
    jobState: "running",
    runners: [
      runner(online, targetId, { lastSeen: -10, expires: 80 }),
      runner(offline, otherTargetId, { lastSeen: -240, expires: -150 }),
    ],
    ...init,
  };
  const requests: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "GET" && request.url().includes("/deployment"))
      requests.push(request.url());
  });
  const count = (part: string) =>
    requests.filter((url) => url.includes(part)).length;
  await page.route("**/deployment-targets?*", (r) =>
    r.fulfill({
      json: {
        items: [
          target(targetId, "local-docker", "docker-compose"),
          target(otherTargetId, "kind-weave-test", "kubernetes"),
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/deployment-runners?*", async (r) => {
    const answer = behavior.runnersFail ?? {
      status: 200,
      json: {
        items: structuredClone(behavior.runners),
        next_cursor: null,
      },
    };
    await behavior.hold;
    await r.fulfill({ status: answer.status, json: answer.json });
  });
  const job = () => ({
    id: jobId,
    scope,
    target_id: targetId,
    target_revision: 1,
    kind: "observe",
    plan_id: null,
    plan_digest: null,
    state: behavior.jobState,
    revision: 2,
    operation_key: "op",
    created_at: "2026-10-08T09:00:00Z",
    deadline: at(600),
    receipt: null,
    observation_id: null,
    reconciliation_started_at: null,
  });
  await page.route("**/deployment-jobs?*", (r) =>
    r.fulfill({ json: { items: [job()], next_cursor: null } }),
  );
  await page.route(`**/deployment-jobs/${jobId}`, (r) =>
    r.fulfill({ json: job() }),
  );
  await page.getByRole("button", { name: "Clusters", exact: true }).click();
  return { behavior, count };
}

test.describe("polling and failures", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("reloads every 15 s, and every 2 s while an opened job runs", async ({
    page,
  }) => {
    const { behavior, count } = await polling(page, ["deployment.read"]);
    await expect(page.getByText("Updated just now")).toBeVisible();
    const targets = () => count("/deployment-targets?");
    const before = targets();
    await page.clock.runFor(12_000);
    expect(targets()).toBe(before);
    await expect(page.getByText(/^Updated 1\d s ago$/)).toBeVisible();
    await page.clock.runFor(4_000);
    await expect.poll(targets).toBe(before + 1);
    await expect(page.getByText("Updated just now")).toBeVisible();

    await page.getByRole("tab", { name: "Jobs" }).click();
    await page.getByRole("button", { name: jobButton }).click();
    await expect(
      page.getByRole("button", { name: "Back to Clusters" }),
    ).toBeVisible();
    const reads = () => count(`/deployment-jobs/${jobId}`);
    await expect.poll(reads).toBeGreaterThan(0);
    // A running job reloads every 2 s as soon as it is open.
    const opened = reads();
    await page.clock.runFor(3_000);
    await expect.poll(reads).toBeGreaterThan(opened);
    const running = reads();
    await page.clock.runFor(3_000);
    await expect.poll(reads).toBeGreaterThan(running);
    // Once it finishes, the page goes back to every 15 s.
    behavior.jobState = "succeeded";
    await page.clock.runFor(3_000);
    await expect(page.locator("p[role=status] strong")).toHaveText("succeeded");
    const finished = reads();
    await page.clock.runFor(10_000);
    expect(reads()).toBe(finished);
    await page.clock.runFor(6_000);
    await expect.poll(reads).toBeGreaterThan(finished);
  });

  test("never reloads, or says it did, while a form is open", async ({
    page,
  }) => {
    const { count } = await polling(page, ["deployment.read", "target.manage"]);
    await expect(page.getByText("Updated just now")).toBeVisible();
    await page
      .getByRole("button", { name: "Register target", exact: true })
      .click();
    await page.getByLabel("Target name").fill("draft-target");
    const reads = () => count("/deployment-");
    const before = reads();
    await page.clock.runFor(31_000);
    expect(reads()).toBe(before);
    await expect(page.getByText(/^Updated 3\d s ago$/)).toBeVisible();
    await expect(page.getByText("Updated just now")).toHaveCount(0);
    // Refresh is the person's own request, and it leaves the draft alone.
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect.poll(reads).toBeGreaterThan(before);
    await expect(page.getByText("Updated just now")).toBeVisible();
    await expect(page.getByLabel("Target name")).toHaveValue("draft-target");
    // Closing the form lets the next scheduled reload through.
    await page.getByRole("button", { name: "Cancel registration" }).click();
    const closed = reads();
    await page.clock.runFor(16_000);
    await expect.poll(reads).toBeGreaterThan(closed);
  });

  test("a tab opened during a reload still loads", async ({ page }) => {
    const { behavior, count } = await polling(page, ["deployment.read"]);
    await expect(page.getByText("Updated just now")).toBeVisible();
    const hold = gate();
    behavior.hold = hold.promise;
    const before = count("/deployment-runners?");
    await page.clock.runFor(16_000);
    await expect.poll(() => count("/deployment-runners?")).toBe(before + 1);
    // The reload is still waiting on its runners read.
    await page.getByRole("tab", { name: "Jobs" }).click();
    await expect(page.getByText("No job has been returned.")).toHaveCount(0);
    hold.release();
    await expect(page.getByRole("button", { name: jobButton })).toBeVisible();
  });

  test("a revoke that lands during a reload still shows the runner revoked", async ({
    page,
  }) => {
    const { behavior, count } = await polling(page, [
      "deployment.read",
      "target.manage",
    ]);
    await page.route(`**/deployment-runners/${online}/revoke`, (r) => {
      behavior.runners[0] = { ...behavior.runners[0], revoked: true };
      return r.fulfill({ json: behavior.runners[0] });
    });
    await page.getByRole("tab", { name: "Runners" }).click();
    const table = page.getByRole("table", { name: "Runners" });
    await expect(table.locator(".resource-row").nth(0)).toContainText("Online");
    const hold = gate();
    behavior.hold = hold.promise;
    const before = count("/deployment-runners?");
    await page.clock.runFor(16_000);
    await expect.poll(() => count("/deployment-runners?")).toBe(before + 1);
    // The reload in flight answered before the revoke, and is still waiting.
    await table
      .getByRole("button", { name: `Revoke runner ${online.slice(0, 8)}` })
      .click();
    await page
      .getByRole("dialog", { name: "Revoke this runner?" })
      .getByRole("button", { name: "Revoke runner" })
      .click();
    await expect(page.getByText("Runner revoked.")).toBeVisible();
    behavior.hold = null;
    hold.release();
    await expect(table.locator(".resource-row").nth(0)).toContainText(
      "Revoked",
    );
  });

  test("a card makes no runner claim before the runner read answers", async ({
    page,
  }) => {
    const hold = gate();
    const { behavior } = await polling(page, ["deployment.read"], {
      hold: hold.promise,
    });
    const cards = page.locator(".resource-cards li");
    await expect(cards.filter({ hasText: "local-docker" })).toBeVisible();
    await expect(page.getByText("No runner has registered")).toHaveCount(0);
    await expect(page.getByText(/Runner (online|offline)/)).toHaveCount(0);
    hold.release();
    await expect(cards.filter({ hasText: "local-docker" })).toContainText(
      "Runner online",
    );
    // Back from a target's screen the fleet reads again, and says nothing yet.
    await page
      .getByRole("button", { name: "local-docker", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "local-docker", exact: true }),
    ).toBeVisible();
    const again = gate();
    behavior.hold = again.promise;
    await page.getByRole("button", { name: "Back to Clusters" }).click();
    await expect(cards.filter({ hasText: "local-docker" })).toBeVisible();
    await expect(page.getByText("No runner has registered")).toHaveCount(0);
    await expect(page.getByText(/Runner (online|offline)/)).toHaveCount(0);
    again.release();
    await expect(cards.filter({ hasText: "local-docker" })).toContainText(
      "Runner online",
    );
  });

  test("a failed runner read leaves the cards silent and says why", async ({
    page,
  }) => {
    await polling(page, ["deployment.read"], {
      runnersFail: {
        status: 500,
        json: { code: "WV-OUTAGE", message: "The runner service is down" },
      },
    });
    const cards = page.locator(".resource-cards li");
    await expect(cards.filter({ hasText: "local-docker" })).toBeVisible();
    await expect(page.getByRole("alert")).toContainText(
      "Runner presence could not be loaded. The runner service is down",
    );
    await expect(page.getByText("No runner has registered")).toHaveCount(0);
    await expect(page.getByText(/Runner (online|offline)/)).toHaveCount(0);
  });

  test("a refused runner read shows the access state", async ({ page }) => {
    await polling(page, ["deployment.read"], {
      runnersFail: {
        status: 403,
        json: { code: "WV-DENIED", message: "Denied" },
      },
    });
    await page.getByRole("tab", { name: "Runners" }).click();
    await expect(
      page.getByRole("heading", { name: "You don't have access to runners" }),
    ).toBeVisible();
    await expect(
      page.getByText(
        "You need deployment.read in this environment. Ask an administrator in Settings › People and access.",
      ),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Try again" })).toHaveCount(
      0,
    );
  });

  test("another failed runner read shows its message and support code", async ({
    page,
  }) => {
    await polling(page, ["deployment.read"], {
      runnersFail: {
        status: 500,
        json: { code: "WV-OUTAGE", message: "The runner service is down" },
      },
    });
    await page.getByRole("tab", { name: "Runners" }).click();
    await expect(page.getByRole("alert")).toContainText(
      "The runner service is down",
    );
    await expect(page.getByText("Support code: WV-OUTAGE")).toBeVisible();
    await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  });

  test("an old failure in another section does not freeze the page", async ({
    page,
  }) => {
    await polling(page, ["deployment.read"], {
      runnersFail: { status: 500, json: { code: "WV-OUTAGE" } },
    });
    await expect(page.getByRole("alert")).toBeVisible();
    await expect(page.getByText(/^Updated /)).toHaveCount(0);
    // The jobs it loads now all arrived: that is what "Updated" counts.
    await page.getByRole("tab", { name: "Jobs" }).click();
    await expect(page.getByRole("button", { name: jobButton })).toBeVisible();
    await expect(page.getByText("Updated just now")).toBeVisible();
  });

  test("a record opened from its address says it just loaded", async ({
    page,
  }) => {
    await connected(page, { capabilities: ["deployment.read"] });
    await page.route(`**/deployment-targets/${targetId}`, (r) =>
      r.fulfill({ json: target(targetId, "owned-target", "docker-compose") }),
    );
    await page.goto(`/operate/clusters/targets/${targetId}`);
    await expect(
      page.getByRole("heading", { name: "owned-target", exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Updated just now")).toBeVisible();
  });

  test("opening a record restarts how fresh the page says it is", async ({
    page,
  }) => {
    await polling(page, ["deployment.read"]);
    await expect(page.getByText("Updated just now")).toBeVisible();
    await page.clock.runFor(6_000);
    await expect(page.getByText(/^Updated \d+ s ago$/)).toBeVisible();
    await page
      .getByRole("button", { name: "local-docker", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "local-docker", exact: true }),
    ).toBeVisible();
    await expect(page.getByText("Updated just now")).toBeVisible();
  });
});

// Last contact and reported operations are the point of the Runners tab, so
// they stay on screen at every width: as columns, or inside the row.
for (const width of [360, 600, 768, 1024, 1440])
  test(`runners show last contact and operations at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 800 });
    await clusters(page, ["deployment.read", "target.manage"]);
    await page.getByRole("tab", { name: "Runners" }).click();
    const table = page.getByRole("table", { name: "Runners" });
    const rows = table.locator(".resource-row");
    await expect(rows).toHaveCount(3);
    const seen = (index: number) =>
      rows.nth(index).evaluate((row) => (row as HTMLElement).innerText);
    expect(await seen(0)).toContain("just now");
    expect(await seen(0)).toContain("observe, update");
    expect(await seen(1)).toContain("4 min ago");
    expect(await seen(1)).toContain("observe, update");
    expect(await seen(2)).toContain("15 min ago");
    await expect(
      rows.nth(1).getByRole("button", { name: /^Revoke/ }),
    ).toBeVisible();
    const sideways = await page.evaluate(() => {
      const table = document.querySelector<HTMLElement>(".runner-table")!;
      return {
        page:
          document.documentElement.scrollWidth -
          document.documentElement.clientWidth,
        table: table.scrollWidth - table.clientWidth,
      };
    });
    expect(sideways).toEqual({ page: 0, table: 0 });
  });
