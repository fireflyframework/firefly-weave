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
// The Runs tab's page object: a workflow with its runs on a mocked platform.
// The list answers with the run views fake's rules (filters, order, pages),
// runs.read, runs.steps and runs.logs from the fixtures here, and the version
// a run used from its export. Every request is recorded.
import { expect, type Locator, type Page } from "@playwright/test";
import {
  answerRunViews,
  type FakeAnswer,
} from "../../src/app/operate/run-views-fake";
import type {
  RunLogEntry,
  RunSummary,
  StepFact,
} from "../../src/app/operate/run-contracts";
import type { RunViewsFixture } from "../../src/app/operate/run-views.fixture";
import { useNewEditor } from "./canvas-po";
import { DesignerPage } from "./designer-po";
import { allCapabilities, connected, newWorkflow } from "./support";

/** The fixtures' day: every run started a little before noon. */
export const NOW = "2026-10-07T12:00:00Z";
/** The session's environment, a UUID as the platform's are. */
export const RUNS_ENV = "4a1b2c3d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
export const VERSIONS = {
  current: "9e8f5b0c-cfee-4af4-8dea-d2ae9fb06b9a",
  older: "7d6e4a9b-bedd-4fe3-9cd9-c19d8eaf5a00",
  test: "d3c29f40-0322-4e38-8b2e-16e2d3f4afde",
} as const;
export const RUN_IDS = {
  running: "4f3a0cbd-7a99-4baf-9e95-8d5f4a6b1c45",
  failed: "1c0d7f8a-4d66-4e7c-8b62-5a2c1d3e8f12",
  test: "5a4b1dce-8baa-4cb0-8fa6-9e6a5b7c2d56",
  waiting: "6b5c2edf-9cbb-4dc1-9ab7-af7b6c8d3e67",
  retry: "3e2f9bac-6f88-4a9e-8d84-7c4e3f5a0b34",
  succeeded: "0b9c6f7e-3c55-4d6b-9a51-4f1b0c2d7e01",
  older: "2d1e8a9b-5e77-4f8d-9c73-6b3d2e4f9a23",
  loop: "8e7f5b0c-1d2e-4f3a-8b4c-5d6e7f8a9b0c",
  call: "9f8a6c1d-2e3f-4a4b-9c5d-6e7f8a9b0c1d",
  unavailable: "8d7e4afb-bedd-4fe3-9cd9-c19d8eaf5a89",
} as const;
/** The run a call step started, and the run older runs were called by. */
export const CHILD_RUN = "9b1c4e2a-3f4a-4b5c-8d6e-7f8a9b0c1d2e";

const step = (id: string, extra: Record<string, unknown>) => ({ id, ...extra });
const fetch = step("fetch", {
  kind: "action",
  uses: "sql.lookup@1.0.0",
  with: {
    object: { parameters: { object: { customerId: { ref: "/input/total" } } } },
  },
});
const check = step("check", {
  kind: "transform",
  value: { ref: "/steps/fetch/output" },
});
const approve = step("approve", {
  kind: "humanTask",
  assignment: "reviewers",
  title: { literal: "Approve the invoice" },
  context: { literal: {} },
  formSchema: { type: "object", properties: {} },
  decisions: ["approve", "reject"],
});
const waitForReply = step("wait-for-reply", {
  kind: "signal",
  name: "reply",
  timeoutSeconds: 600,
  payloadSchema: { type: "object" },
});
const send = step("send", {
  kind: "action",
  uses: "crm.lookup@2.0.0",
  with: {
    object: {
      customer: { ref: "/steps/fetch/output/rows/0/name" },
      total: { ref: "/input/total" },
    },
  },
});
const document = (version: string, steps: Record<string, unknown>[]) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "invoice-approval", version },
  spec: {
    inputSchema: {
      type: "object",
      properties: { total: { type: "number", title: "Total" } },
    },
    outputSchema: { type: "object" },
    steps,
    output: { literal: {} },
  },
});
/** The draft open in the editor: invoice-approval 1.3.0. */
export const runsWorkflow = document("1.3.0", [
  fetch,
  check,
  approve,
  waitForReply,
  send,
]);
/** The version older runs used: no check, a log step, another send input. */
export const runsWorkflowV120 = document("1.2.0", [
  fetch,
  step("log", { kind: "transform", value: { literal: "logged" } }),
  approve,
  waitForReply,
  step("send", {
    kind: "action",
    uses: "crm.lookup@2.0.0",
    with: { object: { customer: { literal: "nobody" } } },
  }),
]);

