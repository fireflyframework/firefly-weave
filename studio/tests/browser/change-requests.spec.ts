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
// Clusters › Change requests: the wizard, a change's progress from checking
// the target to done, the approvals inbox built from change requests, and
// the states and alignment of both tabs at desktop, tablet and phone sizes.
import { mkdirSync } from "node:fs";
import { test, expect, type Page, type Route } from "@playwright/test";
import { connected } from "./support";

const scope = {
  tenant_id: "tenant",
  project_id: "project",
  environment_id: "development",
};
const targetId = "11111111-1111-4111-8111-111111111111";
const deploymentId = "22222222-2222-4222-8222-222222222222";
const releaseId = "33333333-3333-4333-8333-333333333333";
const observationId = "55555555-5555-4555-8555-555555555555";
const ana = "aaaaaaaa-0000-4000-8000-00000000000a";
const workerIds = [
  "77777777-7777-4777-8777-777777777771",
  "77777777-7777-4777-8777-777777777772",
];
const iso = (seconds: number) =>
  new Date(Date.now() + seconds * 1000).toISOString();
const full = [
  "deployment.read",
  "deployment.plan",
  "deployment.approve",
  "deployment.apply",
  "worker.read",
  "worker.drain",
  "target.manage",
];
/** Where the owner's screenshots go; kept out of the repository. */
const shots = process.env.O3_SCREENSHOTS ?? "";

type Change = Record<string, unknown> & {
  id: string;
  state: string;
  revision: number;
};

function changeRecord(overrides: Partial<Change> = {}): Change {
  const id = overrides.id ?? "c0000000-0000-4000-8000-000000000001";
  return {
    id,
    scope,
    target_id: targetId,
    deployment_id: deploymentId,
    expected_deployment_revision: 3,
    intent: "scale_workers",
    components: ["fixture-worker"],
    desired: [{ name: "fixture-worker", replicas: 3 }],
    drain: { mode: "none", timeout_seconds: 300 },
    title: "Scale fixture-worker to 3",
    reason: "",
    plan_ttl_seconds: null,
    revision: 1,
    requested_by: ana,
    created_at: iso(-120),
    deployment_revision: 3,
    approval_policy: { distinct_approver: true },
    state: "awaiting_approval",
    observation_job_id: null,
    observation_id: observationId,
    plan_id: "p" + id.slice(1),
    plan_digest: id.slice(-1).repeat(64),
    plan_expires_at: iso(540),
    approval: null,
    planned_by: ana,
    drain_set: [],
    drained: [],
    apply_job_id: null,
    failure: null,
    warnings: [],
    history: [
      { at: iso(-120), state: "observing", actor_id: ana },
      { at: iso(-90), state: "ready_to_plan", actor_id: null },
      { at: iso(-60), state: "awaiting_approval", actor_id: ana },
    ],
    ...overrides,
  } as Change;
}

function planOf(change: Change) {
  return {
    id: change.plan_id,
    scope,
    target_id: targetId,
    target_revision: 1,
    deployment_id: deploymentId,
    deployment_revision: 3,
    adapter: "docker-compose",
    adapter_version: "1",
    intent: change.intent,
    observation_id: observationId,
    observation_digest: "b".repeat(64),
    steps: [],
    risks:
      (change.drain as { mode: string }).mode === "none"
        ? []
        : ["worker_drain"],
    created_at: iso(-60),
    expires_at: change.plan_expires_at,
    digest: change.plan_digest,
  };
}

interface Platform {
  changes: Map<string, Change>;
  calls: {
    path: string;
    method: string;
    body: unknown;
    headers: Record<string, string>;
  }[];
  /** Answers one command; return undefined to use the default. */
  commands: Partial<Record<string, (change: Change, body: unknown) => unknown>>;
  workerLeases: number[];
  listDelay: number;
  listStatus: number;
  targetsStatus: number;
  observed: number;
}

