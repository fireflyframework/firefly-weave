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
// Handling a step's failure from end to end. On this computer, as a builder
// does it: a Call an API step continues with error output, Send email goes on
// its error path, leaving that setting takes the path away and Undo brings it
// back, then a simulated failure makes Execute workflow run the path; the
// repository's own host code answers (localAuthoring) and its own language
// manifest says the platform continues after errors. On a platform (mocked):
// a recorded run that handled the same failure reads Succeeded with errors,
// the filter With handled errors lists it, and its canvas shows the way the
// run took.
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { parse, stringify } from "yaml";
import { repository } from "../python-path";
import { CanvasPage, openStoredWorkflow } from "./canvas-po";
import { applySource } from "./owned-actions";
import {
  hasPython,
  localAuthoring,
  routeBuiltinActions,
} from "./local-authoring";
import {
  openRunsTab,
  runFixture,
  RUN_IDS,
  type RunFixture,
  type RunsPlatform,
} from "./runs-po";
import { StepDetailsPage } from "./step-details-po";
import { sourceText } from "./support";
import { outputPane } from "./test-data-po";
import type { StepFact } from "../../src/app/operate/run-contracts";

/** The language manifest the Studio host serves, as the repository's Python writes it. */
const manifest = JSON.parse(
  readFileSync(
    join(repository, "studio/tests/fixtures/language/manifest.json"),
    "utf8",
  ),
).manifest as { features: string[] };

/** submit posts the request through an action the workflow owns; summarize follows. */
const INTAKE = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: request-intake, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      requestId: {type: string, default: r-1042}
  outputSchema: {}
  connections:
    service: {connector: weave-http@2.0.0, required: true}
    mail: {connector: weave-email@1.0.0, required: true}
  steps:
    - id: submit
      kind: action
      uses: request-intake.submit@1.0.0
      connection: service
      with:
        object:
          path:
            object:
              requestId: {ref: /input/requestId}
    - id: summarize
      kind: transform
      value: {literal: done}
  output: {literal: null}
