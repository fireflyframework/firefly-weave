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
// A workflow whose transaction move holds, adds, then submits, and four of its
// recorded runs on a mocked platform: one undone, one in doubt, one whose
// transactions the platform refuses, and an older one in doubt Studio hasn't
// read. Read by the Runs tab's transaction specs, the journey and the tour.
import { expect, type Page } from "@playwright/test";
import type { RunSummary, StepFact } from "../../src/app/operate/run-contracts";
import { CanvasPage } from "./canvas-po";
import {
  NOW,
  openRunsTab,
  type RunFixture,
  type RunsPlatform,
} from "./runs-po";
import { type CatalogEntry } from "./support";
import { catalog, withManifest } from "./transaction-po";

export const VERSION = "6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b";
export const IDS = {
  undone: "7b2c4d6e-8f01-4a23-9b45-c6d7e8f90a1b",
  doubt: "8c3d5e7f-9012-4b34-8c56-d7e8f9a01b2c",
  refused: "9d4e6f80-0123-4c45-9d67-e8f9a0b12c3d",
  earlier: "0e5f7091-1234-4d56-8e78-f9a0b1c23d4e",
} as const;
export const INCIDENT = "1f6a8b02-2345-4e67-9f89-a0b1c2d34e5f";
/** A business key the platform recorded: Studio never shows it. */
export const BUSINESS_KEY = "bk-7731-never-shown";

const action = (id: string, uses: string, extra: object = {}) => ({
  id,
  kind: "action",
  uses,
  with: { object: { reference: { ref: "/input/moveId" } } },
  ...extra,
});
const undo = (uses: string) => ({
  kind: "action",
  uses,
  with: { object: { reference: { ref: "/input/moveId" } } },
});
export const WORKFLOW = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "move-between-systems", version: "1.4.0" },
  spec: {
    inputSchema: {
      type: "object",
      properties: { moveId: { type: "string" }, quantity: { type: "integer" } },
    },
    outputSchema: {},
    steps: [
      {
        id: "move",
        kind: "transaction",
        businessKey: { ref: "/input/moveId" },
        steps: [
          action("hold", "source-system.hold-quantity@1.0.0", {
            confirm: undo("source-system.confirm-hold@1.0.0"),
            cancel: undo("source-system.release-hold@1.0.0"),
          }),
          action("add", "target-system.add-quantity@1.0.0", {
            compensate: undo("target-system.remove-quantity@1.0.0"),
          }),
          action("submit", "orders.submit-order@1.0.0", { irreversible: true }),
          action("notify", "notify.send-message@1.0.0"),
        ],
        output: { literal: null },
      },
    ],
    output: { literal: null },
  },
};
const CATALOG: CatalogEntry[] = catalog.definitions
  .filter(({ kind }) => kind === "Action")
  .map((document, index) => ({
    id: `a${index}`,
    document: document as unknown as CatalogEntry["document"],
  }));

const at = (seconds: number) =>
  new Date(Date.parse(NOW) - seconds * 1000).toISOString();