export type RunKind =
  | "running"
  | "failed"
  | "test"
  | "waiting"
  | "retry"
  | "succeeded"
  | "older"
  | "loop"
  | "call";
export interface RunFixture {
  summary: RunSummary;
  /** The runs.read answer. */
  view: Record<string, unknown>;
  /** The step facts, with outputs (runs.steps leaves them out unless asked). */
  facts: StepFact[];
  versionId: string;
}

const at = (seconds: number) =>
  new Date(Date.parse(NOW) - seconds * 1000).toISOString();
function summary(
  id: string,
  facts: Partial<RunSummary> & Pick<RunSummary, "status">,
  startedAgo: number,
  durationS: number | null,
): RunSummary {
  const started = at(startedAgo);
  const ended = durationS === null ? null : at(startedAgo - durationS);
  return {
    id,
    workflow: {
      name: "invoice-approval",
      version: "1.3.0",
      definition_version_id: VERSIONS.current,
    },
    activation_id: "a09f6c1d-d0ff-4b05-9efb-e3bfa0c17cab",
    activation: { name: "invoice-approval", revision: 4 },
    paused: false,
    test: false,
    origin: "webhook",
    caller: null,
    retried_from_run_id: null,
    started_at: started,
    updated_at: ended ?? started,
    ended_at: ended,
    duration_ms: durationS === null ? null : durationS * 1000,
    business_key: null,
    correlation_key: null,
    failed_step: null,
    active_incidents: 0,
    archived: false,
    ...facts,
  };
}
function fact(
  nodeId: string,
  kind: StepFact["kind"],
  status: StepFact["status"],
  extra: Partial<StepFact> = {},
): StepFact {
  return {
    node_id: nodeId,
    instance_key: "",
    iteration: [],
    kind,
    status,
    scheduled_at: null,
    started_at: null,
    ended_at: null,
    duration_ms: null,
    queue_wait_ms: null,
    attempts: 1,
    worker_id: null,
    error_code: null,
    child_run_id: null,
    log_entries: 0,
    ...extra,
  };
}
const outputs = {
  fetch: { rows: [{ name: "Ada", id: "c-1" }] },
  check: { rows: [{ name: "Ada", id: "c-1" }] },
  approve: { decision: "approve", data: {} },
  "wait-for-reply": { payload: { ok: true } },
  send: { message_id: "m-1" },
};
type StepId = keyof typeof outputs;
const kinds: Record<StepId, StepFact["kind"]> = {
  fetch: "action",
  check: "transform",
  approve: "humanTask",
  "wait-for-reply": "signal",
  send: "action",
};
const done = (ids: StepId[]) =>
  ids.map((id) => fact(id, kinds[id], "succeeded", { output: outputs[id] }));
function view(
  run: RunSummary,
  state: Record<string, unknown>,
): Record<string, unknown> {
  return {
    id: run.id,
    activation: {
      id: run.activation_id,
      revision: run.activation.revision,
      name: run.activation.name,
      request: { version_id: run.workflow.definition_version_id },
    },
    state: {
      status: run.status,
      active: [],
      input: { total: 120.5 },
      steps: {},
      waits: {},
      branches: {},
      incidents: {},
      ...state,
    },
  };
}
const bindings = (ids: StepId[]) =>
  Object.fromEntries(ids.map((id) => [id, { output: outputs[id] }]));

