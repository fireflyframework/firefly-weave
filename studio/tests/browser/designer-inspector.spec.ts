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
// The inspector (W3-4, W3-6, W3-7): decision conditions as rule rows, a
// wider panel with folding sections and a clear apply model, and one
// vocabulary for where a value comes from.
import { selectChoice } from "./support";
import { test, expect, type Page } from "@playwright/test";
import {
  closeSheet,
  insertStep,
  newWorkflow,
  offline,
  sourceText,
} from "./support";
import { DesignerPage } from "./designer-po";

const routed = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: { name: routing, version: 1.0.0 }
spec:
  inputSchema:
    type: object
    properties:
      amount: { type: number, title: Amount }
      customerId: { type: string, title: Customer ID }
  outputSchema: { type: object }
  steps:
    - id: decision-1
      kind: switch
      cases:
        - steps: []
          output: { literal: {} }
      default: { steps: [], output: { literal: {} } }
    - id: transform-1
      kind: transform
      value: { literal: {} }
  output: { literal: {} }
`;
const inspector = (page: Page) => new DesignerPage(page).inspector;
const finishField = (page: Page) => page.locator(".inspector-header h2");

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(
    `${viewport.width}x${viewport.height}`,
    { tag: "@xplat" },
    () => {
      test.use({ viewport });

      test("a decision's condition is built from rule rows and labels its path", async ({
        page,
      }) => {
        await offline(page);
        await newWorkflow(page);
        const designer = new DesignerPage(page);
        await designer.setSource(routed);
        await designer.selectStep("decision-1");
        const condition = inspector(page).locator(
          '[data-field="cases/0/when"]',
        );
        // A new case has no condition: it says so instead of "always".
        await expect(condition).not.toContainText(
          "Choose when this path applies.",
        );
        await condition
          .getByRole("combobox", { name: "Condition 1 data" })
          .fill("/input/amount");
        await selectChoice(
          condition.getByLabel("Condition 1 test", { exact: true }),
          {
            label: "is greater than",
          },
        );
        const value = condition.getByLabel("Condition 1 value");
        await expect(value).toHaveAttribute("type", "number");
        await value.fill("1000");
        await expect(condition).not.toContainText(
          "Choose when this path applies.",
        );
        await finishField(page).click();
        const source = await sourceText(page);
        expect(source).toContain("name: gt");
        expect(source).toMatch(/- ref: \/input\/amount\n\s+- literal: 1000/);
        // The canvas names the paths by their conditions.
        await designer.open();
        await closeSheet(page);
        await designer.fit();
        const labels = await page
          .locator(".canvas .lane-header")
          .allTextContents();
        expect(labels.map((l) => l.trim())).toEqual([
          "Amount > 1000",
          "Otherwise",
        ]);
        for (const label of labels)
          expect(label.trim()).not.toMatch(/^Case \d+$|^Default$/);
        await expect(
          page.locator('[data-step="decision-1"] .node-summary'),
        ).toHaveText("Amount > 1000 · otherwise");
        // Another path starts without a condition; Validate reports it.
        await designer.selectStep("decision-1");
        await inspector(page)
          .getByRole("button", { name: "+ Add path" })
          .click();
        await expect(
          inspector(page).locator('[data-field="cases/1/when"]'),
        ).not.toContainText("Choose when this path applies.");
        await closeSheet(page);
        await page
          .getByRole("toolbar", { name: "Workflow commands" })
          .getByRole("button", { name: "Validate" })
          .click();
        const strip = page.getByRole("region", {
          name: "Compiler diagnostics",
        });
        await expect(strip).toContainText("1 error");
        await expect(strip).toContainText("Choose when this path applies.");
        expect(await sourceText(page)).not.toContain("literal: true");
      });

      test("Step name and duration update live and Undo restores the previous field", async ({
        page,
      }) => {
        await offline(page);
        await newWorkflow(page);
        await insertStep(page, "Wait for time");
        const designer = new DesignerPage(page);
        await designer.selectStep("wait-1");
        const name = inspector(page).getByLabel("Step name");
        await name.fill("cool-down");
        const duration = inspector(page).getByLabel("Duration", {
          exact: true,
        });
        await duration.click();
        await expect(designer.node("cool-down")).toHaveCount(1);
        await duration.fill("5");
        await expect(designer.node("cool-down")).toContainText("5 min");
        await duration.press("ControlOrMeta+z");
        await expect(duration).toHaveValue("1");
        await expect(designer.node("cool-down")).toHaveCount(1);
        await duration.fill("5");
        expect(await sourceText(page)).toContain("durationSeconds: 300");
      });

      test("the step menu moves, duplicates and deletes", async ({ page }) => {
        await offline(page);
        await newWorkflow(page);
        await insertStep(page, "Wait for time");
        await insertStep(page, "Transform");
        const designer = new DesignerPage(page);
        await designer.selectStep("wait-1");
        const menu = inspector(page).getByRole("button", {
          name: "Step actions",
        });
        await menu.click();
        await page.getByRole("menuitem", { name: "Duplicate" }).click();
        await expect(designer.node("wait-2")).toHaveCount(1);
        await expect(page.locator(".toast")).toContainText(
          "Duplicated wait-1 as wait-2.",
        );
        await expect
          .poll(() => designer.stepIds())
          .toEqual(["wait-1", "wait-2", "transform-1"]);
        await designer.selectStep("wait-2");
        await inspector(page)
          .getByRole("button", { name: "Step actions" })
          .click();
        await page.getByRole("menuitem", { name: "Move to…" }).click();
        const end = page.getByRole("button", {
          name: "Move wait-2 here, after transform-1",
        });
        await end.click();
        await expect
          .poll(() => designer.stepIds())
          .toEqual(["wait-1", "transform-1", "wait-2"]);
        await designer.selectStep("wait-2");
        await inspector(page)
          .getByRole("button", { name: "Step actions" })
          .click();
        await page.getByRole("menuitem", { name: "Delete step" }).click();
        await expect(designer.node("wait-2")).toHaveCount(0);
        await expect(page.locator(".toast")).toContainText("Deleted wait-2.");
      });
    },
  );

test.describe("1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the inspector is a 360 px or wider column with folding sections", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    await insertStep(page, "Transform");
    const panel = inspector(page);
    const box = await panel.boundingBox();
    expect(box!.width).toBeGreaterThanOrEqual(360);
    // No Kind, Step ID or "Property / Value" rows.
    await expect(panel.locator("thead")).toHaveCount(0);
    await expect(panel.getByText("Kind", { exact: true })).toHaveCount(0);
    // Inputs keep room for their values.
    const input = panel.getByLabel("Step name");
    expect((await input.boundingBox())!.width).toBeGreaterThanOrEqual(200);
    // A section folds and stays folded for this kind of step.
    const summary = panel.locator("summary", { hasText: "Transform data" });
    await summary.click();
    await expect(
      panel.locator("details.inspector-section"),
    ).not.toHaveAttribute("open", "");
    await insertStep(page, "Transform");
    await expect(
      inspector(page).locator("details.inspector-section"),
    ).not.toHaveAttribute("open", "");
    await inspector(page)
      .locator("summary", { hasText: "Transform data" })
      .click();
    await expect(
      inspector(page).locator("details.inspector-section"),
    ).toHaveAttribute("open", "");
  });

  test("Value, Data, Formula, Fields and List; a formula starts by choosing one", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    const designer = new DesignerPage(page);
    await designer.setSource(routed);
    await designer.selectStep("transform-1");
    const modes = inspector(page).getByRole("radiogroup", {
      name: "Value expression mode",
    });
    await expect(modes.getByRole("radio")).toHaveText([
      "Value",
      "Data",
      "Formula",
      "Fields",
      "List",
    ]);
    // The source choices may wrap below their label and contextual help.
    const label = inspector(page).locator(".expression-label").first();
    const [labelBox, modeBox] = [
      await label.boundingBox(),
      await modes.boundingBox(),
    ];
    expect(modeBox!.y).toBeGreaterThanOrEqual(labelBox!.y);
    expect(modeBox!.y - labelBox!.y).toBeLessThanOrEqual(64);
    const inspectorBox = (await inspector(page).boundingBox())!;
    expect(modeBox!.x).toBeGreaterThanOrEqual(inspectorBox.x);
    expect(modeBox!.x + modeBox!.width).toBeLessThanOrEqual(
      inspectorBox.x + inspectorBox.width,
    );
    expect(
      (await modes.getByRole("radio", { name: "Value" }).boundingBox())!.height,
    ).toBeGreaterThanOrEqual(36);
    await modes.getByRole("radio", { name: "Formula" }).click();
    const operator = inspector(page).getByLabel("Value operator", {
      exact: true,
    });
    await expect(operator).toHaveAttribute("placeholder", "Choose a formula…");
    await expect(
      inspector(page).getByText("Choose a formula.", { exact: true }),
    ).toBeHidden();
    await selectChoice(operator, { label: "is greater than" });
    await expect(operator).toHaveAttribute("data-value", "gt");
    // Data shows the workflow input as words, the pointer below it.
    await modes.getByRole("radio", { name: "Data" }).click();
    const reference = inspector(page).getByRole("combobox", {
      name: "Value reference",
    });
    await reference.fill("");
    await reference.press("ArrowDown");
    const options = page.locator(".ref-combo-option");
    await expect(options.first()).toBeVisible();
    for (const option of await options.all()) {
      const name = option.locator(".ref-combo-name");
      const lines = await name.evaluate((e) => {
        const style = getComputedStyle(e);
        return Math.round(
          e.getBoundingClientRect().height /
            parseFloat(style.lineHeight || "18"),
        );
      });
      expect(lines).toBeLessThanOrEqual(1);
      expect((await option.boundingBox())!.height).toBeLessThanOrEqual(56);
    }
    const list = await page.locator(".ref-combo-list").boundingBox();
    expect(list!.width).toBeGreaterThanOrEqual(320);
    await options.filter({ hasText: "Input › Amount" }).click();
    await expect(inspector(page).locator(".ref-combo-token")).toHaveText(
      "Input › Amount",
    );
    await expect(reference).toHaveValue("Input › Amount");
  });

  test("a human task offers a path for each answer in How they answer", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    await insertStep(page, "Human task");
    const lead = inspector(page).getByRole("button", {
      name: "Create a path for each answer",
    });
    await expect(lead).toBeVisible();
    await lead.click();
    const designer = new DesignerPage(page);
    await expect(designer.node("decision-1")).toHaveCount(1);
    await expect(
      page.locator('[data-step="decision-1"] .node-summary'),
    ).toHaveText("decision is approve · decision is reject · otherwise");
  });
});

test("from 768 to 1280 px the inspector is a column beside the canvas", async ({
  page,
}) => {
  for (const width of [1280, 1024, 800]) {
    await page.setViewportSize({ width, height: 800 });
    if (width === 1280) {
      await offline(page);
      await newWorkflow(page);
      await insertStep(page, "Wait for time");
    }
    const panel = page.getByRole("complementary", { name: "Inspector" });
    await expect(panel).toBeVisible();
    await expect(panel).not.toHaveAttribute("aria-modal", "true");
    const a = await panel.boundingBox();
    const c = await page.locator(".canvas").boundingBox();
    expect(a!.x >= c!.x + c!.width || a!.x + a!.width <= c!.x, `${width}`).toBe(
      true,
    );
  }
});
