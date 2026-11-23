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
// A run that went on after a step failed, on the Runs tab: the list says
// "Succeeded with errors" and how many errors a running run handled, the
// filter With handled errors lists only such runs, the canvas marks the step
// "Failed, continued" with the way the run took into its error path, and
// step details in run mode open on the error banner. The platform is mocked.
import { expect, test, type Page } from "@playwright/test";
import type { StepFact } from "../../src/app/operate/run-contracts";
import { CanvasPage } from "./canvas-po";
import {
  openRunsTab,
  runFixture,
  RUN_IDS,
  type RunFixture,
  type RunsPlatform,
} from "./runs-po";
import { StepDetailsPage } from "./step-details-po";

const HANDLED = "4e2a91c7-5b7d-4e8f-9a0b-1c2d3e4f5a6b";
const CALLEE = "7c1d3e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f";
const action = (id: string, uses: string) => ({ id, kind: "action", uses });
/** lookup, then submit with its error path (notify-team, flag-request), then summarize. */
const errorWorkflow = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "invoice-approval", version: "1.3.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [
      action("lookup", "sql.lookup@1.0.0"),
      {
        ...action("submit", "crm.lookup@2.0.0"),
        onError: "errorOutput",
        errorPath: {
          steps: [
            action("notify-team", "crm.lookup@2.0.0"),
            {
              id: "flag-request",
              kind: "transform",
              value: { literal: { flagged: true } },
            },
          ],
          output: { ref: "/steps/flag-request/output" },
        },
      },
      { id: "summarize", kind: "transform", value: { literal: {} } },
    ],
    output: { literal: {} },
  },
};

function fact(
  nodeId: string,
  status: StepFact["status"],
  extra: Partial<StepFact> = {},
): StepFact {
  return {
    ...runFixture("succeeded").facts[0],
    node_id: nodeId,
    instance_key: "",
    iteration: [],
    kind: "action",
    status,
    attempts: 1,
    error_code: null,
    ...extra,
  };
}
/** A run of the workflow in which submit failed with `code` and its error path ran. */
function handledRun(id: string, code: string): RunFixture {
  const base = runFixture("succeeded");
  const summary = { ...base.summary, id, handled_errors: 1 };
  const state = base.view["state"] as Record<string, unknown>;
  const result = { flagged: true };
  return {
    summary,
    view: {
      ...base.view,
      id,
      state: {
        ...state,
        steps: {
          lookup: { output: { rows: 1 } },
          submit: {
            output: result,
            error: {
              code,
              message: "The service refused the request.",
              outcome: "failed",
              attempts: 1,
              instanceKey: "submit",
            },
          },
          summarize: { output: {} },
        },
      },
    },
    facts: [
      fact("lookup", "succeeded", { output: { rows: 1 } }),
      fact("submit", "failed", {
        error_code: code,
        handled: "errorOutput",
        output: result,
      }),
      fact("notify-team", "succeeded", { output: { ok: true } }),
      fact("flag-request", "succeeded", { kind: "transform", output: result }),
      fact("summarize", "succeeded", { kind: "transform", output: {} }),
    ],
    versionId: base.versionId,
  };
}
/** Still going, two errors handled so far. */
function runningRun(): RunFixture {
  const base = runFixture("running");
  return { ...base, summary: { ...base.summary, handled_errors: 2 } };
}

async function openRuns(page: Page) {
  return openRunsTab(page, {
    runs: [
      handledRun(HANDLED, "REQUEST_REFUSED"),
      handledRun(CALLEE, "request-refused"),
      runningRun(),
      runFixture("failed"),
    ],
    workflow: errorWorkflow,
    before: async (platform: RunsPlatform) => {
      await page.route("**/projects/*/workflows/*/export", (route) => {
        platform.requests.push(new URL(route.request().url()));
        return route.fulfill({
          json: { id: "version", document: errorWorkflow },
        });
      });
    },
  });
}

