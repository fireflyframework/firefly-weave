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
// After a reload of a platform draft: the last execution's results come back
// from this computer, with each decision's path, and the Output keeps the
// view the person chose.
import { expect, test } from "@playwright/test";
import { CanvasPage } from "./canvas-po";
import { fresh, openDraft } from "./connected-test-data-po";
import { hasPython, localAuthoring } from "./local-authoring";
import { StepDetailsPage } from "./step-details-po";
import { outputPane } from "./test-data-po";

test.skip(!hasPython, "Create the Python development environment first.");

const document = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "order-intake", version: "1.0.0" },
  spec: {
    inputSchema: {
      type: "object",
      properties: { amount: { type: "number", title: "Amount" } },
    },
    outputSchema: { type: "object" },
    steps: [
      {
        id: "rows",
        kind: "transform",
        value: { literal: [{ a: 1 }, { a: 2, b: "x" }] },
      },
      {
        id: "route",
        kind: "switch",
        cases: [
          {
            when: {
              op: {
                name: "gte",
                args: [{ ref: "/input/amount" }, { literal: 0 }],
              },
            },
            steps: [],
            output: { literal: { tier: "big" } },
          },
        ],
        default: { steps: [], output: { literal: null } },
      },
    ],
    output: { literal: {} },
  },
};

test("after a reload, results, a decision's path and the chosen view come back", async ({
  page,
}) => {
  await openDraft(page, { ...fresh(), document });
  await localAuthoring(page);
  const executed = page.waitForResponse("**/studio/local/debug/execute");
  await page
    .locator(".editor-bar")
    .getByRole("button", { name: "Execute workflow" })
    .click();
  expect((await executed).status()).toBe(200);
  const line = page.locator('[data-step="route"] .tile-output');
  await expect(line).toHaveText("Path: Amount ≥ 0");
  const details = new StepDetailsPage(page);
  await details.open("rows");
  const pane = await outputPane(details);
  await expect(pane.getByRole("radio", { name: "Table" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  await pane.getByRole("radio", { name: "JSON" }).click();
  await details.close();
  await expect
    .poll(() =>
      page.evaluate(() =>
        Object.keys(localStorage).some((key) =>
          key.includes("weave.lastExecution."),
        ),
      ),
    )
    .toBe(true);

  await page.reload();
  await new CanvasPage(page).ready();
  await expect(line).toHaveText("Path: Amount ≥ 0", { timeout: 30_000 });
  await details.open("rows");
  const again = await outputPane(details);
  await expect(again.getByRole("radio", { name: "JSON" })).toHaveAttribute(
    "aria-checked",
    "true",
  );
  await expect(again.locator(".pane-json")).toContainText('"b": "x"');
  await details.close();
  await details.open("route");
  const paths = await outputPane(details);
  await expect(paths.locator(".pane-count")).toHaveText("Took path 1");
});
