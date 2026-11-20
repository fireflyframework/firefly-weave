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
// Operate › Logs › Container logs: a target, a worker component, up to five
// members, Since and Last lines; Fetch logs asks the target's runner, waits
// for its snapshot and shows each member's lines with Copy and Download;
// Follow asks again every 10 seconds; and the runner offline, no read_logs,
// waiting, expired, failed and no-access states. The platform is mocked.
import { mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test, expect, type Page, type Request } from "@playwright/test";
import { connected } from "./support";

const captures = resolve(
  process.env["STUDIO_CONTAINER_CAPTURES"]?.trim() ||
    "test-results/container-logs",
);
mkdirSync(captures, { recursive: true });
const NOW = "2026-11-19T10:54:05Z";
const TARGET = "5e1f2a3b-4c5d-4e6f-8a7b-9c0d1e2f3a4b";
const OTHER = "6f2a3b4c-5d6e-4f70-8b1c-2d3e4f5a6b7c";
const FIRST = "weave-worker-7c9f-x2k4";
const SECOND = "weave-worker-7c9f-p8sd";
const reader = ["deployment_log.read", "deployment.read", "run.read"];

const target = (id: string, name: string, capabilities: string[]) => ({
  id,
  name,
  adapter: "docker-compose",
  external_identity: "engine",
  boundary: "weave-workers",
  runner_principal_id: "0c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
  capabilities,
  scope: { tenant_id: "t", project_id: "p", environment_id: "e" },
  revision: 1,
  disabled: false,
  created_at: "2026-11-19T09:00:00Z",
});
const runner = (lastSeen: string) => ({
  id: "1d2e3f4a-5b6c-4d7e-8f90-a1b2c3d4e5f6",
  principal_id: "0c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f",
  target_id: TARGET,
  adapter: "docker-compose",
  adapter_version: "2",
  capabilities: ["observe", "read_logs"],
  last_seen: lastSeen,
  expires_at: new Date(Date.parse(lastSeen) + 90_000).toISOString(),
  revoked: false,
});
const observation = (members: string[]) => ({
  id: "2e3f4a5b-6c7d-4e8f-9a01-b2c3d4e5f6a7",
  target_id: TARGET,
  target_revision: 1,
  observed_at: "2026-11-19T10:50:00Z",
  expires_at: "2026-11-19T11:05:00Z",
  digest: "a".repeat(64),
  complete: true,
  settled: true,
  resources: [
    {
      name: "weave-worker",
      external_identity: "compose/weave-workers/weave-worker",
      kind: "worker",
      image: null,
      replicas: members.length,
      ready_replicas: members.length,
      version: "v",
      ownership: "managed",
      state: "ready",
      members: members.map((host) => ({
        host,
        ordinal: 1,
        state: "running",
        ready: true,
        started_at: "2026-11-19T10:40:00Z",
        restarts: 0,
        node: null,
      })),
    },
  ],
});
const lines = {
  [FIRST]: [
    [
      "2026-11-19T10:41:52Z",
      "stdout",
      "Worker weave-worker-7c9f-x2k4 ready: 4 slots, task types sql.lookup, crm.lookup",
    ],
    ["2026-11-19T10:41:58Z", "stdout", "Claimed task fetch for run 1c0d7f8a"],
    [
      "2026-11-19T10:42:01Z",
      "stderr",
      "WARN smtp-relay.acme.example answered 550; Authorization: [redacted]",
    ],
    [
      "2026-11-19T10:42:01Z",
      "stdout",
      "Reported failure SMTP_REJECTED for task send",
    ],
  ],
  [SECOND]: [
    [
      "2026-11-19T10:40:11Z",
      "stdout",
      "Worker weave-worker-7c9f-p8sd ready: 4 slots",
    ],
    ["2026-11-19T10:53:48Z", "stdout", "Claimed task fetch for run 8d2c6a1f"],
    ["2026-11-19T10:53:51Z", "stderr", "WARN billing-db answered after 1.8 s"],
  ],
} as Record<string, string[][]>;
const snapshot = (
  state: string,
  facts: Record<string, unknown> = {},
  members: Record<string, string[][]> = lines,
) => ({
  id: "3f4a5b6c-7d8e-4f90-a1b2-c3d4e5f6a7b8",
  target_id: TARGET,
  component: "weave-worker",
  requested_members: [FIRST, SECOND],
  since_seconds: 900,
  tail_lines: 200,
  state,
  created_at: "2026-11-19T10:54:04Z",
  expires_at: "2026-11-19T11:54:04Z",
  read_at: state === "ready" ? NOW : null,
  members:
    state === "ready"
      ? Object.entries(members).map(([member, rows]) => ({
          member,
          lines: rows.map(([at, stream, text]) => ({ at, stream, text })),
        }))
      : [],
  truncated: false,
  failure: null,
  ...facts,
});

