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
// Reproductions of the "Call an action" editor defects, driven with real
// pointer and keyboard input so hit-testing and focus behave as for a person.
import { selectChoice } from "./support";
import { test, expect, Page } from "@playwright/test";
import {
  connected,
  defaultCatalog,
  expectHitTarget,
  insertStep,
  lookupAction,
  newWorkflow,
  offline,
  sourceText,
} from "./support";
import { actionPicker, chooseAction } from "./integrations-po";

const desktopViewports = [
  { width: 1280, height: 720 },
  { width: 1440, height: 900 },
  { width: 1600, height: 1000 },
];
const inspectorHeading = (page: Page) => page.locator(".inspector header h2");
const draftErrors = (page: Page) =>
  page.locator(
    ".inspector .error:visible, .inspector .property-error:visible, .inspector .field-error:visible",
  );
const node = (page: Page, id: string) =>
  page.locator(`[data-step="${id}"] .node-body`);

async function chooseLookup(page: Page) {
  await chooseAction(page, "sql.lookup@1.0.0");
  await page.locator(".action-details > summary").click();
  await expect(
    page
      .locator(".integration-contract dl")
      .getByText("weave-postgresql@1.0.0", {
        exact: true,
      }),
  ).toBeVisible();
}

for (const viewport of desktopViewports) {
  test(`inspector controls receive real clicks at ${viewport.width}x${viewport.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await offline(page);
    await newWorkflow(page);
    await insertStep(page, "Call an action");
    const resizer = page.locator(".pane-resizer.left");
    if (await resizer.isVisible())
      expect(
        await resizer.evaluate((e) => e.getBoundingClientRect().width),
      ).toBeLessThanOrEqual(12);
    await expectHitTarget(page.getByLabel("Action version", { exact: true }));
    const add = page
      .getByRole("button", { name: "+ Add field", exact: true })
      .first();
    await expectHitTarget(add);
    await add.click({ timeout: 3000 });
    await page
      .getByLabel("Field name", { exact: true })
      .first()
      .fill("customer");
    await page.getByLabel("Field name", { exact: true }).first().press("Tab");
    await expect(page.getByLabel("Property value").first()).toBeVisible();
    await expectHitTarget(inspectorHeading(page));
  });

  test(`published action select is clickable at ${viewport.width}x${viewport.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await connected(page);
    await newWorkflow(page);
    await insertStep(page, "Call an action");
    const select = actionPicker(page);
    await expect(select).toBeEnabled();
    await expectHitTarget(select);
    await select.click({ timeout: 3000 });
  });
}

test("an action step added before the account check finishes still gets the catalog", async ({
  page,
}) => {
  const recorder = await connected(page, { identityDelay: 1500 });
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  // Inserted while Studio still checks the account: nothing to list yet.
  expect(recorder.catalogPages).toBe(0);
  await expect(actionPicker(page)).toBeEnabled({ timeout: 5000 });
  expect(recorder.catalogPages).toBe(1);
});

test("overlay inspector keeps fields reachable at 600x500", async ({
  page,
}) => {
  await page.setViewportSize({ width: 600, height: 500 });
  await offline(page);
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await node(page, "call-action-1").click();
  const button = page.getByLabel("Action version", { exact: true });
  await expect(button).toBeVisible();
  await button.scrollIntoViewIfNeeded();
  const box = await button.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.y + box!.height).toBeLessThanOrEqual(500);
  expect(box!.x + box!.width).toBeLessThanOrEqual(600);
  await expectHitTarget(button);
  await button.click({ timeout: 3000 });
});

