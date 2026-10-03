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
import { readFileSync } from "node:fs";
import { test, expect } from "@playwright/test";
import { parse } from "yaml";
import {
  connected,
  offline,
  newWorkflow,
  sourceText,
  closeSheet,
  command,
} from "./support";
import { tabStops } from "./ux-assertions";
import { DesignerPage } from "./designer-po";

const workflow = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: slots, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {type: object}
  connections:
    ledger: {connector: weave-postgresql@1.0.0, required: true}
  steps:
    - id: lookup
      kind: action
      uses: sql.lookup@1.0.0
      connection: ledger
      with: {literal: {}}
    - id: parallel
      kind: parallel
      concurrency: 1
      branches:
        audit:
          steps:
            - id: nested-lookup
              kind: action
              uses: sql.lookup@1.0.0
              connection: ledger
              with: {literal: {}}
          output: {literal: {}}
  output: {literal: {}}
`;
for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe(`Inspector connections at ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    test("renaming a connection slot updates nested uses and undo restores all of them", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(workflow);
      await designer.deselect();
      if (!(await designer.inspector.isVisible()))
        await command(page, "Show inspector");
      const list = designer.inspector.getByRole("region", {
        name: "Connection slots",
        exact: true,
      });
      await expect(
        list.getByText("Used by 2 steps", { exact: true }),
      ).toBeVisible();
      const name = list.getByRole("textbox", {
        name: "Slot name",
        exact: true,
      });
      await name.fill("finance");
      await name.press("Tab");
      await expect(name).toHaveValue("finance");
      const result = parse(await sourceText(page));
      expect(result.spec.connections).toEqual({
        finance: { connector: "weave-postgresql@1.0.0", required: true },
      });
      expect(result.spec.steps[0].connection).toBe("finance");
      expect(result.spec.steps[1].branches.audit.steps[0].connection).toBe(
        "finance",
      );
      await closeSheet(page);
      await command(page, "Undo");
      const restored = parse(await sourceText(page));
      expect(restored.spec.connections.ledger.connector).toBe(
        "weave-postgresql@1.0.0",
      );
      expect(restored.spec.steps[0].connection).toBe("ledger");
      expect(restored.spec.steps[1].branches.audit.steps[0].connection).toBe(
        "ledger",
      );
    });
    test("removing a used slot explains the impact and leaves no dangling references", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(workflow);
      await designer.deselect();
      if (!(await designer.inspector.isVisible()))
        await command(page, "Show inspector");
      const list = designer.inspector.getByRole("region", {
        name: "Connection slots",
        exact: true,
      });
      await list
        .getByRole("button", { name: "Remove ledger", exact: true })
        .click();
      const dialog = page.getByRole("dialog", {
        name: "Remove connection slot?",
        exact: true,
      });
      await expect(dialog).toContainText("2 steps");
      await dialog
        .getByRole("button", { name: "Remove slot", exact: true })
        .click();
      const result = parse(await sourceText(page));
      expect(result.spec.connections).toBeUndefined();
      expect(result.spec.steps[0]).not.toHaveProperty("connection");
      expect(result.spec.steps[1].branches.audit.steps[0]).not.toHaveProperty(
        "connection",
      );
    });
  });
}