function summary(
  id: string,
  over: Partial<RunSummary> & Pick<RunSummary, "status">,
  startedAgo: number,
): RunSummary {
  const ended = over.status === "suspended" ? null : at(startedAgo - 41);
  return {
    id,
    workflow: {
      name: "move-between-systems",
      version: "1.4.0",
      definition_version_id: VERSION,
    },
    activation_id: "a09f6c1d-d0ff-4b05-9efb-e3bfa0c17cab",
    activation: { name: "move-between-systems", revision: 1 },
    paused: false,
    test: false,
    origin: "manual",
    caller: null,
    retried_from_run_id: null,
    started_at: at(startedAgo),
    updated_at: ended ?? at(startedAgo),
    ended_at: ended,
    duration_ms: ended ? 41_000 : null,
    business_key: null,
    correlation_key: null,
    failed_step: null,
    active_incidents: 0,
    archived: false,
    ...over,
  };
}
function fact(
  key: string,
  status: StepFact["status"],
  extra: Partial<StepFact> = {},
): StepFact {
  return {
    node_id: key.replace(/#.*$/, ""),
    instance_key: key,
    iteration: [],
    kind: "action",
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
const OUTPUTS = { hold: { holdId: "h-1" }, add: { id: "a-1" } };
const FORWARD = [
  fact("hold", "succeeded", { output: OUTPUTS.hold }),
  fact("add", "succeeded", { output: OUTPUTS.add }),
  fact("submit", "failed", { error_code: "REQUEST_REFUSED" }),
];
function run(
  over: Partial<RunSummary> & Pick<RunSummary, "status">,
  startedAgo: number,
  incidents: Record<string, unknown>,
  facts: StepFact[],
): RunFixture {
  const head = summary(over.id ?? "", over, startedAgo);
  return {
    summary: head,
    view: {
      id: head.id,
      activation: {
        id: head.activation_id,
        revision: 1,
        name: head.activation.name,
        request: { version_id: VERSION },
      },
      state: {
        status: head.status,
        active: [],
        input: { moveId: "m-0193", quantity: 40 },
        steps: {
          hold: { output: OUTPUTS.hold },
          add: { output: OUTPUTS.add },
        },
        waits: {},
        branches: {},
        incidents,
      },
    },
    facts,
    versionId: VERSION,
  };
}
const FAILED_AT_SUBMIT = {
  node_id: "submit",
  instance_key: "submit",
  error_code: "REQUEST_REFUSED",
};
export const RUNS = [
  run(
    {
      id: IDS.doubt,
      status: "suspended",
      transaction_state: "in_doubt",
      active_incidents: 1,
    },
    12 * 60,
    { i1: { node_id: "add#undo", code: "WV-TXN-UNDO-STUCK" } },
    [
      ...FORWARD,
      fact("add#undo", "failed", { error_code: "UNAVAILABLE", attempts: 10 }),
    ],
  ),
  run(
    {
      id: IDS.undone,
      status: "failed",
      failed_step: FAILED_AT_SUBMIT,
      transaction_state: "compensated",
    },
    60 * 60,
    {},
    FORWARD,
  ),
  run(
    {
      id: IDS.refused,
      status: "failed",
      failed_step: FAILED_AT_SUBMIT,
      transaction_state: "compensated",
    },
    2 * 3600,
    {},
    FORWARD,
  ),
  run(
    {
      id: IDS.earlier,
      status: "suspended",
      transaction_state: "in_doubt",
      active_incidents: 1,
    },
    3 * 3600,
    { i1: { node_id: "add#undo", code: "WV-TXN-UNDO-STUCK" } },
    FORWARD,
  ),
];

const participant = (
  node: string,
  mode: string,
  status: string,
  extra: object = {},
) => ({
  node_id: node,
  instance_key: node,
  mode,
  status,
  effect: "unknown",
  ...extra,
});
const transactionPage = (
  state: string,
  statuses: [string, string, string],
  extra: object = {},
  next: string | null = null,
) => ({
  items: [
    {
      instance_key: "move",
      node_id: "move",
      iteration: [],
      state,
      business_key: BUSINESS_KEY,
      cause: {
        code: "REQUEST_REFUSED",
        instance_key: "submit",
        effect: "none",
      },
      opened_at: at(12 * 60),
      participants: [
        participant("hold", "reserve", statuses[0]),
        participant("add", "compensate", statuses[1], { attempts: 10 }),
        participant("submit", "irreversible", statuses[2], { effect: "none" }),
      ],
      ...extra,
    },
  ],
  next_cursor: next,
});
export const PAGES: Record<string, unknown> = {
  [IDS.undone]: transactionPage(
    "compensated",
    ["cancelled", "undone", "failed"],
    {},
    "c2",
  ),
  [IDS.doubt]: transactionPage("in_doubt", ["done", "undo_stuck", "failed"], {
    incident_id: INCIDENT,
  }),
  [IDS.earlier]: transactionPage("in_doubt", ["done", "undo_stuck", "failed"], {
    incident_id: INCIDENT,
  }),
};

/** The platform's runs.transactions and the run's version, beside the Runs tab's reads. */
export async function routeTransactions(page: Page, platform: RunsPlatform) {
  await withManifest(page);
  await page.route("**/environments/*/runs/*/transactions**", (route) => {
    const url = new URL(route.request().url());
    platform.requests.push(url);
    const id = url.pathname.split("/").at(-2)!;
    if (id === IDS.refused)
      return route.fulfill({
        status: 403,
        contentType: "application/problem+json",
        body: JSON.stringify({
          code: "WV-FORBIDDEN",
          message: "You can't read this run's transactions.",
        }),
      });
    return route.fulfill({
      json: PAGES[id] ?? { items: [], next_cursor: null },
    });
  });
  await page.route("**/projects/*/workflows/*/export", (route) =>
    route.fulfill({ json: { id: VERSION, document: WORKFLOW } }),
  );
}
export /** The Runs tab on those runs, with the console errors that aren't expected. */
async function openTransactionRuns(page: Page) {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    // The refused read answers 403 on purpose; the browser logs it. The test
    // harness has no desktop host to test connections against.
    const url = message.location().url;
    if (
      url.includes(`/runs/${IDS.refused}/transactions`) ||
      url.endsWith("/studio/connection/test")
    )
      return;
    errors.push(`${message.text()} ${url}`);
  });
  page.on("pageerror", (error) => errors.push(error.message));
  const opened = await openRunsTab(page, {
    runs: RUNS,
    workflow: WORKFLOW,
    catalog: CATALOG,
    unavailable: false,
    before: (platform) => routeTransactions(page, platform),
  });
  return { ...opened, errors };
}
/** Chooses a run and waits for its canvas. */
export async function chooseRun(
  page: Page,
  runs: { row(id: string): ReturnType<Page["locator"]> },
  id: string,
) {
  await runs.row(id).click();
  const canvas = new CanvasPage(page);
  await expect(canvas.tileBody("move")).toBeVisible();
  return canvas;
}
export const frameState = (canvas: CanvasPage) =>
  canvas.tileBody("move").locator(".frame-state");
export const word = (canvas: CanvasPage, id: string) =>
  canvas.tile(id).locator(".tile-output");
export const badge = (canvas: CanvasPage, id: string) =>
  canvas.tile(id).locator(".tile-badge");
export const divider = (canvas: CanvasPage) =>
  canvas.root.getByRole("img", {
    name: "Point of no return, after submit",
    exact: true,
  });
