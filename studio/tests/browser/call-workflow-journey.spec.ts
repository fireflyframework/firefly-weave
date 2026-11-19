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
// Calling a workflow from end to end. On this computer, as a builder does it:
// notify-customer becomes callable through its Called by a workflow trigger,
// order-intake calls it with its input mapped, and Execute workflow runs both,
// the run log showing notify-customer's steps under the call; the repository's
// own host code answers (localAuthoring). On a platform (mocked): activating
// order-intake pins the current activation of the workflow it calls.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { parse, stringify } from "yaml";
import { executions } from "./execute-po";
import { localAuthoring } from "./local-authoring";
import {
  addCommonStep,
  openStepFixture,
  StepDetailsPage,
  withLanguageFeatures,
  withNewEditor,
} from "./step-details-po";
import { platform, versionId } from "./activation-po";
import { command, offline, sourceText } from "./support";

const notifyCustomer = stringify({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "notify-customer", version: "1.0.0" },
  spec: {
    inputSchema: {
      type: "object",
      required: ["customerId"],
      properties: { customerId: { type: "string" } },
    },
    outputSchema: { type: "object" },
    steps: [
      {
        id: "render",
        kind: "transform",
        value: { object: { to: { ref: "/input/customerId" } } },
      },
      { id: "send", kind: "transform", value: { literal: { sent: true } } },
    ],
    output: { ref: "/steps/send/output" },
  },
});
const orderIntake = stringify({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "order-intake", version: "1.2.0" },
  spec: {
    inputSchema: {
      type: "object",
      properties: { customerId: { type: "string" } },
    },
    outputSchema: { type: "object" },
    steps: [{ id: "fetch", kind: "transform", value: { ref: "/input" } }],
    output: { literal: {} },
  },
});
const MANIFEST = JSON.parse(
  readFileSync(resolve("tests/fixtures/language/manifest.json"), "utf8"),
) as Record<string, unknown>;
const file = (name: string, text: string) => ({
  name,
  mimeType: "application/yaml",
  buffer: Buffer.from(text),
});

test.use({ viewport: { width: 1440, height: 900 } });

test("a workflow made callable is called with its input mapped, and runs inside the run log", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const details = await openStepFixture(
    page,
    file("notify-customer.yaml", notifyCustomer),
    async (p) => {
      await offline(p);
      await withLanguageFeatures(p, ["flow.callWorkflow"]);
    },
  );
  const calls = await localAuthoring(page);

  // notify-customer becomes callable: its Called by a workflow trigger.
  await addCommonStep(page, "Called by a workflow", "Add a step after send");
  await expect(
    details.dialog.getByRole("radio", { name: "Any workflow in this project" }),
  ).toBeChecked();
  await details.close();
  expect(
    (parse(await sourceText(page)) as { spec: { callable?: unknown } }).spec
      .callable,
  ).toEqual({});

  // order-intake calls it: the version kept on this computer, its input mapped.
  await page.getByRole("button", { name: "Home", exact: true }).click();
  // Leaving keeps it here, where order-intake finds it.
  const leave = page.getByRole("dialog", { name: "Leave notify-customer?" });
  await expect(leave).toContainText("It's kept on this computer.");
  await leave.getByRole("button", { name: "Leave", exact: true }).click();
  await expect(
    page.getByText("notify-customer", { exact: false }).first(),
  ).toBeVisible();
  await page
    .getByLabel("Choose a workflow file")
    .setInputFiles(file("order-intake.yaml", orderIntake));
  await expect(page.locator(".editor-bar")).toContainText("order-intake");
  await addCommonStep(page, "Call a workflow", "Add a step after fetch");
  const call = new StepDetailsPage(page);
  await expect(call.dialogFor("call-workflow-1")).toBeVisible();
  await call.field("workflow").getByRole("combobox").click();
  const options = page.getByRole("listbox", { name: "Workflow options" });
  // The draft saved on this computer, under its group.
  await expect(options).toContainText("On this computer");
  await options
    .getByRole("option", { name: /^notify-customer 1\.0\.0/ })
    .click();
  await expect(call.field("input")).toContainText(
    "From notify-customer's input",
  );
  const customerId = call.field("input.customerId").getByRole("textbox");
  await customerId.fill("c-42");
  await customerId.blur();
  await call.close();

  // Execute workflow runs both; notify-customer shows under the call, with its steps.
  await page
    .locator(".editor-bar")
    .getByRole("button", { name: "Execute workflow" })
    .click();
  await expect.poll(() => executions(calls).length).toBe(1);
  const sent = executions(calls)[0].body;
  expect(
    (sent["workflows"] as Record<string, { source?: string }>)[
      "notify-customer@1.0.0"
    ].source,
  ).toContain("callable");
  const log = page.getByRole("region", { name: "Run log" });
  await expect(log.locator(".run-log-status")).toHaveText(
    "Completed · 2 steps",
  );
  const called = log.getByRole("button", {
    name: "notify-customer 1.0.0, completed",
  });
  await called.click();
  const inside = log.getByRole("list", { name: "Steps of notify-customer" });
  await expect(inside.locator(".run-log-inside strong")).toHaveText([
    "render",
    "send",
  ]);
  // The call went with its input mapped.
  const steps = (
    parse(sent["source"] as string) as {
      spec: { steps: { id: string; with?: unknown }[] };
    }
  ).spec.steps;
  expect(
    JSON.stringify(steps.find((step) => step.id === "call-workflow-1")?.with),
  ).toContain("c-42");
});

