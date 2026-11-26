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
import { describe, expect, it, onTestFinished } from "vitest";
import { ApiError } from "../src/app/api";
import {
  dropDebug,
  handOverDebug,
  takeDebug,
} from "../src/app/editor/runs-tab/run-links";
import {
  FOLLOW_LIMIT_MS,
  OTHER_WORKFLOW,
  RunsTabState,
  type RunsTabHost,
} from "../src/app/editor/runs-tab/runs-tab-state";
import type { Step, Workflow } from "../src/app/model";
import type { PollEnvironment } from "../src/app/operate/operate-store";
import type { RunSummary, StepFact } from "../src/app/operate/run-contracts";
import {
  answerRunViews,
  fakeEnvironment,
} from "../src/app/operate/run-views-fake";
import {
  fixtureIds,
  runViewsFixture,
  type RunViewsFixture,
} from "../src/app/operate/run-views.fixture";

const PROJECT = fakeEnvironment.replace(/\/environments\/.*$/, "");
const ENVIRONMENT_ID = "00000000-0000-4000-8000-000000000003";

/** The browser's clock, timers and visibility, driven by the test. */
class FakeEnvironment implements PollEnvironment {
  time = Date.parse("2026-10-07T12:00:00Z");
  private shown = true;
  private nextHandle = 1;
  private readonly timers = new Map<number, { at: number; run: () => void }>();
  private readonly listeners = new Set<() => void>();
  now() {
    return this.time;
  }
  setTimeout(run: () => void, milliseconds: number) {
    const handle = this.nextHandle++;
    this.timers.set(handle, { at: this.time + milliseconds, run });
    return handle;
  }
  clearTimeout(handle: number) {
    this.timers.delete(handle);
  }
  visible() {
    return this.shown;
  }
  onVisibilityChange(listener: () => void) {
    this.listeners.add(listener);
    return () => void this.listeners.delete(listener);
  }
  async show(shown: boolean) {
    this.shown = shown;
    for (const listener of [...this.listeners]) listener();
    await settle();
  }
  /**
   * Runs every timer due within `milliseconds`, in order, letting answers
   * land between them. Timers that keep firing without the clock reaching
   * the end are a busy loop, and fail the test instead of hanging it.
   */
  async advance(milliseconds: number) {
    const end = this.time + milliseconds;
    for (let fired = 0; ; fired++) {
      if (fired > 10_000) throw new Error("Timers kept firing: a busy loop.");
      await settle();
      const due = [...this.timers]
        .filter(([, timer]) => timer.at <= end)
        .sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      this.timers.delete(due[0]);
      this.time = due[1].at;
      due[1].run();
    }
    this.time = end;
    await settle();
  }
}
/** Lets every answer already given land. */
const settle = async () => {
  for (let i = 0; i < 3; i++) await new Promise((done) => setImmediate(done));
};

interface Scripted {
  status: number;
  body: unknown;
  retryAfter?: number;
}
/** The platform: run views from the fake, runs.read and exports from maps, every request recorded. */
class FakePlatform {
  readonly environment = fakeEnvironment;
  readonly project = PROJECT;
  readonly log: string[] = [];
  readonly views = new Map<string, unknown>();
  readonly documents = new Map<string, Workflow>();
  /** A run's transactions page (runs.transactions), by run ID. */
  readonly transactions = new Map<string, unknown>();
  /** Requests matching this wait until released. */
  hold: ((path: string) => boolean) | null = null;
  /** An error answer for a request, or null to answer normally. */
  fail: ((path: string) => Scripted | null) | null = null;
  private held: (() => void)[] = [];
  constructor(
    readonly data: RunViewsFixture = structuredClone(runViewsFixture),
  ) {}
  request<T>(path: string): Promise<T> {
    this.log.push(path);
    return new Promise<T>((resolve, reject) => {
      const answer = () => {
        try {
          resolve(this.answer(path) as T);
        } catch (error) {
          reject(error);
        }
      };
      if (this.hold?.(path)) this.held.push(answer);
      else answer();
    });
  }
  release() {
    const held = this.held;
    this.held = [];
    for (const answer of held) answer();
  }
  count(fragment: string | RegExp) {
    return this.log.filter((path) =>
      typeof fragment === "string"
        ? path.includes(fragment)
        : fragment.test(path),
    ).length;
  }
  private answer(path: string): unknown {
    const scripted = this.fail?.(path);
    if (scripted)
      throw new ApiError(scripted.status, scripted.body, scripted.retryAfter);
    const views = answerRunViews(path, this.data);
    if (views) {
      if (views.status >= 400) throw new ApiError(views.status, views.body);
      return structuredClone(views.body);
    }
    const run = /\/runs\/([^/?]+)$/.exec(path);
    if (run && this.views.has(run[1]))
      return structuredClone(this.views.get(run[1]));
    const transactions = /\/runs\/([^/?]+)\/transactions\?limit=100$/.exec(
      path,
    );
    if (transactions && this.transactions.has(transactions[1]))
      return structuredClone(this.transactions.get(transactions[1]));
    const exported = /\/workflows\/([^/]+)\/export$/.exec(path);
    if (exported && this.documents.has(exported[1]))
      return { document: structuredClone(this.documents.get(exported[1])) };
    throw new ApiError(404, { code: "WV-NOT-FOUND", message: "Not found" });
  }
}

const summaryOf = (data: RunViewsFixture, id: string): RunSummary =>
  data.runs.find((run) => run.summary.id === id)!.summary;
/** A runs.read answer for a fixture run. */
function viewOf(
  summary: RunSummary,
  state: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: summary.id,
    activation: {
      id: summary.activation_id,
      revision: summary.activation.revision,
      name: summary.activation.name,
      request: { version_id: summary.workflow.definition_version_id },
    },
    state: {
      status: summary.status,
      active: [],
      input: { total: 120.5 },
      steps: { fetch: { output: { rows: [{ name: "Ada" }] } } },
      waits: {},
      branches: {},
      incidents: {},
      ...state,
    },
  };
}
const step = (id: string, extra: Partial<Step> = {}): Step => ({
  id,
  kind: "action",
  action: "crm.lookup@2.0.0",
  ...extra,
});
function workflow(version: string, send: Partial<Step>): Workflow {
  return {
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "invoice-approval", version },
    spec: { steps: [step("fetch"), step("send", send)] },
  };
}

