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
// Operate › Workers › Releases: the releases list and a release's usage (its
// workers by state, the activations that pin it and the deployments on
// targets the reader may see), the CLI command that admits a release, and a
// Roll out that waits for change requests. Then the layout of Workers,
// Incidents and Releases, measured at three sizes and with a touch pointer:
// shared edges and centers, no clipping and no sideways scrolling.
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page } from "@playwright/test";
import { connected } from "./support";

const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";
// Review captures, and the geometry each layout test measured, go to
// test-results/operate, or to the folder STUDIO_OPERATE_CAPTURES names.
const captures = resolve(
  process.env["STUDIO_OPERATE_CAPTURES"]?.trim() || "test-results/operate",
);
mkdirSync(captures, { recursive: true });
const releaseA = "33333333-3333-4333-8333-333333333333";
const releaseB = "34444444-4444-4444-8444-444444444444";
const target = "55555555-5555-4555-8555-555555555555";
const release = (id: string, digit: string, types: string[]) => ({
  id,
  image_digest: "sha256:" + digit.repeat(64),
  capabilities: types.map((type) => {
    const [taskType, taskVersion] = type.split("@");
    return { taskType, taskVersion };
  }),
  credential_capabilities: [],
});
const deployment = (n: number) => ({
  target_id: target,
  target_name: "eu-west",
  deployment_id: `66666666-6666-4666-8666-${String(n).padStart(12, "0")}`,
  deployment_name: `billing-${n}`,
  component: "workers",
  replicas: 2,
});

/** Releases A and B; A runs on 101 deployment components over two pages. */
async function releases(page: Page, capabilities = ["catalog.read"]) {
  await connected(page, { capabilities });
  const usage: URLSearchParams[] = [];
  await page.route(`${environment}/worker-releases?*`, (r) =>
    r.fulfill({
      json: {
        items: [
          release(releaseA, "a", ["crm-lookup@1.0.0", "email-send@2.1.0"]),
          release(releaseB, "b", ["echo@1.2.0"]),
        ],
        next_cursor: null,
      },
    }),
  );
  for (const [id, digit] of [
    [releaseA, "a"],
    [releaseB, "b"],
  ])
    await page.route(`${environment}/worker-releases/${id}`, (r) =>
      r.fulfill({
        json: release(
          id,
          digit,
          id === releaseA ? ["crm-lookup@1.0.0", "email-send@2.1.0"] : [],
        ),
      }),
    );
  await page.route(`${environment}/worker-releases/*/usage?*`, (r) => {
    const query = new URL(r.request().url()).searchParams;
    usage.push(query);
    const first = !query.has("cursor");
    return r.fulfill({
      json: {
        workers: { online: 3, offline: 1, draining: 1, revoked: 5 },
        deployments: first
          ? Array.from({ length: 100 }, (_, i) => deployment(i))
          : [deployment(100)],
        activations: 4,
        ...(first ? { next_cursor: "page-2" } : {}),
      },
    });
  });
  return { usage };
}