/** One run of invoice-approval, as every read of the platform answers it. */
export function runFixture(kind: RunKind): RunFixture {
  const all: StepId[] = ["fetch", "check", "approve", "wait-for-reply", "send"];
  switch (kind) {
    case "running": {
      const run = summary(RUN_IDS.running, { status: "running" }, 14, null);
      return {
        summary: run,
        view: view(run, { steps: bindings(["fetch"]), active: ["check"] }),
        facts: [...done(["fetch"]), fact("check", "transform", "running")],
        versionId: VERSIONS.current,
      };
    }
    case "failed": {
      const run = summary(
        RUN_IDS.failed,
        {
          status: "failed",
          failed_step: {
            node_id: "send",
            instance_key: "send",
            error_code: "SMTP_REJECTED",
          },
          active_incidents: 1,
        },
        12 * 60,
        4,
      );
      const before: StepId[] = ["fetch", "check", "approve", "wait-for-reply"];
      return {
        summary: run,
        view: view(run, {
          steps: bindings(before),
          incidents: { i1: { node_id: "send", code: "SMTP_REJECTED" } },
        }),
        facts: [
          ...done(before),
          fact("send", "action", "failed", { error_code: "SMTP_REJECTED" }),
        ],
        versionId: VERSIONS.current,
      };
    }
    case "test": {
      const run = summary(
        RUN_IDS.test,
        {
          status: "succeeded",
          test: true,
          origin: "test",
          workflow: {
            name: "invoice-approval",
            version: "1.3.0+test.d3c29f400322",
            definition_version_id: VERSIONS.test,
          },
        },
        25 * 60,
        3,
      );
      return {
        summary: run,
        view: view(run, { steps: bindings(all) }),
        facts: done(all),
        versionId: VERSIONS.test,
      };
    }
    case "waiting": {
      const run = summary(
        RUN_IDS.waiting,
        { status: "waiting" },
        60 * 60,
        null,
      );
      return {
        summary: run,
        view: view(run, {
          steps: bindings(["fetch", "check"]),
          active: ["approve"],
          waits: { approve: { kind: "human_task" } },
        }),
        facts: [
          ...done(["fetch", "check"]),
          fact("approve", "humanTask", "waiting"),
        ],
        versionId: VERSIONS.current,
      };
    }
    case "retry": {
      const run = summary(
        RUN_IDS.retry,
        {
          status: "succeeded",
          origin: "retry",
          retried_from_run_id: RUN_IDS.failed,
        },
        2 * 3600,
        5,
      );
      return {
        summary: run,
        view: view(run, { steps: bindings(all) }),
        facts: done(all),
        versionId: VERSIONS.current,
      };
    }
    case "succeeded": {
      const run = summary(
        RUN_IDS.succeeded,
        { status: "succeeded", origin: "manual" },
        3 * 3600,
        6,
      );
      return {
        summary: run,
        view: view(run, { steps: bindings(all) }),
        facts: done(all),
        versionId: VERSIONS.current,
      };
    }
    case "older": {
      const run = summary(
        RUN_IDS.older,
        {
          status: "succeeded",
          origin: "call",
          caller: { run_id: CHILD_RUN, node_id: "notify", instance_key: "" },
          workflow: {
            name: "invoice-approval",
            version: "1.2.0",
            definition_version_id: VERSIONS.older,
          },
        },
        26 * 3600,
        4,
      );
      const ran = ["fetch", "approve", "wait-for-reply", "send"] as StepId[];
      return {
        summary: run,
        view: view(run, {
          steps: { ...bindings(ran), log: { output: "logged" } },
        }),
        facts: [...done(ran), fact("log", "transform", "succeeded")],
        versionId: VERSIONS.older,
      };
    }
    case "loop": {
      const run = summary(
        RUN_IDS.loop,
        {
          status: "failed",
          failed_step: {
            node_id: "send",
            instance_key: "send[1]",
            error_code: "SMTP_REJECTED",
          },
        },
        4 * 3600,
        7,
      );
      return {
        summary: run,
        view: view(run, {
          steps: bindings(["fetch"]),
          incidents: { i1: { node_id: "send[1]", code: "SMTP_REJECTED" } },
        }),
        facts: [
          ...done(["fetch"]),
          fact("send", "action", "succeeded", {
            instance_key: "send[0]",
            iteration: [0],
            output: { message_id: "m-0" },
          }),
          fact("send", "action", "failed", {
            instance_key: "send[1]",
            iteration: [1],
            error_code: "SMTP_REJECTED",
          }),
          fact("send", "action", "succeeded", {
            instance_key: "send[2]",
            iteration: [2],
            output: { message_id: "m-2" },
          }),
        ],
        versionId: VERSIONS.current,
      };
    }
    case "call": {
      const run = summary(RUN_IDS.call, { status: "succeeded" }, 5 * 3600, 9);
      return {
        summary: run,
        view: view(run, { steps: bindings(all) }),
        facts: done(all).map((item) =>
          item.node_id === "send" ? { ...item, child_run_id: CHILD_RUN } : item,
        ),
        versionId: VERSIONS.current,
      };
    }
  }
}