test("re-clicking the selected node keeps live edits and loads the catalog once", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const recorder = await connected(page);
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await chooseLookup(page);
  await page.getByLabel(/^Label(\s*\(optional\))?$/).fill("draft label");
  await node(page, "call-action-1").click();
  await page.waitForTimeout(300);
  await expect(page.locator(".integration-contract dl")).toBeVisible();
  // The picker is the one place the action version shows.
  await expect(page.getByLabel("Action version", { exact: true })).toHaveCount(
    0,
  );
  await expect(page.getByLabel(/^Label(\s*\(optional\))?$/)).toHaveValue(
    "draft label",
  );
  await expect(actionPicker(page)).toHaveValue("sql.lookup@1.0.0");
  expect(recorder.catalogPages).toBe(1);
  await expect(page.locator(".editor-identity .status-chip")).toHaveText(
    "Unsaved",
  );
  expect(await sourceText(page)).not.toContain("positions");
});

test("reselecting an applied action shows its action and enum value", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const recorder = await connected(page);
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await chooseLookup(page);
  await page.getByLabel(/^Customer ID/).fill("customer-104");
  await selectChoice(page.getByLabel(/^Mode(\s*\(optional\))?$/), "safe");
  await inspectorHeading(page).click();
  await insertStep(page, "Transform");
  await expect(page.locator(".inspector header h2")).toHaveText("Transform");
  await node(page, "call-action-1").click();
  await expect(actionPicker(page)).toHaveValue("sql.lookup@1.0.0");
  await expect(page.getByLabel(/^Mode(\s*\(optional\))?$/)).toHaveAttribute(
    "data-value",
    "1",
  );
  await expect(page.getByLabel(/^Customer ID/)).toHaveValue("customer-104");
  expect(recorder.catalogPages).toBe(1);
});

test("the Fields builder and operator select show the stored value", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await offline(page);
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await expect(
    page.getByRole("button", { name: "+ Add field", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Value type", { exact: true })).toHaveCount(0);
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  await page.getByRole("textbox", { name: "Workflow source" }).fill(
    JSON.stringify({
      apiVersion: "weave/v1alpha1",
      kind: "Workflow",
      metadata: { name: "operators", version: "1.0.0" },
      spec: {
        inputSchema: { type: "object" },
        outputSchema: { type: "object" },
        steps: [
          {
            id: "check",
            kind: "transform",
            value: {
              op: {
                name: "and",
                args: [{ literal: true }, { literal: false }],
              },
            },
          },
        ],
        output: { literal: {} },
      },
    }),
  );
  await page
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await page.getByRole("tab", { name: "Designer", exact: true }).click();
  await node(page, "check").click();
  await expect(
    page.getByLabel("Value operator", { exact: true }),
  ).toHaveAttribute("data-value", "and");
});

test("typed JSON stays while invalid and numbers never coerce", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await connected(page);
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await chooseLookup(page);
  // Options is an open object: a list of named entries, each any JSON value.
  await page.getByRole("button", { name: "Add Options entry" }).click();
  // The new entry's name takes focus.
  await expect(
    page.getByLabel("Options name 1", { exact: true }),
  ).toBeFocused();
  const value = page.getByLabel("Options value 1", { exact: true });
  await value.fill('{"a":1');
  await expect(value).toHaveValue('{"a":1');
  await value.press("Tab");
  await expect(draftErrors(page).first()).toBeVisible();
  await value.focus();
  await value.press("End");
  await value.pressSequentially("}");
  const limit = page.getByLabel(/^Limit(\s*\(optional\))?$/);
  await limit.click();
  await page.keyboard.type("-");
  await expect(limit).not.toHaveValue("0");
  await limit.fill("");
  await page.getByLabel(/^Customer ID/).fill("customer-104");
  await expect(draftErrors(page)).toHaveCount(0);
  await inspectorHeading(page).click();
  const source = await sourceText(page);
  expect(source).not.toContain("limit: 0");
  expect(source).toContain("customerId: customer-104");
  expect(source).toContain("entry1:");
  expect(source).toContain("a: 1");
});

test("switching input editing keeps applied values", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await connected(page);
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await chooseAction(page, "crm.lookup@2.0.0");
  await page.getByLabel(/^Customer/).fill("edited");
  await inspectorHeading(page).click();
  await page
    .getByRole("button", { name: "Write one expression for the whole input" })
    .click();
  await page
    .getByRole("button", { name: "Edit the input field by field" })
    .click();
  await expect(page.getByLabel(/^Customer/)).toHaveValue("edited");
  await inspectorHeading(page).click();
  expect(await sourceText(page)).toContain("customer: edited");
});