/** A platform with one Compose target, one worker deployment and its observation. */
async function platform(
  page: Page,
  capabilities: string[],
  changes: Change[] = [],
  options: { targetPolicy?: boolean; observed?: number } = {},
): Promise<Platform> {
  await connected(page, { capabilities });
  const state: Platform = {
    changes: new Map(changes.map((item) => [item.id, item])),
    calls: [],
    commands: {},
    workerLeases: [2, 0],
    listDelay: 0,
    listStatus: 200,
    targetsStatus: 200,
    observed: options.observed ?? 0,
  };
  const target = {
    id: targetId,
    name: "local-docker",
    adapter: "docker-compose",
    external_identity: "docker",
    boundary: "workers",
    runner_principal_id: "44444444-4444-4444-8444-444444444444",
    capabilities: [
      "observe",
      "scale_workers",
      "update",
      "restart_workers",
      "deploy",
    ],
    scope,
    revision: 1,
    disabled: false,
    created_at: "2026-10-03T10:00:00Z",
    ...(options.targetPolicy === undefined
      ? {}
      : { approval_policy: { distinct_approver: options.targetPolicy } }),
  };
  await page.route("**/deployment-targets?*", (r) =>
    state.targetsStatus === 200
      ? r.fulfill({ json: { items: [target], next_cursor: null } })
      : r.fulfill({
          status: state.targetsStatus,
          json: { code: "WV-UNAVAILABLE", message: "Targets are unavailable." },
        }),
  );
  await page.route(`**/deployment-targets/${targetId}`, (r) =>
    r.fulfill({ json: target }),
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
            components: [
              {
                name: "fixture-worker",
                kind: "worker",
                image: "registry.local/fixture@sha256:" + "c".repeat(64),
                configuration: "d".repeat(64),
                replicas: 1,
                cpu_millis: 500,
                memory_mib: 512,
                worker_release_id: releaseId,
              },
            ],
            scope,
            revision: 3,
            created_at: "2026-10-03T10:00:00Z",
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/deployment-observations?*", (r) => {
    const members = Array.from({ length: state.observed }, (_, index) => ({
      host: `w-${index + 1}`,
      ordinal: index + 1,
      state: "running",
      ready: true,
      started_at: iso(-3600),
      restarts: 0,
      node: null,
    }));
    return r.fulfill({
      json: {
        items: [
          {
            id: observationId,
            target_id: targetId,
            target_revision: 1,
            observed_at: iso(-90),
            expires_at: iso(900),
            digest: "b".repeat(64),
            complete: true,
            settled: true,
            resources: [
              {
                name: "fixture-worker",
                external_identity: "weave-o3-fixture-worker",
                kind: "worker",
                image: null,
                replicas: state.observed,
                ready_replicas: state.observed,
                version: "1",
                ownership: "managed",
                state: "ready",
                ...(members.length ? { members } : {}),
              },
            ],
          },
        ],
        next_cursor: null,
      },
    });
  });
  const worker = (index: number) => ({
    id: workerIds[index],
    principal_id: "88888888-8888-4888-8888-888888888888",
    release_id: releaseId,
    task_types: ["fixture.sleep"],
    capacity: 4,
    revoked: false,
    revision: 1,
    draining:
      state.changes.size > 0 &&
      [...state.changes.values()].some((c) =>
        (c.drained as string[]).includes(workerIds[index]),
      ),
    presence: "recent",
    last_seen_at: iso(-5),
    presence_expires_at: iso(55),
    observed_at: iso(-5),
    active_leases: state.workerLeases[index],
    available_capacity: 2,
    identity: { host: `w-${3 - index}` },
  });
  await page.route("**/workers?*", (r) =>
    r.fulfill({ json: { items: [worker(0), worker(1)], next_cursor: null } }),
  );
  await page.route("**/workers/*", (r) => {
    const id = new URL(r.request().url()).pathname.split("/").at(-1)!;
    const index = workerIds.indexOf(id);
    return index < 0
      ? r.fulfill({
          status: 404,
          json: { code: "WV-NOT-FOUND", message: "Gone" },
        })
      : r.fulfill({ json: worker(index) });
  });
  await page.route("**/deployment-plans?*", (r) =>
    r.fulfill({
      json: {
        items: [...state.changes.values()].map(planOf),
        next_cursor: null,
      },
    }),
  );
  await page.route("**/deployment-plans/*/approval", (r) =>
    r.fulfill({ json: null }),
  );
  await page.route(/\/deployment-change-requests(\/|\?|$)/, (r) =>
    answer(r, state),
  );
  return state;
}

const DEFAULTS: Record<
  string,
  (change: Change, body: unknown) => Partial<Change>
> = {
  advance: (change) => ({
    state: "awaiting_approval",
    plan_id: "p" + change.id.slice(1),
    plan_digest: change.id.slice(-1).repeat(64),
    plan_expires_at: iso(540),
    planned_by: "human",
  }),
  approve: () => ({
    state: "approved",
    approval: { principal_id: "human", approved_at: iso(0) },
  }),
  drain: (change) => ({
    state: "draining",
    drained: (change.drain_set as { worker_id: string }[]).map(
      (m) => m.worker_id,
    ),
  }),
  apply: () => ({
    state: "applying",
    apply_job_id: "99999999-9999-4999-8999-999999999999",
  }),
  replan: () => ({
    state: "observing",
    plan_id: null,
    plan_digest: null,
    plan_expires_at: null,
    failure: null,
  }),
  decline: () => ({ state: "declined" }),
  cancel: () => ({ state: "cancelled" }),
};

async function answer(route: Route, state: Platform) {
  const request = route.request();
  const url = new URL(request.url());
  const parts = url.pathname.split("/");
  const at = parts.indexOf("deployment-change-requests");
  const id = parts[at + 1];
  const command = parts[at + 2];
  const body = request.postDataJSON?.() ?? null;
  state.calls.push({
    path: url.pathname + url.search,
    method: request.method(),
    body,
    headers: request.headers(),
  });
  if (!id && request.method() === "GET") {
    if (state.listDelay)
      await new Promise((done) => setTimeout(done, state.listDelay));
    if (state.listStatus !== 200)
      return route.fulfill({
        status: state.listStatus,
        json: {
          code: state.listStatus === 403 ? "WV-FORBIDDEN" : "WV-UNAVAILABLE",
          message:
            state.listStatus === 403
              ? "Not allowed."
              : "Change requests are unavailable.",
        },
      });
    const states = url.searchParams.getAll("state");
    const items = [...state.changes.values()].filter(
      (item) =>
        !states.length ||
        states.includes(
          item.state === "awaiting_approval" &&
            Date.parse(String(item.plan_expires_at)) <= Date.now()
            ? "expired"
            : item.state,
        ),
    );
    return route.fulfill({ json: { items, next_cursor: null } });
  }
  if (!id) {
    const created = changeRecord({
      ...(body as object),
      id: "c0000000-0000-4000-8000-0000000000c1",
      requested_by: "human",
      planned_by: null,
      state: "observing",
      plan_id: null,
      plan_digest: null,
      plan_expires_at: null,
      observation_id: null,
      history: [{ at: iso(0), state: "observing", actor_id: "human" }],
    });
    state.changes.set(created.id, created);
    return route.fulfill({ status: 201, json: created });
  }
  const change = state.changes.get(id);
  if (!change)
    return route.fulfill({
      status: 404,
      json: { code: "WV-NOT-FOUND", message: "No such change request." },
    });
  if (!command) return route.fulfill({ json: change });
  const custom = state.commands[command]?.(change, body);
  if (custom && typeof custom === "object" && "status" in custom)
    return route.fulfill(custom as { status: number; json: unknown });
  const next = {
    ...change,
    ...((custom as Partial<Change> | undefined) ??
      DEFAULTS[command](change, body)),
    revision: change.revision + 1,
  } as Change;
  next.history = [
    ...(change.history as object[]),
    { at: iso(0), state: next.state, actor_id: "human" },
  ];
  state.changes.set(id, next);
  if (command === "drain")
    return route.fulfill({
      json: {
        change_request: next,
        members: (next.drain_set as { worker_id: string; host: string }[]).map(
          (member, index) => ({
            ...member,
            active_leases: state.workerLeases[index],
            draining: true,
            presence: "recent",
            revoked: false,
          }),
        ),
        remaining: state.workerLeases.filter(Boolean).length,
      },
    });
  return route.fulfill({ status: command === "apply" ? 201 : 200, json: next });
}

