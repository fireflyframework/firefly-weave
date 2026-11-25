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
// Listen for test event: a webhook trigger, connected with test calls on,
// gets a test URL on a test activation of the saved draft, shown with a
// signed cURL and Stop listening while its tile pulses (still, with reduced
// motion). The run a signed request starts brings its payload back as the
// workflow's test input (from a test call) and opens in the Runs tab; Stop
// listening retires the test URL. With test calls off it says so and offers
// Paste sample.
import { expect, test, type Page, type Request } from "@playwright/test";
import { PRODUCTION, crmWorkflow, openConnected } from "./connections-po";
import { parse } from "yaml";
import { RUN_IDS, VERSIONS, routeRuns, runFixture } from "./runs-po";
import { routeStepTests } from "./step-tests-po";
import { StepDetailsPage } from "./step-details-po";
import { allCapabilities, showView } from "./support";

test.use({ viewport: { width: 1440, height: 900 } });

const WEBHOOK = "trigger-1b2c3d4e";
const TEST_URL = "https://platform.test/hooks/test/7c1d";
const workflow = crmWorkflow();
const canvas = {
  schemaVersion: 2,
  kind: "weave.studio/canvas",
  triggers: [
    {
      id: WEBHOOK,
      kind: "webhook",
      name: "orders-in",
      secretRef: "orders-signing",
      testSecretRef: "orders-test",
    },
  ],
};

interface Listening {
  listens: Request[];
  retired: string[];
  /** The platform received a signed request: the activation's test run exists. */
  receive(): void;
}
/** The platform's test activations, its listen route, and the run a signed request starts. */
async function routeListening(page: Page, input: unknown): Promise<Listening> {
  let received = false;
  const recorded: Listening = {
    listens: [],
    retired: [],
    receive: () => (received = true),
  };
  const run = runFixture("test");
  await routeRuns(page, [
    {
      ...run,
      summary: {
        ...run.summary,
        id: RUN_IDS.test,
        workflow: { ...run.summary.workflow, name: "order-intake" },
      },
      view: {
        ...run.view,
        state: {
          ...(run.view["state"] as Record<string, unknown>),
          status: "running",
          input,
        },
      },
    },
  ]);
  // The run's version is this draft's own workflow, so the Runs tab draws it.
  await page.route(`**/projects/*/workflows/${VERSIONS.test}/export`, (route) =>
    route.fulfill({
      json: { id: VERSIONS.test, document: parse(workflow.buffer.toString()) },
    }),
  );
  await page.route("**/environments/*/run-summaries**", (route) => {
    const url = new URL(route.request().url());
    if (!url.searchParams.get("activation_id")) return route.fallback();
    return route.fulfill({
      json: {
        items: received
          ? [{ id: RUN_IDS.test, status: "running", test: true }]
          : [],
        next_cursor: null,
      },
    });
  });
  await page.route("**/environments/*/test-activations", (route) =>
    route.fulfill({
      status: 201,
      json: {
        id: "ta-1",
        activation_id: "a7a7a7a7-1111-4222-8333-444455556666",
        artifact_digest: "a".repeat(64),
        side_effect: "read_only",
        expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
      },
    }),
  );
  await page.route("**/environments/*/test-activations/*/listen", (route) => {
    recorded.listens.push(route.request());
    return route.fulfill({
      json: {
        test_url: TEST_URL,
        expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
      },
    });
  });
  await page.route("**/environments/*/test-activations/*", (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    recorded.retired.push(route.request().url().split("/").at(-1)!);
    return route.fulfill({ status: 204, body: "" });
  });
  return recorded;
}

/** The webhook workflow open, connected to Production, with its trigger's step details open. */
async function openWebhook(
  page: Page,
  policy?: "disabled",
  input: unknown = { customerId: "c-77" },
) {
  await openConnected(page, {
    bindable: { [PRODUCTION]: [] },
    // Listening creates a test URL: the step tester and trigger manager roles.
    capabilities: [
      ...allCapabilities,
      "compile",
      "step.test",
      "trigger.manage",
    ],
    ...(policy ? { policy: { [PRODUCTION]: policy } } : {}),
  });
  // Registered after connecting, so they answer before the fake host does.
  const listening = await routeListening(page, input);
  const steps = await routeStepTests(page, { starts: [], views: [] });
  await page.getByLabel("Choose a workflow file").setInputFiles([
    workflow,
    {
      name: "order-intake.canvas.json",
      mimeType: "application/json",
      buffer: Buffer.from(JSON.stringify(canvas)),
    },
  ]);
  await expect(page.locator(".editor-bar")).toBeVisible();
  const show = page.getByRole("button", { name: "Show canvas", exact: true });
  if (await show.isVisible()) await show.click();
  await page
    .locator(`.tile-node[data-tile="$trigger:${WEBHOOK}"] .tile-body`)
    .dblclick();
  const details = new StepDetailsPage(page);
  const dialog = details.dialogFor("Webhook trigger");
  await expect(dialog).toBeVisible();
  return { dialog, steps, listening };
}
const tileShape = (page: Page) =>
  page.locator(`.tile-node[data-tile="$trigger:${WEBHOOK}"] .tile-shape`);