interface Platform {
  /** Each deployment_log_requests.create body, in order. */
  creates: Record<string, unknown>[];
  /** Each create's Idempotency-Key. */
  keys: string[];
  /** What a read of the request answers, in turn; the last repeats. */
  reads: unknown[];
  /** The create's own answer: a pending snapshot, or a problem. */
  created: { status: number; json: unknown };
}
async function platform(
  page: Page,
  {
    capabilities = reader,
    lastSeen = "2026-11-19T10:54:00Z",
    targetCapabilities = ["observe", "read_logs"],
    members = [FIRST, SECOND],
  }: {
    capabilities?: string[];
    lastSeen?: string | null;
    targetCapabilities?: string[];
    members?: string[];
  } = {},
): Promise<Platform> {
  await page.clock.install({ time: new Date(NOW) });
  const identity = page.waitForResponse("**/studio/api/api/v1/identity");
  await connected(page, { capabilities, environmentName: "local" });
  await identity;
  const result: Platform = {
    creates: [],
    keys: [],
    reads: [snapshot("ready")],
    created: { status: 201, json: snapshot("pending") },
  };
  await page.route("**/deployment-targets?**", (route) =>
    route.fulfill({
      json: {
        items: [
          target(TARGET, "local-docker", targetCapabilities),
          target(OTHER, "k3d-weave-test", ["observe"]),
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/deployment-runners?**", (route) =>
    route.fulfill({
      json: { items: lastSeen ? [runner(lastSeen)] : [], next_cursor: null },
    }),
  );
  await page.route("**/deployment-observations?**", (route) =>
    route.fulfill({
      json: { items: [observation(members)], next_cursor: null },
    }),
  );
  // Creating answers at the collection, reading at a request's own address.
  await page.route(/\/deployment-log-requests(\/[^/?]+)?$/, async (route) => {
    const request: Request = route.request();
    if (request.method() === "POST") {
      result.creates.push(request.postDataJSON() as Record<string, unknown>);
      result.keys.push(request.headers()["idempotency-key"] ?? "");
      return route.fulfill(result.created);
    }
    const next =
      result.reads.length > 1 ? result.reads.shift() : result.reads[0];
    return route.fulfill({ json: next });
  });
  return result;
}
const openContainers = async (page: Page) => {
  await page.goto("/operate/logs?tab=containers");
  await expect(page.locator("#main h1")).toHaveText("Logs");
};
const fetchButton = (page: Page) =>
  page
    .locator(".fetchbar")
    .getByRole("button", { name: /Fetch logs|Fetching/ });
const member = (page: Page, name: string) => page.getByRole("region", { name });
const shot = (page: Page, name: string) =>
  page.screenshot({ path: `${captures}/${name}.png`, fullPage: true });
/**
 * Nothing scrolls sideways, and nothing of the tab reaches past the window's right edge, where the shell would clip
 * it without a scroll bar.
 */
const noSideways = (page: Page) =>
  page.evaluate(
    () =>
      document.scrollingElement!.scrollWidth <= window.innerWidth &&
      [...document.querySelectorAll("weave-container-logs *")].every(
        (element) =>
          element.getBoundingClientRect().right <= window.innerWidth + 1,
      ),
  );

test.use({ timezoneId: "UTC" });

for (const size of [
  { width: 1440, height: 900 },
  { width: 360, height: 740 },
]) {
  test.describe(`Operate › Logs › Container logs at ${size.width} px`, () => {
    test.use({ viewport: size });

    test("fetches a snapshot of the observed members and shows each member's lines", async ({
      page,
    }) => {
      const logs = await platform(page);
      await openContainers(page);
      await expect(
        page.getByRole("tab", { name: "Container logs" }),
      ).toHaveAttribute("aria-selected", "true");
      await expect(page.locator("#main .subtitle")).toHaveText(
        "Log entries from every run in this environment, and snapshots of container logs.",
      );
      await expect(page.getByLabel("Target")).toHaveValue(TARGET);
      await expect(page.getByLabel("Component")).toHaveValue("weave-worker");
      await expect(
        page.getByLabel("Since").locator("option:checked"),
      ).toHaveText("Last 15 minutes");
      await expect(page.getByLabel("Last lines")).toHaveValue("200");
      await expect(page.locator(".chip-text")).toHaveText([FIRST, SECOND]);
      await expect(page.locator(".fetchbar")).toContainText(
        "Fetching asks the runner of local-docker for a snapshot. It changes nothing on the target.",
      );
      await fetchButton(page).click();
      await expect(fetchButton(page)).toHaveAttribute("aria-busy", "true");
      await expect(fetchButton(page)).toHaveText("Fetching…");
      await expect(page.locator(".fetchbar")).toContainText(
        "Asking the runner of local-docker. A snapshot usually arrives within 10 seconds.",
      );
      await page.clock.runFor(1_000);
      await expect(member(page, FIRST)).toBeVisible();
      expect(logs.creates).toEqual([
        {
          target_id: TARGET,
          component: "weave-worker",
          members: [FIRST, SECOND],
          since_seconds: 900,
          tail_lines: 200,
        },
      ]);
      expect(logs.keys[0]).toMatch(/^[0-9a-f-]{36}$/);
      await expect(page.locator(".snapshot-note")).toHaveText(
        "Snapshot from 10:54:05 · Snapshots expire after 1 hour. Secrets are redacted before they leave the runner.",
      );
      const first = member(page, FIRST);
      await expect(first.locator(".count")).toHaveText("4 lines");
      await expect(first.locator(".line .t")).toHaveText([
        "10:41:52",
        "10:41:58",
        "10:42:01",
        "10:42:01",
      ]);
      await expect(first.locator(".line").nth(2).locator(".stream")).toHaveText(
        "stderr",
      );
      await expect(first.locator(".line").nth(2).locator(".text")).toHaveText(
        "WARN smtp-relay.acme.example answered 550; Authorization: [redacted]",
      );
      await expect(member(page, SECOND).locator(".count")).toHaveText(
        "3 lines",
      );
      await expect(fetchButton(page)).toHaveText("Fetch logs");
      expect(await noSideways(page)).toBe(true);
      await shot(page, `container-logs-snapshot-${size.width}`);
    });
  });
}

test.describe("Operate › Logs › Container logs", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the tabs follow the address, and the keyboard moves between them", async ({
    page,
  }) => {
    await platform(page);
    await page.goto("/operate/logs");
    const runs = page.getByRole("tab", { name: "Run logs" });
    await expect(runs).toHaveAttribute("aria-selected", "true");
    await runs.focus();
    await page.keyboard.press("ArrowRight");
    await expect(
      page.getByRole("tab", { name: "Container logs" }),
    ).toBeFocused();
    await expect(page).toHaveURL(/\/operate\/logs\?tab=containers$/);
    await expect(page.getByLabel("Target")).toBeVisible();
    await page.keyboard.press("Home");
    await expect(runs).toHaveAttribute("aria-selected", "true");
    await expect(page).toHaveURL(/\/operate\/logs$/);
    await page.goBack();
    await expect(
      page.getByRole("tab", { name: "Container logs" }),
    ).toHaveAttribute("aria-selected", "true");
  });

  test("the download is plain text with a fixed file name, and Copy says what it copied", async ({
    page,
    context,
  }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await platform(page);
    await openContainers(page);
    await fetchButton(page).click();
    await page.clock.runFor(1_000);
    const first = member(page, FIRST);
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      first
        .getByRole("button", { name: `Download the lines of ${FIRST}` })
        .click(),
    ]);
    expect(download.suggestedFilename()).toBe(
      `local-docker-weave-worker-${FIRST}-20261119T105405Z.log`,
    );
    const text = readFileSync((await download.path())!, "utf8");
    expect(text).toBe(
      lines[FIRST].map(
        ([at, stream, line]) =>
          `${new Date(at).toISOString()} ${stream} ${line}\n`,
      ).join(""),
    );
    await first
      .getByRole("button", { name: `Copy the lines of ${FIRST}` })
      .click();
    await expect(
      page.locator(".container-logs [role=status].sr-only"),
    ).toHaveText(`Copied 4 lines of ${FIRST}.`);
  });

  test("markup in a line shows as text", async ({ page }) => {
    const logs = await platform(page);
    logs.reads = [
      snapshot(
        "ready",
        {},
        {
          [FIRST]: [
            [NOW, "stdout", '<img src=x onerror="alert(1)"><b>bold</b>'],
          ],
        },
      ),
    ];
    await openContainers(page);
    await fetchButton(page).click();
    await page.clock.runFor(1_000);
    const text = member(page, FIRST).locator(".line .text");
    await expect(text).toHaveText('<img src=x onerror="alert(1)"><b>bold</b>');
    await expect(member(page, FIRST).locator("img, b")).toHaveCount(0);
  });

  test("Follow asks for a new snapshot every 10 seconds", async ({ page }) => {
    const logs = await platform(page);
    await openContainers(page);
    await page.getByRole("switch", { name: "Follow" }).check();
    await page.clock.runFor(1_000);
    await expect(member(page, FIRST)).toBeVisible();
    expect(logs.creates).toHaveLength(1);
    await page.clock.runFor(10_000);
    await expect.poll(() => logs.creates.length).toBe(2);
    await page.getByRole("switch", { name: "Follow" }).uncheck();
    await page.clock.runFor(30_000);
    expect(logs.creates).toHaveLength(2);
  });

  test("at most five members are asked for, and none means every running member", async ({
    page,
  }) => {
    const hosts = ["a", "b", "c", "d", "e", "f", "g"].map(
      (n) => `weave-worker-7c9f-${n}`,
    );
    const logs = await platform(page, { members: hosts });
    await openContainers(page);
    await expect(page.locator(".chip-text")).toHaveText(hosts.slice(0, 5));
    await expect(page.getByLabel("Add a member")).toHaveCount(0);
    await page.getByRole("button", { name: `Remove ${hosts[0]}` }).click();
    await page.getByLabel("Add a member").selectOption(hosts[6]);
    await expect(page.locator(".chip-text")).toHaveText([
      ...hosts.slice(1, 5),
      hosts[6],
    ]);
    await fetchButton(page).click();
    expect((logs.creates[0]["members"] as string[]).length).toBe(5);
    for (const host of [...hosts.slice(1, 5), hosts[6]])
      await page.getByRole("button", { name: `Remove ${host}` }).click();
    await expect(page.locator(".chips")).toContainText(
      "Every running member, up to 5",
    );
    await page.clock.runFor(1_000);
    await fetchButton(page).click();
    await expect.poll(() => logs.creates.length).toBe(2);
    expect(logs.creates[1]["members"]).toEqual([]);
  });

  test("a runner out of touch turns Fetch logs off and says for how long", async ({
    page,
  }) => {
    const logs = await platform(page, { lastSeen: "2026-11-19T10:52:01Z" });
    await openContainers(page);
    await expect(page.locator(".container-logs .notice")).toHaveText(
      "The runner for local-docker hasn't been in touch for 2 min, so Weave can't fetch its logs.",
    );
    await expect(fetchButton(page)).toHaveAttribute("aria-disabled", "true");
    await fetchButton(page).click({ force: true });
    expect(logs.creates).toHaveLength(0);
    await shot(page, "container-logs-runner-offline-1440");
  });

  test("a target whose runner can't read logs says so", async ({ page }) => {
    await platform(page, { targetCapabilities: ["observe", "scale_workers"] });
    await openContainers(page);
    await expect(page.locator(".container-logs .notice")).toHaveText(
      "This target's runner can't fetch logs.",
    );
    await expect(fetchButton(page)).toHaveAttribute("aria-disabled", "true");
    await shot(page, "container-logs-no-read-logs-1440");
  });

  test("a snapshot past its hour, or one the runner never sent, says what to do", async ({
    page,
  }) => {
    const logs = await platform(page);
    logs.reads = [snapshot("failed", { failure: "no_answer" })];
    await openContainers(page);
    await fetchButton(page).click();
    await page.clock.runFor(1_000);
    await expect(page.getByRole("alert")).toHaveText(
      "The runner of local-docker didn't answer within 2 minutes. Try again, or check its runner in Operate › Clusters.",
    );
    await shot(page, "container-logs-no-answer-1440");
    logs.reads = [snapshot("ready")];
    await fetchButton(page).click();
    await page.clock.runFor(1_000);
    await expect(member(page, FIRST)).toBeVisible();
    // An hour later the same snapshot shows nothing of its lines.
    await page.clock.fastForward(3_600_000);
    await page.getByLabel("Since").selectOption("3600");
    // The runner, last in touch an hour ago too, now reads offline above it.
    await expect(
      page.getByText(
        "This snapshot expired after 1 hour. Fetch logs again to see current lines.",
      ),
    ).toBeVisible();
    await expect(member(page, FIRST)).toHaveCount(0);
    await shot(page, "container-logs-expired-1440");
  });

  test("waiting for the runner shows Fetching… until the snapshot arrives", async ({
    page,
  }) => {
    const logs = await platform(page);
    logs.reads = [snapshot("pending"), snapshot("pending"), snapshot("ready")];
    await openContainers(page);
    await fetchButton(page).click();
    await expect(fetchButton(page)).toHaveAttribute("aria-busy", "true");
    await shot(page, "container-logs-waiting-1440");
    await page.clock.runFor(2_000);
    await expect(fetchButton(page)).toHaveAttribute("aria-busy", "true");
    await page.clock.runFor(1_000);
    await expect(member(page, FIRST)).toBeVisible();
    await expect(fetchButton(page)).not.toHaveAttribute("aria-busy", "true");
  });

  test("a person without deployment_log.read sees what to ask for and reads nothing", async ({
    page,
  }) => {
    const logs = await platform(page, {
      capabilities: ["run.read", "deployment.read"],
    });
    await openContainers(page);
    await expect(page.locator(".container-logs .logs-access")).toHaveText(
      "You need deployment_log.read in local. Ask an administrator in Settings › People and access.",
    );
    await expect(page.getByLabel("Target")).toHaveCount(0);
    expect(logs.creates).toHaveLength(0);
    await shot(page, "container-logs-no-access-1440");
  });

  test("a refused fetch shows the platform's words and keeps the form", async ({
    page,
  }) => {
    const logs = await platform(page);
    logs.created = {
      status: 429,
      json: {
        code: "WV-LOG-RATE",
        message:
          "This target takes at most 10 log requests a minute; try again shortly",
      },
    };
    await openContainers(page);
    await fetchButton(page).click();
    await expect(page.getByRole("alert")).toContainText(
      "This target takes at most 10 log requests a minute; try again shortly",
    );
    await expect(page.getByLabel("Target")).toBeVisible();
  });

  test("a very long line wraps inside its member and the page never scrolls sideways", async ({
    page,
  }) => {
    const long =
      'Traceback (most recent call last): File "/app/weave_worker/handlers/crm.py", line 214, in lookup_customer raise ' +
      'UpstreamError(f"crm answered {status} for {path}") weave_worker.errors.UpstreamError: crm answered 550 for ' +
      "/v2/customers/c-1042/notify?channel=email&retry=3&trace=[redacted]" +
      "x".repeat(600);
    const logs = await platform(page);
    logs.reads = [snapshot("ready", {}, { [FIRST]: [[NOW, "stderr", long]] })];
    for (const width of [1440, 360]) {
      await page.setViewportSize({ width, height: 900 });
      await openContainers(page);
      await fetchButton(page).click();
      await page.clock.runFor(1_000);
      await expect(member(page, FIRST).locator(".line .text")).toHaveText(long);
      expect(await noSideways(page)).toBe(true);
    }
  });
});