/** Moves a stored change request, as the platform would between reads. */
function move(state: Platform, id: string, changes: Partial<Change>) {
  const current = state.changes.get(id)!;
  state.changes.set(id, {
    ...current,
    ...changes,
    revision: current.revision + 1,
  } as Change);
}

/** Every element in `selector` that hides part of its text. */
async function clipped(page: Page, selector: string) {
  return page.evaluate((selector) => {
    const found: string[] = [];
    for (const element of document.querySelectorAll<HTMLElement>(selector)) {
      if (!element.offsetParent) continue;
      if (element.scrollWidth > element.clientWidth + 1)
        found.push(element.textContent?.trim().slice(0, 40) ?? "");
    }
    return found;
  }, selector);
}

/** The page never scrolls sideways. */
async function noSideScroll(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

test.describe("the change request wizard", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("scales from 0 to 3, waits for a second approver, applies and finishes", async ({
    page,
  }) => {
    const state = await platform(page, full);
    await page.goto("/operate/clusters");
    const card = page
      .locator(".resource-cards li")
      .filter({ hasText: "local-docker" });
    await expect(card).toContainText("Approvals: a second person must approve");
    await card.getByRole("button", { name: "New change request" }).click();
    await expect(
      page.getByRole("heading", { name: "New change request" }),
    ).toBeVisible();
    const wizard = page.locator("weave-change-request-wizard");
    await expect(wizard.getByLabel("Target")).toHaveValue(targetId);
    await expect(wizard.getByRole("button", { name: "Next" })).toBeDisabled();
    await wizard.getByLabel("Scale workers").check();
    await expect(wizard.getByLabel("Deployment")).toHaveValue(deploymentId);
    await expect(
      wizard.getByRole("checkbox", { name: "fixture-worker" }),
    ).toBeChecked();
    // Only Compose targets offer Deploy, and only what the target permits shows.
    await expect(wizard.getByLabel("Deploy (Compose only)")).toBeEnabled();
    await wizard.getByRole("button", { name: "Next" }).click();
    await expect(
      wizard.getByRole("spinbutton", { name: "Workers" }),
    ).toHaveValue("0");
    for (let i = 0; i < 3; i++)
      await wizard.getByRole("button", { name: "One worker more" }).click();
    await expect(
      wizard.getByRole("spinbutton", { name: "Workers" }),
    ).toHaveValue("3");
    await expect(wizard).toContainText(
      "The runner's local limits may be lower.",
    );
    await expect(wizard.locator(".drain-preview")).toHaveCount(0);
    if (shots) {
      mkdirSync(shots, { recursive: true });
      await page.screenshot({ path: `${shots}/wizard-details-1440x900.png` });
    }
    await wizard.getByRole("button", { name: "Next" }).click();
    await expect(wizard.getByLabel("Title")).toHaveValue(
      "Scale fixture-worker to 3",
    );
    await expect(wizard).toContainText("Now 0 running · 3 after the change");
    await expect(wizard).toContainText("10 minutes.");
    if (shots)
      await page.screenshot({ path: `${shots}/wizard-review-1440x900.png` });
    await wizard.getByRole("button", { name: "Create change request" }).click();

    const created = state.calls.find(
      (call) => call.method === "POST" && /change-requests$/.test(call.path),
    )!;
    expect(created.body).toMatchObject({
      target_id: targetId,
      deployment_id: deploymentId,
      expected_deployment_revision: 3,
      intent: "scale_workers",
      components: ["fixture-worker"],
      desired: [{ name: "fixture-worker", replicas: 3 }],
      drain: { mode: "none", timeout_seconds: 300 },
      title: "Scale fixture-worker to 3",
    });
    expect(created.headers["idempotency-key"]).toMatch(/^[0-9a-f-]{36}$/);
    await expect(page).toHaveURL(
      /\/operate\/clusters\/change-requests\/c0000000-/,
    );
    const progress = page.locator("weave-change-request-progress");
    await expect(progress.locator(".change-summary .status-pill")).toHaveText(
      "Checking the target",
    );

    // The runner finished observing: the page makes the plan with the revision it read.
    const id = "c0000000-0000-4000-8000-0000000000c1";
    move(state, id, { state: "ready_to_plan", observation_id: observationId });
    await expect(progress.locator(".change-summary .status-pill")).toHaveText(
      "Waiting for approval",
      { timeout: 5000 },
    );
    await expect
      .poll(
        () =>
          state.calls.find((call) => call.path.endsWith("/advance"))?.headers[
            "if-match"
          ],
      )
      .toBe('"2"');
    // The person who asked can't approve it.
    await expect(
      progress.getByRole("button", { name: "Approve plan" }),
    ).toBeDisabled();
    await expect(progress).toContainText(
      "You requested or planned this change. Another approver must review it.",
    );

    // A second person approves it.
    move(state, id, {
      state: "approved",
      approval: { principal_id: ana, approved_at: iso(0) },
    });
    await expect(progress).toContainText(
      `Approved by Person aaaaaaaa — ready to apply`,
      { timeout: 5000 },
    );
    await progress.getByRole("button", { name: "Apply plan" }).click();
    await expect
      .poll(
        () => state.calls.find((call) => call.path.endsWith("/apply"))?.body,
      )
      .toEqual({ digest: "1".repeat(64) });
    await expect(progress.locator(".change-summary .status-pill")).toHaveText(
      "Applying",
    );
    move(state, id, { state: "succeeded" });
    await expect(progress.locator(".change-summary .status-pill")).toHaveText(
      "Done",
      { timeout: 5000 },
    );
    await expect(progress.getByRole("button")).toHaveCount(0);
  });

  test("scaling 3 to 1 lets two workers finish, waits for them, then applies", async ({
    page,
  }) => {
    const id = "c0000000-0000-4000-8000-0000000000d1";
    const state = await platform(
      page,
      full,
      [
        changeRecord({
          id,
          title: "Scale fixture-worker to 1",
          desired: [{ name: "fixture-worker", replicas: 1 }],
          drain: { mode: "drain_first", timeout_seconds: 300 },
          state: "approved",
          approval: { principal_id: ana, approved_at: iso(-10) },
          requested_by: "human",
          plan_expires_at: iso(840),
          drain_set: [
            { worker_id: workerIds[0], host: "w-3", active_leases: 2 },
            { worker_id: workerIds[1], host: "w-2", active_leases: 0 },
          ],
        }),
      ],
      { observed: 3 },
    );
    await page.goto(`/operate/clusters/change-requests/${id}`);
    const progress = page.locator("weave-change-request-progress");
    await expect(progress.locator(".change-summary .status-pill")).toHaveText(
      "Approved",
    );
    await progress.getByRole("button", { name: "Let workers finish" }).click();
    await expect(progress.locator(".change-summary .status-pill")).toHaveText(
      "Letting workers finish",
    );
    const drain = progress.locator(".drain-progress");
    await expect(drain).toContainText("w-3");
    await expect(drain).toContainText("2 tasks left");
    await expect(drain).toContainText("Finished");
    state.commands.apply = () => ({
      status: 409,
      json: {
        code: "WV-DRAIN-PENDING",
        message: "Workers are still finishing their tasks.",
      },
    });
    await progress.getByRole("button", { name: "Apply plan" }).click();
    await expect(page.getByRole("alert")).toContainText(
      "Workers are still finishing their tasks.",
    );
    await expect(page.getByRole("alert")).toContainText("WV-DRAIN-PENDING");
    state.workerLeases = [0, 0];
    await expect(drain).not.toContainText("tasks left", { timeout: 5000 });
    state.commands.apply = undefined;
    await progress.getByRole("button", { name: "Apply plan" }).click();
    await expect(progress.locator(".change-summary .status-pill")).toHaveText(
      "Applying",
    );
    move(state, id, { state: "succeeded" });
    await expect(progress.locator(".change-summary .status-pill")).toHaveText(
      "Done",
      { timeout: 5000 },
    );
  });

  test("a scale-down shows which workers finish first and how many tasks they hold", async ({
    page,
  }) => {
    await platform(page, full, [], { observed: 3 });
    await page.goto("/operate/clusters");
    await page
      .locator(".resource-cards li")
      .filter({ hasText: "local-docker" })
      .getByRole("button", { name: "New change request" })
      .click();
    const wizard = page.locator("weave-change-request-wizard");
    await wizard.getByLabel("Scale workers").check();
    await wizard.getByRole("button", { name: "Next" }).click();
    await wizard.getByRole("spinbutton", { name: "Workers" }).fill("1");
    const preview = wizard.locator(".drain-preview");
    await expect(preview.locator("li")).toHaveText([
      /w-3\s*2 tasks in progress/,
      /w-2\s*0 tasks in progress/,
    ]);
    await wizard.getByLabel("Time to finish (minutes)").fill("10");
    await wizard.getByRole("button", { name: "Next" }).click();
    await wizard.getByLabel("Plan lifetime (minutes)").fill("12");
    await expect(wizard.getByRole("status")).toHaveText(
      "The plan must live at least 13 minutes to let the workers finish and apply.",
    );
    await expect(
      wizard.getByRole("button", { name: "Create change request" }),
    ).toBeDisabled();
  });

  test("a change that ran out of time to let workers finish offers a new plan", async ({
    page,
  }) => {
    const id = "c0000000-0000-4000-8000-0000000000e1";
    const state = await platform(page, full, [
      changeRecord({
        id,
        state: "failed",
        requested_by: "human",
        drain: { mode: "drain_first", timeout_seconds: 300 },
        failure: {
          code: "WV-DRAIN-WINDOW",
          message:
            "The observation ends before the workers could drain and apply; observe again",
        },
      }),
    ]);
    await page.goto(`/operate/clusters/change-requests/${id}`);
    const progress = page.locator("weave-change-request-progress");
    await expect(progress.locator(".change-summary .status-pill")).toHaveText(
      "Failed",
    );
    await progress
      .getByRole("button", { name: "Re-observe and re-plan" })
      .click();
    const replan = state.calls.find((call) => call.path.endsWith("/replan"))!;
    expect(replan.headers["if-match"]).toBe('"1"');
    await expect(progress.locator(".change-summary .status-pill")).toHaveText(
      "Checking the target",
    );
  });
});