test.describe("releases", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("lists releases with their task types and shows the CLI command that admits one", async ({
    page,
  }) => {
    await releases(page);
    await page.goto("/operate/workers");
    await page.getByRole("tab", { name: "Releases" }).click();
    await expect(page).toHaveURL(/\/operate\/workers\/releases$/);
    await expect(page.getByRole("tab", { name: "Releases" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    const table = page.getByRole("table", { name: "Releases" });
    await expect(table.locator(".resource-row")).toHaveCount(2);
    await expect(table.locator(".resource-row").first()).toContainText(
      "sha256:aaaaaaaaaaaa",
    );
    await expect(table.locator(".resource-row").first()).toContainText(
      "crm-lookup@1.0.0, email-send@2.1.0",
    );
    await expect(page.locator(".command-line code")).toHaveText(
      "weave workers releases create --request release.json",
    );
    // Studio never offers to admit a release itself.
    await expect(
      page.getByRole("button", { name: /create|admit/i }),
    ).toHaveCount(0);
    await page.getByRole("tab", { name: "Fleet" }).click();
    await expect(page).toHaveURL(/\/operate\/workers$/);
  });

  test("a release shows its workers, pinning activations and the deployments it may see, with more on request", async ({
    page,
  }) => {
    const { usage } = await releases(page);
    await page.goto(`/operate/workers/releases/${releaseA}`);
    const detail = page.locator("weave-release-detail");
    await expect(detail.locator("#release-detail-title")).toHaveText(
      "Release sha256:aaaaaaaaaaaa",
    );
    const states = detail.locator(".release-workers li");
    await expect(states).toHaveCount(4);
    await expect(states.nth(0)).toContainText("3Online");
    await expect(states.nth(3)).toContainText("5Revoked");
    await expect(
      detail.locator("dt:text-is('Activations that pin it') + dd"),
    ).toHaveText("4");
    const used = detail.getByRole("table", { name: "Used by" });
    await expect(used.locator(".resource-row")).toHaveCount(100);
    await expect(used.locator(".resource-row").first()).toContainText(
      "eu-west › billing-0 › workers",
    );
    await detail.getByRole("button", { name: "Load more deployments" }).click();
    await expect(used.locator(".resource-row")).toHaveCount(101);
    expect(usage.at(-1)!.get("cursor")).toBe("page-2");
    expect(usage.at(-1)!.get("limit")).toBe("100");
    // Roll out waits for change requests, and says so.
    const rollout = detail.getByRole("button", {
      name: "Roll out to a target…",
    });
    await expect(rollout).toHaveAttribute("aria-disabled", "true");
    await expect(rollout).toHaveAccessibleDescription(
      /comes with change requests/,
    );
    await detail.getByRole("button", { name: "Show its workers" }).click();
    await expect(page).toHaveURL(
      new RegExp(`/operate/workers\\?release=${releaseA}$`),
    );
  });

  test("a release no target the person may see runs says so, and shows no other target", async ({
    page,
  }) => {
    await releases(page);
    await page.route(
      `${environment}/worker-releases/${releaseB}/usage?*`,
      (r) =>
        r.fulfill({
          json: {
            workers: { online: 0, offline: 0, draining: 0, revoked: 0 },
            deployments: [],
            activations: 0,
          },
        }),
    );
    await page.goto(`/operate/workers/releases/${releaseB}`);
    const detail = page.locator("weave-release-detail");
    await expect(detail).toContainText(
      "No deployment on a target you can see runs this release.",
    );
    await expect(detail.getByRole("table")).toHaveCount(0);
  });

  test("without catalog.read the releases show the access they need; a missing release says so", async ({
    page,
  }) => {
    await releases(page, ["status.read"]);
    await page.goto("/operate/workers/releases");
    await expect(page.locator("weave-releases-page")).toContainText(
      "You need catalog.read in this environment.",
    );
    await page.unrouteAll();
    await releases(page);
    await page.route(`${environment}/worker-releases/${releaseB}`, (r) =>
      r.fulfill({
        status: 404,
        json: { code: "WV-NOT-FOUND", message: "Release not found" },
      }),
    );
    await page.goto(`/operate/workers/releases/${releaseB}`);
    await expect(
      page.getByRole("heading", { name: "This release can't be found" }),
    ).toBeVisible();
  });
});

// ---------------------------------------------------------------- layout

const round = (value: number) => Math.round(value * 100) / 100;
const spread = (values: number[]) =>
  values.length ? round(Math.max(...values) - Math.min(...values)) : 0;
const record = (name: string, figures: Record<string, unknown>) =>
  writeFileSync(
    `${captures}/${name}.json`,
    JSON.stringify(figures, null, 2) + "\n",
  );
const at = (seconds: number) =>
  new Date(Date.now() + seconds * 1000).toISOString();

/** Workers with every fact the fleet shows, long values included. */
async function fleet(page: Page) {
  const worker = (n: number, extra: Record<string, unknown> = {}) => ({
    id: `1${n}111111-1111-4111-8111-111111111111`,
    principal_id: "22222222-2222-4222-8222-222222222222",
    release_id: releaseA,
    task_types: ["crm-lookup@1.0.0", "email-send@2.1.0"],
    capacity: 8,
    revoked: false,
    revision: 2,
    draining: false,
    presence: "recent",
    last_seen_at: at(-5),
    presence_expires_at: at(55),
    observed_at: at(0),
    active_leases: 3,
    available_capacity: 5,
    registered_at: at(-86_400),
    identity: {
      host: `billing-workers-7f9c4d8b6-x2p${n}q`,
      runtime: "container",
      sdk_version: "0.1.0a15",
      language_version: "3.12.4",
      platform_hint: "kubernetes",
    },
    placement: {
      target_id: target,
      target_name: "eu-west-production",
      deployment_id: "66666666-6666-4666-8666-666666666666",
      component: "billing-workers",
    },
    ...extra,
  });
  const items = [
    worker(1),
    worker(2, { draining: true, drained_at: at(-60), active_leases: 9 }),
    worker(3, {
      presence: "stale",
      last_seen_at: at(-900),
      presence_expires_at: at(-840),
      placement: undefined,
      identity: { runtime: "process" },
    }),
    worker(4, {
      presence: "unknown",
      last_seen_at: null,
      presence_expires_at: null,
      identity: undefined,
      placement: undefined,
      registered_at: undefined,
    }),
  ];
  await page.route(`${environment}/workers?*`, (r) =>
    r.fulfill({ json: { items, next_cursor: null } }),
  );
  await page.route(`${environment}/workers/*`, (r) => {
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({ json: items.find((item) => item.id === id) });
  });
}
/** Incidents with workflows, step instances and opening times. */
async function incidentList(page: Page) {
  const run = "5e6f7a8b-0000-4000-8000-000000000001";
  const incident = (n: number, extra: Record<string, unknown> = {}) => ({
    id: `9${n}111111-1111-4111-8111-111111111111`,
    run_id: run,
    incident_key: `charge-customer-card:${n}`,
    node_id: "charge-customer-card",
    instance_key: `charge-customer-card#${n}`,
    generation: n,
    origin_code: "WV-TASK-AMBIGUOUS",
    code: "WV-TASK-AMBIGUOUS",
    status: "active",
    revision: 2,
    external_effects_may_continue: n === 1,
    opened_at: at(-3600 * n),
    updated_at: at(-60),
    workflow: { name: "invoice-approval-with-a-long-name", version: "1.3.0" },
    run_status: "blocked",
    ...extra,
  });
  const items = [
    incident(1),
    incident(2, { opened_at_estimated: true, test: true }),
    incident(3, {
      code: "WV-TASK-RETRIES-EXHAUSTED",
      opened_at: undefined,
      workflow: undefined,
    }),
  ];
  await page.route(`${environment}/incidents?*`, (r) =>
    r.fulfill({ json: { items, next_cursor: null } }),
  );
  await page.route(`${environment}/incidents/*`, (r) => {
    const id = new URL(r.request().url()).pathname.split("/").pop();
    return r.fulfill({ json: items.find((item) => item.id === id) });
  });
  await page.route(`${environment}/runs/${run}/history?*`, (r) =>
    r.fulfill({
      json: {
        events: ["run_started", "task_scheduled", "incident_opened"].map(
          (type, i) => ({ type, sequence: i + 1 }),
        ),
        next_cursor: null,
      },
    }),
  );
}

/**
 * Measures the page: no sideways scroll, nothing cut off (an ellipsis only
 * with the whole value in its title), filter controls on one line sharing
 * their bottom edge, table columns sharing their left edge with their
 * heading, rows sharing their height, controls as tall as the target, and
 * every row's cells centered on the row's line.
 */
async function measure(page: Page, host: string, target: number) {
  return page.evaluate(
    ({ host, target }) => {
      const problems: string[] = [];
      const root = document.documentElement;
      if (root.scrollWidth > root.clientWidth + 1)
        problems.push("page scrolls sideways");
      const content = document.querySelector<HTMLElement>(".page-content");
      if (content && content.scrollWidth > content.clientWidth + 1)
        problems.push("content scrolls sideways");
      for (const node of document.querySelectorAll<HTMLElement>(`${host} *`)) {
        const style = getComputedStyle(node);
        if (style.display === "none" || !node.getClientRects().length) continue;
        if (["SELECT", "INPUT", "TEXTAREA", "OPTION"].includes(node.tagName))
          continue;
        // Visually hidden text is clipped on purpose, for screen readers.
        if (node.closest("svg, pre, .retired-ids, datalist, .sr-only"))
          continue;
        if (
          node.scrollWidth > node.clientWidth + 1 &&
          style.overflowX !== "visible"
        ) {
          const whole =
            style.textOverflow === "ellipsis" &&
            node.title !== "" &&
            node.title.includes((node.textContent ?? "").trim());
          if (!whole)
            problems.push(
              `${node.tagName.toLowerCase()}.${node.className} is cut off`,
            );
        }
        const box = node.getBoundingClientRect();
        if (box.right > root.clientWidth + 1 && style.position !== "fixed")
          problems.push(
            `${node.tagName.toLowerCase()}.${node.className} ends past the window`,
          );
      }
      const rect = (node: Element) => node.getBoundingClientRect();
      // Filter controls: each line's controls share their bottom edge.
      const controls = [
        ...document.querySelectorAll<HTMLElement>(
          `${host} .operate-filters :is(select, input:not([type=checkbox]), .checkbox-field, button)`,
        ),
      ].filter((node) => node.getClientRects().length);
      const lines = new Map<number, number[]>();
      const short: string[] = [];
      for (const node of controls) {
        const box = rect(node);
        const line = [...lines.keys()].find(
          (bottom) => Math.abs(bottom - box.bottom) < box.height / 2,
        );
        lines.set(line ?? box.bottom, [
          ...(lines.get(line ?? box.bottom) ?? []),
          box.bottom,
        ]);
        if (box.height < target - 0.5)
          short.push(
            `${node.tagName.toLowerCase()} ${node.id || node.textContent?.trim()} ${box.height}`,
          );
      }
      const filterSpread = Math.max(
        0,
        ...[...lines.values()].map(
          (values) => Math.max(...values) - Math.min(...values),
        ),
      );
      // Columns: every visible cell starts where its heading starts.
      const table = document.querySelector(`${host} [role=table]`);
      let columnSpread = 0;
      let rowHeights: number[] = [];
      let centerSpread = 0;
      if (table) {
        const head = table.querySelector(".table-head");
        const heads = head
          ? [...head.children].filter(
              (cell) => getComputedStyle(cell).display !== "none",
            )
          : [];
        const rows = [...table.querySelectorAll(".resource-row")];
        rowHeights = rows.map((row) => rect(row).height);
        if (head && getComputedStyle(head).display !== "none")
          for (const row of rows) {
            const cells = [...row.children].filter(
              (cell) => getComputedStyle(cell).display !== "none",
            );
            if (cells.length !== heads.length) continue;
            cells.forEach((cell, index) => {
              columnSpread = Math.max(
                columnSpread,
                Math.abs(rect(cell).left - rect(heads[index]).left),
              );
            });
          }
        // On a one-line row, the cells' centers share the row's line.
        for (const row of rows) {
          const box = rect(row);
          const cells = [...row.children].filter(
            (cell) => getComputedStyle(cell).display !== "none",
          );
          const tops = cells.map((cell) => rect(cell).top);
          if (Math.max(...tops) - Math.min(...tops) > box.height / 2) continue;
          const centers = cells
            .filter((cell) => rect(cell).height > 0)
            .map((cell) => rect(cell).top + rect(cell).height / 2);
          centerSpread = Math.max(
            centerSpread,
            Math.max(...centers) - Math.min(...centers),
          );
        }
      }
      return {
        problems,
        short,
        filterSpread: Math.round(filterSpread * 100) / 100,
        columnSpread: Math.round(columnSpread * 100) / 100,
        rowHeights,
        centerSpread: Math.round(centerSpread * 100) / 100,
      };
    },
    { host, target },
  );
}

const pages = [
  {
    name: "workers",
    host: "weave-workers-page",
    capabilities: [
      "status.read",
      "worker.drain",
      "release.retire",
      "catalog.read",
    ],
    setup: fleet,
    path: "/operate/workers",
    open: (page: Page) =>
      page.getByRole("button", { name: "Worker 11111111" }).click(),
    detail: "weave-worker-detail",
  },
  {
    name: "incidents",
    host: "weave-incidents-page",
    capabilities: ["incident.read", "incident.resolve", "run.read"],
    setup: incidentList,
    path: "/operate/incidents",
    open: (page: Page) =>
      page
        .getByRole("button", {
          name: "Open incident WV-TASK-AMBIGUOUS at charge-customer-card",
        })
        .first()
        .click(),
    detail: "weave-incident-drawer",
  },
  {
    name: "releases",
    host: "weave-releases-page",
    capabilities: ["status.read", "catalog.read"],
    setup: async (page: Page) => {
      await page.route(`${environment}/worker-releases?*`, (r) =>
        r.fulfill({
          json: {
            items: [
              release(releaseA, "a", ["crm-lookup@1.0.0", "email-send@2.1.0"]),
              release(releaseB, "b", ["echo@1.2.0"]),
            ],
            next_cursor: null,
          },
        }),
      );
      await page.route(`${environment}/worker-releases/*`, (r) =>
        r.fulfill({
          json: release(releaseA, "a", [
            "crm-lookup@1.0.0",
            "email-send@2.1.0",
          ]),
        }),
      );
      await page.route(`${environment}/worker-releases/*/usage?*`, (r) =>
        r.fulfill({
          json: {
            workers: { online: 3, offline: 1, draining: 1, revoked: 5 },
            deployments: [deployment(1), deployment(2)],
            activations: 4,
          },
        }),
      );
    },
    path: "/operate/workers/releases",
    open: (page: Page) =>
      page.getByRole("button", { name: "sha256:aaaaaaaaaaaa" }).click(),
    detail: "weave-release-detail",
  },
];

test.describe("layout", () => {
  for (const size of [
    { tag: "1440x900", width: 1440, height: 900, touch: false },
    { tag: "600x500", width: 600, height: 500, touch: false },
    { tag: "360x640", width: 360, height: 640, touch: false },
    { tag: "1440x900-touch", width: 1440, height: 900, touch: true },
  ])
    test.describe(size.tag, () => {
      test.use({
        viewport: { width: size.width, height: size.height },
        hasTouch: size.touch,
      });
      for (const subject of pages)
        test(`${subject.name} lines up, fits and never scrolls sideways`, async ({
          page,
        }) => {
          await connected(page, { capabilities: subject.capabilities });
          await subject.setup(page);
          await page.goto(subject.path);
          await expect(
            page.locator(`${subject.host} [role=table] .resource-row`).first(),
          ).toBeVisible();
          await page.evaluate(() => document.fonts.ready);
          const coarse = await page.evaluate(
            () => matchMedia("(pointer: coarse)").matches,
          );
          const target = coarse || size.width < 768 ? 44 : 36;
          // Narrow, the filters open from "Filters (n)" and are measured open.
          const toggle = page.locator(`${subject.host} .filters-toggle`);
          if (await toggle.isVisible()) await toggle.click();
          const list = await measure(page, subject.host, target);
          await page.screenshot({
            path: `${captures}/${subject.name}-${size.tag}.png`,
            fullPage: true,
          });
          await subject.open(page);
          await expect(page.locator(subject.detail)).toBeVisible();
          await page.waitForTimeout(400);
          const open = await measure(page, subject.host, target);
          await page.screenshot({
            path: `${captures}/${subject.name}-${size.tag}-detail.png`,
          });
          record(`${subject.name}-${size.tag}`, { target, list, open });
          for (const figures of [list, open]) {
            expect(figures.problems).toEqual([]);
            expect(figures.short).toEqual([]);
            expect(figures.filterSpread).toBeLessThanOrEqual(1);
            expect(figures.columnSpread).toBeLessThanOrEqual(1);
            expect(figures.centerSpread).toBeLessThanOrEqual(1);
          }
          // Wide, every row is one line; narrower, a long code or several
          // badges may wrap and make a row taller, never cut it.
          if (size.width >= 1100)
            expect(spread(list.rowHeights)).toBeLessThanOrEqual(1);
          expect(Math.min(...list.rowHeights)).toBeGreaterThanOrEqual(target);
        });
    });
});