interface Setup {
  env: FakeEnvironment;
  platform: FakePlatform;
  host: RunsTabHost & {
    setScope(scope: string): void;
    setWorkflow(name: string): void;
  };
  state: RunsTabState;
}
function setup(data?: RunViewsFixture, canRead = true): Setup {
  const env = new FakeEnvironment();
  const platform = new FakePlatform(data);
  for (const run of platform.data.runs)
    platform.views.set(run.summary.id, viewOf(run.summary));
  let scope = "account-a|env-1";
  let name = "invoice-approval";
  const host = {
    api: platform,
    scope: () => scope,
    workflowName: () => name,
    draft: () => workflow("1.3.0", { with: { ref: "/input/other" } }),
    canRead: () => canRead,
    environmentLabel: () => "development",
    environmentId: () => ENVIRONMENT_ID,
    setScope: (next: string) => void (scope = next),
    setWorkflow: (next: string) => void (name = next),
  };
  return { env, platform, host, state: new RunsTabState(host, env) };
}
async function started(data?: RunViewsFixture): Promise<Setup> {
  const ready = setup(data);
  ready.state.start();
  await ready.env.advance(0);
  return ready;
}
const LIST = "/run-summaries";

describe("the list", () => {
  it("lists the workflow's runs newest first, then says when it was updated", async () => {
    const { state, platform, env } = await started();
    expect(state.state()).toBe("ready");
    expect(platform.log[0]).toContain(
      "/run-summaries?workflow=invoice-approval&include_test=true&order=started_desc&limit=50",
    );
    const started_at = state
      .items()
      .map((item) => ("started_at" in item ? item.started_at : ""));
    expect(started_at).toEqual([...started_at].sort().reverse());
    expect(state.rows()[0].id).toBe(state.items()[0].id);
    expect(state.updatedAt()).toBe(env.time);
  });

  it("refreshes the first page every 5 seconds while visible", async () => {
    const { platform, env, state } = await started();
    expect(platform.count(LIST)).toBe(1);
    await env.advance(5000);
    expect(platform.count(LIST)).toBe(2);
    // A run that started since leads the list on the next refresh.
    const newest = {
      summary: {
        ...summaryOf(platform.data, fixtureIds.succeeded),
        id: "9f8e7d6c-5b4a-4c3d-8e2f-1a0b9c8d7e6f",
        started_at: "2026-10-07T11:59:00Z",
      },
    };
    platform.data.runs.push(newest);
    await env.advance(5000);
    expect(platform.count(LIST)).toBe(3);
    expect(state.rows()[0].id).toBe(newest.summary.id);
    await env.show(false);
    await env.advance(60_000);
    expect(platform.count(LIST)).toBe(3);
    await env.show(true);
    expect(platform.count(LIST)).toBe(4);
  });

  it("backs off after failures and honors Retry-After", async () => {
    const { platform, env, state } = setup();
    platform.fail = (path) =>
      path.includes(LIST)
        ? { status: 500, body: { code: "WV-INTERNAL", message: "It broke." } }
        : null;
    state.start();
    await env.advance(0);
    expect(state.state()).toBe("error");
    expect(state.error()?.code).toBe("WV-INTERNAL");
    expect(platform.count(LIST)).toBe(1);
    await env.advance(9_999);
    expect(platform.count(LIST)).toBe(1);
    await env.advance(1);
    expect(platform.count(LIST)).toBe(2);
    await env.advance(19_999);
    expect(platform.count(LIST)).toBe(2);
    await env.advance(1);
    expect(platform.count(LIST)).toBe(3);
    // Asked to wait 30 seconds, it asks again after 30 seconds, not before.
    platform.fail = (path) =>
      path.includes(LIST)
        ? {
            status: 429,
            body: { code: "WV-REQUEST-CAPACITY", message: "Busy." },
            retryAfter: 30,
          }
        : null;
    await env.advance(40_000);
    const at = platform.count(LIST);
    platform.fail = null;
    await env.advance(29_999);
    expect(platform.count(LIST)).toBe(at);
    await env.advance(1);
    expect(platform.count(LIST)).toBe(at + 1);
    expect(state.state()).toBe("ready");
    expect(state.error()).toBeNull();
  });

  it("filters change the query, and Show older runs reads the next page", async () => {
    const data = structuredClone(runViewsFixture);
    const base = summaryOf(data, fixtureIds.succeeded);
    data.runs = Array.from({ length: 120 }, (_, index) => ({
      summary: {
        ...base,
        id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
        started_at: new Date(
          Date.parse("2026-10-07T11:00:00Z") - index * 60_000,
        ).toISOString(),
      },
    }));
    const { state, platform, env } = await started(data);
    expect(state.items()).toHaveLength(50);
    expect(state.older()).toBe("more");
    await state.more();
    expect(platform.log.at(-1)).toMatch(/cursor=/);
    expect(state.items()).toHaveLength(100);
    // A refresh keeps the older page below the first one.
    await env.advance(5000);
    expect(state.items()).toHaveLength(100);
    await state.more();
    expect(state.items()).toHaveLength(120);
    expect(state.older()).toBe("none");
    state.setFilters({
      status: ["failed"],
      version: null,
      origin: null,
      includeTest: true,
    });
    await env.advance(0);
    expect(platform.log.at(-1)).toContain("&status=failed");
    expect(state.state()).toBe("empty");
    expect(state.items()).toEqual([]);
  });

  it("keeps the versions its runs used while a filter narrows the list", async () => {
    const data = structuredClone(runViewsFixture);
    const older = summaryOf(data, fixtureIds.succeeded);
    older.workflow = { ...older.workflow, version: "1.2.0" };
    const { state, env } = await started(data);
    // Test runs and runs the platform can't show name no version to filter by.
    expect([...state.usedVersions()].sort()).toEqual(["1.2.0", "1.3.0"]);
    state.setFilters({
      status: ["failed"],
      version: null,
      origin: null,
      includeTest: true,
    });
    await env.advance(0);
    expect(state.rows().map((row) => [row.label, row.version])).toEqual([
      ["Failed", "1.3.0"],
    ]);
    expect([...state.usedVersions()].sort()).toEqual(["1.2.0", "1.3.0"]);
  });

  it("forgets the versions another workflow's runs used", async () => {
    const { state, host, env } = await started();
    expect(state.usedVersions()).toEqual(["1.3.0"]);
    host.setWorkflow("notify-customer");
    await env.advance(5000);
    expect(state.usedVersions()).toEqual(["2.0.0"]);
  });

  it("stops at 500 runs and says so", async () => {
    const data = structuredClone(runViewsFixture);
    const base = summaryOf(data, fixtureIds.succeeded);
    data.runs = Array.from({ length: 520 }, (_, index) => ({
      summary: {
        ...base,
        id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
        started_at: new Date(
          Date.parse("2026-10-07T11:00:00Z") - index * 60_000,
        ).toISOString(),
      },
    }));
    const { state, platform } = await started(data);
    for (let page = 0; page < 12; page++) await state.more();
    expect(state.items()).toHaveLength(500);
    expect(platform.count(LIST)).toBe(10);
    expect(state.older()).toBe("limit");
  });

  it("stops listing on a platform that can't list runs", async () => {
    const { platform, env, state } = setup();
    platform.fail = (path) =>
      path.includes(LIST)
        ? {
            status: 501,
            body: { code: "WV-UNAVAILABLE", message: "Not served." },
          }
        : null;
    state.start();
    await env.advance(0);
    expect(state.state()).toBe("unavailable");
    await env.advance(5 * 60_000);
    expect(platform.count(LIST)).toBe(1);
  });

  it("asks nothing without run.read, and a refused list keeps nothing", async () => {
    const without = setup(undefined, false);
    without.state.start();
    await without.env.advance(10_000);
    expect(without.state.state()).toBe("access");
    expect(without.platform.log).toEqual([]);

    const { platform, env, state } = await started();
    await state.choose(fixtureIds.failed);
    platform.fail = (path) =>
      path.includes(LIST)
        ? {
            status: 403,
            body: { code: "WV-FORBIDDEN", message: "Access denied" },
          }
        : null;
    await env.advance(5000);
    expect(state.state()).toBe("access");
    expect(state.items()).toEqual([]);
    expect(state.rows()).toEqual([]);
    expect(state.chosen()).toBeNull();
  });

  it("a refused list stays refused until Refresh or another account or environment", async () => {
    const { platform, env, state, host } = await started();
    platform.fail = (path) =>
      path.includes(LIST)
        ? {
            status: 403,
            body: { code: "WV-FORBIDDEN", message: "Access denied" },
          }
        : null;
    await env.advance(5000);
    expect(state.state()).toBe("access");
    const asked = platform.count(LIST);
    // The access screen doesn't flip to loading, and Studio stops asking.
    platform.hold = (path) => path.includes(LIST);
    await env.advance(10_000);
    expect(state.state()).toBe("access");
    await env.advance(120_000);
    expect(platform.count(LIST)).toBe(asked);
    // Refresh asks again, and keeps asking once it is allowed.
    platform.hold = null;
    platform.fail = null;
    await state.refresh();
    expect(platform.count(LIST)).toBe(asked + 1);
    expect(state.state()).toBe("ready");
    await env.advance(5000);
    expect(platform.count(LIST)).toBe(asked + 2);
    // Refused again; another environment asks again on its own.
    platform.fail = (path) =>
      path.includes(LIST)
        ? {
            status: 403,
            body: { code: "WV-FORBIDDEN", message: "Access denied" },
          }
        : null;
    await env.advance(5000);
    expect(state.state()).toBe("access");
    platform.fail = null;
    host.setScope("account-a|env-2");
    state.sync();
    await settle();
    expect(platform.count(LIST)).toBe(asked + 4);
    expect(state.state()).toBe("ready");
  });

  it("a failed first load keeps saying so while it tries again", async () => {
    const { platform, env, state } = setup();
    platform.fail = (path) =>
      path.includes(LIST)
        ? { status: 500, body: { code: "WV-INTERNAL", message: "It broke." } }
        : null;
    state.start();
    await env.advance(0);
    expect(state.state()).toBe("error");
    platform.hold = (path) => path.includes(LIST);
    await env.advance(10_000);
    expect(platform.count(LIST)).toBe(2);
    expect(state.state()).toBe("error");
  });

  it("waits at most 5 minutes, whatever Retry-After asks", async () => {
    const { platform, env, state } = setup();
    platform.fail = (path) =>
      path.includes(LIST)
        ? {
            status: 429,
            body: { code: "WV-REQUEST-CAPACITY", message: "Busy." },
            retryAfter: 10 ** 12,
          }
        : null;
    state.start();
    await env.advance(0);
    platform.fail = null;
    await env.advance(5 * 60_000 - 1);
    expect(platform.count(LIST)).toBe(1);
    await env.advance(1);
    expect(platform.count(LIST)).toBe(2);
    expect(state.state()).toBe("ready");
  });

  it("waits at least the list's interval, however little Retry-After asks", async () => {
    const { platform, env, state } = setup();
    platform.fail = (path) =>
      path.includes(LIST)
        ? {
            status: 429,
            body: { code: "WV-REQUEST-CAPACITY", message: "Busy." },
            retryAfter: 0,
          }
        : null;
    state.start();
    await env.advance(0);
    await env.advance(4_999);
    expect(platform.count(LIST)).toBe(1);
    await env.advance(1);
    expect(platform.count(LIST)).toBe(2);
  });

  it("Show older runs reads each page once, however often it's pressed", async () => {
    const data = structuredClone(runViewsFixture);
    const base = summaryOf(data, fixtureIds.succeeded);
    data.runs = Array.from({ length: 120 }, (_, index) => ({
      summary: {
        ...base,
        id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
        started_at: new Date(
          Date.parse("2026-10-07T11:00:00Z") - index * 60_000,
        ).toISOString(),
      },
    }));
    const { state, platform } = await started(data);
    await Promise.all([state.more(), state.more()]);
    expect(platform.count(LIST)).toBe(2);
    expect(state.items()).toHaveLength(100);
    await state.more();
    expect(state.items()).toHaveLength(120);
  });

  it("drops a list answer that arrives after the account or environment changed", async () => {
    const { platform, env, state, host } = await started();
    const rows = state.rows().length;
    platform.hold = (path) => path.includes(LIST);
    await env.advance(5000);
    // While that page is read, the session moves to another environment.
    host.setScope("account-a|env-2");
    platform.hold = null;
    platform.release();
    await settle();
    // The late page isn't shown, and nothing of the old environment stays.
    expect(state.items()).toEqual([]);
    expect(state.rows()).toEqual([]);
    expect(state.state()).not.toBe("ready");
    // The next read is the new environment's.
    await env.advance(5000);
    expect(state.rows()).toHaveLength(rows);
    expect(state.state()).toBe("ready");
  });
});