test.describe("approvals from change requests", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("lists changes the soonest to approve first, with plain plans beside them", async ({
    page,
  }) => {
    const draining = changeRecord({
      id: "c0000000-0000-4000-8000-000000000002",
      title: "Scale fixture-worker to 1",
      drain: { mode: "drain_first", timeout_seconds: 300 },
      plan_expires_at: iso(840),
    });
    await platform(page, full, [
      changeRecord({
        title: "Scale fixture-worker to 3",
        plan_expires_at: iso(900),
      }),
      draining,
    ]);
    await page.goto("/operate/clusters?tab=approvals");
    const rows = page
      .getByRole("table", { name: "Plans waiting for an approval" })
      .locator(".resource-row");
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0)).toContainText("Scale fixture-worker to 1");
    await expect(rows.nth(0)).toContainText("Approve within");
    await expect(rows.nth(0)).toContainText("Person aaaaaaaa");
    await expect(rows.nth(0)).toContainText("Worker drain");
    await expect(rows.nth(1)).toContainText("Scale fixture-worker to 3");
    await expect(
      page.getByRole("tab", { name: "Approvals (2)" }),
    ).toBeVisible();
    // The navigation counts what this person can approve.
    await expect(page.locator(".nav-badge")).toHaveText("2", {
      timeout: 20000,
    });
    if (shots) {
      mkdirSync(shots, { recursive: true });
      await page.screenshot({ path: `${shots}/approvals-inbox-1440x900.png` });
    }
  });

  test("Approve plan confirms the plan and the row moves on", async ({
    page,
  }) => {
    const state = await platform(page, full, [changeRecord()]);
    await page.goto("/operate/clusters?tab=approvals");
    await page
      .getByRole("button", { name: "Approve plan Scale fixture-worker to 3" })
      .click();
    const dialog = page.getByRole("dialog", { name: "Approve this plan?" });
    await expect(dialog).toContainText(`plan ${"1".repeat(12)}`);
    await dialog.getByRole("button", { name: "Approve plan" }).click();
    await expect(page.getByText("Plan approved.")).toBeVisible();
    const call = state.calls.find((item) => item.path.endsWith("/approve"))!;
    expect(call.body).toEqual({ digest: "1".repeat(64) });
    const row = page
      .locator(".approval-table .resource-row")
      .filter({ hasText: "Scale fixture-worker to 3" });
    await expect(row.locator(".countdown .status-pill")).toHaveText("Approved");
  });

  test("Review opens the change, where Decline asks why", async ({ page }) => {
    const state = await platform(page, full, [changeRecord()]);
    await page.goto("/operate/clusters?tab=approvals");
    await page
      .getByRole("button", { name: "Review Scale fixture-worker to 3" })
      .click();
    await expect(page).toHaveURL(/\/change-requests\/c0000000-/);
    const progress = page.locator("weave-change-request-progress");
    await progress.getByRole("button", { name: "Decline" }).click();
    const dialog = page.getByRole("dialog", { name: "Decline this plan?" });
    await dialog.getByLabel("Reason").fill("Not during the release freeze.");
    await dialog.getByRole("button", { name: "Decline" }).click();
    await expect
      .poll(
        () => state.calls.find((item) => item.path.endsWith("/decline"))?.body,
      )
      .toEqual({ reason: "Not during the release freeze." });
    await expect(progress.locator(".change-summary .status-pill")).toHaveText(
      "Declined",
    );
  });

  test("an expired change offers Re-observe and re-plan", async ({ page }) => {
    const state = await platform(page, full, [
      changeRecord({
        title: "Scale fixture-worker to 3",
        plan_expires_at: iso(-30),
      }),
    ]);
    await page.goto("/operate/clusters?tab=approvals");
    const row = page.locator(".approval-table .resource-row");
    await expect(row.locator(".countdown .status-pill")).toHaveText("Expired");
    await expect(
      row.getByRole("button", { name: /^Approve plan/ }),
    ).toHaveCount(0);
    await row.getByRole("button", { name: "Re-observe and re-plan" }).click();
    await expect
      .poll(() => state.calls.some((call) => call.path.endsWith("/replan")))
      .toBe(true);
    await expect(page).toHaveURL(/\/change-requests\/c0000000-/);
    await expect(
      page.locator(
        "weave-change-request-progress .change-summary .status-pill",
      ),
    ).toHaveText("Checking the target");
  });

  test("the person who asked sees Approve plan turned off and why", async ({
    page,
  }) => {
    await platform(page, full, [changeRecord({ requested_by: "human" })]);
    await page.goto("/operate/clusters?tab=approvals");
    const approve = page.getByRole("button", {
      name: "Approve plan Scale fixture-worker to 3",
    });
    await expect(approve).toBeDisabled();
    await expect(approve).toHaveAttribute(
      "title",
      "You requested or planned this change. Another approver must review it.",
    );
    await expect(
      page.getByRole("tab", { name: "Approvals", exact: true }),
    ).toBeVisible();
  });

  test("the platform's refusal of a self-approval shows its reason and code", async ({
    page,
  }) => {
    // This change was requested where one person may approve, so Studio
    // offers Approve; the platform still refuses it.
    const state = await platform(
      page,
      full,
      [
        changeRecord({
          requested_by: "human",
          approval_policy: { distinct_approver: false },
        }),
      ],
      { targetPolicy: false },
    );
    state.commands.approve = () => ({
      status: 403,
      json: {
        code: "WV-APPROVAL-SELF",
        message:
          "You requested or planned this change. Another approver must review it.",
      },
    });
    await page.goto("/operate/clusters?tab=approvals");
    await page
      .getByRole("button", { name: "Approve plan Scale fixture-worker to 3" })
      .click();
    await page
      .getByRole("dialog", { name: "Approve this plan?" })
      .getByRole("button", { name: "Approve plan" })
      .click();
    const alert = page
      .getByRole("alert")
      .filter({ hasText: "Another approver" });
    await expect(alert).toContainText(
      "You requested or planned this change. Another approver must review it.",
    );
    await expect(alert).toContainText("WV-APPROVAL-SELF");
  });

  test("a new change waiting for this person brings a notice that opens it", async ({
    page,
  }) => {
    await page.clock.install();
    const state = await platform(page, full, [changeRecord()]);
    await page.goto("/operate/clusters?tab=targets");
    await expect(page.locator(".nav-badge")).toHaveText("1");
    state.changes.set(
      "c0000000-0000-4000-8000-000000000009",
      changeRecord({
        id: "c0000000-0000-4000-8000-000000000009",
        title: "Restart fixture-worker",
        intent: "restart_workers",
        plan_expires_at: iso(615),
      }),
    );
    await page.clock.fastForward(16_000);
    const notice = page.getByText(
      /^Plan ready for approval: Restart fixture-worker — expires in \d+:\d\d$/,
    );
    await expect(notice).toBeVisible();
    await expect(page.locator(".nav-badge")).toHaveText("2");
    await page.getByRole("button", { name: "Review" }).click();
    await expect(page).toHaveURL(
      /\/change-requests\/c0000000-0000-4000-8000-000000000009$/,
    );
  });
});

