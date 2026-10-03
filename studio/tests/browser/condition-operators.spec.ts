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
import { selectChoice } from "./support";
import { test, expect } from "@playwright/test";
import { parse } from "yaml";
import { offline, newWorkflow, sourceText } from "./support";
import { DesignerPage } from "./designer-po";

const fixture = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: operators, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      name: {type: string, title: Name}
      amount: {type: number, title: Amount}
      region: {type: string, title: Region, enum: [eu, us]}
      enabled: {type: boolean, title: Enabled}
      scores: {type: array, title: Scores, items: {type: integer}}
  outputSchema: {type: object}
  steps:
    - id: route
      kind: switch
      cases:
        - steps: []
          output: {literal: {}}
      default: {steps: [], output: {literal: {}}}
  output: {literal: {}}
`;
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    test("text comparisons write real operators and exclude numeric tests", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(fixture);
      await designer.selectStep("route");
      const field = designer.inspector.locator('[data-field="cases/0/when"]');
      await field
        .getByRole("combobox", { name: "Condition 1 data", exact: true })
        .fill("/input/name");
      const operator = field.getByLabel("Condition 1 test", { exact: true });
      await operator.click();
      await expect(
        field.locator('[role="option"][data-value="gt"]'),
      ).toHaveCount(0);
      await operator.press("Escape");
      for (const name of [
        "contains",
        "notContains",
        "startsWith",
        "endsWith",
      ]) {
        await selectChoice(operator, name);
        await field
          .getByLabel("Condition 1 value", { exact: true })
          .fill("Ann");
        await page.locator(".inspector-header h2").click();
        expect(
          parse(await sourceText(page)).spec.steps[0].cases[0].when,
        ).toEqual({
          op: { name, args: [{ ref: "/input/name" }, { literal: "Ann" }] },
        });
        await designer.selectStep("route");
      }
    });
    test("membership builds a numeric list and reopens without rewriting it", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(fixture);
      await designer.selectStep("route");
      const field = designer.inspector.locator('[data-field="cases/0/when"]');
      await field
        .getByRole("combobox", { name: "Condition 1 data", exact: true })
        .fill("/input/amount");
      await field.getByLabel("Condition 1 test", { exact: true }).click();
      await expect(
        field.locator('[role="option"][data-value="in"]'),
      ).toHaveCount(1);
      await field
        .getByLabel("Condition 1 test", { exact: true })
        .press("Escape");
      await selectChoice(
        field.getByLabel("Condition 1 test", { exact: true }),
        "in",
      );
      const first = field.getByLabel("Condition 1 item 1", { exact: true });
      await expect(first).toHaveAttribute("type", "number");
      await first.fill("1");
      await field
        .getByRole("button", { name: "Add item", exact: true })
        .click();
      const second = field.getByLabel("Condition 1 item 2", { exact: true });
      await expect(second).toBeFocused();
      await second.fill("2.5");
      await page.locator(".inspector-header h2").click();
      const source = await sourceText(page);
      expect(parse(source).spec.steps[0].cases[0].when).toEqual({
        op: {
          name: "in",
          args: [{ ref: "/input/amount" }, { literal: [1, 2.5] }],
        },
      });
      await designer.selectStep("route");
      await expect(second).toHaveValue("2.5");
      expect(await sourceText(page)).toBe(source);
      await designer.selectStep("route");
      await selectChoice(
        field.getByLabel("Condition 1 test", { exact: true }),
        "notIn",
      );
      await field
        .getByRole("button", { name: "Remove item 1", exact: true })
        .click();
      await page.locator(".inspector-header h2").click();
      expect(parse(await sourceText(page)).spec.steps[0].cases[0].when).toEqual(
        {
          op: {
            name: "notIn",
            args: [{ ref: "/input/amount" }, { literal: [2.5] }],
          },
        },
      );
    });
    test("a list contains a typed item, not serialized text", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(fixture);
      await designer.selectStep("route");
      const field = designer.inspector.locator('[data-field="cases/0/when"]');
      await field
        .getByRole("combobox", { name: "Condition 1 data", exact: true })
        .fill("/input/scores");
      await field.getByLabel("Condition 1 test", { exact: true }).click();
      await expect(
        field.locator('[role="option"][data-value="contains"]'),
      ).toHaveCount(1);
      await field
        .getByLabel("Condition 1 test", { exact: true })
        .press("Escape");
      await selectChoice(
        field.getByLabel("Condition 1 test", { exact: true }),
        "contains",
      );
      const value = field.getByLabel("Condition 1 value", { exact: true });
      await expect(value).toHaveAttribute("type", "number");
      await value.fill("7");
      await page.locator(".inspector-header h2").click();
      expect(parse(await sourceText(page)).spec.steps[0].cases[0].when).toEqual(
        {
          op: {
            name: "contains",
            args: [{ ref: "/input/scores" }, { literal: 7 }],
          },
        },
      );
    });
    test("membership uses choices and yes/no controls without JSON entry", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(fixture);
      for (const [ref, choice, literal] of [
        ["/input/region", '"eu"', "eu"],
        ["/input/enabled", "false", false],
      ] as const) {
        await designer.selectStep("route");
        const field = designer.inspector.locator('[data-field="cases/0/when"]');
        await field
          .getByRole("combobox", { name: "Condition 1 data", exact: true })
          .fill(ref);
        await field
          .getByRole("combobox", { name: "Condition 1 data", exact: true })
          .press("Enter");
        await selectChoice(
          field.getByLabel("Condition 1 test", { exact: true }),
          "in",
        );
        await selectChoice(
          field.getByRole("combobox", {
            name: "Condition 1 item 1",
            exact: true,
          }),
          choice,
        );
        await page.locator(".inspector-header h2").click();
        expect(
          parse(await sourceText(page)).spec.steps[0].cases[0].when,
        ).toEqual({
          op: { name: "in", args: [{ ref }, { literal: [literal] }] },
        });
      }
    });
  });
}