describe("the chosen run", () => {
  it("reads the run, its version and its steps, and draws it over the draft", async () => {
    const { state, platform } = await started();
    const failed = summaryOf(platform.data, fixtureIds.failed);
    platform.documents.set(
      failed.workflow.definition_version_id,
      workflow("1.2.0", {
        with: {
          object: {
            customer: { ref: "/steps/fetch/output/rows/0/name" },
            total: { ref: "/input/total" },
          },
        },
      }),
    );
    platform.views.set(
      failed.id,
      viewOf(failed, {
        incidents: {
          i1: { node_id: "send[3]", generation: 0, code: "SMTP_REJECTED" },
        },
      }),
    );
    await state.choose(fixtureIds.failed);
    const chosen = state.chosen()!;
    expect(chosen.error).toBeNull();
    expect(chosen.record?.id).toBe(fixtureIds.failed);
    expect(chosen.following).toBe(false);
    expect(chosen.overlay?.canvas.failures).toEqual({
      send: "SMTP_REJECTED",
      items: "SMTP_REJECTED",
    });
    // The input is recomputed with the run's version, never the draft's.
    expect(chosen.data?.received("send")).toEqual({
      ok: true,
      value: { customer: "Ada", total: 120.5 },
    });
    expect(chosen.overlay?.diff?.changed).toEqual(["send"]);
    expect(chosen.data?.environmentId).toBe(ENVIRONMENT_ID);
    expect(platform.count(`/runs/${fixtureIds.failed}/steps?limit=500`)).toBe(
      1,
    );
  });

  it("says why a run can't be read, and keeps the list", async () => {
    const { state, platform } = await started();
    platform.views.delete(fixtureIds.failed);
    await state.choose(fixtureIds.failed);
    expect(state.chosen()?.error?.code).toBe("WV-NOT-FOUND");
    expect(state.rows().length).toBeGreaterThan(0);
    platform.views.set(fixtureIds.failed, { id: fixtureIds.failed, state: [] });
    await state.choose(fixtureIds.failed);
    expect(state.chosen()?.error?.code).toBe("WV-STUDIO-RUN-UNREADABLE");
    expect(state.chosen()?.record).toBeNull();
  });

  it("refuses a run of another workflow, keeping nothing of it", async () => {
    const { state, host, platform } = await started();
    const failed = summaryOf(platform.data, fixtureIds.failed);
    platform.documents.set(
      failed.workflow.definition_version_id,
      workflow("1.2.0", {}),
    );
    // A link names a run of this project; the open workflow is another one.
    host.setWorkflow("refund-approval");
    await state.choose(fixtureIds.failed);
    expect(state.chosen()?.error).toEqual({
      code: OTHER_WORKFLOW,
      message: "invoice-approval",
    });
    expect(state.chosen()?.data).toBeNull();
    expect(state.chosen()?.overlay).toBeNull();
  });

  it("going back to the list forgets a Debug request from Operate", async () => {
    onTestFinished(() => dropDebug());
    const { state } = await started();
    const fence = { scope: "3:principal-a", environment: ENVIRONMENT_ID };
    handOverDebug("d-1", fixtureIds.failed, "debug", fence);
    void state.choose(fixtureIds.failed);
    state.leave();
    expect(takeDebug("d-1", fixtureIds.failed, fence)).toBeNull();
  });

  it("draws nothing of an unlisted run whose version can't be read", async () => {
    // The platform has no export of the version the runs used.
    const { state, host } = await started();
    // A listed run is this workflow's: it still draws, over the draft.
    await state.choose(fixtureIds.failed);
    expect(state.chosen()?.error).toBeNull();
    expect(state.chosen()?.data).not.toBeNull();
    // Unlisted, nothing says whose run it is: it could match this
    // workflow's steps by ID only.
    host.setWorkflow("refund-approval");
    await state.choose(fixtureIds.failed);
    expect(state.chosen()?.error).toEqual({
      code: "WV-STUDIO-RUN-VERSION-UNREADABLE",
      message: "Studio couldn't read the version this run used.",
    });
    expect(state.chosen()?.data).toBeNull();
    expect(state.chosen()?.overlay).toBeNull();
  });

  it("draws a linked run whose version couldn't be read once the list says it is this workflow's", async () => {
    // A link chooses the run before the list answers; the platform has no
    // export of the version it used.
    const { state, platform, env } = setup();
    platform.hold = (path) => path.includes(LIST);
    state.start();
    await env.advance(0);
    await state.choose(fixtureIds.failed);
    expect(state.chosen()?.error?.code).toBe(
      "WV-STUDIO-RUN-VERSION-UNREADABLE",
    );
    platform.hold = null;
    platform.release();
    await env.advance(0);
    await env.advance(0);
    expect(state.chosen()?.id).toBe(fixtureIds.failed);
    expect(state.chosen()?.error).toBeNull();
    expect(state.chosen()?.data).not.toBeNull();
  });

  it("applies an answer only to the run, workflow, account and environment it was asked for", async () => {
    const cases: [string, (ready: Setup) => Promise<void> | void][] = [
      [
        "another run",
        ({ state, platform }) => {
          platform.hold = null;
          return state.choose(fixtureIds.retry);
        },
      ],
      ["another workflow", ({ host }) => host.setWorkflow("notify-customer")],
      ["another account", ({ host }) => host.setScope("account-b|env-1")],
      ["another environment", ({ host }) => host.setScope("account-a|env-2")],
      ["leaving the run", ({ state }) => state.leave()],
      ["closing the tab", ({ state }) => state.destroy()],
    ];
    for (const [name, act] of cases) {
      const ready = await started();
      const { state, platform } = ready;
      const rows = state.rows();
      platform.hold = (path) => path.endsWith(`/runs/${fixtureIds.failed}`);
      const pending = state.choose(fixtureIds.failed);
      await settle();
      await act(ready);
      platform.release();
      await pending;
      await settle();
      const chosen = state.chosen();
      expect(chosen?.record?.id, name).not.toBe(fixtureIds.failed);
      expect(chosen?.data ?? null, name).not.toEqual(
        expect.objectContaining({ runId: fixtureIds.failed }),
      );
      if (name === "another run") {
        expect(chosen?.id, name).toBe(fixtureIds.retry);
        expect(chosen?.record?.id, name).toBe(fixtureIds.retry);
      } else expect(chosen, name).toBeNull();
      if (name === "leaving the run" || name === "closing the tab")
        expect(state.rows(), name).toBe(rows);
      else if (name !== "another run") expect(state.rows(), name).toEqual([]);
    }
  });

  it("forgets the list and the run when the account or environment changes", async () => {
    const { state, platform, env, host } = await started();
    await state.choose(fixtureIds.failed);
    platform.hold = (path) => path.includes(LIST);
    host.setScope("account-a|env-2");
    await env.advance(5000);
    expect(state.rows()).toEqual([]);
    expect(state.chosen()).toBeNull();
    expect(platform.count(LIST)).toBe(2);
    platform.hold = null;
    platform.release();
    await settle();
    expect(state.rows().length).toBeGreaterThan(0);
  });

  it("follows a run every 2 seconds until it ends, for 30 minutes at most", async () => {
    const { state, platform, env } = await started();
    const running = summaryOf(platform.data, fixtureIds.running);
    platform.views.set(running.id, viewOf(running, { active: ["support"] }));
    const reads = () => platform.count(new RegExp(`/runs/${running.id}$`));
    await state.choose(running.id);
    expect(state.chosen()?.following).toBe(true);
    expect(reads()).toBe(1);
    await env.advance(2000);
    expect(reads()).toBe(2);
    await env.advance(2000);
    expect(reads()).toBe(3);
    platform.views.set(
      running.id,
      viewOf(
        { ...running, status: "succeeded" },
        { steps: { fetch: { output: {} }, support: { output: {} } } },
      ),
    );
    await env.advance(2000);
    expect(reads()).toBe(4);
    expect(state.chosen()?.following).toBe(false);
    expect(state.chosen()?.record?.status).toBe("succeeded");
    expect(state.chosen()?.overlay?.canvas.done).toContain("support");
    await env.advance(10_000);
    expect(reads()).toBe(4);

    // A run that doesn't end is followed for 30 minutes, then until Refresh.
    const waiting = summaryOf(platform.data, fixtureIds.test);
    const waits = () => platform.count(new RegExp(`/runs/${waiting.id}$`));
    await state.choose(waiting.id);
    expect(state.chosen()?.following).toBe(true);
    await env.advance(FOLLOW_LIMIT_MS);
    expect(state.chosen()).toMatchObject({ following: false, stopped: true });
    const after = waits();
    expect(after).toBeGreaterThan(800);
    await env.advance(60_000);
    expect(waits()).toBe(after);
    await state.refresh();
    expect(waits()).toBe(after + 1);
    expect(state.chosen()).toMatchObject({ following: true, stopped: false });
  });

  it("stops following while the page is hidden", async () => {
    const { state, platform, env } = await started();
    const running = summaryOf(platform.data, fixtureIds.running);
    const reads = () => platform.count(new RegExp(`/runs/${running.id}$`));
    await state.choose(running.id);
    await env.show(false);
    await env.advance(30_000);
    expect(reads()).toBe(1);
    await env.show(true);
    expect(reads()).toBe(2);
  });

  it("a refused read while following keeps nothing", async () => {
    const { state, platform, env } = await started();
    const running = summaryOf(platform.data, fixtureIds.running);
    await state.choose(running.id);
    expect(state.chosen()?.following).toBe(true);
    platform.fail = (path) =>
      path.endsWith(`/runs/${running.id}`)
        ? {
            status: 403,
            body: { code: "WV-FORBIDDEN", message: "Access denied" },
          }
        : null;
    await env.advance(2000);
    expect(state.state()).toBe("access");
    expect(state.chosen()).toBeNull();
    expect(state.rows()).toEqual([]);
    const reads = platform.count(new RegExp(`/runs/${running.id}$`));
    await env.advance(60_000);
    expect(platform.count(new RegExp(`/runs/${running.id}$`))).toBe(reads);
  });

  it("a refused read of the run's steps keeps nothing", async () => {
    const { platform, state } = await started();
    platform.fail = (path) =>
      path.includes(`/runs/${fixtureIds.failed}/steps`)
        ? {
            status: 403,
            body: { code: "WV-FORBIDDEN", message: "Access denied" },
          }
        : null;
    await state.choose(fixtureIds.failed);
    expect(state.state()).toBe("access");
    expect(state.chosen()).toBeNull();
    expect(state.rows()).toEqual([]);
  });

  it("other failures to read the steps still draw the run from its state", async () => {
    const { platform, state } = await started();
    platform.fail = (path) =>
      path.includes(`/runs/${fixtureIds.failed}/steps`)
        ? { status: 500, body: { code: "WV-INTERNAL", message: "It broke." } }
        : null;
    await state.choose(fixtureIds.failed);
    expect(state.state()).toBe("ready");
    expect(state.chosen()?.record?.id).toBe(fixtureIds.failed);
    expect(state.chosen()?.overlay?.canvas.done).toEqual(["fetch"]);
  });

  it("drops a followed run's answer that arrives after the run was left", async () => {
    // The late answer is the run's state, or a page of its steps read after it.
    for (const late of ["", "/steps"]) {
      const { state, platform, env } = await started();
      const running = summaryOf(platform.data, fixtureIds.running);
      await state.choose(running.id);
      const data = state.chosen()!.data!;
      const stamp = data.version();
      platform.hold = (path) =>
        path.replace(/\?.*$/, "").endsWith(`/runs/${running.id}${late}`);
      await env.advance(2000);
      state.leave();
      // Another run is shown before the late answer arrives.
      platform.hold = null;
      await state.choose(fixtureIds.failed);
      const shown = state.chosen()!;
      platform.views.set(
        running.id,
        viewOf(
          { ...running, status: "succeeded" },
          { steps: { support: { output: {} } } },
        ),
      );
      platform.release();
      await settle();
      expect(state.chosen(), late).toBe(shown);
      // None of the left run's step instances reach the run shown now.
      expect(shown.data?.iterations("support"), late).toEqual([]);
      // The data shown before isn't updated with the late answer.
      expect(data.version(), late).toBe(stamp);
      // And the run left isn't read again.
      const reads = platform.count(new RegExp(`/runs/${running.id}$`));
      await env.advance(10_000);
      expect(platform.count(new RegExp(`/runs/${running.id}$`)), late).toBe(
        reads,
      );
    }
  });

  it("drops a run whose version arrives after the environment changed", async () => {
    const { state, platform, host } = await started();
    const failed = summaryOf(platform.data, fixtureIds.failed);
    platform.documents.set(
      failed.workflow.definition_version_id,
      workflow("1.3.0", {}),
    );
    platform.hold = (path) => path.endsWith("/export");
    const pending = state.choose(failed.id);
    await settle();
    expect(platform.count("/export")).toBe(1);
    host.setScope("account-a|env-2");
    platform.hold = null;
    platform.release();
    await pending;
    expect(state.chosen()).toBeNull();
    expect(state.rows()).toEqual([]);
    // Chosen in the new environment, its version is read there again.
    await state.choose(failed.id);
    expect(state.chosen()?.record?.id).toBe(failed.id);
    expect(platform.count("/export")).toBe(2);
  });

  it("reads at most 2,000 step instances", async () => {
    const data = structuredClone(runViewsFixture);
    const facts: StepFact[] = Array.from({ length: 2500 }, (_, index) => ({
      ...data.steps[fixtureIds.failed].items[3],
      instance_key: `send[${index}]`,
      iteration: [index],
      status: "succeeded",
    }));
    data.steps[fixtureIds.failed] = { complete: true, items: facts };
    const { state, platform } = await started(data);
    await state.choose(fixtureIds.failed);
    expect(platform.count(`/runs/${fixtureIds.failed}/steps`)).toBe(4);
    expect(state.chosen()?.limited).toBe(true);
    expect(state.chosen()?.data?.iterations("send")).toHaveLength(2000);
  });
});

