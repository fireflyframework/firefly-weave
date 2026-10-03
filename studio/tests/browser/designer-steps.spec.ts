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
// WP-14: steps can be renamed with their references, deleted safely, read at
// a glance on the canvas, timed in natural units, and a human task can branch
// on its decisions in one click.
import { selectChoice } from "./support";
import { test, expect, Page } from "@playwright/test";
import { parse } from "yaml";
import {
  closeSheet,
  command,
  newWorkflow,
  offline,
  sourceText,
} from "./support";
import { DesignerPage } from "./designer-po";

const onboarding = `# Customer onboarding
apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: onboarding
  version: 1.0.0
spec:
  inputSchema: { type: object }
  outputSchema: { type: object }
  steps:
    - id: action-1 # looks up the customer
      kind: action
      uses: crm.lookup@1.0.0
      with: { literal: {} }
    - id: summarize
      kind: transform
      value:
        object:
          customer: { ref: /steps/action-1/output }
          note: { literal: /steps/action-1/output }
  output: { ref: /steps/summarize/output }
`;
const node = (page: Page, id: string) =>
  page.locator(`[data-step="${id}"] .node-body`);
/** Inserts from the palette and waits for a narrow-layout palette to close. */
async function insertStep(page: Page, label: string) {
  // A narrow-layout inspector is a modal sheet over the toolbar.
  await closeSheet(page);
  const item = page
    .locator(".palette")
    .getByRole("button", { name: label, exact: true });
  if (!(await item.isVisible()))
    await page.getByRole("button", { name: "Insert step" }).click();
  await item.click();
  await expect(page.locator(".palette.popover-visible")).toHaveCount(0);
}
/** Closes a narrow-layout inspector that covers the canvas. */
async function closeInspector(page: Page) {
  const close = page.getByRole("button", { name: "Close inspector" });
  if (await close.isVisible()) {
    await close.click();
    await expect(close).toBeHidden();
  }
}
/** An inspector button for the selected step. */
const stepControl = (designer: DesignerPage, name: string) =>
  designer.inspector.getByRole("button", { name, exact: true });
