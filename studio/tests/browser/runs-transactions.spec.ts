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
// A recorded run's transactions on the Runs tab, from runs.transactions on a
// mocked platform: the frame's state and each step's word, the point of no
// return, the header and list captions of a run in doubt, its incident in
// Operate, and never a business key.
import { expect, test, type Locator, type Page } from "@playwright/test";
import type { RunSummary, StepFact } from "../../src/app/operate/run-contracts";
import { CanvasPage } from "./canvas-po";
import {
  NOW,
  openRunsTab,
  type RunFixture,
  type RunsPlatform,
} from "./runs-po";
import { StepDetailsPage } from "./step-details-po";
import { type CatalogEntry } from "./support";
import { outputPane } from "./test-data-po";
import { catalog, withManifest } from "./transaction-po";

const VERSION = "6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b";
const IDS = {
  undone: "7b2c4d6e-8f01-4a23-9b45-c6d7e8f90a1b",
  doubt: "8c3d5e7f-9012-4b34-8c56-d7e8f9a01b2c",
  refused: "9d4e6f80-0123-4c45-9d67-e8f9a0b12c3d",
  earlier: "0e5f7091-1234-4d56-8e78-f9a0b1c23d4e",
} as const;
const INCIDENT = "1f6a8b02-2345-4e67-9f89-a0b1c2d34e5f";
/** A business key the platform recorded: Studio never shows it. */
const BUSINESS_KEY = "bk-7731-never-shown";

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
const WORKFLOW = {
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
const RUNS = [
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
const PAGES: Record<string, unknown> = {
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
async function routeTransactions(page: Page, platform: RunsPlatform) {
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
async function openTab(page: Page) {
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
async function choose(
  page: Page,
  runs: { row(id: string): ReturnType<Page["locator"]> },
  id: string,
) {
  await runs.row(id).click();
  const canvas = new CanvasPage(page);
  await expect(canvas.tileBody("move")).toBeVisible();
  return canvas;
}
const frameState = (canvas: CanvasPage) =>
  canvas.tileBody("move").locator(".frame-state");
const word = (canvas: CanvasPage, id: string) =>
  canvas.tile(id).locator(".tile-output");
const badge = (canvas: CanvasPage, id: string) =>
  canvas.tile(id).locator(".tile-badge");
const divider = (canvas: CanvasPage) =>
  canvas.root.getByRole("img", {
    name: "Point of no return, after submit",
    exact: true,
  });
const transactionReads = (platform: RunsPlatform, id: string) =>
  platform.requests.filter((url) =>
    url.pathname.endsWith(`/runs/${id}/transactions`),
  );
/** The run header's caption parts that a line break splits in two. */
const splitParts = (header: Locator) =>
  header
    .locator(".run-caption > span:not(.run-in-doubt), .run-in-doubt-part")
    .evaluateAll((parts) =>
      parts
        .filter((part) => part.getClientRects().length > 1)
        .map((part) => part.textContent?.trim()),
    );
/** Where the caption's first in-doubt part sits: at the start of a line of
 * its own below "Following this run", or after something on its line. */
const firstDoubtPart = (header: Locator) =>
  header.locator(".run-caption").evaluate((caption) => {
    const first = caption.querySelector(".run-in-doubt-part")!;
    const follow = caption.querySelector(".run-follow-caption")!;
    const part = first.getBoundingClientRect();
    return {
      indent: Math.round(part.left - caption.getBoundingClientRect().left),
      belowFollow: part.top >= follow.getBoundingClientRect().bottom - 1,
    };
  });

test.describe("a recorded run's transactions at 1440x900", () => {
  test.use({
    viewport: { width: 1440, height: 900 },
    contextOptions: { reducedMotion: "reduce" },
  });

  test("a recorded rollback reads Undone, each step's word and the point of no return", async ({
    page,
  }) => {
    const { runs, platform, errors } = await openTab(page);
    const canvas = await choose(page, runs, IDS.undone);
    await expect(frameState(canvas)).toHaveText("Undone");
    await expect(frameState(canvas)).toHaveAttribute("data-tone", "warning");
    await expect(word(canvas, "hold")).toHaveText("Released");
    await expect(word(canvas, "add")).toHaveText("Undone");
    await expect(word(canvas, "submit")).toHaveText("Failed");
    await expect(badge(canvas, "submit")).toHaveAttribute(
      "data-badge",
      "failed",
    );
    await expect(divider(canvas)).toHaveCount(1);
    // One page of 100, and the header says more follow.
    expect(
      transactionReads(platform, IDS.undone).map((url) => url.search),
    ).toEqual(["?limit=100"]);
    await expect(runs.canvas.locator(".run-transactions-note")).toHaveText(
      "Showing the first 100 transactions.",
    );
    // Its step details say the step was undone.
    const details = new StepDetailsPage(page);
    await details.open("add");
    const pane = await outputPane(details);
    await expect(pane).toContainText(
      "This step was undone after the transaction failed (try 10 of 10).",
    );
    expect(await page.locator("body").innerText()).not.toContain(BUSINESS_KEY);
    expect(errors).toEqual([]);
  });

  test("a run in doubt names the step and the transaction, keeps what is reserved, and opens its incident", async ({
    page,
  }) => {
    const { runs, errors } = await openTab(page);
    // A run in doubt whose transactions Studio hasn't read.
    await expect(runs.row(IDS.earlier).locator(".run-caption-line")).toHaveText(
      "Transaction in doubt",
    );
    const canvas = await choose(page, runs, IDS.doubt);
    const header = runs.canvas.locator(".run-header");
    await expect(header.locator(".run-pill")).toHaveText("On hold");
    await expect(header.locator(".run-in-doubt")).toHaveText(
      "Failed at submit: REQUEST_REFUSED · transaction move is in doubt · WV-TXN-UNDO-STUCK",
    );
    // The caption wraps only between whole parts: when it started and how
    // long it ran never split.
    expect(await splitParts(header)).toEqual([]);
    await expect(runs.row(IDS.doubt).locator(".run-caption-line")).toHaveText(
      "Failed at submit · move in doubt",
    );
    await expect(runs.row(IDS.earlier).locator(".run-caption-line")).toHaveText(
      "Transaction in doubt",
    );
    await expect(frameState(canvas)).toHaveText("In doubt · action needed");
    await expect(frameState(canvas)).toHaveAttribute("data-tone", "danger");
    // Its header says how it stands: nothing more under the frame.
    await expect(word(canvas, "move")).toHaveCount(0);
    await expect(word(canvas, "hold")).toHaveText("Still reserved");
    await expect(word(canvas, "hold")).toHaveAttribute("data-tone", "warning");
    // Never the success check: the hold is still applied.
    await expect(badge(canvas, "hold")).toHaveAttribute("data-tone", "warning");
    await expect(badge(canvas, "hold").locator("weave-icon")).toHaveAttribute(
      "data-icon",
      "lock",
    );
    await expect(word(canvas, "add")).toHaveText("Undo failed");
    await expect(divider(canvas)).toHaveCount(1);

    const details = new StepDetailsPage(page);
    await details.open("move");
    const pane = await outputPane(details);
    await expect(pane.locator(".pill").first()).toHaveText(
      "In doubt · action needed",
    );
    const banner = pane.locator(".pane-in-doubt");
    await expect(banner).toContainText(
      "This transaction is in doubt. Someone must resolve it in Operate.",
    );
    await expect(banner).toContainText(
      "Undo of add failed after 10 tries: UNAVAILABLE. Weave stopped undoing, so hold is still reserved.",
    );
    await expect(pane).toContainText(
      "No result: the run stopped while move was in doubt.",
    );
    // Settings: the transaction's own, read only, Give up undoing after among them.
    await details.dialog.getByRole("tab", { name: "Settings" }).click();
    const settings = details.dialog.locator(".settings-form");
    for (const label of [
      "Business key",
      "Hold reservations for",
      "Undo retries",
      "Give up undoing after",
    ])
      await expect(settings).toContainText(label);
    await expect(
      settings.locator(
        "input:not([disabled]):not([readonly]), select:not([disabled]), textarea:not([disabled]):not([readonly])",
      ),
    ).toHaveCount(0);
    // The business key's mapping, never the value it read in this run.
    const key = settings.locator(".param", { hasText: "Business key" }).first();
    await expect(key).toContainText("moveId");
    const shown = await key.evaluate((element) =>
      [
        element.textContent ?? "",
        ...[...element.querySelectorAll("*")].flatMap((node) => [
          node.getAttribute("title") ?? "",
          node.getAttribute("aria-label") ?? "",
          node.getAttribute("aria-description") ?? "",
        ]),
      ].join("\n"),
    );
    expect(shown).not.toContain("m-0193");
    expect(await page.locator("body").innerText()).not.toContain(BUSINESS_KEY);

    await details.dialog.getByRole("tab", { name: "Parameters" }).click();
    await (await outputPane(details))
      .getByRole("button", { name: "Open in Operate" })
      .click();
    await expect(page).toHaveURL(
      new RegExp(`/operate/incidents\\?incident=${INCIDENT}$`),
    );
    expect(errors).toEqual([]);
  });

  test("a run whose transactions are refused says so, and draws no transaction words", async ({
    page,
  }) => {
    const { runs, errors } = await openTab(page);
    const canvas = await choose(page, runs, IDS.refused);
    await expect(runs.canvas.locator(".run-transactions-note")).toHaveText(
      "You can't see this run's transactions here.",
    );
    await expect(frameState(canvas)).toHaveCount(0);
    await expect(canvas.tile("hold")).not.toContainText("Released");
    await expect(canvas.tile("add")).not.toContainText("Undone");
    // The line comes from the version, so it stays.
    await expect(divider(canvas)).toHaveCount(1);
    expect(errors).toEqual([]);
  });
});

test.describe("a recorded run in doubt on a phone", () => {
  test.use({
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    isMobile: true,
    contextOptions: { reducedMotion: "reduce" },
  });

  test("its captions wrap whole, never cut", async ({ page }) => {
    const { runs, errors } = await openTab(page);
    await runs.row(IDS.doubt).click();
    const parts = runs.canvas.locator(".run-in-doubt-part");
    await expect(parts).toHaveCount(3);
    for (const part of await parts.all()) {
      const box = (await part.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(390);
    }
    const header = runs.canvas.locator(".run-header");
    expect(await splitParts(header)).toEqual([]);
    // "Following this run" has a line of its own, so the next line starts
    // with the step that failed, never with a separator.
    expect(await firstDoubtPart(header)).toEqual({
      indent: 0,
      belowFollow: true,
    });
    // Also where both would fit on one line.
    await page.setViewportSize({ width: 470, height: 844 });
    expect(await firstDoubtPart(header)).toEqual({
      indent: 0,
      belowFollow: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await runs.canvas.getByRole("button", { name: "Back to runs" }).click();
    const caption = runs.row(IDS.doubt).locator(".run-caption-line");
    await expect(caption).toHaveText("Failed at submit · move in doubt");
    const cut = await caption.evaluate(
      (element) => element.scrollWidth > element.clientWidth,
    );
    expect(cut).toBe(false);
    const row = (await runs.row(IDS.doubt).boundingBox())!;
    expect(row.height).toBeGreaterThanOrEqual(56);
    expect(errors).toEqual([]);
  });
});