test.describe("Mapping controls", () => {
  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 600, height: 500 },
  ])
    test(`typed data mapping preserves values with at most thirty input tab stops at ${viewport.width}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      await connected(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(
        workflow.replace(
          "inputSchema: {type: object}",
          "inputSchema: {type: object, properties: {customerId: {type: string, title: Customer ID}, amount: {type: number}}}",
        ),
      );
      await designer.selectStep("lookup");
      const form = designer.inspector.locator("weave-task-form");
      const customer = form
        .locator(".schema-field")
        .filter({ has: page.getByText("Customer ID", { exact: true }) });
      await customer
        .getByLabel("Customer ID", { exact: true })
        .fill("manual-customer");
      await customer
        .getByRole("button", { name: "Use data", exact: true })
        .click();
      const combo = customer.getByRole("combobox");
      await expect(combo).toHaveValue("");
      await combo.press("ArrowDown");
      const list = page.getByRole("listbox");
      await expect(list.getByRole("option").first()).toContainText(
        "Customer ID",
      );
      await expect(list.locator(".ref-combo-path")).toHaveCount(0);
      await list.locator('[data-ref="/input/customerId"]').click();
      await expect(combo).toHaveValue("Input › Customer ID");
      await customer
        .getByRole("button", { name: "Remove data", exact: true })
        .click();
      await expect(
        customer.getByLabel("Customer ID", { exact: true }),
      ).toHaveValue("manual-customer");
      expect(
        parse(await sourceText(page)).spec.steps[0].with.literal.parameters
          .customerId,
      ).toBe("manual-customer");
      await designer.selectStep("lookup");
      await form.getByRole("button").first().focus();
      expect(await tabStops(page, form)).toBeLessThanOrEqual(30);
    });
});

test("rules compare data to data and keep unfinished rules when switching to a formula", async ({
  page,
}) => {
  await offline(page);
  await newWorkflow(page);
  const designer = new DesignerPage(page);
  await designer.setSource(`apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: comparisons, version: 1.0.0}
spec:
  inputSchema: {type: object, properties: {amount: {type: number}, limit: {type: number}}}
  outputSchema: {type: object}
  steps:
    - id: route
      kind: switch
      cases: [{steps: [], output: {literal: {}}}]
      default: {steps: [], output: {literal: {}}}
  output: {literal: {}}
