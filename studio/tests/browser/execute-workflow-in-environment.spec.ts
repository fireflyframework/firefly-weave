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
// Execute workflow in <environment>, from the editor bar's split button and
// the trigger's step details: the saved draft runs for real as a test run on
// a test activation, after a question when steps change data there; the run
// opens in the Runs tab, marked Test; an activation about to expire is
// replaced on the next Execute; with test calls off the entry says why.
import { expect, test, type Page, type Request } from "@playwright/test";
import { PRODUCTION, crmWorkflow, openConnected } from "./connections-po";
import { parse } from "yaml";
import { RUN_IDS, VERSIONS, routeRuns, runFixture } from "./runs-po";
import { routeStepTests } from "./step-tests-po";
import { showView } from "./support";

test.use({ viewport: { width: 1440, height: 900 } });

const workflow = crmWorkflow();

interface Activations {
  creates: Request[];
  runs: Request[];
}
/**
 * The environment's test activations: each create answers the next scripted
 * answer (the last repeats), each run starts the Test run of the fixtures.
 */
async function routeActivations(
  page: Page,
  creates: { status: number; json: unknown }[],
): Promise<Activations> {
  const recorded: Activations = { creates: [], runs: [] };
  await page.route("**/environments/*/test-activations", (route) => {
    recorded.creates.push(route.request());
    const answer =
      creates[Math.min(recorded.creates.length - 1, creates.length - 1)];
    return route.fulfill(answer);
  });
  await page.route("**/environments/*/test-activations/*/runs", (route) => {
    recorded.runs.push(route.request());
    return route.fulfill({
      status: 201,
      json: { id: RUN_IDS.test, status: "running", test: true },
    });
  });
  await page.route("**/environments/*/test-activations/*", (route) =>
    route.request().method() === "DELETE"
      ? route.fulfill({ status: 204, body: "" })
      : route.fallback(),
  );
  return recorded;
}
const created = (id: string, expiresIn: number) => ({
  status: 201,
  json: {
    id,
    activation_id: `activation-${id}`,
    artifact_digest: "a".repeat(64),
    side_effect: "read_only",
    expires_at: new Date(Date.now() + expiresIn).toISOString(),
  },
});
/** The Runs tab draws the Test run over the draft: its version is the draft's own workflow. */
async function testRunVersion(page: Page) {
  await page.route(`**/projects/*/workflows/${VERSIONS.test}/export`, (route) =>
    route.fulfill({
      json: { id: VERSIONS.test, document: parse(workflow.buffer.toString()) },
    }),
  );
}
/** The Test run, as the order-intake workflow's run. */
function testRun() {
  const run = runFixture("test");
  const workflowOf = {
    name: "order-intake",
    version: "1.0.0+test.d3c29f400322",
  };
  return {
    ...run,
    summary: {
      ...run.summary,
      workflow: { ...run.summary.workflow, ...workflowOf },
    },
  };
}
const moreWays = (page: Page) =>
  page
    .locator(".editor-toolbar")
    .getByRole("button", { name: "More ways to execute" });

test("Execute workflow in Production runs the saved draft as a test run and opens it in the Runs tab", async ({
  page,
}) => {
  await openConnected(page, { bindable: { [PRODUCTION]: [] } }, workflow);
  const steps = await routeStepTests(page, { starts: [], views: [] });
  await routeRuns(page, [testRun()]);
  await testRunVersion(page);
  const activations = await routeActivations(page, [
    created("ta-1", 15 * 60_000),
  ]);
  await moreWays(page).click();
  const item = page.getByRole("menuitem", {
    name: /^Execute workflow in Production/,
  });
  await expect(item).toContainText(
    "Runs the whole workflow for real, as a test run",
  );
  await item.click();
  await expect.poll(() => activations.runs.length, { timeout: 10_000 }).toBe(1);
  // The draft was saved first, and the activation is of what was saved.
  expect(steps.drafts.length).toBeGreaterThan(0);
  const body = activations.creates[0].postDataJSON() as Record<string, unknown>;
  expect(body["draft_revision"]).toBe(steps.drafts.length);
  expect(activations.runs[0].postDataJSON()).toEqual({
    input: { customerId: expect.any(String) },
  });
  await expect(page.locator("#editor-view")).toHaveAttribute(
    "aria-labelledby",
    "view-tab-runs",
  );
  // The run is chosen, and its row in the list says Test.
  await expect(page.locator(".runs-canvas")).toContainText(
    `Run ${RUN_IDS.test.slice(0, 8)}`,
  );
  await expect(
    page.locator(`.runs-list [data-run="${RUN_IDS.test}"]`),
  ).toContainText("Test");
});