test("Delete and arrows from inspector focus never touch steps", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await offline(page);
  await newWorkflow(page);
  await insertStep(page, "Wait for time");
  await insertStep(page, "Call an action");
  const add = page
    .getByRole("button", { name: "+ Add field", exact: true })
    .first();
  await add.focus();
  await page.keyboard.press("Delete");
  await page.keyboard.press("ArrowUp");
  await expect(page.locator('[data-step="call-action-1"]')).toHaveCount(1);
  await expect(page.locator(".inspector header h2")).toHaveText(
    "Call an action",
  );
  await inspectorHeading(page).focus();
  await page.keyboard.press("Delete");
  await expect(page.locator('[data-step="call-action-1"]')).toHaveCount(1);
  await page.locator(".pane-resizer.left").focus();
  await page.keyboard.press("Delete");
  await page.keyboard.press("ArrowDown");
  await expect(page.locator('[data-step="call-action-1"]')).toHaveCount(1);
  await expect(page.locator(".inspector header h2")).toHaveText(
    "Call an action",
  );
  await node(page, "call-action-1").click();
  await page.keyboard.press("Delete");
  await expect(page.locator('[data-step="call-action-1"]')).toHaveCount(0);
});

test("overlapping catalog loads never restore a stale contract", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await connected(page, { catalogDelays: [0, 300, 1500, 1500] });
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await chooseAction(page, "crm.lookup@2.0.0");
  await page.getByLabel(/^Customer/).fill("customer-104");
  await inspectorHeading(page).click();
  await insertStep(page, "Transform");
  await node(page, "call-action-1").click();
  const select = actionPicker(page);
  await expect(select).toBeEnabled({ timeout: 5000 });
  await chooseAction(page, "sql.lookup@1.0.0");
  const contract = page.locator(".integration-contract dl");
  await page.locator(".action-details > summary").click();
  await expect(
    contract.getByText("weave-postgresql@1.0.0", { exact: true }),
  ).toBeVisible();
  await page.waitForTimeout(2000);
  await expect(actionPicker(page)).toHaveValue("sql.lookup@1.0.0");
  await expect(
    contract.getByText("weave-postgresql@1.0.0", { exact: true }),
  ).toBeVisible();
  await expect(contract.getByText("crm-lookup")).toHaveCount(0);
});

test("a nested invalid value keeps an inline error after editing a sibling", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await offline(page);
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  const names = page.getByLabel("Field name", { exact: true });
  await page.getByRole("button", { name: "+ Add field", exact: true }).click();
  await names.first().fill("a");
  await names.first().press("Tab");
  await page.getByRole("button", { name: "a options", exact: true }).click();
  await page
    .getByRole("menuitem", { name: "Advanced: JSON value", exact: true })
    .click();
  await page.getByLabel("Advanced JSON value", { exact: true }).fill("0");
  await page
    .getByRole("button", { name: "Done editing JSON", exact: true })
    .click();
  await page.getByRole("button", { name: "+ Add field", exact: true }).click();
  await names.nth(1).fill("b");
  await names.nth(1).press("Tab");
  await expect(page.getByLabel("Property value")).toHaveCount(2);
  await expect(page.getByLabel("Property value").first()).toHaveValue("0");
  await page.getByLabel("Property value").first().fill("");
  await expect(page.getByText("Enter a finite number.")).toBeVisible();
  await expect(draftErrors(page).first()).toBeVisible();
  await page.getByLabel("Property value").nth(1).pressSequentially("hello");
  await expect(draftErrors(page).first()).toBeVisible();
  await expect(page.getByText("Enter a finite number.")).toBeVisible();
  await page.getByLabel("Property value").first().fill("0");
  const source = await sourceText(page);
  expect(source).toContain("a: 0");
  expect(source).toContain("b: hello");
});