describe("one step's recorded output", () => {
  it("reads it when asked, halving the page when the platform says it's too large", async () => {
    const { state, platform } = await started();
    await state.choose(fixtureIds.failed);
    platform.fail = (path) => {
      const limit = Number(/[?&]limit=(\d+)/.exec(path)?.[1]);
      return path.includes("include=output") && limit > 50
        ? {
            status: 429,
            body: { code: "WV-PAGE-LIMIT", message: "Too large." },
          }
        : null;
    };
    await state.readOutput("send");
    const reads = platform.log.filter((path) =>
      path.includes("include=output"),
    );
    expect(reads.map((path) => /limit=(\d+)/.exec(path)?.[1])).toEqual([
      "200",
      "100",
      "50",
    ]);
    expect(reads[0]).toContain("step=send&include=output");
    const data = state.chosen()!.data!;
    expect(data.output("send[1]")).toEqual({
      kind: "value",
      value: { message_id: "m-1" },
    });
    expect(data.output("send[2]")).toEqual({
      kind: "omitted",
      reason: "classified_secret",
    });

    platform.fail = (path) =>
      path.includes("include=output")
        ? {
            status: 429,
            body: { code: "WV-PAGE-LIMIT", message: "Too large." },
          }
        : null;
    await state.readOutput("fetch");
    expect(data.output("fetch")).toEqual({ kind: "too-large" });
    expect(platform.log.at(-1)).toContain("limit=1");

    platform.fail = (path) =>
      path.includes("include=output")
        ? { status: 500, body: { code: "WV-INTERNAL", message: "It broke." } }
        : null;
    await state.readOutput("notify");
    expect(data.output("notify")).toMatchObject({ kind: "error" });
  });

  it("drops an output that answers after the run was left", async () => {
    const { state, platform } = await started();
    await state.choose(fixtureIds.failed);
    const data = state.chosen()!.data!;
    platform.hold = (path) => path.includes("include=output");
    const pending = state.readOutput("send");
    await settle();
    state.leave();
    platform.release();
    await pending;
    expect(data.output("send[1]")).toEqual({ kind: "loading" });
  });
});

