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