test.describe("who approves a target", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the target's Approvals switch saves the policy", async ({ page }) => {
    await platform(page, full, []);
    const bodies: unknown[] = [];
    await page.route(`**/deployment-targets/${targetId}`, (r) => {
      if (r.request().method() !== "PUT") return r.fallback();
      bodies.push(r.request().postDataJSON());
      return r.fulfill({
        json: {
          id: targetId,
          name: "local-docker",
          adapter: "docker-compose",
          external_identity: "docker",
          boundary: "workers",
          runner_principal_id: "44444444-4444-4444-8444-444444444444",
          capabilities: ["observe"],
          scope,
          revision: 2,
          disabled: false,
          created_at: "2026-10-03T10:00:00Z",
          approval_policy: { distinct_approver: false },
        },
      });
    });
    await page.goto(`/operate/clusters/targets/${targetId}`);
    const toggle = page.getByRole("switch", {
      name: /A second person must approve changes/,
    });
    await expect(toggle).toBeChecked();
    await toggle.click();
    await expect(
      page.getByText("Turn on for shared environments."),
    ).toBeVisible();
    expect(bodies[0]).toMatchObject({
      approval_policy: { distinct_approver: false },
    });
  });

  test("a refused policy change shows the access it needs", async ({
    page,
  }) => {
    await platform(page, full, []);
    await page.route(`**/deployment-targets/${targetId}`, (r) =>
      r.request().method() === "PUT"
        ? r.fulfill({
            status: 403,
            json: { code: "WV-FORBIDDEN", message: "Not allowed." },
          })
        : r.fallback(),
    );
    await page.goto(`/operate/clusters/targets/${targetId}`);
    await page
      .getByRole("switch", { name: /A second person must approve changes/ })
      .click();
    await expect(page.getByText("You can't change who approves")).toBeVisible();
    await expect(page.getByText("environment.manage")).toBeVisible();
  });

  test("Add target offers this computer, Kubernetes and other runtimes", async ({
    page,
  }) => {
    await platform(page, full, []);
    await page.goto("/operate/clusters");
    await page.getByRole("button", { name: "Add target" }).click();
    await expect(page.getByText("Docker on this computer")).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Register a cluster" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Register target" }),
    ).toBeHidden();
    await page.getByText("Other", { exact: true }).click();
    await page.getByRole("button", { name: "Register target" }).click();
    await expect(
      page.getByRole("heading", { name: "Register an existing target" }),
    ).toBeVisible();
  });
});

