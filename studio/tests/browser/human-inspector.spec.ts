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
import { expect, test } from "@playwright/test";
import { parse } from "yaml";
import { DesignerPage } from "./designer-po";
import {
  allCapabilities,
  connected,
  newWorkflow,
  offline,
  sourceText,
} from "./support";
import { expectInViewport } from "./ux-assertions";

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    test("human task sections expose typed answers, preview and deadlines", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("humanTask");
      await designer.selectStep("approval-1");
      const inspector = designer.inspector;
      await expect(inspector.locator(".human-section > h3")).toHaveText([
        "Who",
        "What they see",
        "How they answer",
      ]);
      await expect(inspector).toContainText(
        "Choose the people or groups when activating this workflow.",
      );
      await inspector
        .getByLabel("Title", { exact: true })
        .fill("Review invoice");
      const form = inspector.locator('[data-field="formSchema"]');
      await form
        .getByRole("button", { name: "Add field", exact: true })
        .click();
      await form.getByLabel("Field name", { exact: true }).fill("comment");
      await expect(form.locator(".sd-preview")).toContainText("Comment");
      await inspector.locator(".human-deadlines > summary").click();
      const due = inspector.getByLabel("Due after", { exact: true });
      await due.fill("2");
      await selectChoice(
        inspector.getByLabel("Due after unit", { exact: true }),
        "h",
      );
      await expectInViewport(due);
      await expect(inspector).toContainText("marks the task as overdue");
      await expect(inspector).toContainText(
        "the task expires and can no longer be answered",
      );
      const step = parse(await sourceText(page)).spec.steps[0];
      expect(step.title).toEqual({ literal: "Review invoice" });
      expect(step.formSchema.properties.comment).toEqual({ type: "string" });
      expect(step.dueSeconds).toBe(7200);
      expect(step.decisions).toEqual(["approve", "reject"]);
    });
    test("answer chips reject duplicates and renames update generated paths with one undo", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("humanTask");
      await designer.selectStep("approval-1");
      const inspector = designer.inspector;
      await inspector
        .getByRole("button", {
          name: "Create a path for each answer",
          exact: true,
        })
        .click();
      await expect(designer.node("decision-1")).toHaveCount(1);
      await designer.selectStep("approval-1");
      const first = inspector.getByLabel("Answer 1", { exact: true });
      await first.fill("reject");
      await first.press("Tab");
      await expect(
        inspector.getByText("Use a unique answer.", { exact: true }),
      ).toBeVisible();
      await first.fill("accept");
      await first.press("Tab");
      await expect(
        inspector.getByText("Use a unique answer.", { exact: true }),
      ).toHaveCount(0);
      let doc = parse(await sourceText(page));
      expect(doc.spec.steps[0].decisions).toEqual(["accept", "reject"]);
      expect(doc.spec.steps[1].cases[0].when.op.args[1]).toEqual({
        literal: "accept",
      });
      await designer.selectStep("approval-1");
      await inspector.getByLabel("Step name").press("ControlOrMeta+z");
      doc = parse(await sourceText(page));
      expect(doc.spec.steps[0].decisions).toEqual(["approve", "reject"]);
      expect(doc.spec.steps[1].cases[0].when.op.args[1]).toEqual({
        literal: "approve",
      });
    });
  });
}

test("connected human tasks suggest enabled bindings and title data stays typed", async ({
  page,
}) => {
  await connected(page, {
    capabilities: [...allCapabilities, "assignment.read"],
  });
  await page.route("**/human-assignments", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            binding_id: "11111111-1111-4111-8111-111111111111",
            name: "finance-reviewers",
            enabled: true,
          },
          {
            binding_id: "22222222-2222-4222-8222-222222222222",
            name: "old-team",
            enabled: false,
          },
        ],
      },
    }),
  );
  await newWorkflow(page);
  const designer = new DesignerPage(page);
  await designer.setSource(`apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: review-invoice, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      summary: {type: string, title: Summary}
      amount: {type: number, title: Amount}
  outputSchema: {type: object}
  steps:
    - id: approval
      kind: humanTask
      assignment: reviewers
      title: {literal: Review request}
      context: {literal: {}}
      decisions: [approve, reject]
      formSchema: {type: object, properties: {}}
  output: {literal: {}}
`);
  await designer.selectStep("approval");
  const inspector = designer.inspector;
  await expect(inspector.locator("datalist option")).toHaveAttribute(
    "value",
    "finance-reviewers",
  );
  await inspector
    .getByLabel("Assignment binding", { exact: true })
    .fill("finance-reviewers");
  await inspector
    .getByRole("button", { name: "Insert data", exact: true })
    .click();
  const titleData = inspector.getByRole("combobox", {
    name: "Title data",
    exact: true,
  });
  await titleData.fill("Summary");
  await page.getByRole("option", { name: /Summary/ }).click();
  await expect(inspector.locator(".title-token")).toContainText(
    "Input › Summary",
  );
  const doc = parse(await sourceText(page));
  expect(doc.spec.steps[0].title).toEqual({ ref: "/input/summary" });
  expect(doc.spec.steps[0].assignment).toBe("finance-reviewers");
  await designer.selectStep("approval");
  await inspector
    .getByRole("button", { name: "Remove title data", exact: true })
    .click();
  expect(parse(await sourceText(page)).spec.steps[0].title).toEqual({
    literal: "Review request",
  });
});