test("J long action names fit and fields stay reachable at 1280x720", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  const long = structuredClone(lookupAction);
  long.metadata.name =
    "customer-relationship-management.lookup-customer-by-identifier-and-region";
  await connected(page, {
    catalog: [{ id: "a1", document: long }, defaultCatalog[1]],
  });
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await chooseAction(page, `${long.metadata.name}@1.0.0`);
  const inspector = await page.locator(".inspector").boundingBox();
  const select = await actionPicker(page).boundingBox();
  expect(select!.x + select!.width).toBeLessThanOrEqual(
    inspector!.x + inspector!.width,
  );
  const contract = page.locator(".integration-contract");
  expect(
    await contract.evaluate((e) => getComputedStyle(e).borderTopStyle),
  ).not.toBe("none");
  await expectHitTarget(inspectorHeading(page));
});

test("a duplicate mapped key keeps an inline error after editing a sibling", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await offline(page);
  await newWorkflow(page);
  await insertStep(page, "Transform");
  const names = page.getByLabel("Field name", { exact: true });
  await names.first().fill("field1");
  await names.first().press("Tab");
  await page.getByRole("button", { name: "+ Add field", exact: true }).click();
  await names.nth(1).fill("field2");
  await names.nth(1).press("Tab");
  await names.first().fill("field2");
  await names.first().press("Tab");
  await expect(
    page.getByText("Use a unique field name.").first(),
  ).toBeVisible();
  await expect(draftErrors(page).first()).toBeVisible();
  await page.getByLabel("Property value").nth(1).fill("Keep");
  await expect(draftErrors(page).first()).toBeVisible();
  await expect(
    page.getByText("Use a unique field name.").first(),
  ).toBeVisible();
});

test("a contract still loading is shown for the next step that uses it", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await connected(page);
  await page.route("**/projects/project/actions/a1/export", async (r) => {
    await new Promise((resolve) => setTimeout(resolve, 800));
    await r.fulfill({
      json: {
        id: "a1",
        name: "sql.lookup",
        version: "1.0.0",
        document: lookupAction,
      },
    });
  });
  await newWorkflow(page);
  const step = (id: string) => ({
    id,
    kind: "action",
    uses: "sql.lookup@1.0.0",
    with: { literal: { parameters: { customerId: id } } },
  });
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  await page.getByRole("textbox", { name: "Workflow source" }).fill(
    JSON.stringify({
      apiVersion: "weave/v1alpha1",
      kind: "Workflow",
      metadata: { name: "two-lookups", version: "1.0.0" },
      spec: {
        inputSchema: { type: "object" },
        outputSchema: { type: "object" },
        steps: [step("first"), step("second")],
        output: { literal: {} },
      },
    }),
  );
  await page
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await page.getByRole("tab", { name: "Designer", exact: true }).click();
  await node(page, "first").click();
  await expect(page.locator(".integration-contract")).toContainText(
    "Loading the action's details",
  );
  await node(page, "second").click();
  await page.locator(".action-details > summary").click();
  await expect(page.locator(".integration-requirements")).toBeVisible();
  await expect(page.getByLabel(/^Customer ID/)).toHaveValue("second");
  await expect(page.locator(".apply-state")).toHaveCount(0);
});