describe("a recorded loop's items", () => {
  const loopDraft: Workflow = {
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "invoice-approval", version: "1.3.0" },
    spec: {
      steps: [
        step("fetch"),
        {
          id: "orders",
          kind: "forEach",
          items: { ref: "/steps/fetch/output/rows" },
          concurrency: 1,
          maxItems: 1000,
          collect: "all",
          body: {
            steps: [step("check"), step("send")],
            output: { ref: "/item" },
          },
        },
      ],
    },
  };
  const loopFact = (key: string, status: StepFact["status"]): StepFact => ({
    node_id: key.replace(/\[.*$/, ""),
    instance_key: key,
    iteration: [...key.matchAll(/\[(\d+)\]/g)].map((match) => Number(match[1])),
    kind: "action",
    status,
    scheduled_at: null,
    started_at: null,
    ended_at: null,
    duration_ms: null,
    queue_wait_ms: null,
    attempts: 1,
    worker_id: null,
    error_code: status === "failed" ? "SMTP_REJECTED" : null,
    child_run_id: null,
    log_entries: 0,
  });

  it("opens on the first failed item, and a pick draws the steps inside in that item", async () => {
    const { state, platform, host } = await started();
    Object.assign(host, { draft: () => loopDraft });
    const summary = summaryOf(platform.data, fixtureIds.failed);
    platform.views.set(
      fixtureIds.failed,
      viewOf(summary, { loops: { orders: { owner: "orders", count: 3 } } }),
    );
    platform.data.steps[fixtureIds.failed] = {
      complete: true,
      items: [
        loopFact("check[0]", "succeeded"),
        loopFact("send[0]", "succeeded"),
        loopFact("check[1]", "succeeded"),
        loopFact("send[1]", "failed"),
      ],
    };
    await state.choose(fixtureIds.failed);
    const first = state.chosen()!.overlay!;
    expect(first.canvas.picks).toEqual({ orders: 1 });
    expect(first.states.get("send")).toBe("failed");
    first.canvas.pick!("orders", 0);
    const second = state.chosen()!.overlay!;
    expect(second.canvas.picks).toEqual({ orders: 0 });
    expect(second.states.get("send")).toBe("completed");
    // Choosing the run again opens it on its first failed item again.
    await state.choose(fixtureIds.failed);
    expect(state.chosen()!.overlay!.canvas.picks).toEqual({ orders: 1 });
  });
});