`;
const INTAKE_CANVAS = {
  schemaVersion: 2,
  kind: "weave.studio/canvas",
  ownedActions: ["request-intake.submit"],
  actionRecipes: {
    "request-intake.submit": {
      kind: "http",
      method: "POST",
      pathTemplate: "/requests/{requestId}",
      statuses: [200],
      timeoutSeconds: 30,
    },
  },
};

/** What Send email answers once the mail server took the message. */
const SENT = {
  id: "e-1",
  conversation_id: "c-1",
  message_id: "m-1",
  state: "sent",
  accepted_recipients: ["team@example.com"],
  rejected_recipients: [],
};

/** The source Studio keeps for the draft on this computer. */
const stored = (page: Page) =>
  page.evaluate(
    () =>
      (
        JSON.parse(
          localStorage.getItem("local:weave.localDraft.d-intake") ?? "{}",
        ) as { source?: string }
      ).source ?? "",
  );
/** The steps of submit's error path, read from Source. */
async function errorPathOf(page: Page): Promise<string[]> {
  const source = parse(await sourceText(page)) as {
    spec: { steps: { id: string; errorPath?: { steps: { id: string }[] } }[] };
  };
  const submit = source.spec.steps.find((step) => step.id === "submit");
  return (submit?.errorPath?.steps ?? []).map((step) => step.id);
}

test.describe("handling a step's failure at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("continue with error output, add to the path, simulate the failure and execute", async ({
    page,
  }) => {
    test.skip(!hasPython, "needs the repository's Python");
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.route(
      /\/studio\/(?:contracts\/language|api\/api\/v1\/tenants\/[^/]+\/projects\/[^/]+\/language)$/,
      (route) => route.fulfill({ json: manifest }),
    );
    // Send email's action and its output fields, as the platform builds them.
    await routeBuiltinActions(page);
    const calls = await localAuthoring(page);
    const canvas = await openStoredWorkflow(page, {
      id: "d-intake",
      source: INTAKE,
      canvas: INTAKE_CANVAS,
    });
    const details = new StepDetailsPage(page);

    // Settings: On error starts at Stop the run; Continue with error
    // output writes the setting and an empty path as one undo step.
    await details.open("submit");
    await details.tab("Settings").click();
    const onError = details
      .field("onError")
      .getByRole("combobox", { name: "On error" });
    await expect(onError).toHaveValue("Stop the run");
    const before = await stored(page);
    const choose = async (name: RegExp) => {
      await onError.click();
      await page
        .getByRole("listbox", { name: "On error options" })
        .getByRole("option", { name })
        .click();
    };
    await choose(/^Continue with error output/);
    await expect(details.field("resultOnError")).toContainText(
      "Result on error",
    );
    await expect
      .poll(() => stored(page))
      .toMatch(
        /onError: errorOutput\n\s+errorPath:\n\s+steps: \[\]\n\s+output:\n\s+literal: null/,
      );
    await page.keyboard.press("ControlOrMeta+z");
    await expect(onError).toHaveValue("Stop the run");
    await expect.poll(() => stored(page)).toBe(before);
    await choose(/^Continue with error output/);
    await expect(onError).toHaveValue("Continue with error output");
    await details.close();

    // The tile has two outputs, Success and Error.
    await expect(canvas.branchLabel("submit/success")).toHaveText("Success");
    await expect(canvas.branchLabel("submit/errorPath")).toHaveText("Error");

    // The Error output's "+" adds Send email to the error path, not after submit.
    await canvas.insertTarget("Add a step to the error path of submit").click();
    await canvas.pick("Send email");
    await canvas.closeInspector();
    expect(await errorPathOf(page)).toEqual(["send-email"]);
    expect(await canvas.rootSteps()).toEqual(["submit", "summarize"]);

    // Continue takes the path away, says so, and Undo brings it back.
    await details.open("submit");
    await details.tab("Settings").click();
    await choose(/^Continue\s*The run goes on/);
    const toast = page.locator(".toast");
    await expect(toast.locator("strong")).toHaveText(
      "Removed the error path (1 step)",
    );
    await expect.poll(() => stored(page)).not.toContain("send-email");
    await toast.getByRole("button", { name: "Undo" }).click();
    await expect(onError).toHaveValue("Continue with error output");
    await expect.poll(() => stored(page)).toContain("- id: send-email");

    // Simulate a failure of submit; Output says it fails when executed.
    let pane = await outputPane(details);
    await pane.getByRole("button", { name: "More output actions" }).click();
    await page.getByRole("menuitem", { name: "Simulate a failure…" }).click();
    const dialog = page.getByRole("dialog", {
      name: "Simulate a failure of submit",
    });
    await dialog.getByLabel("Code").fill("REQUEST_REFUSED");
    await dialog.getByRole("button", { name: "Simulate failure" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(
      pane.locator("weave-failure-banner").getByRole("status"),
    ).toHaveText(
      "This step fails when you execute it in Studio. Real runs ignore it.",
    );
    await details.close();
    // The email's fields, from Source.
    const document = parse(await sourceText(page)) as {
      spec: { steps: { id: string; errorPath?: { steps: object[] } }[] };
    };
    const email = document.spec.steps[0].errorPath!.steps[0] as {
      with?: object;
    };
    email.with = {
      literal: {
        to: ["team@example.com"],
        subject: "A request was refused",
        text: "The service refused a request.",
      },
    };
    await applySource(page, stringify(document));
    // The email isn't sent in a test: its result is pinned. The host's check
    // of it stands in for the repository's check_test_data, which answers an
    // object of the action's output fields as fitting.
    await page.route("**/studio/local/testdata/check", (route) => {
      const { testData } = route.request().postDataJSON() as {
        testData: { pins?: object };
      };
      const pins = Object.keys(testData.pins ?? {});
      if (!pins.includes("send-email")) return route.fallback();
      return route.fulfill({
        json: {
          entries: pins.map((key) => ({ path: `/pins/${key}`, ok: true })),
        },
      });
    });
    await details.open("send-email");
    pane = await outputPane(details);
    await pane.getByRole("button", { name: "Set test output" }).click();
    await pane
      .locator(".output-editor")
      .getByRole("textbox")
      .first()
      .fill(JSON.stringify(SENT));
    await pane.getByRole("button", { name: "Save", exact: true }).click();
    await details.close();

    // Execute workflow: submit fails, continues, and its error path runs.
    await page
      .locator(".editor-bar")
      .getByRole("button", { name: "Execute workflow" })
      .click();
    await expect.poll(() => calls.length).toBeGreaterThan(0);
    const execute = calls.find((call) => call.path.endsWith("/debug/execute"));
    expect((execute?.body as { errors?: object }).errors).toEqual({
      "node:submit": { code: "REQUEST_REFUSED" },
    });
    const log = page.getByRole("region", { name: "Run log" });
    await expect(log.locator(".run-log-status")).toHaveText(
      "Succeeded with errors · 3 steps",
    );
    await expect(log.locator('[data-entry="send-email"]')).toHaveAccessibleName(
      /^send-email, in the error path of submit, finished/,
    );
    await expect(canvas.tile("submit").locator(".tile-badge")).toHaveAttribute(
      "data-badge",
      "handled",
    );
    await expect(canvas.tileBody("submit")).toHaveAccessibleName(
      /, failed and continued, REQUEST_REFUSED,/,
    );
    await expect(canvas.edgeLine("submit>submit:path:errorPath")).toHaveClass(
      /\btaken\b/,
    );
    await details.open("submit");
    pane = await outputPane(details);
    await expect(pane.locator(".pane-sub .pill").first()).toHaveText(
      "Failed, continued",
    );
    await expect(pane.locator("weave-error-banner p").first()).toContainText(
      "This step failed: REQUEST_REFUSED.",
    );
  });
});

/** submit's error path holds send-email; summarize follows. */
const RECORDED = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "invoice-approval", version: "1.3.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [
      {
        id: "submit",
        kind: "action",
        uses: "crm.lookup@2.0.0",
        onError: "errorOutput",
        errorPath: {
          steps: [
            { id: "send-email", kind: "action", uses: "crm.lookup@2.0.0" },
          ],
          output: { literal: null },
        },
      },
      { id: "summarize", kind: "transform", value: { literal: {} } },
    ],
    output: { literal: {} },
  },
};
const HANDLED = "4e2a91c7-5b7d-4e8f-9a0b-1c2d3e4f5a6b";
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
/** submit failed with REQUEST_REFUSED, its error path sent the email, summarize ran. */
function handledRun(): RunFixture {
  const base = runFixture("succeeded");
  const state = base.view["state"] as Record<string, unknown>;
  return {
    summary: { ...base.summary, id: HANDLED, handled_errors: 1 },
    view: {
      ...base.view,
      id: HANDLED,
      state: {
        ...state,
        steps: {
          submit: {
            output: null,
            error: {
              code: "REQUEST_REFUSED",
              message: "The service refused the request.",
              outcome: "failed",
              attempts: 1,
              instanceKey: "submit",
            },
          },
          "send-email": { output: SENT },
          summarize: { output: {} },
        },
      },
    },
    facts: [
      fact("submit", "failed", {
        error_code: "REQUEST_REFUSED",
        handled: "errorOutput",
        output: null,
      }),
      fact("send-email", "succeeded", { output: SENT }),
      fact("summarize", "succeeded", { kind: "transform", output: {} }),
    ],
    versionId: base.versionId,
  };
}

test.describe("a recorded run that handled the failure at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("reads Succeeded with errors, is listed by With handled errors, and shows the way it took", async ({
    page,
  }) => {
    const { runs, platform } = await openRunsTab(page, {
      runs: [handledRun(), runFixture("failed")],
      workflow: RECORDED,
      before: async (platform: RunsPlatform) => {
        await page.route("**/projects/*/workflows/*/export", (route) => {
          platform.requests.push(new URL(route.request().url()));
          return route.fulfill({ json: { id: "version", document: RECORDED } });
        });
      },
    });
    await expect(runs.row(HANDLED).locator(".run-status")).toHaveText(
      "Succeeded with errors",
    );
    await runs.filters.click();
    await page
      .getByRole("dialog", { name: "Filters" })
      .getByRole("checkbox", { name: "With handled errors" })
      .check();
    await page.getByRole("button", { name: "Done" }).click();
    expect(platform.listQueries().at(-1)?.get("has_handled_errors")).toBe(
      "true",
    );
    await expect(runs.row(RUN_IDS.failed)).toHaveCount(0);
    await runs.row(HANDLED).click();
    const canvas = new CanvasPage(page);
    await expect(canvas.tile("submit").locator(".tile-badge")).toHaveAttribute(
      "data-badge",
      "handled",
    );
    await expect(canvas.tileBody("submit")).toHaveAttribute(
      "aria-label",
      /, failed and continued, REQUEST_REFUSED,/,
    );
    for (const edge of [
      "submit>submit:path:errorPath",
      "submit:path:errorPath>send-email",
    ])
      await expect(canvas.edgeLine(edge)).toHaveClass(/\btaken\b/);
    await expect(canvas.edgeLine("submit>submit:path:success")).not.toHaveClass(
      /\btaken\b/,
    );
  });
});
