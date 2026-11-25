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
// Adding a transaction from Add a step: its entry in Flow and the words that
// find it, what it inserts and what it still needs, and why it isn't offered
// inside another one. The platform is mocked; the check's answer is the one
// the Studio host gives for a transaction without steps.
import { expect, test, type Page } from "@playwright/test";
import { parse } from "yaml";
import { AddStepPanel, languageFeatures } from "./add-step-po";
import { CanvasPage } from "./canvas-po";
import { openStepFixture, yamlFile } from "./step-details-po";
import { offline, showView, sourceText } from "./support";

const DESCRIPTION = "Run steps together; if one fails, undo what's done";
const workflow = (steps: string) =>
  yamlFile(
    "move-between-systems.yaml",
    `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: move-between-systems, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - id: lookup
      kind: transform
      value: {literal: {}}
${steps}  output: {literal: null}
`,
  );
/** move holds hold. */
const MOVE = workflow(`    - id: move
      kind: transaction
      steps:
        - id: hold
          kind: transform
          value: {literal: {}}
      output: {literal: null}
`);

async function open(
  page: Page,
  steps = workflow(""),
  features = ["text.concat", "flow.transaction"],
) {
  await openStepFixture(page, steps, async (p) => {
    await offline(p);
    await languageFeatures(p, features);
    // The host answers a transaction without steps on the main sequence's step.
    await p.route("**/studio/local/validate", (route) => {
      const empty = (
        route.request().postDataJSON() as { source: string }
      ).source.includes("steps: []");
      return route.fulfill({
        json: {
          validationOk: !empty,
          errorCount: empty ? 1 : 0,
          diagnostics: empty
            ? [
                {
                  code: "WV-SCHEMA-INVALID_INSTANCE",
                  severity: "error",
                  path: "/spec/steps/1",
                  message: "Value does not satisfy the declared contract.",
                },
              ]
            : [],
          partial: true,
          artifact: null,
        },
      });
    });
  });
  const canvas = new CanvasPage(page);
  await canvas.ready();
  return canvas;
}
interface Read {
  id: string;
  kind: string;
  steps?: Read[];
}
const stepsOf = async (page: Page) =>
  (parse(await sourceText(page)) as { spec: { steps: Read[] } }).spec.steps;

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`adding a transaction at ${size.width}x${size.height}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize(size);
    });

    test("Flow lists Transaction after Loop over items; it inserts an empty one that asks for a step", async ({
      page,
    }) => {
      const canvas = await open(page);
      await canvas.insertTarget("Add a step after lookup").click();
      const panel = new AddStepPanel(page);
      await expect(panel.search).toBeFocused();
      await panel.option("Flow").click();
      await expect(panel.title).toHaveText("Flow");
      expect(await panel.labels()).toEqual([
        "Decision",
        "Parallel",
        "Loop over items",
        "Transaction",
        "Call a workflow",
        "Stop with error",
      ]);
      const entry = panel.option("Transaction");
      await expect(entry).toContainText(DESCRIPTION);
      await expect(entry.locator("weave-icon").first()).toHaveAttribute(
        "data-icon",
        "transaction",
      );
      await expect(page.locator("weave-icon[data-icon-missing]")).toHaveCount(
        0,
      );
      await entry.click();
      await expect(
        page.getByRole("dialog", {
          name: "Step details: transaction-1",
          exact: true,
        }),
      ).toBeVisible();
      await canvas.closeInspector();
      await expect(canvas.tile("transaction-1")).toContainText("Transaction");
      // The check says what to do, not that a value doesn't fit.
      await expect(page.locator(".diagnostics-headline")).toContainText(
        "1 error",
      );
      const toggle = page.getByRole("button", { name: "Diagnostics" });
      if ((await toggle.getAttribute("aria-expanded")) === "false")
        await toggle.click();
      await expect(
        page.getByRole("list", { name: "Problems found" }),
      ).toContainText("Add a step to the transaction.");
      await expect(canvas.tileBody("transaction-1")).toHaveAccessibleName(
        /Add a step to the transaction\./,
      );
      expect(await stepsOf(page)).toEqual([
        { id: "lookup", kind: "transform", value: { literal: {} } },
        {
          id: "transaction-1",
          kind: "transaction",
          steps: [],
          output: { literal: null },
        },
      ]);
    });

    test("typing rollback finds Transaction, and nothing on screen says it", async ({
      page,
    }) => {
      const canvas = await open(page);
      await canvas.insertTarget("Add a step after lookup").click();
      const panel = new AddStepPanel(page);
      await expect(panel.search).toBeFocused();
      await page.keyboard.type("rollback");
      await expect.poll(() => panel.activeLabel()).toBe("Transaction");
      await expect(panel.root).not.toContainText(/rollback/i);
      await page.keyboard.press("Enter");
      await expect(
        page.getByRole("dialog", { name: "Step details: transaction-1" }),
      ).toBeVisible();
    });

    test("without transactions on the platform, the entry stays listed with its reason", async ({
      page,
    }) => {
      const canvas = await open(page, workflow(""), ["text.concat"]);
      await canvas.insertTarget("Add a step after lookup").click();
      const panel = new AddStepPanel(page);
      await expect(panel.search).toBeFocused();
      await panel.option("Flow").click();
      const entry = panel.option("Transaction");
      await expect(entry).toHaveAttribute("aria-disabled", "true");
      await expect(entry).toContainText(
        "Needs a platform with transactions (flow.transaction)",
      );
    });
  });

test.describe("adding a step inside a transaction", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("inside a transaction, Send email goes into its own steps and Transaction says why it can't", async ({
    page,
  }) => {
    const canvas = await open(page, MOVE);
    await showView(page, "Outline");
    await page.getByRole("treeitem", { name: /^hold/ }).click();
    await showView(page, "Designer");
    await canvas.ready();
    await page
      .getByRole("button", { name: "Add a step", exact: true })
      .first()
      .click();
    const panel = new AddStepPanel(page);
    await expect(panel.search).toBeFocused();
    await panel.option("Flow").click();
    const entry = panel.option("Transaction");
    await expect(entry).toHaveAttribute("aria-disabled", "true");
    await expect(entry).toContainText(
      "A transaction can't contain another transaction.",
    );
    await page.keyboard.press("ArrowLeft");
    await panel.choose("Send email");
    await expect(
      page.getByRole("dialog", { name: "Step details: send-email" }),
    ).toBeVisible();
    await canvas.closeInspector();
    const move = (await stepsOf(page))[1];
    expect(move.steps!.map((step) => step.id)).toEqual(["hold", "send-email"]);
  });
});
