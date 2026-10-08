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
// Clusters › Approvals: plans waiting for an approval, a countdown to each
// plan's expiry, and Approve plan for people who may approve.
import { test, expect, Page } from "@playwright/test";
import { connected } from "./support";

const scope = {
  tenant_id: "tenant",
  project_id: "project",
  environment_id: "development",
};
const targetId = "11111111-1111-4111-8111-111111111111";
const deploymentId = "22222222-2222-4222-8222-222222222222";
const soon = "66666666-6666-4666-8666-666666666661";
const later = "66666666-6666-4666-8666-666666666662";
const approved = "66666666-6666-4666-8666-666666666663";
const plan = (id: string, seconds: number) => ({
  id,
  scope,
  target_id: targetId,
  target_revision: 1,
  deployment_id: deploymentId,
  deployment_revision: 3,
  adapter: "docker-compose",
  adapter_version: "1",
  intent: id === later ? "update" : "scale_workers",
  observation_id: "55555555-5555-4555-8555-555555555555",
  observation_digest: "b".repeat(64),
  steps: [],
  risks: id === later ? [] : ["worker_drain"],
  created_at: new Date(Date.now() - 60_000).toISOString(),
  expires_at: new Date(Date.now() + seconds * 1000).toISOString(),
  digest: id.slice(-1).repeat(64),
});

async function inbox(page: Page, capabilities: string[]) {
  await connected(page, { capabilities });
  const plans = [plan(later, 600), plan(approved, 400), plan(soon, 125)];
  const approvals = new Map<string, unknown>([
    [
      approved,
      {
        plan_id: approved,
        digest: plans[1].digest,
        principal_id: "someone",
        approved_at: new Date().toISOString(),
      },
    ],
  ]);
  await page.route("**/deployment-targets?*", (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: targetId,
            name: "local-docker",
            adapter: "docker-compose",
            external_identity: "docker",
            boundary: "workers",
            runner_principal_id: "44444444-4444-4444-8444-444444444444",
            capabilities: ["observe", "scale_workers"],
            scope,
            revision: 1,
            disabled: false,
            created_at: "2026-10-03T10:00:00Z",
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/deployments?*", (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: deploymentId,
            target_id: targetId,
            name: "fixture-workers",
            ownership: "managed",
            components: [],
            scope,
            revision: 3,
            created_at: "2026-10-03T10:00:00Z",
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/deployment-plans?*", (r) =>
    r.fulfill({ json: { items: plans, next_cursor: null } }),
  );
  await page.route("**/deployment-plans/*/approval", (r) => {
    const id = new URL(r.request().url()).pathname.split("/").at(-2)!;
    return r.fulfill({ json: approvals.get(id) ?? null });
  });
  await page.goto("/operate/clusters?tab=approvals");
  return { plans, approvals };
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("lists plans waiting for an approval, soonest expiry first", async ({
      page,
    }) => {
      await inbox(page, ["deployment.read", "deployment.approve"]);
      await expect(
        page.getByRole("tab", { name: "Approvals (2)" }),
      ).toHaveAttribute("aria-selected", "true");
      const rows = page
        .getByRole("table", { name: "Plans waiting for an approval" })
        .locator(".resource-row");
      await expect(rows).toHaveCount(2);
      await expect(rows.nth(0)).toContainText(
        "Scale workers · fixture-workers",
      );
      await expect(rows.nth(0)).toContainText("local-docker");
      await expect(rows.nth(1)).toContainText("Update · fixture-workers");
      await expect(
        rows.nth(1).getByRole("button", { name: /^Approve plan/ }),
      ).toBeVisible();
    });

    test("the countdown warns, turns danger, then shows Expired at the plan's end", async ({
      page,
    }) => {
      await page.clock.install();
      await inbox(page, ["deployment.read", "deployment.approve"]);
      // Rows re-sort as plans expire: follow this plan by its name.
      const row = page
        .getByRole("table", { name: "Plans waiting for an approval" })
        .locator(".resource-row")
        .filter({ hasText: "Scale workers · fixture-workers" });
      const clock = row.locator(".countdown .status-pill");
      await expect(clock).toHaveText(/^2:0[0-5]$/);
      await page.clock.fastForward(10_000);
      await expect(clock).toHaveText(/^1:5\d$/);
      await expect(row).toContainText("Expires soon");
      await expect(clock).toHaveAttribute("data-tone", "warning");
      await page.clock.fastForward(95_000);
      await expect(clock).toHaveAttribute("data-tone", "danger");
      await page.clock.fastForward(25_000);
      await expect(clock).toHaveText("Expired");
      await expect(
        row.getByRole("button", { name: /^Approve plan/ }),
      ).toHaveCount(0);
      await expect(
        row.getByRole("button", { name: "Open deployment" }),
      ).toBeVisible();
    });

    test("Approve plan confirms the digest and the plan leaves the inbox", async ({
      page,
    }) => {
      const { plans, approvals } = await inbox(page, [
        "deployment.read",
        "deployment.approve",
      ]);
      const bodies: unknown[] = [];
      await page.route(`**/deployment-plans/${soon}/approve`, (r) => {
        bodies.push(r.request().postDataJSON());
        const approval = {
          plan_id: soon,
          digest: plans[2].digest,
          principal_id: "human",
          approved_at: new Date().toISOString(),
        };
        approvals.set(soon, approval);
        return r.fulfill({ json: approval });
      });
      await page
        .getByRole("button", {
          name: "Approve plan Scale workers · fixture-workers",
        })
        .click();
      const dialog = page.getByRole("dialog", { name: "Approve this plan?" });
      await expect(dialog).toContainText(`digest ${"1".repeat(12)}`);
      await dialog.getByRole("button", { name: "Approve plan" }).click();
      await expect(page.getByText("Plan approved.")).toBeVisible();
      expect(bodies).toEqual([{ digest: plans[2].digest }]);
      await expect(
        page
          .getByRole("table", { name: "Plans waiting for an approval" })
          .locator(".resource-row"),
      ).toHaveCount(1);
      await expect(
        page.getByRole("tab", { name: "Approvals (1)" }),
      ).toBeVisible();
    });

    test("a stale plan's approval is refused and explained", async ({
      page,
    }) => {
      await inbox(page, ["deployment.read", "deployment.approve"]);
      await page.route(`**/deployment-plans/${soon}/approve`, (r) =>
        r.fulfill({
          status: 409,
          json: {
            code: "WV-PLAN-STALE",
            message: "Approval differs from reviewed plan",
          },
        }),
      );
      await page
        .getByRole("button", {
          name: "Approve plan Scale workers · fixture-workers",
        })
        .click();
      await page
        .getByRole("dialog", { name: "Approve this plan?" })
        .getByRole("button", { name: "Approve plan" })
        .click();
      await expect(page.getByRole("alert")).toContainText(
        "Approval differs from reviewed plan",
      );
      await expect(page.getByRole("alert")).toContainText(
        "Support code: WV-PLAN-STALE",
      );
      await expect(
        page
          .getByRole("table", { name: "Plans waiting for an approval" })
          .locator(".resource-row"),
      ).toHaveCount(2);
    });

    test("a reader sees the inbox without Approve plan", async ({ page }) => {
      await inbox(page, ["deployment.read"]);
      await expect(page.getByRole("tab", { name: "Approvals" })).toBeVisible();
      await expect(
        page
          .getByRole("table", { name: "Plans waiting for an approval" })
          .locator(".resource-row"),
      ).toHaveCount(2);
      await expect(
        page.getByRole("button", { name: /^Approve plan/ }),
      ).toHaveCount(0);
      await page
        .getByRole("button", { name: "Review Scale workers · fixture-workers" })
        .click();
      await expect(page).toHaveURL(
        new RegExp(`/operate/clusters/plans/${soon}$`),
      );
    });
  });