describe("a recorded run's transactions", () => {
  const action = (id: string, extra: object = {}) =>
    ({ id, kind: "action", uses: `${id}.run@1.0.0`, ...extra }) as Step;
  const undo = { kind: "action", uses: "undo.run@1.0.0", with: {} };
  /** invoice-approval with its steps in a transaction. */
  const moving: Workflow = {
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "invoice-approval", version: "1.3.0" },
    spec: {
      steps: [
        step("fetch"),
        {
          id: "move",
          kind: "transaction",
          steps: [
            action("hold", { confirm: undo, cancel: undo }),
            action("add", { compensate: undo }),
            action("submit", { irreversible: true }),
          ],
          output: { literal: null },
        } as unknown as Step,
      ],
    },
  };
  const participant = (node: string, mode: string, status: string) => ({
    node_id: node,
    instance_key: node,
    mode,
    status,
    effect: "unknown",
  });
  const page = (state: string, statuses: [string, string, string]) => ({
    items: [
      {
        instance_key: "move",
        node_id: "move",
        iteration: [],
        state,
        business_key: "bk-never-kept",
        cause: {
          code: "REQUEST_REFUSED",
          instance_key: "submit",
          effect: "none",
        },
        opened_at: "2026-10-07T11:00:00Z",
        participants: [
          participant("hold", "reserve", statuses[0]),
          participant("add", "compensate", statuses[1]),
          participant("submit", "irreversible", statuses[2]),
        ],
      },
    ],
    next_cursor: null,
  });
  const UNDONE = page("compensated", ["cancelled", "undone", "failed"]);
  const STUCK = page("in_doubt", ["done", "undo_stuck", "failed"]);
  const TRANSACTIONS = /\/transactions\?limit=100$/;
  async function moved(id: string, transactions: unknown = UNDONE) {
    const ready = await started();
    const { platform, host } = ready;
    Object.assign(host, { draft: () => moving });
    const summary = summaryOf(platform.data, id);
    platform.documents.set(summary.workflow.definition_version_id, moving);
    platform.transactions.set(id, transactions);
    return ready;
  }

  it("reads one page of them for a run of a workflow with a transaction, and draws them", async () => {
    const { state, platform } = await moved(fixtureIds.failed);
    await state.choose(fixtureIds.failed);
    await settle();
    expect(platform.count(TRANSACTIONS)).toBe(1);
    const chosen = state.chosen()!;
    expect(chosen.transactions).toMatchObject({ more: false });
    const run = chosen.overlay!.canvas.transactions!;
    expect(run.byFrame.get("move")?.state).toBe("compensated");
    expect(run.byStep.get("hold")).toBe("cancelled");
    expect(JSON.stringify(chosen)).not.toContain("bk-never-kept");
    // Step details read them from the run's data.
    expect(chosen.data?.transaction("add", "add")).toEqual({
      kind: "undone",
      line: "This step was undone after the transaction failed.",
    });
  });

  it("asks nothing for a run of a workflow without one", async () => {
    const { state, platform } = await started();
    await state.choose(fixtureIds.failed);
    await settle();
    expect(platform.count("/transactions")).toBe(0);
    expect(state.chosen()?.transactions).toBeNull();
  });

  it("keeps the run drawn when its transactions are refused, without their words", async () => {
    const { state, platform } = await moved(fixtureIds.failed);
    platform.fail = (path) =>
      TRANSACTIONS.test(path)
        ? { status: 403, body: { code: "WV-FORBIDDEN", message: "No." } }
        : null;
    await state.choose(fixtureIds.failed);
    await settle();
    const chosen = state.chosen()!;
    expect(chosen.transactions).toEqual({ refused: "forbidden" });
    expect(chosen.record?.id).toBe(fixtureIds.failed);
    expect(chosen.overlay?.canvas.transactions).toBeUndefined();
    // The list and the run stay: only the transactions are refused.
    expect(state.state()).toBe("ready");
  });

  it("waits 5 s after a failed read while it follows the run, then 10 s", async () => {
    const { state, platform, env } = await moved(fixtureIds.running);
    const running = summaryOf(platform.data, fixtureIds.running);
    platform.views.set(running.id, viewOf(running, { active: ["hold"] }));
    platform.fail = (path) =>
      TRANSACTIONS.test(path)
        ? { status: 503, body: { code: "WV-UNAVAILABLE", message: "Busy." } }
        : null;
    await state.choose(running.id);
    await settle();
    expect(platform.count(TRANSACTIONS)).toBe(1);
    // Followed every 2 s; the transactions wait 5 s after the failure.
    await env.advance(4000);
    expect(platform.count(TRANSACTIONS)).toBe(1);
    await env.advance(2000);
    expect(platform.count(TRANSACTIONS)).toBe(2);
    await env.advance(8000);
    expect(platform.count(TRANSACTIONS)).toBe(2);
    platform.fail = null;
    await env.advance(2000);
    expect(platform.count(TRANSACTIONS)).toBe(3);
    expect(state.chosen()?.overlay?.canvas.transactions).toBeDefined();
    // Read again with the run while it is followed.
    await env.advance(2000);
    expect(platform.count(TRANSACTIONS)).toBe(4);
  });

  it("reads a finished run's transactions again after a failed read: 5 s, then 10 s", async () => {
    const { state, platform, env } = await moved(fixtureIds.failed);
    platform.fail = (path) =>
      TRANSACTIONS.test(path)
        ? { status: 503, body: { code: "WV-UNAVAILABLE", message: "Busy." } }
        : null;
    await state.choose(fixtureIds.failed);
    await settle();
    expect(state.chosen()?.following).toBe(false);
    expect(platform.count(TRANSACTIONS)).toBe(1);
    await env.advance(4999);
    expect(platform.count(TRANSACTIONS)).toBe(1);
    await env.advance(1);
    expect(platform.count(TRANSACTIONS)).toBe(2);
    await env.advance(9999);
    expect(platform.count(TRANSACTIONS)).toBe(2);
    platform.fail = null;
    await env.advance(1);
    expect(platform.count(TRANSACTIONS)).toBe(3);
    expect(state.chosen()?.overlay?.canvas.transactions).toBeDefined();
    // Read, they aren't asked for again.
    await env.advance(120_000);
    expect(platform.count(TRANSACTIONS)).toBe(3);
  });

  it("asks no more for them once the run is left or another is chosen", async () => {
    const { state, platform, env } = await moved(fixtureIds.failed);
    const ofFailed = new RegExp(`/runs/${fixtureIds.failed}/transactions`);
    platform.fail = (path) =>
      ofFailed.test(path)
        ? { status: 503, body: { code: "WV-UNAVAILABLE", message: "Busy." } }
        : null;
    await state.choose(fixtureIds.failed);
    await settle();
    await env.advance(5000);
    expect(platform.count(ofFailed)).toBe(2);
    state.leave();
    await env.advance(120_000);
    expect(platform.count(ofFailed)).toBe(2);
    // Chosen again, it starts over at 5 s.
    await state.choose(fixtureIds.failed);
    await settle();
    expect(platform.count(ofFailed)).toBe(3);
    await env.advance(5000);
    expect(platform.count(ofFailed)).toBe(4);
    await state.choose(fixtureIds.succeeded);
    await settle();
    await env.advance(120_000);
    expect(platform.count(ofFailed)).toBe(4);
  });

  it("starts no second read of them while one is on its way", async () => {
    const { state, platform, env } = await moved(fixtureIds.running);
    const running = summaryOf(platform.data, fixtureIds.running);
    platform.views.set(running.id, viewOf(running, { active: ["hold"] }));
    platform.hold = (path) => TRANSACTIONS.test(path);
    const chosen = state.choose(running.id);
    await settle();
    expect(platform.count(TRANSACTIONS)).toBe(1);
    // The run is read again every 2 s; its transactions aren't.
    await env.advance(6000);
    expect(platform.count(TRANSACTIONS)).toBe(1);
    platform.hold = null;
    platform.release();
    await chosen;
    await settle();
    expect(state.chosen()?.overlay?.canvas.transactions).toBeDefined();
  });

  it("drops transactions that answer for another run, account or environment", async () => {
    const { state, platform, host } = await moved(fixtureIds.failed, STUCK);
    const retry = summaryOf(platform.data, fixtureIds.retry);
    platform.documents.set(retry.workflow.definition_version_id, moving);
    platform.hold = (path) => TRANSACTIONS.test(path);
    const first = state.choose(fixtureIds.failed);
    await settle();
    expect(platform.count(TRANSACTIONS)).toBe(1);
    // Another run is chosen before the answer lands.
    platform.hold = null;
    const second = state.choose(fixtureIds.retry);
    platform.release();
    await Promise.all([first, second]);
    await settle();
    expect(state.chosen()?.id).toBe(fixtureIds.retry);
    expect(state.chosen()?.overlay?.canvas.transactions).toBeUndefined();
    expect(state.captions().has(fixtureIds.failed)).toBe(false);

    // The environment changes while the answer is on its way.
    platform.transactions.set(fixtureIds.failed, STUCK);
    platform.hold = (path) => TRANSACTIONS.test(path);
    const third = state.choose(fixtureIds.failed);
    await settle();
    host.setScope("account-a|env-2");
    platform.hold = null;
    platform.release();
    await third;
    await settle();
    expect(state.chosen()).toBeNull();
    expect(state.captions().size).toBe(0);
  });

  it("names the transaction in doubt on the row of a run it read", async () => {
    const data = structuredClone(runViewsFixture);
    for (const run of data.runs)
      if (run.summary.id === fixtureIds.failed)
        run.summary.transaction_state = "in_doubt";
    const ready = setup(data);
    ready.state.start();
    await ready.env.advance(0);
    const { state, platform, host } = ready;
    Object.assign(host, { draft: () => moving });
    const summary = summaryOf(platform.data, fixtureIds.failed);
    platform.documents.set(summary.workflow.definition_version_id, moving);
    platform.transactions.set(fixtureIds.failed, STUCK);
    const caption = () =>
      state.rows().find((row) => row.id === fixtureIds.failed)?.transaction;
    expect(caption()).toBe("Transaction in doubt");
    await state.choose(fixtureIds.failed);
    await settle();
    expect(caption()).toBe("Failed at submit · move in doubt");
    // Read once, it keeps its caption while another run is shown.
    await state.choose(fixtureIds.succeeded);
    await settle();
    expect(caption()).toBe("Failed at submit · move in doubt");
  });
});
