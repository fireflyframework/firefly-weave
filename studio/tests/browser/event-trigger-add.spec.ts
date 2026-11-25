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
// The Platform event trigger on the canvas: added from Add a step with the
// workflow's input (asking before it replaces another one), what its tile
// says, and the question before it goes while triggers created for it react.
// The platform and the host's language manifest are mocked.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { parse, stringify } from "yaml";
import { AddStepPanel, languageFeatures } from "./add-step-po";
import {
  openNewWorkflow,
  openStoredWorkflow,
  type CanvasPage,
} from "./canvas-po";
import { addCommonStep, StepDetailsPage } from "./step-details-po";
import { command, sourceText } from "./support";

const REFERENCE = { $ref: "weave.platform-event-v1" };
const FORM = {
  type: "object",
  required: ["reference"],
  properties: { reference: { type: "string" }, message: { type: "string" } },
};
const definition = (spec: Record<string, unknown> = {}) =>
  stringify({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "notify-team", version: "1.2.0" },
    spec: {
      inputSchema: FORM,
      outputSchema: { type: "object" },
      steps: [{ id: "send", kind: "transform", value: { ref: "/input" } }],
      output: { literal: {} },
      ...spec,
    },
  });
const spec = async (page: Page) =>
  (parse(await sourceText(page)) as { spec: Record<string, unknown> }).spec;
const tile = (page: Page): Locator =>
  page.locator('.tile-node[data-tile^="$trigger"]').filter({
    hasText: "Platform event",
  });
const toast = (page: Page, text: string) =>
  page.locator(".toast").filter({ hasText: text });
const undo = (page: Page) => command(page, "Undo");
const question = (page: Page) =>
  page.getByRole("dialog", { name: "Replace this workflow's input?" });