test("a webhook listens on a test URL with a signed cURL, and the event's payload becomes the test input", async ({
  page,
}) => {
  const { dialog, steps, listening } = await openWebhook(page);
  // Not activated yet: the production URL comes with the activation.
  await expect(
    dialog.getByText(
      "The production URL appears once you activate this workflow in Production.",
    ),
  ).toBeVisible();
  await dialog
    .locator(".sd-actions")
    .getByRole("button", { name: "Listen for test event", exact: true })
    .click();
  await expect(dialog.getByRole("status")).toContainText(
    "Listening for test event",
  );
  await expect(
    dialog.getByRole("textbox", { name: "Make a POST request to:" }),
  ).toHaveValue(TEST_URL);
  const curl = dialog.getByLabel("Signed cURL");
  await expect(curl).toContainText(`curl -sS -X POST '${TEST_URL}'`);
  await expect(curl).toContainText("X-Weave-Signature: $SIGNATURE");
  // It names the test secret to export, and how to set that secret's value.
  await expect(curl).toContainText(
    "with the value of the test secret orders-test first",
  );
  // The platform here isn't on this computer: no local command to set it.
  await expect(dialog.getByLabel("Set the test secret")).toHaveCount(0);
  // Listen and Stop listening live in the header only.
  await expect(
    dialog.getByRole("button", { name: "Stop listening", exact: true }),
  ).toHaveCount(1);
  // The listen request names the webhook's test secret, never its production one.
  expect(listening.listens.map((request) => request.postDataJSON())).toEqual([
    { secret_ref: "orders-test" },
  ]);
  await expect(tileShape(page)).toHaveClass(/\blistening\b/);

  listening.receive();
  await expect(page.locator("#editor-view")).toHaveAttribute(
    "aria-labelledby",
    "view-tab-runs",
    { timeout: 10_000 },
  );
  // Step details shows the run the event started, as Execute's run is followed.
  await expect(dialog).toContainText(
    `You're looking at run ${RUN_IDS.test.slice(0, 8)}`,
  );
  // Reading a run, nothing listens: its input never becomes a cURL to paste.
  await expect(
    dialog.getByRole("button", { name: "Listen for test event" }),
  ).toHaveCount(0);
  // The payload is the workflow's test input now, kept as from a test call.
  await expect
    .poll(() =>
      steps.documents
        .map((request) => request.postDataJSON() as Record<string, unknown>)
        .map((body) => (body["document"] as Record<string, unknown>)?.["input"])
        .filter(Boolean)
        .at(-1),
    )
    .toMatchObject({
      value: { customerId: "c-77" },
      source: "test-call",
      runId: RUN_IDS.test,
    });
  await expect(tileShape(page)).not.toHaveClass(/\blistening\b/);
});

test("a real platform event a test call brings becomes the test input with sample IDs", async ({
  page,
}) => {
  const REAL = {
    event: "a3d0c1b2-1111-4222-8333-000000000001",
    run: "a3d0c1b2-1111-4222-8333-000000000002",
    incident: "a3d0c1b2-1111-4222-8333-000000000003",
  };
  const { dialog, steps, listening } = await openWebhook(page, undefined, {
    customerId: "c-77",
    event: {
      version: "weave/platform-event-v1",
      event_id: REAL.event,
      type: "transaction.in_doubt",
      transaction: {
        run_id: REAL.run,
        incident_id: REAL.incident,
        instance_key: "move",
        business_key: "REF-4410",
        state: "in_doubt",
      },
    },
  });
  await dialog
    .locator(".sd-actions")
    .getByRole("button", { name: "Listen for test event", exact: true })
    .click();
  await expect(
    dialog.getByRole("textbox", { name: "Make a POST request to:" }),
  ).toHaveValue(TEST_URL);
  listening.receive();
  const saved = () =>
    steps.documents.map((request) => request.postData() ?? "");
  await expect
    .poll(() => saved().filter((body) => body.includes("REF-4410")).length, {
      timeout: 10_000,
    })
    .toBeGreaterThan(0);
  expect(saved().at(-1)).toContain('"source":"test-call"');
  for (const id of Object.values(REAL))
    expect(saved().filter((body) => body.includes(id))).toEqual([]);
});

