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
// Trigger step details: a webhook, schedule, Kafka, email or app event tile
// opens step details with its settings, which the workflow's canvas keeps; the
// Test event pane says what the trigger can do here; an email trigger asks
// before it replaces an input that doesn't take an email.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { openStoredWorkflow } from "./canvas-po";

const WEBHOOK = "trigger-1b2c3d4e";
const SCHEDULE = "trigger-2c3d4e5f";
const KAFKA = "trigger-3d4e5f6a";
const EMAIL = "trigger-4e5f6a7b";
const APP = "trigger-5f6a7b8c";

const EMAIL_MESSAGE = {
  type: "object",
  properties: {
    sender: { type: "string" },
    to: { type: "array", items: { type: "string" } },
    subject: { type: "string" },
    text: { type: "string" },
  },
  required: ["sender", "to", "subject", "text"],
};

const definition = (input: Record<string, unknown>) =>
  stringify({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "order-intake", version: "1.0.0" },
    spec: {
      inputSchema: input,
      outputSchema: { type: "object" },
      steps: [{ id: "check", kind: "transform", value: { ref: "/input" } }],
      output: { literal: {} },
    },
  });
const ORDER_INPUT = {
  type: "object",
  properties: { region: { type: "string", title: "Region" } },
  required: ["region"],
};
const canvas = (triggers: unknown[]) => ({
  schemaVersion: 2,
  kind: "weave.studio/canvas",
  triggers,
});

async function open(
  page: Page,
  id: string,
  triggers: unknown[],
  input = ORDER_INPUT,
) {
  await page.route("**/studio/contracts/email-message", (r) =>
    r.fulfill({ json: EMAIL_MESSAGE }),
  );
  const view = await openStoredWorkflow(page, {
    id: `trigger-settings-${id}-${page.viewportSize()?.width}`,
    source: definition(input),
    canvas: canvas(triggers),
  });
  return view;
}

const dialog = (page: Page, name: string) =>
  page.getByRole("dialog", {
    name: `Step details: ${name} trigger`,
    exact: true,
  });
const field = (details: Locator, id: string) =>
  details.locator(`.sd-tabpanel:not([hidden]) [data-param="${id}"]`).first();