// When each countdown ends, what a failed read says, and how a failure on the
// Approvals tab weighs on the rest of the page. The timing tests drive the
// page clock.
test.describe("expiry and failures", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  const waiting = (page: Page) =>
    page
      .getByRole("table", { name: "Plans waiting for an approval" })
      .locator(".resource-row");
  const outage = {
    status: 500,
    json: { code: "WV-OUTAGE", message: "The plan service is down" },
  };

  test("a countdown says Expired at the moment its plan ends", async ({
    page,
  }) => {
    await page.clock.install();
    const { plans } = await inbox(page, [
      "deployment.read",
      "deployment.approve",
    ]);
    const row = waiting(page).filter({
      hasText: "Scale workers · fixture-workers",
    });
    const clock = row.locator(".countdown .status-pill");
    await expect(clock).toHaveText(/^2:0[0-5]$/);
    const ends = Date.parse(plans[2].expires_at);
    // Half a second off the whole seconds, so a plain 1 s tick would miss it.
    await page.clock.pauseAt(ends - 1500);
    await page.clock.runFor(1498);
    await expect(clock).toHaveText("0:01");
    await expect(
      row.getByRole("button", { name: /^Approve plan/ }),
    ).toBeVisible();
    // The plan ends; the page draws a millisecond later.
    await page.clock.runFor(3);
    await expect(clock).toHaveText("Expired");
    await expect(
      row.getByRole("button", { name: /^Approve plan/ }),
    ).toHaveCount(0);
    await page.clock.resume();
    await row.getByRole("button", { name: "Open deployment" }).click();
    await expect(page).toHaveURL(
      new RegExp(`/operate/clusters/deployments/${deploymentId}$`),
    );
  });

  test("a failed approvals read holds back only the Approvals tab", async ({
    page,
  }) => {
    await page.clock.install();
    await inbox(page, ["deployment.read", "deployment.approve"]);
    await expect(page.getByRole("tab", { name: /^Approvals/ })).toHaveText(
      "Approvals (2)",
    );
    await expect(page.getByText("Updated just now")).toBeVisible();
    let reads = 0;
    await page.route("**/deployment-plans?*", (r) => {
      reads++;
      return r.fulfill(outage);
    });
    await page.clock.runFor(16_000);
    await expect(page.getByRole("alert")).toContainText(
      "The plan service is down",
    );
    await expect(page.getByText("Support code: WV-OUTAGE")).toBeVisible();
    // The tab shown failed: the page waits longer and "Updated" keeps its age.
    await page.clock.runFor(16_000);
    expect(reads).toBe(1);
    await expect(page.getByText(/^Updated 3\d s ago$/)).toBeVisible();
    // Targets judges only what it loads, now and on every later reload, and
    // the Approvals tab drops a count it can no longer vouch for.
    await page.getByRole("tab", { name: "Targets" }).click();
    await expect(page.getByText("Updated just now")).toBeVisible();
    await expect(page.getByRole("tab", { name: /^Approvals/ })).toHaveText(
      "Approvals",
    );
    await page.clock.runFor(16_000);
    await expect(page.getByText("Updated just now")).toBeVisible();
    await page.clock.runFor(16_000);
    await expect(page.getByText("Updated just now")).toBeVisible();
  });

  test("an approvals read that fails after leaving the tab does not hold back the page", async ({
    page,
  }) => {
    await page.clock.install();
    await inbox(page, ["deployment.read", "deployment.approve"]);
    await expect(page.getByRole("tab", { name: /^Approvals/ })).toHaveText(
      "Approvals (2)",
    );
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    let reads = 0;
    await page.route("**/deployment-plans?*", async (r) => {
      reads++;
      await held;
      await r.fulfill(outage);
    });
    // A reload of the Approvals tab waits on its plans read…
    await page.clock.runFor(16_000);
    await expect.poll(() => reads).toBe(1);
    await page.getByRole("tab", { name: "Targets" }).click();
    // …which fails once Targets is shown.
    release();
    await expect(page.getByRole("tab", { name: /^Approvals/ })).toHaveText(
      "Approvals",
    );
    await page.clock.runFor(16_000);
    await expect(page.getByText("Updated just now")).toBeVisible();
    await page.clock.runFor(16_000);
    await expect(page.getByText("Updated just now")).toBeVisible();
    expect(reads).toBe(1);
  });

  test("a plan whose approval could not be read is not shown as waiting", async ({
    page,
  }) => {
    await inbox(page, ["deployment.read", "deployment.approve"]);
    await expect(waiting(page)).toHaveCount(2);
    await page.route(`**/deployment-plans/${soon}/approval`, (r) =>
      r.fulfill(outage),
    );
    await page.reload();
    await expect(page.getByRole("alert")).toContainText(
      "1 plan could not be checked for an approval.",
    );
    await expect(waiting(page)).toHaveCount(1);
    await expect(waiting(page)).toContainText("Update · fixture-workers");
    await expect(page.getByRole("tab", { name: /^Approvals/ })).toHaveText(
      "Approvals (1)",
    );
    // Part of the tab did not load, so the page does not say it is current.
    await expect(page.getByText(/^Updated /)).toHaveCount(0);
  });

  test("names that could not be loaded are explained", async ({ page }) => {
    await inbox(page, ["deployment.read", "deployment.approve"]);
    await page.route("**/deployment-targets?*", (r) =>
      r.fulfill({
        status: 500,
        json: { code: "WV-OUTAGE", message: "The target service is down" },
      }),
    );
    await page.reload();
    await expect(page.getByRole("alert")).toContainText(
      "Target and deployment names could not be loaded. The target service is down",
    );
    await expect(waiting(page).first()).toContainText(
      `Target ${targetId.slice(0, 8)}`,
    );
    await expect(page.getByText(/^Updated /)).toHaveCount(0);
  });

  test("a refused plans read shows the access state", async ({ page }) => {
    await inbox(page, ["deployment.read", "deployment.approve"]);
    await page.route("**/deployment-plans?*", (r) =>
      r.fulfill({
        status: 403,
        json: { code: "WV-DENIED", message: "Denied" },
      }),
    );
    await page.reload();
    await expect(
      page.getByRole("heading", { name: "You don't have access to plans" }),
    ).toBeVisible();
    await expect(
      page.getByText(
        "You need deployment.read in this environment. Ask an administrator in Settings › People and access.",
      ),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Try again" })).toHaveCount(
      0,
    );
    await expect(page.getByRole("tab", { name: /^Approvals/ })).toHaveText(
      "Approvals",
    );
  });
});
