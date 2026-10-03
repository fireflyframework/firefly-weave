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
import { test, expect } from "@playwright/test";
import { connected } from "./support";
for (const width of [1440, 390])
  test.describe(`Worker operations at ${width}`, () => {
    test.use({ viewport: { width, height: 900 } });
    test("drain stops new claims without claiming existing work is finished", async ({
      page,
    }) => {
      await connected(page, { capabilities: ["status.read", "worker.drain"] });
      const id = "11111111-1111-4111-8111-111111111111";
      const worker = {
        id,
        principal_id: "22222222-2222-4222-8222-222222222222",
        release_id: "33333333-3333-4333-8333-333333333333",
        task_types: ["test@1.0.0"],
        capacity: 8,
        revoked: false,
        revision: 2,
        draining: false,
        presence: "recent",
        last_seen_at: new Date().toISOString(),
        presence_expires_at: new Date(Date.now() + 60000).toISOString(),
        presence_ttl_seconds: 60,
        observed_at: new Date().toISOString(),
        active_leases: 2,
        available_capacity: 6,
      };
      await page.route("**/workers?*", (route) =>
        route.fulfill({ json: { items: [worker], next_cursor: null } }),
      );
      await page.route("**/workers/" + id, (route) =>
        route.fulfill({ json: worker }),
      );
      let drains = 0;
      await page.route("**/workers/" + id + "/drain", async (route) => {
        drains++;
        expect(route.request().postDataJSON()).toEqual({
          expected_revision: 2,
        });
        expect(route.request().headers()["idempotency-key"]).toBeTruthy();
        worker.draining = true;
        worker.revision = 3;
        worker.available_capacity = 0;
        await route.fulfill({ json: worker });
      });
      await page.getByRole("button", { name: "Workers", exact: true }).click();
      await page
        .getByRole("button", { name: "Worker 11111111", exact: true })
        .click();
      const view = page.locator("weave-worker-controls");
      await expect(
        view.getByText("Recent contact", { exact: true }),
      ).toBeVisible();
      await expect(view.getByText("6", { exact: true })).toBeVisible();
      await view
        .getByRole("button", { name: "Drain worker", exact: true })
        .click();
      await expect(view.getByText("Draining", { exact: true })).toBeVisible();
      await expect(view.getByText("2", { exact: true })).toBeVisible();
      await expect(
        view.getByRole("button", { name: "Resume claims", exact: true }),
      ).toBeVisible();
      expect(drains).toBe(1);
    });
    test("stale contact leaves available capacity unknown and readers cannot drain", async ({
      page,
    }) => {
      await connected(page, { capabilities: ["status.read"] });
      const worker = {
        id: "11111111-1111-4111-8111-111111111111",
        capacity: 8,
        revoked: false,
        revision: 1,
        draining: false,
        presence: "stale",
        last_seen_at: "2020-01-01T00:00:00Z",
        presence_expires_at: "2020-01-01T00:01:00Z",
        active_leases: 0,
        available_capacity: null,
      };
      await page.route("**/workers?*", (route) =>
        route.fulfill({ json: { items: [worker], next_cursor: null } }),
      );
      await page.route("**/workers/" + worker.id, (route) =>
        route.fulfill({ json: worker }),
      );
      await page.getByRole("button", { name: "Workers", exact: true }).click();
      await page
        .getByRole("button", { name: "Worker 11111111", exact: true })
        .click();
      const view = page.locator("weave-worker-controls");
      await expect(
        view.getByText("Contact expired", { exact: true }),
      ).toBeVisible();
      await expect(
        view
          .locator("dt")
          .filter({ hasText: "Available capacity at observation" })
          .locator("+ dd"),
      ).toHaveText("Unknown");
      await expect(
        view.getByRole("button", { name: "Drain worker", exact: true }),
      ).toHaveCount(0);
    });
  });