test("Stop listening retires the test URL, and the tile stops pulsing", async ({
  page,
}) => {
  const { dialog, listening } = await openWebhook(page);
  await dialog
    .getByRole("button", { name: "Listen for test event", exact: true })
    .click();
  await expect(
    dialog.getByRole("textbox", { name: "Make a POST request to:" }),
  ).toHaveValue(TEST_URL);
  await dialog
    .getByRole("button", { name: "Stop listening", exact: true })
    .click();
  await expect.poll(() => listening.retired).toEqual(["ta-1"]);
  await expect(
    dialog.getByRole("textbox", { name: "Make a POST request to:" }),
  ).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: "Listen for test event", exact: true }),
  ).toHaveCount(1);
  await expect(tileShape(page)).not.toHaveClass(/\blistening\b/);
});

test("with reduced motion the listening tile is still", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const { dialog } = await openWebhook(page);
  await dialog
    .getByRole("button", { name: "Listen for test event", exact: true })
    .first()
    .click();
  await expect(tileShape(page)).toHaveClass(/\blistening\b/);
  expect(
    await tileShape(page).evaluate(
      (node) => getComputedStyle(node).animationName,
    ),
  ).toBe("none");
});

test("with test calls off the webhook offers Paste sample, and the header executes the workflow", async ({
  page,
}) => {
  const { dialog } = await openWebhook(page, "disabled");
  await expect(
    dialog.getByText(
      "Test calls are off in Production. Paste a sample payload instead.",
    ),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Paste sample", exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Listen for test event" }),
  ).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: /^Execute workflow/ }).first(),
  ).toBeVisible();
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 360, height: 780 } });

  test("Listen for test event shows the Test event pane, where the test URL is", async ({
    page,
  }) => {
    const { dialog, listening } = await openWebhook(page);
    await dialog
      .locator(".sd-actions")
      .getByRole("button", { name: "Listen for test event", exact: true })
      .click();
    await expect(
      dialog
        .getByRole("tablist", { name: "Step details panes" })
        .getByRole("tab", { name: "Test event" }),
    ).toHaveAttribute("aria-selected", "true");
    await expect(
      dialog.getByRole("textbox", { name: "Make a POST request to:" }),
    ).toBeVisible();
    // The header sits with Parameters on a phone: the pane can stop
    // listening, from right under its status, in view as listening starts.
    const stop = dialog
      .locator(".trigger-event")
      .getByRole("button", { name: "Stop listening", exact: true });
    await expect(stop).toBeInViewport();
    await stop.click();
    await expect.poll(() => listening.retired).toEqual(["ta-1"]);
  });
});

test("a changed draft retires the test URL and listening says why; leaving the editor retires it too", async ({
  page,
}) => {
  const { dialog, listening } = await openWebhook(page);
  const listen = () =>
    dialog
      .locator(".sd-actions")
      .getByRole("button", { name: "Listen for test event", exact: true })
      .click();
  await listen();
  await expect(
    dialog.getByRole("textbox", { name: "Make a POST request to:" }),
  ).toHaveValue(TEST_URL);
  // The workflow changes and is saved: the test URL ran the old draft.
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await showView(page, "Source");
  const source = page.getByRole("textbox", { name: "Workflow source" });
  await source.fill(
    (await source.inputValue()).replace("version: 1.0.0", "version: 1.0.1"),
  );
  await showView(page, "Designer");
  await page
    .getByRole("dialog", { name: "Apply the changes typed in Source?" })
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await page.keyboard.press("ControlOrMeta+s");
  await expect
    .poll(() => listening.retired, { timeout: 15_000 })
    .toEqual(["ta-1"]);
  await expect(tileShape(page)).not.toHaveClass(/\blistening\b/);
  await page
    .locator(`.tile-node[data-tile="$trigger:${WEBHOOK}"] .tile-body`)
    .dblclick();
  await expect(dialog).toContainText(
    "The workflow changed, so Studio stopped listening. Listen again to test the change.",
  );

  // Listening again, then leaving the editor, retires the new test URL.
  await listen();
  await expect(
    dialog.getByRole("textbox", { name: "Make a POST request to:" }),
  ).toHaveValue(TEST_URL);
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await page.getByRole("button", { name: "Home", exact: true }).click();
  await expect
    .poll(() => listening.retired, { timeout: 15_000 })
    .toEqual(["ta-1", "ta-1"]);
});