// Every state at the sizes the design was drawn and reviewed at, for the UX critique; each also checks nothing
// scrolls sideways.
const sizes = [
  { width: 1440, height: 900 },
  { width: 1920, height: 1080 },
  { width: 768, height: 1024 },
  { width: 360, height: 740 },
];
for (const size of sizes)
  test.describe(`Container logs states at ${size.width}×${size.height}`, () => {
    test.use({ viewport: size });
    const name = (state: string) =>
      `states-${size.width}x${size.height}-${state}`;

    test("snapshot, waiting, failed and expired", async ({ page }) => {
      const logs = await platform(page);
      logs.reads = [snapshot("pending"), snapshot("ready")];
      await openContainers(page);
      await shot(page, name("1-ready-to-fetch"));
      await fetchButton(page).click();
      await expect(fetchButton(page)).toHaveAttribute("aria-busy", "true");
      await shot(page, name("2-waiting"));
      await page.clock.runFor(2_000);
      await expect(member(page, SECOND)).toBeVisible();
      expect(await noSideways(page)).toBe(true);
      await shot(page, name("3-snapshot"));
      logs.reads = [snapshot("failed", { failure: "unreadable" })];
      await fetchButton(page).click();
      await page.clock.runFor(1_000);
      await expect(page.getByRole("alert")).toBeVisible();
      await shot(page, name("4-failed"));
      logs.reads = [snapshot("ready")];
      await fetchButton(page).click();
      await page.clock.runFor(1_000);
      await expect(member(page, FIRST)).toBeVisible();
      await page.clock.fastForward(3_600_000);
      await page.getByLabel("Since").selectOption("3600");
      await expect(
        page.getByText("This snapshot expired after 1 hour."),
      ).toBeVisible();
      await shot(page, name("5-expired"));
    });

    test("runner offline, no read_logs and no access", async ({ page }) => {
      await platform(page, { lastSeen: "2026-11-19T10:52:01Z" });
      await openContainers(page);
      await expect(page.locator(".container-logs .notice")).toBeVisible();
      expect(await noSideways(page)).toBe(true);
      await shot(page, name("6-runner-offline"));
    });

    test("a target whose runner can't fetch logs", async ({ page }) => {
      await platform(page, { targetCapabilities: ["observe"] });
      await openContainers(page);
      await expect(page.locator(".container-logs .notice")).toBeVisible();
      await shot(page, name("7-no-read-logs"));
    });

    test("no deployment_log.read", async ({ page }) => {
      await platform(page, { capabilities: ["run.read", "deployment.read"] });
      await openContainers(page);
      await expect(page.locator(".container-logs .logs-access")).toBeVisible();
      await shot(page, name("8-no-access"));
    });
  });