/** The Test event pane: beside Parameters on a wide window, behind its tab on a narrow one. */
async function testEvent(details: Locator): Promise<Locator> {
  const tab = details.getByRole("tab", { name: "Test event", exact: true });
  if (await tab.isVisible()) await tab.click();
  return details.locator("#sd-region-input");
}
async function parameters(details: Locator) {
  const tab = details
    .getByRole("tablist", { name: "Step details panes" })
    .getByRole("tab", { name: "Parameters" });
  if (await tab.isVisible()) await tab.click();
}

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`trigger step details at ${size.width}x${size.height}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize(size);
    });

    test("a webhook tile opens its settings, and the Test event pane says listening needs a platform", async ({
      page,
    }) => {
      const view = await open(page, "webhook", [
        { id: WEBHOOK, kind: "webhook" },
      ]);
      await view.tileBody(`$trigger:${WEBHOOK}`).dblclick();
      const details = dialog(page, "Webhook");
      await expect(details).toBeVisible();
      await expect(
        page
          .locator(".toast")
          .filter({ hasText: "is set up when you activate" }),
      ).toHaveCount(0);
      await parameters(details);
      for (const id of ["name", "secretRef", "testSecretRef", "payload"])
        await expect(field(details, id)).toBeVisible();
      await expect(
        details.getByRole("button", { name: /^Execute workflow/ }).first(),
      ).toBeVisible();
      const pane = await testEvent(details);
      await expect(
        pane.getByText(
          "Listening needs a platform. Paste a sample payload instead.",
        ),
      ).toBeVisible();
      await pane
        .getByRole("button", { name: "Paste sample", exact: true })
        .click();
      await pane.getByLabel("Sample payload (JSON)").fill('{"region": "eu"}');
      await pane
        .getByRole("button", { name: "Use this sample", exact: true })
        .click();
      await expect(
        page.getByText("Pasted a sample test event."),
      ).toBeAttached();
      await pane
        .getByRole("button", { name: "Paste sample", exact: true })
        .click();
      await pane.getByLabel("Sample payload (JSON)").fill("{region");
      await pane
        .getByRole("button", { name: "Use this sample", exact: true })
        .click();
      await expect(pane.getByRole("alert")).toHaveText(
        'Paste JSON, such as {"customerId": "123"}.',
      );
    });

    test("a trigger's settings stay in the workflow's canvas, and the tile still says it's set up at activation", async ({
      page,
    }) => {
      const view = await open(page, "kept", [{ id: WEBHOOK, kind: "webhook" }]);
      await view.tileBody(`$trigger:${WEBHOOK}`).dblclick();
      let details = dialog(page, "Webhook");
      await parameters(details);
      await field(details, "name").getByRole("textbox").fill("orders-in");
      await field(details, "secretRef")
        .getByRole("textbox")
        .fill("orders-signing");
      await field(details, "testSecretRef")
        .getByRole("textbox")
        .fill("orders-signing");
      await field(details, "testSecretRef").getByRole("textbox").blur();
      await expect(field(details, "testSecretRef")).toContainText(
        "Use a test secret other than the production secret.",
      );
      await details.getByRole("button", { name: "Close", exact: true }).click();
      await expect(details).toHaveCount(0);
      await expect(
        page.locator(".canvas-v2").getByRole("button", {
          name: "Webhook trigger, Set up at activation",
          exact: true,
        }),
      ).toBeAttached();
      await view.tileBody(`$trigger:${WEBHOOK}`).dblclick();
      details = dialog(page, "Webhook");
      await parameters(details);
      await expect(field(details, "name").getByRole("textbox")).toHaveValue(
        "orders-in",
      );
      await expect(
        field(details, "secretRef").getByRole("textbox"),
      ).toHaveValue("orders-signing");
    });

    test("a schedule is set up with a preset, and shows its next five times in UTC", async ({
      page,
    }) => {
      const view = await open(page, "schedule", [
        { id: SCHEDULE, kind: "schedule", input: { region: "eu" } },
      ]);
      await view.tileBody(`$trigger:${SCHEDULE}`).dblclick();
      const details = dialog(page, "Schedule");
      await parameters(details);
      const builder = field(details, "cron");
      await builder.getByLabel("Repeats").selectOption({ label: "Weekly on" });
      await builder.getByLabel("Day").selectOption({ label: "Monday" });
      await builder.getByLabel("At (UTC)").fill("09:00");
      await builder.getByLabel("At (UTC)").blur();
      await expect(
        builder.getByText("Mondays 09:00 UTC", { exact: true }),
      ).toBeVisible();
      await expect(
        builder
          .getByRole("list", { name: "Next five times" })
          .getByRole("listitem"),
      ).toHaveCount(5);
      await builder
        .getByLabel("Repeats")
        .selectOption({ label: "Custom UTC cron" });
      await builder.getByLabel("Cron").fill("0 9 1 * 1");
      await builder.getByLabel("Cron").blur();
      await expect(builder.getByRole("alert")).toHaveText(
        "Set the day of month or the day of week, not both. Leave the other as *.",
      );
    });

    for (const [id, kind, title, offer] of [
      [KAFKA, "broker", "Kafka message", "Paste a sample message"],
      [EMAIL, "email", "Email", "Paste a sample email"],
      [APP, "provider", "App event", "Paste a sample event"],
    ] as const)
      test(`${title} offers ${offer.toLowerCase()} instead of listening`, async ({
        page,
      }) => {
        const view = await open(
          page,
          kind,
          [{ id, kind }],
          kind === "email" ? EMAIL_MESSAGE : ORDER_INPUT,
        );
        await view.tileBody(`$trigger:${id}`).dblclick();
        const details = dialog(page, title);
        await expect(details).toBeVisible();
        const pane = await testEvent(details);
        await expect(
          pane.getByRole("button", { name: offer, exact: true }),
        ).toBeVisible();
        await expect(
          pane.getByRole("button", { name: /Listen for test event/ }),
        ).toHaveCount(0);
      });

    test("an email trigger asks before it replaces an input that doesn't take an email", async ({
      page,
    }) => {
      const view = await open(page, "email-fit", [
        { id: EMAIL, kind: "email" },
      ]);
      await view.tileBody(`$trigger:${EMAIL}`).dblclick();
      const details = dialog(page, "Email");
      await parameters(details);
      const question = details.getByText(
        "The email message doesn't fit this workflow's input. Replace the input with the email message?",
      );
      await expect(question).toBeVisible();
      await details
        .getByRole("button", { name: "Cancel", exact: true })
        .click();
      await expect(question).toHaveCount(0);
      await details.getByRole("button", { name: "Close", exact: true }).click();
      await view.tileBody(`$trigger:${EMAIL}`).dblclick();
      await parameters(details);
      await expect(question).toBeVisible();
      await details
        .getByRole("button", { name: "Replace", exact: true })
        .click();
      await expect(question).toHaveCount(0);
      await expect(
        page.getByText("Replaced the input with the email message."),
      ).toBeAttached();
      await details.getByRole("button", { name: "Close", exact: true }).click();
      await view.tileBody(`$trigger:${EMAIL}`).dblclick();
      await parameters(details);
      await expect(question).toHaveCount(0);
    });
  });