`);
  await designer.selectStep("route");
  const rules = designer.inspector.locator("weave-condition-editor");
  await expect(rules.locator(".field-error")).toHaveCount(0);
  await rules
    .getByRole("combobox", { name: "Condition 1 data", exact: true })
    .fill("/input/amount");
  await selectChoice(
    rules.getByRole("combobox", { name: "Condition 1 test", exact: true }),
    "gt",
  );
  await rules.getByRole("button", { name: "Use data", exact: true }).click();
  await rules
    .getByRole("combobox", { name: "Rule 1 comparison data", exact: true })
    .fill("/input/limit");
  await rules.getByRole("button", { name: "Add rule", exact: true }).click();
  await expect(
    rules.getByRole("combobox", { name: "Condition 2 data", exact: true }),
  ).toBeFocused();
  await rules
    .getByRole("combobox", { name: "Condition 2 data", exact: true })
    .fill("/input/amount");
  await rules
    .getByRole("button", { name: "Edit as formula", exact: true })
    .click();
  await expect(rules).toBeVisible();
  await expect(rules).toContainText("Finish this condition");
  await rules
    .getByRole("spinbutton", { name: "Condition 2 value", exact: true })
    .fill("10");
  await rules
    .getByRole("button", { name: "Edit as formula", exact: true })
    .click();
  const source = parse(await sourceText(page));
  expect(source.spec.steps[0].cases[0].when).toEqual({
    op: {
      name: "and",
      args: [
        {
          op: {
            name: "gt",
            args: [{ ref: "/input/amount" }, { ref: "/input/limit" }],
          },
        },
        {
          op: { name: "eq", args: [{ ref: "/input/amount" }, { literal: 10 }] },
        },
      ],
    },
  });
});

test("Decision paths keep rules, result and reorder actions in one card with Otherwise fixed last", async ({
  page,
}) => {
  await offline(page);
  await newWorkflow(page);
  const designer = new DesignerPage(page);
  await designer.setSource(
    readFileSync("tests/fixtures/vendor-payment-approval.yaml", "utf8"),
  );
  await designer.selectStep("route");
  const cards = designer.inspector.locator(".decision-path");
  await expect(cards).toHaveCount(3);
  await expect(
    cards.first().getByRole("heading", { name: "Approve", exact: true }),
  ).toBeVisible();
  await expect(
    cards.last().getByRole("heading", { name: "Otherwise", exact: true }),
  ).toBeVisible();
  await expect(
    cards.first().getByText("Not set (optional)", { exact: false }),
  ).toBeVisible();
  await cards
    .nth(1)
    .getByRole("button", { name: "Move path up", exact: true })
    .click();
  await expect(
    cards.first().getByRole("heading", { name: "Reject", exact: true }),
  ).toBeVisible();
  await designer.inspector
    .getByRole("button", { name: "+ Add path", exact: true })
    .click();
  await expect(cards).toHaveCount(4);
  await expect(
    cards.last().getByRole("heading", { name: "Otherwise", exact: true }),
  ).toBeVisible();
  const doc = parse(await sourceText(page));
  expect(
    doc.spec.steps.find((s: any) => s.id === "route").cases[0].when.op.args[1],
  ).toEqual({ literal: "reject" });
});

test("Workflow settings group compact fields and let Choice fields constrain inputs", async ({
  page,
}) => {
  await offline(page);
  await newWorkflow(page);
  const designer = new DesignerPage(page);
  await designer.setSource(
    workflow.replace(
      "inputSchema: {type: object}",
      "inputSchema: {type: object, properties: {customer: {type: string}}, required: [customer]}",
    ),
  );
  await page
    .getByRole("button", { name: "Start — workflow settings", exact: true })
    .click();
  const inspector = designer.inspector;
  for (const name of ["Inputs", "Result", "Connections", "Limits"])
    await expect(
      inspector.getByRole("heading", { name, exact: true }),
    ).toBeVisible();
  const inputs = inspector.locator('[data-field="spec/inputSchema"]');
  await expect(
    inputs.getByRole("textbox", { name: "Field name", exact: true }),
  ).toBeHidden();
  await inputs
    .getByRole("button", { name: "Edit customer", exact: true })
    .click();
  await expect(
    inputs.getByRole("textbox", { name: "Field name", exact: true }),
  ).toHaveValue("customer");
  await selectChoice(
    inputs.getByRole("combobox", { name: "Type of “customer”" }),
    "choice",
  );
  await inputs
    .getByRole("textbox", { name: "Allowed values", exact: true })
    .fill("Standard\nPriority");
  const doc = parse(await sourceText(page));
  expect(doc.spec.inputSchema.properties.customer).toEqual({
    type: "string",
    enum: ["Standard", "Priority"],
  });
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe(`Compact schema row actions at ${viewport.width}`, () => {
    test.use({ viewport });
    test("reorders and removes collapsed fields with keyboard focus and undo", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(
        workflow.replace(
          "inputSchema: {type: object}",
          "inputSchema: {type: object, properties: {customer: {type: string}, amount: {type: number}}, required: [customer]}",
        ),
      );
      await page
        .getByRole("button", { name: "Start — workflow settings", exact: true })
        .click();
      const inputs = designer.inspector.locator(
        '[data-field="spec/inputSchema"]',
      );
      const colors = await inputs.evaluate((element) => [
        getComputedStyle(element.querySelector("button.sd-summary")!).color,
        getComputedStyle(element).color,
      ]);
      expect(colors[0]).toBe(colors[1]);
      await inputs
        .getByRole("button", { name: "Options for customer", exact: true })
        .click();
      await expect(
        page.getByRole("menuitem", { name: "Move up", exact: true }),
      ).toHaveAttribute("aria-disabled", "true");
      await page
        .getByRole("menuitem", { name: "Move down", exact: true })
        .click();
      await expect(
        inputs.getByRole("button", { name: "Edit customer", exact: true }),
      ).toBeFocused();
      await expect(inputs.locator(".sd-summary")).toHaveText([
        "amountNumber",
        "customerText · Required",
      ]);
      expect(
        Object.keys(parse(await sourceText(page)).spec.inputSchema.properties),
      ).toEqual(["amount", "customer"]);
      await page
        .getByRole("button", { name: "Start — workflow settings", exact: true })
        .click();
      await inputs
        .getByRole("button", { name: "Options for customer", exact: true })
        .click();
      await page
        .getByRole("menuitem", { name: "Remove field", exact: true })
        .click();
      await expect(
        inputs.getByRole("button", { name: "Edit amount", exact: true }),
      ).toBeFocused();
      expect(
        Object.keys(parse(await sourceText(page)).spec.inputSchema.properties),
      ).toEqual(["amount"]);
      await closeSheet(page);
      await command(page, "Undo");
      expect(
        Object.keys(parse(await sourceText(page)).spec.inputSchema.properties),
      ).toEqual(["amount", "customer"]);
    });
  });
}
