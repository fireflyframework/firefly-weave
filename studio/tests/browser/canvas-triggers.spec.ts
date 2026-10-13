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
// Trigger choices on the new canvas: a tile per choice that says it is set
// up when you activate, one "+" at the start, and removal with Undo.
import { expect, test, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { openStoredWorkflow } from "./canvas-po";

const definition = (steps: unknown[], extra: Record<string, unknown> = {}) =>
  stringify({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "order-intake", version: "1.0.0" },
    spec: {
      inputSchema: {
        type: "object",
        properties: { amount: { type: "number", title: "Amount" } },
      },
      outputSchema: { type: "object" },
      steps,
      output: { literal: {} },
      ...extra,
    },
  });
const twoSteps = [
  { id: "check", kind: "transform", value: { ref: "/input" } },
  { id: "record", kind: "transform", value: { literal: {} } },
];
const canvas = (triggers: unknown[]) => ({
  schemaVersion: 2,
  kind: "weave.studio/canvas",
  triggers,
});
const tileNamed = (page: Page, name: string) =>
  page.locator(".canvas-v2").getByRole("button", { name, exact: true });
const toast = (page: Page, text: string) =>
  page.locator(".toast").filter({ hasText: text });

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`trigger tiles at ${size.width}x${size.height}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize(size);
    });

    test("trigger choices draw as tiles that say they are set up when you activate", async ({
      page,
    }) => {
      const view = await openStoredWorkflow(page, {
        id: `triggers-tiles-${size.width}`,
        source: definition(twoSteps),
        canvas: canvas([
          { id: "trigger-0a1b2c3d", kind: "manual" },
          { id: "trigger-1b2c3d4e", kind: "webhook" },
          { id: "trigger-2c3d4e5f", kind: "schedule" },
        ]),
      });
      await expect(
        tileNamed(page, "Manual form trigger, 1 field"),
      ).toBeVisible();
      await expect(
        tileNamed(page, "Webhook trigger, Set up at activation"),
      ).toBeAttached();
      await expect(
        tileNamed(page, "Schedule trigger, Set up at activation"),
      ).toBeAttached();
      await expect(
        view.root.getByRole("button", {
          name: "Insert a step before check",
          exact: true,
        }),
      ).toHaveCount(1);
      await view.tileBody("$trigger:trigger-1b2c3d4e").focus();
      await page.keyboard.press("Enter");
      await expect(
        toast(page, "Webhook is set up when you activate this workflow."),
      ).toBeVisible();
      await expect(
        page.getByRole("dialog", { name: /^Step details: / }),
      ).toHaveCount(0);
    });

    test("removing a trigger offers Undo, and Delete on a focused trigger tile removes it too", async ({
      page,
    }) => {
      const view = await openStoredWorkflow(page, {
        id: `triggers-remove-${size.width}`,
        source: definition(twoSteps),
        canvas: canvas([
          { id: "trigger-0a1b2c3d", kind: "manual" },
          { id: "trigger-1b2c3d4e", kind: "webhook" },
          { id: "trigger-2c3d4e5f", kind: "email" },
        ]),
      });
      await view.tileBody("$trigger:trigger-1b2c3d4e").hover();
      await view.root
        .getByRole("button", {
          name: "Remove the Webhook trigger",
          exact: true,
        })
        .click();
      await expect(view.tile("$trigger:trigger-1b2c3d4e")).toHaveCount(0);
      await toast(page, "Removed the Webhook trigger.")
        .getByRole("button", { name: "Undo", exact: true })
        .click();
      await expect(view.tile("$trigger:trigger-1b2c3d4e")).toHaveCount(1);
      await view.tileBody("$trigger:trigger-2c3d4e5f").focus();
      await page.keyboard.press("Delete");
      await expect(view.tile("$trigger:trigger-2c3d4e5f")).toHaveCount(0);
    });

    test("removing the last choice brings back the input form every workflow has", async ({
      page,
    }) => {
      const view = await openStoredWorkflow(page, {
        id: `triggers-last-${size.width}`,
        source: definition(twoSteps),
        canvas: canvas([{ id: "trigger-1b2c3d4e", kind: "webhook" }]),
      });
      await view.tileBody("$trigger:trigger-1b2c3d4e").focus();
      await page.keyboard.press("Delete");
      await expect(
        tileNamed(page, "Manual form trigger, 1 field"),
      ).toBeAttached();
      await expect(view.tile("$trigger:manual")).toHaveCount(1);
    });

    test("without steps the triggers show instead of Add first step, and only the first offers +", async ({
      page,
    }) => {
      const view = await openStoredWorkflow(page, {
        id: `triggers-no-steps-${size.width}`,
        source: definition([]),
        canvas: canvas([
          { id: "trigger-1b2c3d4e", kind: "webhook" },
          { id: "trigger-2c3d4e5f", kind: "schedule" },
        ]),
      });
      await expect(view.insertTarget("Add first step")).toHaveCount(0);
      await expect(
        view.root.getByRole("button", {
          name: "Add a step at the start",
          exact: true,
        }),
      ).toHaveCount(1);
    });

    test("spec.callable shows Called by a workflow next to the input form", async ({
      page,
    }) => {
      await openStoredWorkflow(page, {
        id: `triggers-callable-${size.width}`,
        source: definition(twoSteps, { callable: {} }),
      });
      await expect(
        tileNamed(page, "Called by a workflow trigger, Any workflow here"),
      ).toBeAttached();
      await expect(
        tileNamed(page, "Manual form trigger, 1 field"),
      ).toBeAttached();
    });
  });

test("trigger choices survive a reload", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const view = await openStoredWorkflow(page, {
    id: "triggers-reload",
    source: definition(twoSteps),
    canvas: canvas([
      { id: "trigger-0a1b2c3d", kind: "manual" },
      { id: "trigger-1b2c3d4e", kind: "webhook" },
    ]),
  });
  await view.tileBody("$trigger:trigger-1b2c3d4e").focus();
  await page.keyboard.press("Delete");
  await expect
    .poll(() =>
      page.evaluate(() =>
        localStorage.getItem("weave.localDraft.triggers-reload"),
      ),
    )
    .not.toContain("trigger-1b2c3d4e");
  await page.reload();
  await view.ready();
  await expect(view.tile("$trigger:trigger-0a1b2c3d")).toHaveCount(1);
  await expect(view.tile("$trigger:trigger-1b2c3d4e")).toHaveCount(0);
});

for (const size of [
  { width: 1440, height: 900, touch: false },
  { width: 600, height: 500, touch: false },
  { width: 360, height: 640, touch: false },
  { width: 360, height: 640, touch: true },
])
  test.describe(`trigger tile labels at ${size.width}x${size.height}${size.touch ? " on touch" : ""}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
    });

    test("each trigger tile is titled by its trigger, and its title and subtitle fit unclipped", async ({
      page,
    }) => {
      await openStoredWorkflow(page, {
        id: `trigger-labels-${size.width}-${size.touch}`,
        source: definition(twoSteps, { callable: {} }),
        canvas: canvas([
          { id: "trigger-0a1b2c3d", kind: "manual" },
          { id: "trigger-1b2c3d4e", kind: "webhook" },
          { id: "trigger-2c3d4e5f", kind: "broker" },
          { id: "trigger-3d4e5f60", kind: "called" },
          { id: "trigger-4e5f6071", kind: "provider" },
        ]),
      });
      const labels = await page
        .locator(".canvas-v2 [data-trigger] .tile-label")
        .evaluateAll((elements) =>
          elements.map((label) => {
            const box = label.getBoundingClientRect();
            return [...label.querySelectorAll<HTMLElement>("strong, span")].map(
              (text) => {
                const own = text.getBoundingClientRect();
                return {
                  text: text.innerText,
                  clipped:
                    text.scrollWidth > text.clientWidth + 1 ||
                    text.scrollHeight > text.clientHeight + 1,
                  inside:
                    own.left >= box.left - 0.5 && own.right <= box.right + 0.5,
                };
              },
            );
          }),
        );
      expect(labels.map((parts) => parts.map((part) => part.text))).toEqual([
        ["Manual form", "1 field"],
        ["Webhook", "Set up at activation"],
        ["Kafka message", "Set up at activation"],
        ["Called by a workflow", "Any workflow here"],
        ["App event", "Set up at activation"],
      ]);
      for (const part of labels.flat()) {
        expect(part.clipped, part.text).toBe(false);
        expect(part.inside, part.text).toBe(true);
      }
    });
  });