test("steps that change data are named before they run, and Cancel runs nothing", async ({
  page,
}) => {
  await openConnected(page, { bindable: { [PRODUCTION]: [] } }, workflow);
  await routeStepTests(page, { starts: [], views: [] });
  await routeRuns(page, [testRun()]);
  await testRunVersion(page);
  const activations = await routeActivations(page, [
    {
      status: 409,
      json: {
        code: "WV-STEPTEST-CONFIRM",
        message: "Confirm the side effect.",
        result: { side_effect: "non_idempotent", steps: ["crm-lookup"] },
      },
    },
    created("ta-1", 15 * 60_000),
  ]);
  const ask = async () => {
    await moreWays(page).click();
    await page
      .getByRole("menuitem", { name: /^Execute workflow in Production/ })
      .click();
    const dialog = page.getByRole("dialog", {
      name: "Execute steps that change data?",
    });
    await expect(dialog).toContainText(
      "crm-lookup changes data in Production. Executing the workflow repeats the change.",
    );
    await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
    return dialog;
  };
  let dialog = await ask();
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toHaveCount(0);
  expect(activations.runs).toHaveLength(0);

  // The next answer asks again (the first create answers 409 once more), and Run it runs.
  activations.creates.length = 0;
  dialog = await ask();
  await dialog.getByRole("button", { name: "Run it" }).click();
  await expect.poll(() => activations.runs.length).toBe(1);
  expect(activations.creates.at(-1)!.postDataJSON()).toMatchObject({
    acknowledged_side_effect: "non_idempotent",
  });
});

test("an activation about to expire is replaced on the next Execute", async ({
  page,
}) => {
  await openConnected(page, { bindable: { [PRODUCTION]: [] } }, workflow);
  await routeStepTests(page, { starts: [], views: [] });
  await routeRuns(page, [testRun()]);
  await testRunVersion(page);
  // It expires in 30 s: inside the minute Studio keeps before expiry.
  const activations = await routeActivations(page, [created("ta-1", 30_000)]);
  for (const round of [1, 2]) {
    await moreWays(page).click();
    await page
      .getByRole("menuitem", { name: /^Execute workflow in Production/ })
      .click();
    await expect.poll(() => activations.runs.length).toBe(round);
    await expect(page.locator("#editor-view")).toHaveAttribute(
      "aria-labelledby",
      "view-tab-runs",
    );
    await showView(page, "Designer");
  }
  expect(activations.creates).toHaveLength(2);
});

test("with test calls off in Production the entry is disabled with the reason", async ({
  page,
}) => {
  await openConnected(
    page,
    { bindable: { [PRODUCTION]: [] }, policy: { [PRODUCTION]: "disabled" } },
    workflow,
  );
  await moreWays(page).click();
  const item = page.getByRole("menuitem", {
    name: /^Execute workflow in Production/,
  });
  await expect(item).toHaveAttribute("aria-disabled", "true");
  await expect(item).toContainText(
    "Test calls are off in Production. Choose an environment where they're on to execute the workflow there.",
  );
});

test("the trigger's step details offer Execute workflow in Production too", async ({
  page,
}) => {
  const { details } = await openConnected(
    page,
    { bindable: { [PRODUCTION]: [] } },
    workflow,
  );
  await details.trigger().dblclick();
  await expect(details.dialogFor("Manual form trigger")).toBeVisible();
  await details.dialog
    .getByRole("button", { name: "More ways to execute" })
    .click();
  await expect(
    page.getByRole("menuitem", { name: /^Execute workflow in Production/ }),
  ).toContainText("Runs the whole workflow for real, as a test run");
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 360, height: 780 } });

  test("Execute workflow in Production is in More, and the bar keeps More on screen", async ({
    page,
  }) => {
    await openConnected(page, { bindable: { [PRODUCTION]: [] } }, workflow);
    const toolbar = page.getByRole("toolbar", { name: "Workflow commands" });
    await expect(
      toolbar.getByRole("button", { name: "More ways to execute" }),
    ).toHaveCount(0);
    const more = toolbar.getByRole("button", { name: "More", exact: true });
    const box = (await more.boundingBox())!;
    expect(box.x + box.width).toBeLessThanOrEqual(360);
    await more.click();
    await expect(
      page.getByRole("menuitem", { name: /^Execute workflow in Production/ }),
    ).toContainText("Runs the whole workflow for real, as a test run");
  });
});