test("activating the calling workflow pins the current activation of the one it calls", async ({
  page,
}) => {
  const captured = await platform(page);
  const calling = parse(orderIntake) as {
    spec: { steps: Record<string, unknown>[] };
  };
  calling.spec.steps.push({
    id: "notify",
    kind: "callWorkflow",
    uses: "notify-customer@1.0.0",
    mode: "wait",
    with: { object: { customerId: { ref: "/input/customerId" } } },
  });
  const CALLEE = "22222222-2222-4222-8222-222222222222";
  // The platform runs calls: the editor's step kinds come from its manifest.
  await page.route("**/projects/project/language", (route) =>
    route.fulfill({ json: { ...MANIFEST, features: ["flow.callWorkflow"] } }),
  );
  await page.route("**/projects/project/workflows", (route) =>
    route.fulfill({
      status: 201,
      json: {
        id: versionId,
        name: "order-intake",
        version: "1.2.0",
        digest: "e".repeat(64),
      },
    }),
  );
  await page.route(`**/projects/project/workflows/${versionId}/export`, (r) =>
    r.fulfill({
      json: {
        id: versionId,
        document: calling,
        artifact: {
          executable: {
            features: ["flow.callWorkflow"],
            connections: {},
            dependencies: [],
          },
        },
      },
    }),
  );
  // notify-customer 1.0.0 is published, and active twice: the newest wins.
  await page.route("**/projects/project/workflows?*", (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: CALLEE,
            name: "notify-customer",
            version: "1.0.0",
            digest: "c".repeat(64),
          },
          {
            id: versionId,
            name: "order-intake",
            version: "1.2.0",
            digest: "e".repeat(64),
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/environments/development/activations?*", (r) =>
    r.fulfill({
      json: {
        items: [2, 3].map((revision) => ({
          id: `act-${revision}`,
          name: "notify-customer",
          revision,
          retired: false,
          request: { version_id: CALLEE },
        })),
        next_cursor: null,
      },
    }),
  );
  await withNewEditor(page);
  await page.reload();
  await page
    .getByLabel("Choose a workflow file")
    .setInputFiles(file("order-intake.yaml", stringify(calling)));
  await expect(page.locator(".editor-bar")).toBeVisible();
  await command(page, "Publish…");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Publish version" })
    .click();
  await command(page, "Activate…");
  const dialog = page.getByRole("dialog");
  await expect(
    dialog
      .getByRole("list", { name: "Called workflows" })
      .getByRole("listitem"),
  ).toHaveText(["notify-customer 1.0.0 · activation 3"]);
  await dialog
    .getByRole("button", { name: "Activate version", exact: true })
    .click();
  await expect.poll(() => captured.body).not.toBeNull();
  expect(captured.body!["workflow_activation_ids"]).toEqual({
    "notify-customer@1.0.0": "act-3",
  });
});