test.describe("handled failures on the Runs tab at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the canvas shows the step Failed, continued and the way the run took into its error path", async ({
    page,
  }) => {
    const { runs } = await openRuns(page);
    await runs.row(HANDLED).click();
    const header = runs.canvas.locator(".run-header");
    await expect(header.locator(".run-pill")).toHaveText(
      "Succeeded with errors",
    );
    await expect(header.locator(".run-handled")).toHaveText(
      "1 error handled · submit: REQUEST_REFUSED",
    );
    await expect(
      header.getByRole("button", { name: "Debug in editor" }),
    ).toBeVisible();

    const canvas = new CanvasPage(page);
    const submit = canvas.tile("submit");
    await expect(submit.locator(".tile-badge")).toHaveAttribute(
      "data-badge",
      "handled",
    );
    // The badge's tooltip says what failed and how the run went on; the
    // tile's name says it too, with the code.
    await expect(submit.locator(".tile-body")).toHaveAttribute(
      "title",
      "submit, Call an action. Failed: REQUEST_REFUSED, The service refused the request. The run continued (On error: Continue with error output).",
    );
    await expect(submit.locator(".tile-body")).toHaveAttribute(
      "aria-label",
      /, failed and continued, REQUEST_REFUSED,/,
    );
    await expect(submit.locator(".tile-output")).toHaveText("REQUEST_REFUSED");
    for (const edge of [
      "submit>submit:path:errorPath",
      "submit:path:errorPath>notify-team",
      "flag-request>$join:submit",
    ])
      await expect(canvas.edgeLine(edge)).toHaveClass(/\btaken\b/);
    await expect(canvas.edgeLine("submit>submit:path:success")).not.toHaveClass(
      /\btaken\b/,
    );
    await expect(
      canvas.tile("notify-team").locator(".tile-badge"),
    ).toHaveAttribute("data-badge", "done");
  });

  test("step details in run mode open on the error banner, then the error path's result", async ({
    page,
  }) => {
    const { runs } = await openRuns(page);
    await runs.row(HANDLED).click();
    await expect(runs.canvas.locator(".run-handled")).toBeVisible();
    const details = new StepDetailsPage(page);
    await details.open("submit");
    const pane = details.dialog.locator('[data-region="output"]');
    await expect(pane.locator(".pane-sub .pill").first()).toHaveText(
      "Failed, continued",
    );
    await expect(pane.locator(".pane-sub .pane-source-tag")).toHaveText(
      "From run 4e2a91c7",
    );
    await expect(pane.locator("weave-error-banner p")).toHaveText([
      "This step failed: REQUEST_REFUSED. The service refused the request.",
      "Outcome: Didn't happen. Attempts: 1.",
      "The run continued (On error: Continue with error output).",
    ]);
    // The run's failure is no "failed in this run" notice: the run went on.
    await expect(pane.locator(".pane-run-notice")).toHaveCount(0);
    await expect(pane.locator(".pane-result-caption")).toHaveText(
      "Result on error · from flag-request",
    );
  });

  test("a code the run gave in any shape shows as text", async ({ page }) => {
    const { runs } = await openRuns(page);
    await runs.row(CALLEE).click();
    const canvas = new CanvasPage(page);
    const submit = canvas.tile("submit");
    await expect(submit.locator(".tile-body")).toHaveAttribute(
      "title",
      /\. Failed: request-refused, The service refused the request\./,
    );
    await expect(submit.locator(".tile-body")).toHaveAttribute(
      "aria-label",
      /, failed and continued, request-refused,/,
    );
    await expect(submit.locator(".tile-output")).toHaveText("request-refused");
    await expect(runs.canvas.locator(".run-handled")).toHaveText(
      "1 error handled · submit: request-refused",
    );
    const details = new StepDetailsPage(page);
    await details.open("submit");
    await expect(
      details.dialog
        .locator('[data-region="output"] weave-error-banner p')
        .first(),
    ).toHaveText(
      "This step failed: request-refused. The service refused the request.",
    );
  });
});