const viewports = [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
  { width: 360, height: 640 },
];
for (const viewport of viewports)
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    const desktop = viewport.width > 767;
    const rowHeight = desktop ? 36 : 44;

    for (const tab of ["Change requests", "Approvals"] as const) {
      test(`${tab}: loading, empty, error, partial and access`, async ({
        page,
      }) => {
        const state = await platform(page, full, []);
        await page.goto("/operate/clusters");
        const open = async () => {
          await page.getByRole("tab", { name: tab }).click();
        };
        // Loading
        state.listDelay = 1500;
        await open();
        await expect(
          page.locator("weave-operate-state[kind='loading']"),
        ).toBeVisible();
        await expect(
          page.getByText(
            tab === "Approvals" ? "Nothing to approve" : "No change requests",
          ),
        ).toBeVisible({ timeout: 5000 });
        state.listDelay = 0;
        await noSideScroll(page);
        // Error
        await page.getByRole("tab", { name: "Targets" }).click();
        if (tab === "Approvals") {
          await page.route("**/deployment-plans?*", (r) =>
            r.fulfill({
              status: 503,
              json: {
                code: "WV-UNAVAILABLE",
                message: "Plans are unavailable.",
              },
            }),
          );
        } else state.listStatus = 503;
        await open();
        await expect(
          page.getByText(
            tab === "Approvals"
              ? "Plans are unavailable."
              : "Change requests are unavailable.",
          ),
        ).toBeVisible();
        await expect(page.getByText("WV-UNAVAILABLE").first()).toBeVisible();
        await expect(
          page.getByRole("button", { name: "Try again" }),
        ).toBeVisible();
        // Partial
        await page.getByRole("tab", { name: "Targets" }).click();
        if (tab === "Approvals") {
          await page.unroute("**/deployment-plans?*");
          await page.route("**/deployment-plans?*", (r) =>
            r.fulfill({ json: { items: [], next_cursor: null } }),
          );
          state.listStatus = 503;
        } else {
          state.listStatus = 200;
          state.targetsStatus = 503;
        }
        await open();
        await expect(
          page.getByText(
            tab === "Approvals"
              ? /Change requests could not be read/
              : /Target names could not be loaded/,
          ),
        ).toBeVisible();
        state.targetsStatus = 200;
        // Access
        await page.getByRole("tab", { name: "Targets" }).click();
        if (tab === "Approvals") {
          await page.unroute("**/deployment-plans?*");
          await page.route("**/deployment-plans?*", (r) =>
            r.fulfill({
              status: 403,
              json: { code: "WV-FORBIDDEN", message: "Not allowed." },
            }),
          );
        } else state.listStatus = 403;
        await open();
        await expect(
          page.locator("weave-operate-state[kind='access']"),
        ).toBeVisible();
        await noSideScroll(page);
      });
    }

    test("rows line up: one height, nothing clipped, countdowns right-aligned", async ({
      page,
    }, testInfo) => {
      await platform(page, full, [
        changeRecord({
          title: "Scale fixture-worker to 3",
          plan_expires_at: iso(600),
        }),
        changeRecord({
          id: "c0000000-0000-4000-8000-000000000002",
          title: "Restart fixture-worker",
          intent: "restart_workers",
          plan_expires_at: iso(90),
        }),
        changeRecord({
          id: "c0000000-0000-4000-8000-000000000003",
          title: "Scale fixture-worker to 0",
          state: "succeeded",
        }),
      ]);
      await page.goto("/operate/clusters");
      await page.getByRole("tab", { name: "Change requests" }).click();
      const changeRows = page.locator(".change-table .resource-row");
      await expect(changeRows).toHaveCount(3);
      await changeRows.last().scrollIntoViewIfNeeded();
      await page.screenshot({
        path: testInfo.outputPath("change-requests.png"),
        fullPage: true,
      });
      const heights = await changeRows.evaluateAll((rows) =>
        rows.map((row) => Math.round(row.getBoundingClientRect().height)),
      );
      // One line per change with a mouse; a phone adds the state's own line.
      for (const height of heights)
        if (viewport.width > 600) expect(height).toBe(rowHeight);
        else {
          expect(height).toBeGreaterThanOrEqual(44);
          expect(Math.abs(height - heights[0])).toBeLessThanOrEqual(1);
        }
      expect(
        await clipped(
          page,
          ".change-table [role='cell'], .change-table .status-pill",
        ),
      ).toEqual([]);
      await noSideScroll(page);

      await page.getByRole("tab", { name: /^Approvals/ }).click();
      const rows = page.locator(".approval-table .resource-row");
      await expect(rows).toHaveCount(2);
      await rows.last().scrollIntoViewIfNeeded();
      await page.screenshot({
        path: testInfo.outputPath("approvals.png"),
        fullPage: true,
      });
      const geometry = await page.evaluate(() => {
        const head = document.querySelector<HTMLElement>(
          ".approval-table .table-head > :nth-child(3)",
        );
        return {
          heights: [
            ...document.querySelectorAll<HTMLElement>(
              ".approval-table .resource-row",
            ),
          ].map((row) => row.getBoundingClientRect().height),
          pills: [
            ...document.querySelectorAll<HTMLElement>(
              ".approval-table .countdown .status-pill",
            ),
          ].map((pill) => ({
            right: pill.getBoundingClientRect().right,
            numeric: getComputedStyle(pill).fontVariantNumeric,
          })),
          cells: [
            ...document.querySelectorAll<HTMLElement>(
              ".approval-table .countdown",
            ),
          ].map((cell) => cell.getBoundingClientRect().right),
          head:
            head && getComputedStyle(head).display !== "none"
              ? head.getBoundingClientRect().right
              : null,
          buttons: [
            ...document.querySelectorAll<HTMLElement>(
              ".approval-table .approval-actions button",
            ),
          ].map((button) => button.getBoundingClientRect().height),
        };
      });
      if (desktop && viewport.width > 1024)
        for (const height of geometry.heights)
          expect(Math.round(height)).toBe(36);
      else
        for (const height of geometry.heights)
          expect(height).toBeGreaterThanOrEqual(44);
      // Every countdown ends on the same right edge, its column's, in tabular figures.
      for (const pill of geometry.pills) {
        expect(pill.numeric).toContain("tabular-nums");
        expect(Math.abs(pill.right - geometry.cells[0])).toBeLessThanOrEqual(1);
      }
      if (geometry.head !== null)
        expect(Math.abs(geometry.head - geometry.cells[0])).toBeLessThanOrEqual(
          1,
        );
      for (const height of geometry.buttons)
        expect(Math.round(height)).toBe(desktop ? 32 : 44);
      expect(
        await clipped(
          page,
          ".approval-table [role='cell'], .approval-table button, .approval-table .status-pill",
        ),
      ).toEqual([]);
      await noSideScroll(page);
    });

    test("the wizard fits, with aligned controls at every step", async ({
      page,
    }, testInfo) => {
      await platform(page, full, [], { observed: 3 });
      await page.goto("/operate/clusters");
      await page
        .locator(".resource-cards li")
        .filter({ hasText: "local-docker" })
        .getByRole("button", { name: "New change request" })
        .click();
      const wizard = page.locator("weave-change-request-wizard");
      await wizard.getByLabel("Scale workers").check();
      const controls = async () =>
        wizard.evaluate((root) => {
          const select = [
            ...root.querySelectorAll<HTMLElement>(
              "select, input:not([type=radio]):not([type=checkbox]), .choice, .wizard-actions button",
            ),
          ]
            .filter((element) => element.offsetParent)
            .map((element) => element.getBoundingClientRect().height);
          const actions = [
            ...root.querySelectorAll<HTMLElement>(".wizard-actions button"),
          ].map((b) => b.getBoundingClientRect());
          return { select, actions };
        });
      for (let step = 0; step < 3; step++) {
        await wizard.locator(".wizard-actions").scrollIntoViewIfNeeded();
        await page.screenshot({
          path: testInfo.outputPath(`wizard-${step}.png`),
          fullPage: true,
        });
        const { select, actions } = await controls();
        for (const height of select)
          expect(Math.round(height)).toBeGreaterThanOrEqual(desktop ? 36 : 44);
        if (desktop) {
          for (const height of select)
            expect(Math.round(height)).toBeLessThanOrEqual(38);
          // The actions share one line and one top edge.
          for (const box of actions)
            expect(Math.abs(box.top - actions[0].top)).toBeLessThanOrEqual(1);
        } else
          for (const box of actions)
            expect(Math.abs(box.left - actions[0].left)).toBeLessThanOrEqual(1);
        expect(
          await clipped(
            page,
            "weave-change-request-wizard label, weave-change-request-wizard button, weave-change-request-wizard li",
          ),
        ).toEqual([]);
        await noSideScroll(page);
        if (step === 1)
          await wizard.getByRole("spinbutton", { name: "Workers" }).fill("1");
        if (step < 2)
          await wizard.getByRole("button", { name: "Next" }).click();
      }
    });
  });