/** The list the specs start from, newest first, with one run the platform can't show. */
export const listedKinds: RunKind[] = [
  "running",
  "failed",
  "test",
  "waiting",
  "retry",
  "older",
];
/** `count` older succeeded runs, a day apart from the fixtures, for paging. */
export function olderRuns(count: number): RunFixture[] {
  return Array.from({ length: count }, (_, index) => {
    const base = runFixture("succeeded");
    const id = `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`;
    return {
      ...base,
      summary: {
        ...base.summary,
        id,
        started_at: at(2 * 86400 + index * 60),
        ended_at: at(2 * 86400 + index * 60 - 6),
        updated_at: at(2 * 86400 + index * 60 - 6),
      },
      view: { ...base.view, id },
    };
  });
}

export interface RunsPlatform {
  /** The runs the platform knows; a spec may add or change them. */
  runs: RunFixture[];
  /** Every request to the run views, runs.read and exports, in order. */
  requests: URL[];
  /** The list's queries. */
  listQueries(): URLSearchParams[];
  /** Holds the list's answers until released; returns the release. */
  holdList(): () => void;
  /** Answers the list with this status (and code) while set; null answers normally. */
  listFailure: { status: number; code: string; message: string } | null;
  /** Answers runs.read with this status while set. */
  readFailure: { status: number; code: string; message: string } | null;
  /** The logs runs.logs answers, or 501 (the platform doesn't serve them yet). */
  logs: 501 | RunLogEntry[];
}

const problem = (status: number, code: string, message: string) => ({
  status,
  contentType: "application/problem+json",
  body: JSON.stringify({ code, message }),
});

