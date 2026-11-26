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
// A workflow that reacts to a transaction, from end to end: notify-team gets
// its Platform event trigger for transactions of move-between-systems that
// need attention, maps the event's business key, runs on a sample event, is
// published and activated (its trigger is created), then a version that also
// reacts to undone transactions replaces that trigger, and Starts shows the
// run a transaction started, which Open run opens in the Runs tab. The
// platform is mocked; the event's fields, the local checks and Execute are the
// repository's own Python.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { parse, stringify } from "yaml";
import { FakePlatform, requested } from "../event-platform";
import {
  activate,
  activationDialog,
  eventRows,
  publish,
  routeEventTriggers,
  showStarts,
} from "./event-activation-po";
import { declared, eventTile, LOCK, spec } from "./event-trigger-po";
import { executions } from "./execute-po";
import {
  hasPython,
  localAuthoring,
  routePlatformEvent,
  routeValidate,
} from "./local-authoring";
import { Activations } from "./platform-activations";
import { routeRuns, runFixture, runsPage, type RunFixture } from "./runs-po";
import {
  addCommonStep,
  openStepFixture,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";
import { allCapabilities, connected, showView } from "./support";

const RUN = "9b1c4e2a-1d2e-4f3a-8b4c-5d6e7f8a9b0c";
const SOURCE = "3fa2c1d0-6b5e-4d7c-9a8f-2e1d0c9b8a7f";
/** The activation of each version published here, as the mocked platform names them. */
const ACTIVATIONS = ["a-old", "a-new"] as const;

interface Version {
  id: string;
  name: string;
  version: string;
  document: Record<string, unknown>;
}
/** The project and the environment: what is published, active and triggered in them. */
async function project(page: Page) {
  const platform = new FakePlatform();
  const activations = new Activations();
  const versions: Version[] = [];
  const runs = await routeRuns(page, [], { unavailable: false });
  const base = "**/projects/project/workflows";
  await page.route(base, (route) => {
    const body = route.request().postDataJSON() as { source: string };
    const document = parse(body.source) as Record<string, unknown>;
    const metadata = document["metadata"] as { name: string; version: string };
    versions.push({
      id: `${versions.length + 1}`.repeat(8) + "-1111-4111-8111-111111111111",
      ...metadata,
      document,
    });
    const { document: _document, ...made } = versions.at(-1)!;
    return route.fulfill({
      status: 201,
      json: { ...made, digest: "e".repeat(64) },
    });
  });
  await page.route(`${base}?*`, (route) =>
    route.fulfill({
      json: {
        items: versions.map(({ id, name, version }) => ({ id, name, version })),
        next_cursor: null,
      },
    }),
  );
  await page.route(`${base}/*`, (route) =>
    route.fulfill({
      json: versions.find((item) => route.request().url().endsWith(item.id)),
    }),
  );
  await page.route(`${base}/*/export`, (route) => {
    const found = versions.find((item) =>
      route.request().url().includes(`/${item.id}/`),
    )!;
    return route.fulfill({
      json: {
        id: found.id,
        document: found.document,
        artifact: {
          executable: {
            features: ["flow.eventTrigger"],
            connections: {},
            dependencies: [],
          },
        },
      },
    });
  });
  await page.route(
    "**/environments/development/activations?*",
    activations.answerList,
  );
  await page.route("**/environments/development/activations", (route) =>
    route.fulfill(
      activations.create(
        route.request(),
        "notify-team",
        ACTIVATIONS[versions.length - 1]!,
      ),
    ),
  );
  await page.route("**/environments/development/activations/*", (route) =>
    route.fulfill({
      json: activations.items.find((item) =>
        route.request().url().endsWith(`/${item.id}`),
      ),
    }),
  );
  await routeEventTriggers(page, platform);
  await page.route(/\/projects\/[^/]+\/catalog$/, (route) =>
    route.fulfill({ json: LOCK }),
  );
  return { platform, activations, versions, runs: runs.runs };
}

/** The run the event started, as the platform answers it. */
function reaction(versionId: string): RunFixture {
  const run = runFixture("succeeded");
  return {
    ...run,
    versionId,
    facts: [],
    summary: {
      ...run.summary,
      id: RUN,
      origin: "event",
      business_key: "REF-2071",
      workflow: {
        name: "notify-team",
        version: "1.1.0",
        definition_version_id: versionId,
      },
    },
    view: {
      ...run.view,
      id: RUN,
      activation: {
        id: "a-new",
        revision: 2,
        name: "notify-team",
        request: { version_id: versionId },
      },
    },
  };
}
const row = (dialog: Locator) => eventRows(dialog);

test.describe("a workflow that reacts to a transaction at 1440x900", () => {
  test.skip(!hasPython, "needs the repository's Python");
  test.use({ viewport: { width: 1440, height: 900 } });

  test("from the trigger to the run a transaction started", async ({
    page,
  }) => {
    test.setTimeout(150_000);
    let world!: Awaited<ReturnType<typeof project>>;
    const details = await openStepFixture(
      page,
      yamlFile(
        "notify-team.yaml",
        stringify({
          apiVersion: "weave/v1alpha1",
          kind: "Workflow",
          metadata: { name: "notify-team", version: "1.0.0" },
          spec: {
            inputSchema: { type: "object" },
            outputSchema: {},
            steps: [
              {
                id: "send",
                kind: "transform",
                value: { object: { reference: { literal: "" } } },
              },
            ],
            output: { ref: "/steps/send/output" },
          },
        }),
      ),
      async (p) => {
        await routePlatformEvent(p);
        const result = await connected(p, {
          capabilities: [...allCapabilities, "trigger.manage"],
          environmentName: "development",
        });
        await routeValidate(p);
        await withLanguageFeatures(p, ["flow.eventTrigger"]);
        world = await project(p);
        return result;
      },
    );
    const calls = await localAuthoring(page);
    const { platform, activations, versions, runs } = world;

    // The trigger: transactions of move-between-systems that need attention.
    await addCommonStep(page, "Platform event", "Add a step after send");
    const trigger = details.dialogFor("Platform event trigger");
    await expect(
      trigger.getByRole("checkbox", { name: "A transaction needs attention" }),
    ).toBeChecked();
    await trigger.getByRole("radio", { name: "Only these workflows" }).check();
    await trigger.getByRole("combobox", { name: "Add a workflow" }).fill("mo");
    await page
      .getByRole("listbox", { name: "Workflow suggestions" })
      .getByRole("option", { name: /move-between-systems/ })
      .click();
    // A sample event to run with.
    const pane = trigger.getByRole("region", { name: "Input data" });
    await pane.getByRole("button", { name: "Generate sample" }).click();
    await expect(
      pane.getByText("Sample ready. Execute workflow starts from it."),
    ).toBeVisible();
    await details.close();
    await expect(eventTile(page)).toContainText("Needs attention · 1 workflow");
    expect((await spec(page))["inputSchema"]).toEqual({
      $ref: "weave.platform-event-v1",
    });

    // send takes the event's business key.
    await details.open("send");
    const key = details.dialog.locator(
      '[data-ref="/input/transaction/business_key"]',
    );
    await expect(key).toContainText('"REF-1029"');
    await key.dragTo(
      details.dialog.getByRole("textbox", { name: "Value", exact: true }),
    );
    await expect(
      details.dialog.getByRole("button", {
        name: "reference Platform event transaction business_key",
      }),
    ).toBeVisible();
    await details.close();
    await page
      .locator(".editor-bar")
      .getByRole("button", { name: "Execute workflow" })
      .click();
    await expect.poll(() => executions(calls).length).toBe(1);
    expect(executions(calls)[0]!.body).toMatchObject({
      input: { type: "transaction.in_doubt" },
    });
    await expect(
      page.getByRole("region", { name: "Run log" }).locator(".run-log-status"),
    ).toHaveText("Completed · 1 step");

    // Activated: its trigger is created, as the version declares it.
    await publish(page);
    let dialog = await activationDialog(page, "1.0.0");
    await expect(row(dialog)).toHaveText([
      /^\s*Create platform event trigger notify-team\s*For Needs attention · from move-between-systems\.\s*$/,
    ]);
    await activate(dialog);
    await expect(dialog).toHaveCount(0);
    expect(platform.enabled()).toMatchObject([
      {
        name: "notify-team",
        activation_id: "a-old",
        events: ["transaction.in_doubt"],
        source_workflows: ["move-between-systems"],
      },
    ]);

    // 1.1.0 reacts to undone transactions too: the trigger is replaced.
    await showView(page, "Source");
    const source = page.getByRole("textbox", { name: "Workflow source" });
    await source.fill(
      (await source.inputValue()).replace("version: 1.0.0", "version: 1.1.0"),
    );
    await showView(page, "Designer");
    await page
      .getByRole("dialog", { name: "Apply the changes typed in Source?" })
      .getByRole("button", { name: "Apply changes", exact: true })
      .click();
    await eventTile(page).locator(".tile-body").dblclick();
    await trigger
      .getByRole("checkbox", { name: "A transaction was undone" })
      .check();
    await details.close();
    expect(await declared(page)).toEqual({
      events: ["transaction.compensated", "transaction.in_doubt"],
      from: ["move-between-systems"],
    });
    await publish(page);
    dialog = await activationDialog(page, "1.1.0");
    await expect(row(dialog).getByRole("definition")).toHaveText([
      "Needs attention · from move-between-systems",
      "Needs attention and Undone · from move-between-systems",
    ]);
    await activate(dialog);
    await expect(dialog).toHaveCount(0);
    expect(activations.ifMatch).toEqual([null, '"1"']);
    expect(platform.writes()).toEqual([
      "POST /event-triggers",
      "POST /event-triggers",
      "POST /event-triggers/t-made-1/disable",
    ]);
    expect(platform.enabled()).toMatchObject([
      { name: "notify-team-2", activation_id: "a-new" },
    ]);

    // A transaction needs attention: Starts shows the run it started.
    platform.starts.set("t-made-2", [
      {
        ...requested("s-1"),
        trigger_id: "t-made-2",
        status: "started",
        run_id: RUN,
        source_run_id: SOURCE,
      },
    ]);
    runs.push(reaction(versions[1]!.id));
    const { panel } = await showStarts(page);
    await expect(panel).toContainText(
      /Trigger notify-team-2\s*·\s*reacts for 1\.1\.0/,
    );
    await expect(
      panel.getByRole("list", { name: "Starts" }).getByRole("listitem"),
    ).toHaveText([
      /Started run 9b1c4e2a\s*Needs attention in run 3fa2c1d0 of move-between-systems/,
    ]);
    await panel.getByRole("button", { name: "Open run 9b1c4e2a" }).click();
    await expect(
      page.getByRole("tab", { name: "Runs", exact: true }),
    ).toHaveAttribute("aria-selected", "true");
    const shown = runsPage(page);
    await expect(shown.canvas.locator(".run-header h2")).toHaveText(
      "Run 9b1c4e2a",
    );
    await expect(shown.canvas.locator(".run-header")).toContainText(
      "Succeeded",
    );
    await expect(shown.row(RUN)).toContainText("Platform event");
    await expect(shown.row(RUN)).toContainText("1.1.0");
  });
});