/** A stored workflow whose platform has platform event triggers. */
async function open(
  page: Page,
  id: string,
  source: string,
  triggers?: unknown[],
): Promise<CanvasPage> {
  await languageFeatures(page, ["flow.eventTrigger"], []);
  return openStoredWorkflow(page, {
    id,
    source,
    ...(triggers
      ? {
          canvas: { schemaVersion: 2, kind: "weave.studio/canvas", triggers },
        }
      : {}),
  });
}
const choosePlatformEvent = (page: Page) =>
  addCommonStep(page, "Platform event", "Add a step after send");

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`the Platform event trigger at ${size.width}x${size.height}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize(size);
    });

    test("starts a new workflow from a transaction that needs attention, and opens its step details", async ({
      page,
    }) => {
      await languageFeatures(page, ["flow.eventTrigger"], []);
      const canvas = await openNewWorkflow(page);
      await canvas.insertTarget("Add first step").click();
      const panel = new AddStepPanel(page);
      await expect(panel.title).toHaveText("What triggers this workflow?");
      const option = panel.option("Platform event");
      await expect(option).toContainText(
        "Start a run when a transaction needs attention",
      );
      await panel.choose("Platform event");
      const details = new StepDetailsPage(page);
      await expect(details.dialogFor("Platform event trigger")).toBeVisible();
      await details.close();
      await expect(tile(page).locator("strong")).toHaveText("Platform event");
      await expect(tile(page)).toContainText("Needs attention · any workflow");
      await expect(
        canvas.root.getByRole("button", {
          name: "Platform event trigger: Needs attention, from any workflow in this project",
          exact: true,
        }),
      ).toBeAttached();
      const written = await spec(page);
      expect(written["eventTrigger"]).toEqual({
        events: ["transaction.in_doubt"],
      });
      expect(written["inputSchema"]).toEqual(REFERENCE);
      await undo(page);
      expect("eventTrigger" in (await spec(page))).toBe(false);
      expect((await spec(page))["inputSchema"]).toEqual({ type: "object" });
    });

    test("asks before the platform event replaces a form's fields; keeping them is the safe answer", async ({
      page,
    }) => {
      await open(page, `event-ask-${size.width}`, definition());
      await choosePlatformEvent(page);
      const asked = question(page);
      await expect(asked).toContainText(
        "This workflow's input doesn't fit it: it has 2 fields.",
      );
      await expect(asked.getByRole("listitem")).toHaveText([
        "reference",
        "message",
      ]);
      await expect(
        asked.getByRole("button", { name: "Keep the input", exact: true }),
      ).toBeFocused();
      // Escape is the safe answer too: the trigger joins, the fields stay.
      await page.keyboard.press("Escape");
      const details = new StepDetailsPage(page);
      await expect(details.dialogFor("Platform event trigger")).toBeVisible();
      await details.close();
      await expect(tile(page)).toHaveCount(1);
      const kept = await spec(page);
      expect(kept["inputSchema"]).toEqual(FORM);
      expect(kept["eventTrigger"]).toEqual({
        events: ["transaction.in_doubt"],
      });
    });

    test("replaces the input when asked to, and one undo brings back the fields without the trigger", async ({
      page,
    }) => {
      await open(page, `event-replace-${size.width}`, definition());
      await choosePlatformEvent(page);
      await question(page)
        .getByRole("button", { name: "Replace the input", exact: true })
        .click();
      const details = new StepDetailsPage(page);
      await expect(details.dialogFor("Platform event trigger")).toBeVisible();
      await details.close();
      expect((await spec(page))["inputSchema"]).toEqual(REFERENCE);
      await undo(page);
      const before = await spec(page);
      expect(before["inputSchema"]).toEqual(FORM);
      expect("eventTrigger" in before).toBe(false);
    });

    test("removing it asks while a trigger created for it reacts, starting on Cancel, and keeps the input", async ({
      page,
    }) => {
      const stored = {
        id: "trigger-0a1b2c3d",
        kind: "event",
        resources: { "0d9f7e61-0000-4000-8000-000000000000": "et-1" },
      };
      const canvas = await open(
        page,
        `event-remove-${size.width}`,
        definition({
          inputSchema: REFERENCE,
          eventTrigger: { events: ["transaction.in_doubt"] },
        }),
        [stored],
      );
      await canvas.tileBody("$trigger:trigger-0a1b2c3d").focus();
      await page.keyboard.press("Delete");
      const asked = page.getByRole("dialog", {
        name: "Remove the platform event trigger?",
      });
      await expect(asked).toContainText(
        "Triggers already created keep reacting on their versions until you stop them when you next activate.",
      );
      await expect(asked.getByRole("listitem")).toContainText([
        "Environment 0d9f7e61",
      ]);
      const cancel = asked.getByRole("button", { name: "Cancel", exact: true });
      await expect(cancel).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(asked).toHaveCount(0);
      await expect(tile(page)).toHaveCount(1);
      await expect(canvas.tileBody("$trigger:trigger-0a1b2c3d")).toBeFocused();
      await page.keyboard.press("Delete");
      await asked.getByRole("button", { name: "Remove", exact: true }).click();
      await expect(tile(page)).toHaveCount(0);
      const after = await spec(page);
      expect("eventTrigger" in after).toBe(false);
      expect(after["inputSchema"]).toEqual(REFERENCE);
    });

    test("without a created trigger it goes at once, with Undo", async ({
      page,
    }) => {
      const canvas = await open(
        page,
        `event-remove-plain-${size.width}`,
        definition({
          inputSchema: REFERENCE,
          eventTrigger: { events: ["transaction.in_doubt"] },
        }),
        [{ id: "trigger-0a1b2c3d", kind: "event" }],
      );
      await canvas.tileBody("$trigger:trigger-0a1b2c3d").focus();
      await page.keyboard.press("Delete");
      await expect(tile(page)).toHaveCount(0);
      await toast(page, "Removed the Platform event trigger.")
        .getByRole("button", { name: "Undo", exact: true })
        .click();
      await expect(tile(page)).toHaveCount(1);
      expect((await spec(page))["eventTrigger"]).toEqual({
        events: ["transaction.in_doubt"],
      });
    });
  });

test.describe("the Platform event tile and its entry in Add a step", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a workflow that declares one opens with its tile, whose label fits at 100% with the most workflows a trigger can name", async ({
    page,
  }) => {
    await open(
      page,
      "event-tile",
      definition({
        inputSchema: REFERENCE,
        eventTrigger: {
          events: ["transaction.in_doubt"],
          from: Array.from({ length: 100 }, (_, i) => `workflow-${i}`),
        },
      }),
    );
    await expect(tile(page)).toContainText("Needs attention · 100 workflows");
    const parts = await tile(page)
      .locator(".tile-label")
      .locator("strong, span")
      .evaluateAll((texts) =>
        texts.map((text) => ({
          text: (text as HTMLElement).innerText,
          clipped: text.scrollWidth > text.clientWidth + 1,
        })),
      );
    expect(parts.map((part) => part.text)).toContain(
      "Needs attention · 100 workflows",
    );
    expect(parts.filter((part) => part.clipped)).toEqual([]);
  });

  test("says what the platform lacks, and that a workflow has one", async ({
    page,
  }) => {
    await languageFeatures(page, ["text.concat"]);
    const canvas = await openStoredWorkflow(page, {
      id: "event-disabled",
      source: definition(),
    });
    await canvas.root.focus();
    await page.keyboard.press("n");
    const panel = new AddStepPanel(page);
    await panel.search.fill("Platform event");
    const option = panel.option("Platform event");
    await expect(option).toHaveAttribute("aria-disabled", "true");
    await expect(option).toContainText(
      "Needs a platform with platform event triggers (flow.eventTrigger)",
    );
  });

  test("is disabled once the workflow has one", async ({ page }) => {
    const canvas = await open(
      page,
      "event-once",
      definition({
        inputSchema: REFERENCE,
        eventTrigger: { events: ["transaction.in_doubt"] },
      }),
    );
    await canvas.root.focus();
    await page.keyboard.press("n");
    const panel = new AddStepPanel(page);
    await panel.search.fill("Platform event");
    await expect(panel.option("Platform event")).toContainText(
      "This workflow already starts from platform events.",
    );
  });
});