test.describe("the progress view for someone who did not request the change", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a checked change is planned only when this person chooses Plan this change", async ({
    page,
  }) => {
    // Ana requested the change; this person opens it from a link while it is checked. Making its plan would make
    // them its planner, who can't approve it, so the page never does it on its own.
    const id = "c0000000-0000-4000-8000-0000000000f1";
    const state = await platform(page, full, [
      changeRecord({
        id,
        state: "ready_to_plan",
        plan_id: null,
        plan_digest: null,
        plan_expires_at: null,
        planned_by: null,
      }),
    ]);
    await page.clock.install();
    await page.goto(`/operate/clusters/change-requests/${id}`);
    const progress = page.locator("weave-change-request-progress");
    await expect(progress.locator(".change-summary .status-pill")).toHaveText(
      "Making the plan",
    );
    // Two polls pass without a plan being made.
    await page.clock.fastForward(5_000);
    await page.clock.fastForward(5_000);
    expect(
      state.calls.filter((call) => call.path.endsWith("/advance")),
    ).toEqual([]);
    const plan = progress.getByRole("button", { name: "Plan this change" });
    await expect(plan).toHaveAccessibleDescription(
      "The target was checked. Whoever makes the plan can't approve it.",
    );
    await plan.click();
    await expect
      .poll(
        () =>
          state.calls.filter((call) => call.path.endsWith("/advance")).length,
      )
      .toBe(1);
    expect(
      state.calls.find((call) => call.path.endsWith("/advance"))!.headers[
        "if-match"
      ],
    ).toBe('"1"');
    await expect(progress.locator(".change-summary .status-pill")).toHaveText(
      "Waiting for approval",
    );
    // This person made the plan: Approve plan is off, and says why to a screen reader.
    const approve = progress.getByRole("button", { name: "Approve plan" });
    await expect(approve).toBeDisabled();
    await expect(approve).toHaveAccessibleDescription(
      "You requested or planned this change. Another approver must review it.",
    );
  });

  test("a direct link names the target and the plan's two deadlines", async ({
    page,
  }) => {
    const id = "c0000000-0000-4000-8000-0000000000f2";
    await platform(page, full, [
      changeRecord({
        id,
        drain: { mode: "drain_first", timeout_seconds: 300 },
        plan_expires_at: iso(840),
      }),
    ]);
    await page.goto(`/operate/clusters/change-requests/${id}`);
    const progress = page.locator("weave-change-request-progress");
    await expect(progress.locator(".change-summary")).toContainText(
      "local-docker",
    );
    // The plan expires in 14 minutes; the workers need 5 minutes and the hand-off 2, so approve within 7.
    await expect(progress.locator(".progress-steps")).toContainText(
      /Expires in 1[34]:\d\d · Approve within [67]:\d\d/,
    );
  });

  test("before the workers finish, Let workers finish is the main action", async ({
    page,
  }) => {
    const id = "c0000000-0000-4000-8000-0000000000f3";
    await platform(page, full, [
      changeRecord({
        id,
        state: "approved",
        approval: { principal_id: ana, approved_at: iso(-5) },
        drain: { mode: "drain_first", timeout_seconds: 300 },
        drain_set: [{ worker_id: workerIds[0], host: "w-3", active_leases: 1 }],
        plan_expires_at: iso(840),
      }),
    ]);
    await page.goto(`/operate/clusters/change-requests/${id}`);
    const progress = page.locator("weave-change-request-progress");
    await expect(
      progress.getByRole("button", { name: "Let workers finish" }),
    ).toHaveClass(/primary/);
    await expect(
      progress.getByRole("button", { name: "Apply plan" }),
    ).not.toHaveClass(/primary/);
  });
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 360, height: 640 },
])
  test(`progress dots sit on their step's first line at ${viewport.width}x${viewport.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const id = "c0000000-0000-4000-8000-0000000000f4";
    await platform(page, full, [changeRecord({ id })]);
    await page.goto(`/operate/clusters/change-requests/${id}`);
    const steps = page.locator(".progress-steps li");
    await expect(steps.first()).toBeVisible();
    const offsets = await steps.evaluateAll((items) =>
      items.map((item) => {
        const dot = item.querySelector(".step-dot")!.getBoundingClientRect();
        const label = item
          .querySelector(".step-text strong")!
          .getBoundingClientRect();
        return dot.top + dot.height / 2 - (label.top + label.height / 2);
      }),
    );
    for (const offset of offsets)
      expect(Math.abs(offset)).toBeLessThanOrEqual(1);
  });

test("with icons only, the Clusters entry keeps its name beside the count", async ({
  page,
}) => {
  await page.setViewportSize({ width: 360, height: 640 });
  await platform(page, full, [changeRecord()]);
  await page.goto("/operate/clusters");
  await expect(page.locator(".nav-badge")).toHaveText("1", { timeout: 20_000 });
  await expect(
    page.getByRole("button", {
      name: "Clusters, 1 change waits for your approval",
      exact: true,
    }),
  ).toBeVisible();
});