/** A command in the inspector's ⋯ menu for the selected step. */
async function stepMenu(designer: DesignerPage, name: string) {
  await designer.inspector
    .getByRole("button", { name: "Step actions", exact: true })
    .click();
  await designer.page.getByRole("menuitem", { name, exact: true }).click();
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("renaming a step rewrites its references and keeps comments", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(onboarding);
      await designer.selectStep("action-1");
      // "Step name" commits on Enter or when it loses focus.
      const id = designer.inspectorField("Step name");
      await id.fill("summarize");
      await id.press("Enter");
      await expect(designer.inspector.getByRole("alert")).toHaveText(
        "Another step is already named summarize.",
      );
      await id.fill("look up");
      await id.press("Enter");
      await expect(designer.inspector.getByRole("alert")).toContainText(
        "letters, numbers",
      );
      await id.fill("lookup");
      await id.press("Enter");
      await expect(page.locator('[data-step="lookup"]')).toBeVisible();
      await expect(page.locator('[data-step="action-1"]')).toHaveCount(0);
      await expect(page.locator(".graph-node.selected")).toHaveAttribute(
        "data-step",
        "lookup",
      );
      const text = await sourceText(page);
      expect(text).toContain("# Customer onboarding");
      expect(text).toContain("- id: lookup # looks up the customer");
      expect(text).toContain("customer: { ref: /steps/lookup/output }");
      expect(text).toContain("note: { literal: /steps/action-1/output }");
    });

    test("deleting names the steps that still read it, and groups ask first", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(onboarding);
      await designer.selectStep("action-1");
      await stepMenu(designer, "Delete step");
      await expect(page.getByRole("alert")).toContainText(
        "This step is referenced by summarize. Update it before deleting it.",
      );
      await expect(page.locator('[data-step="action-1"]')).toHaveCount(1);
      await insertStep(page, "Decision");
      await closeInspector(page);
      await designer.insertAt(
        "wait",
        "Add a step here, in Case 1 of decision-1",
      );
      await designer.selectStep("decision-1");
      await stepMenu(designer, "Delete group with 1 step");
      const confirm = page.getByRole("dialog", {
        name: "Delete decision-1 and the 1 step inside it?",
      });
      await confirm.getByRole("button", { name: "Delete group" }).click();
      await expect(page.locator('[data-step="decision-1"]')).toHaveCount(0);
      await expect(page.locator('[data-step="wait-1"]')).toHaveCount(0);
    });

    test("nodes summarize their configuration and flag what is missing", async ({
      page,
    }) => {
      await offline(page);
      await page.route("**/studio/local/validate", (r) =>
        r.fulfill({
          json: {
            validationOk: false,
            errorCount: 1,
            partial: true,
            diagnostics: [
              {
                code: "WV-COMP-TYPE_MISMATCH",
                severity: "error",
                path: "/spec/steps/2/value",
                message: "Expression type does not match.",
              },
            ],
          },
        }),
      );
      await newWorkflow(page);
      await insertStep(page, "Call an action");
      await insertStep(page, "Decision");
      await insertStep(page, "Transform");
      await insertStep(page, "Wait for time");
      await expect(node(page, "call-action-1")).toContainText(
        "Choose an action",
      );
      await expect(node(page, "call-action-1")).toHaveAccessibleName(
        /Needs attention: Choose the action to call\./,
      );
      await expect(node(page, "decision-1")).toContainText(
        "Condition not set · otherwise",
      );
      await expect(node(page, "decision-1")).toHaveAccessibleName(
        /Case 1: choose when this path applies\./,
      );
      await expect(node(page, "wait-1")).toContainText("1 min");
      await expect(node(page, "wait-1")).not.toHaveAccessibleName(
        /Needs attention/,
      );
      // Not a colour-only dot: a dashed border and a chip that says what's next.
      await expect(page.locator('[data-step="call-action-1"]')).toHaveClass(
        /\bincomplete\b/,
      );
      await expect(
        page.locator('[data-step="call-action-1"] .node-chip'),
      ).toHaveText("Choose an action");
      await page.getByRole("button", { name: "Validate", exact: true }).click();
      await expect(node(page, "transform-1")).toHaveAccessibleName(/1 error/);
      await expect(page.locator('[data-step="transform-1"]')).toHaveClass(
        /\bhas-error\b/,
      );
      await expect(
        page.locator('[data-step="transform-1"] .node-chip'),
      ).toHaveText("1 problem");
    });

    test("durations are entered in natural units", async ({ page }) => {
      await offline(page);
      await newWorkflow(page);
      await insertStep(page, "Wait for time");
      const designer = new DesignerPage(page);
      await designer.selectStep("wait-1");
      const amount = designer.inspector.getByRole("spinbutton", {
        name: "Duration",
        exact: true,
      });
      const unit = designer.inspector.getByRole("combobox", {
        name: "Duration unit",
      });
      await expect(amount).toHaveValue("1");
      await expect(unit).toHaveValue("min");
      await selectChoice(unit, "s");
      await amount.fill("0.5");
      await amount.press("Tab");
      await expect(designer.inspector.getByRole("alert")).toHaveText(
        "Enter a positive number of seconds.",
      );
      await selectChoice(unit, "s");
      await amount.fill("2");
      await selectChoice(unit, "h");
      await page.locator(".inspector-header h2").click();
      await expect(node(page, "wait-1")).toContainText("2 h");
      expect(await sourceText(page)).toContain("durationSeconds: 7200");
      // Workflow timeout uses the same control and stays optional.
      await designer.deselect();
      if (!(await designer.inspector.isVisible()))
        await command(page, "Show inspector");
      await designer.inspector
        .getByRole("spinbutton", { name: "Workflow timeout", exact: true })
        .fill("3");
      await selectChoice(
        designer.inspector.getByRole("combobox", {
          name: "Workflow timeout unit",
        }),
        "d",
      );
      await page.locator(".inspector-header h2").click();
      expect(await sourceText(page)).toContain("timeoutSeconds: 259200");
    });

    test("a human task can branch on its decisions in one click", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      await insertStep(page, "Human task");
      const designer = new DesignerPage(page);
      await designer.selectStep("approval-1");
      await stepControl(designer, "Create a path for each answer").click();
      await expect(page.locator('[data-step="decision-1"]')).toBeVisible();
      await expect(
        designer.target("Add a step here, in Reject of decision-1"),
      ).toHaveCount(1);
      const doc = parse(await sourceText(page)) as {
        spec: { steps: { id: string; cases?: { when: unknown }[] }[] };
      };
      expect(doc.spec.steps.map((s) => s.id)).toEqual([
        "approval-1",
        "decision-1",
      ]);
      expect(doc.spec.steps[1].cases?.map((c) => c.when)).toEqual([
        {
          op: {
            name: "eq",
            args: [
              { ref: "/steps/approval-1/output/decision" },
              { literal: "approve" },
            ],
          },
        },
        {
          op: {
            name: "eq",
            args: [
              { ref: "/steps/approval-1/output/decision" },
              { literal: "reject" },
            ],
          },
        },
      ]);
      await expect(node(page, "decision-1")).not.toHaveAccessibleName(
        /choose when this path applies/,
      );
    });
  });