test("invalid advanced JSON blocks integration edits without page errors", async ({
  page,
}) => {
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(String(error)));
  await page.setViewportSize({ width: 1440, height: 900 });
  await connected(page);
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await chooseLookup(page);
  await page.getByRole("button", { name: "Advanced JSON" }).click();
  await page.getByLabel("Step configuration JSON").fill("{ broken");
  await expect(actionPicker(page)).toBeDisabled();
  await expect(
    page.getByLabel("Connection slot", { exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", {
      name: "Write one expression for the whole input",
    }),
  ).toBeDisabled();
  await expect(page.getByLabel(/^Label(\s*\(optional\))?$/)).toBeDisabled();
  await expect(page.locator(".integration-issues")).toContainText(
    "Fix the step configuration JSON.",
  );
  await page.getByLabel("Step configuration JSON").fill(
    JSON.stringify({
      id: "call-action-1",
      kind: "action",
      uses: "sql.lookup@1.0.0",
      with: { literal: { label: "from json" } },
    }),
  );
  await expect(page.getByLabel(/^Label(\s*\(optional\))?$/)).toBeEnabled();
  await chooseAction(page, "crm.lookup@2.0.0");
  await expect(page.getByLabel("Step configuration JSON")).toHaveValue(
    /crm\.lookup@2\.0\.0/,
  );
  expect(failures).toEqual([]);
});

test("the slot picker offers no slots to an action without a connection", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await connected(page);
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await chooseLookup(page);
  await page
    .getByRole("button", {
      name: "Add a PostgreSQL connection slot",
      exact: true,
    })
    .click();
  // Fill the required input before leaving this step.
  await page.getByLabel("Customer ID", { exact: true }).fill("C-1");
  await inspectorHeading(page).click();
  await expect(page.getByText("Not applied yet")).toHaveCount(0);
  await insertStep(page, "Call an action");
  await chooseAction(page, "crm.lookup@2.0.0");
  await expect(page.locator(".integration-requirements")).toContainText(
    "No connection needed",
  );
  const slot = page.getByLabel("Connection slot", { exact: true });
  await slot.click();
  await expect(page.getByRole("listbox").getByRole("option")).toHaveText([
    "No connection",
  ]);
  await page.keyboard.press("Escape");
  await expect(page.locator(".integration-contract")).toContainText(
    "This action does not use a connection.",
  );
  await expect(page.getByText("Add connection slot")).toHaveCount(0);
});

test("a created slot can be renamed and updates its action", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await connected(page);
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await chooseLookup(page);
  await page
    .getByRole("button", {
      name: "Add a PostgreSQL connection slot",
      exact: true,
    })
    .click();
  const name = page.getByLabel("Slot name", { exact: true });
  await expect(name).toHaveValue("weave-postgresql");
  await expect(name).toBeFocused();
  // A click after the text puts the caret at its end (End doesn't move the
  // caret in a macOS text field).
  const field = (await name.boundingBox())!;
  await name.click({ position: { x: field.width - 6, y: field.height / 2 } });
  await name.press("End");
  for (let i = 0; i < "weave-postgresql".length; i++)
    await name.press("Backspace");
  await expect(name).toHaveValue("");
  await name.pressSequentially("orders");
  await expect(name).toHaveValue("orders");
  await name.press("Tab");
  await expect(
    page.getByLabel("Connection slot", { exact: true }),
  ).toHaveAttribute("data-value", "orders");
});

test("an optional object's required fields apply only once it is used", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const action = structuredClone(lookupAction);
  action.spec.inputSchema.required = [];
  await connected(page, { catalog: [{ id: "a1", document: action }] });
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await chooseLookup(page);
  const customer = page.getByLabel(/^Customer ID/);
  await expect(customer).not.toHaveAttribute("required");
  await page.getByLabel(/^Label(\s*\(optional\))?$/).fill("optional");
  await inspectorHeading(page).click();
  await expect(page.locator(".apply-state")).toHaveCount(0);
  await selectChoice(page.getByLabel(/^Region(\s*\(optional\))?$/), "eu");
  await expect(customer).toHaveAttribute("required");
  await expect(page.locator(".integration-issues")).toContainText(
    "Parameters › Customer ID",
  );
  await selectChoice(page.getByLabel(/^Region(\s*\(optional\))?$/), "");
  await expect(customer).not.toHaveAttribute("required");
  await inspectorHeading(page).click();
  await expect(page.locator(".apply-state")).toHaveCount(0);
  const source = await sourceText(page);
  expect(source).toContain("label: optional");
  expect(source).not.toContain("parameters");
});
