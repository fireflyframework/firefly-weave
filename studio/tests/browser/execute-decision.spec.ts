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
// A decision's Output after Execute step: each path as Taken, Not taken or
// Not checked, then the taken path's result.
import { expect, test } from "@playwright/test";
import { openExecuteWorkflow, shownExecute } from "./execute-po";
import { hasPython } from "./local-authoring";
import { sizes, yamlFile } from "./step-details-po";
import { outputPane } from "./test-data-po";

test.skip(!hasPython, "Create the Python development environment first.");

/** A decision on the amount; without test input Studio executes with a sample (amount 1). */
const decision = (
  first: number,
  second: number,
  result = "{literal: {tier: big}}",
) =>
  yamlFile(
    "decision.yaml",
    `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: routed, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      amount: {type: number, title: Amount}
  outputSchema: {}
  steps:
    - id: route
      kind: switch
      cases:
        - when: {op: {name: gte, args: [{ref: /input/amount}, {literal: ${first}}]}}
          steps: []
          output: ${result}
        - when: {op: {name: gt, args: [{ref: /input/amount}, {literal: ${second}}]}}
          steps: []
          output: {literal: null}
      default: {steps: [], output: {literal: null}}
  output: {literal: null}
`,
  );

for (const size of sizes)
  test.describe(`a decision's paths at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a decision's Output lists each path as Taken, Not taken or Not checked", async ({
      page,
    }) => {
      const { details } = await openExecuteWorkflow(page, decision(0, 1000));
      await details.open("route");
      await (await shownExecute(details)).click();
      const pane = await outputPane(details);
      await expect(pane.locator(".pane-source-tag")).toHaveText("Simulated");
      await expect(pane.locator(".pane-count")).toHaveText("Took path 1");
      const rows = pane
        .getByRole("list", { name: "Paths" })
        .getByRole("listitem");
      await expect(rows).toHaveCount(3);
      await expect(rows.nth(0)).toContainText("Path 1 ·");
      await expect(rows.nth(0)).toContainText("Taken");
      await expect(rows.nth(1)).toContainText("Path 2 ·");
      await expect(rows.nth(1)).toContainText("Not checked");
      await expect(rows.nth(2)).toContainText("Otherwise");
      await expect(rows.nth(2)).toContainText("Not taken");
      await expect(pane.locator(".path-result-caption")).toHaveText(
        "Path 1 result",
      );
      await expect(pane.locator(".path-result")).toContainText('"tier": "big"');
    });

    test("when no path applies, Otherwise is taken and every case not", async ({
      page,
    }) => {
      const { details } = await openExecuteWorkflow(page, decision(2, 1000));
      await details.open("route");
      await (await shownExecute(details)).click();
      const pane = await outputPane(details);
      await expect(pane.locator(".pane-count")).toHaveText("Took Otherwise");
      const rows = pane
        .getByRole("list", { name: "Paths" })
        .getByRole("listitem");
      await expect(rows.nth(0)).toContainText("Not taken");
      await expect(rows.nth(1)).toContainText("Not taken");
      await expect(rows.nth(2)).toContainText("Taken");
      await expect(pane.locator(".path-result-empty")).toHaveText(
        "Otherwise has no result, so later steps receive an empty object. Add a path result to pass data on.",
      );
    });
  });

test("a decision keeps its paths after executing another step", async ({
  page,
}) => {
  const { details } = await openExecuteWorkflow(
    page,
    yamlFile(
      "kept-paths.yaml",
      `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: kept-paths, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      amount: {type: number, title: Amount}
  outputSchema: {}
  steps:
    - id: first
      kind: transform
      value: {object: {amount: {ref: /input/amount}}}
    - id: route
      kind: switch
      cases:
        - when: {op: {name: gte, args: [{ref: /input/amount}, {literal: 0}]}}
          steps: []
          output: {literal: {tier: big}}
      default: {steps: [], output: {literal: null}}
  output: {literal: null}
`,
    ),
  );
  await details.open("route");
  await (await shownExecute(details)).click();
  let pane = await outputPane(details);
  await expect(pane.locator(".pane-count")).toHaveText("Took path 1");
  await details.close();
  // Executing an earlier step doesn't reach the decision.
  await details.open("first");
  const answered = page.waitForResponse("**/studio/local/debug/execute");
  await (await shownExecute(details)).click();
  await answered;
  await details.close();
  await details.open("route");
  pane = await outputPane(details);
  await expect(pane.locator(".pane-count")).toHaveText("Took path 1");
  await expect(
    pane.getByRole("list", { name: "Paths" }).getByRole("listitem").first(),
  ).toContainText("Taken");
  await details.close();
  const line = page.locator('[data-step="route"] .tile-output');
  await expect(line).toHaveText("Path: Amount ≥ 0");
});