/** Answers the Runs tab's reads from `runs`; register it after connecting. */
export async function routeRuns(
  page: Page,
  runs: RunFixture[],
  options: { logs?: 501 | RunLogEntry[]; unavailable?: boolean } = {},
): Promise<RunsPlatform> {
  let hold: Promise<void> | null = null;
  const platform: RunsPlatform = {
    runs,
    requests: [],
    listQueries: () =>
      platform.requests
        .filter((url) => url.pathname.endsWith("/run-summaries"))
        .map((url) => url.searchParams),
    holdList() {
      let release = () => {};
      hold = new Promise((resolve) => (release = resolve));
      return () => {
        hold = null;
        release();
      };
    },
    listFailure: null,
    readFailure: null,
    logs: options.logs ?? 501,
  };
  const fixture = (): RunViewsFixture => ({
    runs: [
      ...platform.runs.map((run) => ({ summary: run.summary })),
      ...(options.unavailable === false
        ? []
        : [
            {
              summary: {
                ...runFixture("succeeded").summary,
                id: RUN_IDS.unavailable,
                started_at: at(27 * 3600),
              },
              unavailable: true as const,
            },
          ]),
    ],
    steps: Object.fromEntries(
      platform.runs.map((run) => [
        run.summary.id,
        { complete: true, items: run.facts },
      ]),
    ),
    logs: Object.fromEntries(
      platform.runs.map((run) => [
        run.summary.id,
        Array.isArray(platform.logs) ? platform.logs : [],
      ]),
    ),
  });
  const fulfil = (answer: FakeAnswer) => ({
    status: answer.status,
    contentType:
      answer.status >= 400 ? "application/problem+json" : "application/json",
    body: JSON.stringify(answer.body),
  });
  await page.route("**/environments/*/run-summaries**", async (route) => {
    const url = new URL(route.request().url());
    platform.requests.push(url);
    if (hold) await hold;
    const failure = platform.listFailure;
    if (failure)
      return route.fulfill(
        problem(failure.status, failure.code, failure.message),
      );
    await route
      .fulfill(fulfil(answerRunViews(url.pathname + url.search, fixture())!))
      .catch(() => undefined);
  });
  await page.route("**/environments/*/runs/**", async (route) => {
    const url = new URL(route.request().url());
    platform.requests.push(url);
    const read = /\/runs\/([^/]+)$/.exec(url.pathname);
    if (read) {
      const failure = platform.readFailure;
      if (failure)
        return route.fulfill(
          problem(failure.status, failure.code, failure.message),
        );
      const run = platform.runs.find((item) => item.summary.id === read[1]);
      return run
        ? route.fulfill({ json: run.view })
        : route.fulfill(problem(404, "WV-NOT-FOUND", "Run not found."));
    }
    if (url.pathname.endsWith("/logs") && !Array.isArray(platform.logs))
      return route.fulfill(
        problem(501, "WV-UNAVAILABLE", "This platform doesn't serve logs yet."),
      );
    const answer = answerRunViews(url.pathname + url.search, fixture());
    await route.fulfill(
      answer
        ? fulfil(answer)
        : problem(404, "WV-STUDIO-ROUTE", "Unknown platform operation"),
    );
  });
  await page.route("**/projects/*/workflows/*/export", async (route) => {
    const url = new URL(route.request().url());
    platform.requests.push(url);
    const id = url.pathname.split("/").at(-2);
    const document =
      id === VERSIONS.older
        ? runsWorkflowV120
        : id === VERSIONS.current || id === VERSIONS.test
          ? runsWorkflow
          : null;
    return document
      ? route.fulfill({ json: { id, document } })
      : route.fulfill(problem(404, "WV-NOT-FOUND", "Version not found."));
  });
  return platform;
}

export interface RunsPage {
  tabs: Locator;
  /** The list column. */
  panel: Locator;
  list: Locator;
  row(id: string): Locator;
  rows: Locator;
  filters: Locator;
  /** The canvas column: the run's header, its banner and the canvas. */
  canvas: Locator;
}

/**
 * A connected workflow open in the new editor on its Runs tab, its runs
 * answered by `runs`. The page clock starts at NOW and runs.
 */
export async function openRunsTab(
  page: Page,
  options: {
    runs?: RunFixture[];
    capabilities?: string[];
    unavailable?: boolean;
    logs?: 501 | RunLogEntry[];
    /** Registers more routes after connecting, before the tab opens. */
    before?: (platform: RunsPlatform) => Promise<void> | void;
  } = {},
): Promise<{ runs: RunsPage; platform: RunsPlatform }> {
  await page.clock.install({ time: new Date(NOW) });
  await useNewEditor(page);
  await connected(page, {
    capabilities: options.capabilities ?? [...allCapabilities, "compile"],
    environmentId: RUNS_ENV,
    environmentName: "development",
  });
  const platform = await routeRuns(
    page,
    options.runs ?? listedKinds.map(runFixture),
    { logs: options.logs, unavailable: options.unavailable },
  );
  await options.before?.(platform);
  await newWorkflow(page);
  await new DesignerPage(page).setSource(JSON.stringify(runsWorkflow, null, 2));
  const tabs = page.getByRole("tablist", { name: "Workflow views" });
  await tabs.getByRole("tab", { name: "Runs" }).click();
  await expect(tabs.getByRole("tab", { name: "Runs" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  return { runs: runsPage(page), platform };
}

export function runsPage(page: Page): RunsPage {
  const panel = page.locator(".runs-list");
  const list = panel.getByRole("listbox");
  return {
    tabs: page.getByRole("tablist", { name: "Workflow views" }),
    panel,
    list,
    rows: list.getByRole("option"),
    row: (id: string) => list.locator(`[data-run="${id}"]`),
    filters: panel.getByRole("button", { name: /^Filters/ }),
    canvas: page.locator(".runs-canvas"),
  };
}
