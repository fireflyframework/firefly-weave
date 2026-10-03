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
// WP-08, WP-10, WP-11, WP-12 and WP-15: forms send exactly what the platform
// accepts (required yes/no answers, never a secret), suggest only the data
// the compiler lets a field read, bind each action input to a value, data or
// a formula, give every common schema a real editor, and design schemas
// field by field. Real clicks and keys at 1440x900 and 600x500.
import { test, expect, Locator, Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { parse } from "yaml";
import {
  allCapabilities,
  connected,
  newWorkflow,
  offline,
  sourceText,
} from "./support";
import { DesignerPage } from "./designer-po";
import { chooseAction } from "./integrations-po";

const apply = (page: Page) =>
  page.getByRole("button", { name: "Apply changes", exact: true });
const action = (name: string, inputSchema: object) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name, version: "1.0.0" },
  spec: {
    implementation: { kind: "worker", taskType: name, taskVersion: "1.0.0" },
    sideEffect: "read_only",
    timeoutSeconds: 30,
    inputSchema,
    outputSchema: {
      type: "object",
      properties: { eligible: { type: "boolean" } },
    },
  },
});
const workflow = (steps: unknown[], inputSchema: object = { type: "object" }) =>
  JSON.stringify({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "forms", version: "1.0.0" },
    spec: {
      inputSchema,
      outputSchema: { type: "object" },
      steps,
      output: { literal: {} },
    },
  });
/** Opens a reference combobox's suggestions and lists their pointers. */
async function suggestions(combobox: Locator) {
  await combobox.click();
  await combobox.press("ArrowDown");
  const list = combobox.page().locator(".ref-combo-list:not([hidden])");
  await expect(list.getByRole("option").first()).toBeVisible();
  return list
    .getByRole("option")
    .evaluateAll((items) => items.map((o) => o.getAttribute("data-ref")));
}
/** Closes a narrow-layout inspector that covers the canvas. */
async function closeInspector(page: Page) {
  const close = page.getByRole("button", { name: "Close inspector" });
  if (await close.isVisible()) await close.click();
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("a human task sends false for an untouched required yes/no and no secret", async ({
      page,
    }) => {
      await connected(page, {
        capabilities: [...allCapabilities, "human_task.complete"],
      });
      const task = {
        id: "task-1",
        run_id: "run",
        node_id: "review",
        revision: 1,
        status: "claimed",
        claimant_id: "human",
        title: "Review the order",
        decisions: ["approve"],
        form_schema: {
          type: "object",
          required: ["confirmed"],
          properties: {
            confirmed: { type: "boolean", title: "Confirmed" },
            notify: { type: "boolean", title: "Notify the customer" },
            pin: { type: "string", title: "PIN", writeOnly: true },
          },
        },
      };
      await page.route("**/human-tasks?*", (r) =>
        r.fulfill({ json: { items: [task], next_cursor: null } }),
      );
      await page.route("**/human-tasks/task-1", (r) =>
        r.fulfill({ json: task }),
      );
      const sent: Record<string, unknown>[] = [];
      await page.route("**/human-tasks/task-1/complete", (r) => {
        sent.push(r.request().postDataJSON());
        return r.fulfill({ json: { ...task, status: "completed" } });
      });
      await page.getByRole("button", { name: "My tasks", exact: true }).click();
      await page.locator(".resource-row").click();
      const confirmed = page.getByRole("checkbox", { name: /^Confirmed/ });
      await expect(confirmed).not.toBeChecked();
      // An optional yes/no answer can stay unset (absent is not "No").
      await expect(
        page.getByLabel(/^Notify the customer(\s*\(optional\))?$/),
      ).toHaveValue("");
      await expect(page.locator(".human-decision-form")).toContainText(
        "Secret: Studio never enters, sends or stores this value.",
      );
      await expect(page.getByRole("textbox", { name: "PIN" })).toHaveCount(0);
      await page.getByRole("button", { name: "Approve", exact: true }).click();
      // Deciding asks once, inline.
      await page
        .locator(".decision-confirm")
        .getByRole("button", { name: "Approve", exact: true })
        .click();
      await expect.poll(() => sent.length).toBe(1);
      expect(sent[0]["data"]).toEqual({ confirmed: false });
    });

    test("an action's secret input is locked and yes/no inputs start as the schema says", async ({
      page,
    }) => {
      const secretive = action("secret.lookup", {
        type: "object",
        required: ["confirm", "apiKey"],
        properties: {
          confirm: { type: "boolean", title: "Confirm" },
          verbose: { type: "boolean", title: "Verbose" },
          apiKey: { type: "string", title: "API key", "x-secret": true },
        },
      });
      await connected(page, {
        catalog: [{ id: "s1", document: secretive as never }],
      });
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("action");
      await designer.selectStep("call-action-1");
      await chooseAction(page);
      const form = page.locator(".action-input-form");
      await expect(form).toContainText(
        "Supplied by the connection's credentials; workflows can't set it.",
      );
      await expect(form.getByRole("textbox", { name: "API key" })).toHaveCount(
        0,
      );
      await expect(
        form.getByRole("checkbox", { name: /^Confirm/ }),
      ).not.toBeChecked();
      await expect(form.getByLabel(/^Verbose(\s*\(optional\))?$/)).toHaveValue(
        "",
      );
      await expect(page.locator(".integration-issues")).toHaveCount(0);
      await apply(page).click();
      const step = parse(await sourceText(page)).spec.steps[0];
      expect(step.with).toEqual({ literal: { confirm: false } });
    });

    test("a list item's error is shown, linked and blocks Apply", async ({
      page,
    }) => {
      const scored = action("score.lookup", {
        type: "object",
        properties: {
          scores: {
            type: "array",
            title: "Scores",
            items: { type: "integer" },
          },
        },
      });
      await connected(page, {
        catalog: [{ id: "n1", document: scored as never }],
      });
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("action");
      await designer.selectStep("call-action-1");
      await chooseAction(page);
      await page.getByRole("button", { name: "Add Scores item" }).click();
      const item = page.getByLabel("Scores item 1", { exact: true });
      await expect(item).toBeFocused();
      await item.fill("1.5");
      const error = page.getByText("Scores item 1 must be a whole number.");
      await expect(error).toBeVisible();
      await expect(item).toHaveAccessibleDescription(
        "Scores item 1 must be a whole number.",
      );
      await expect(apply(page)).toBeDisabled();
      await item.fill("2");
      await expect(error).toHaveCount(0);
      await expect(apply(page)).toBeEnabled();
    });

    test("switching an expression's kind and back keeps what it held", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("transform");
      await designer.selectStep("transform-1");
      // Value | Data | Formula | Fields | List: one switch on the label row.
      const mode = designer.inspector.getByRole("group", {
        name: "Value expression mode",
        exact: true,
      });
      const type = designer.inspector.getByLabel("Value type").first();
      await type.selectOption("string");
      await designer.inspector
        .getByLabel("Property value")
        .first()
        .fill("kept text");
      await mode.getByRole("button", { name: "Data" }).click();
      await mode.getByRole("button", { name: "Value" }).click();
      await expect(
        designer.inspector.getByLabel("Property value").first(),
      ).toHaveValue("kept text");
    });

    test("references offer only data the compiler lets each field read", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(
        workflow([
          {
            id: "route",
            kind: "switch",
            cases: [
              {
                when: { literal: true },
                steps: [{ id: "a", kind: "transform", value: { literal: 1 } }],
                output: { literal: {} },
              },
            ],
            default: {
              steps: [{ id: "b", kind: "transform", value: { literal: 2 } }],
              output: { literal: {} },
            },
          },
          { id: "after", kind: "transform", value: { literal: null } },
        ]),
      );
      await designer.selectStep("after");
      await designer.inspector
        .getByRole("group", { name: "Value expression mode", exact: true })
        .getByRole("button", { name: "Data" })
        .click();
      const after = await suggestions(
        designer.inspector.getByRole("combobox", { name: "Value reference" }),
      );
      expect(after).toContain("/steps/route/output");
      expect(after).not.toContain("/steps/a/output");
      expect(after).not.toContain("/steps/b/output");
      await page.keyboard.press("Escape");
      // Apply the reference, so moving to another step asks nothing.
      await apply(page).click();
      await designer.selectStep("route");
      await designer.inspector
        .getByRole("group", {
          name: "Case 1 output expression mode",
          exact: true,
        })
        .getByRole("button", { name: "Data" })
        .click();
      const branch = await suggestions(
        designer.inspector.getByRole("combobox", {
          name: "Case 1 output reference",
        }),
      );
      expect(branch).toContain("/steps/a/output");
      expect(branch).not.toContain("/steps/b/output");
      expect(branch).not.toContain("/steps/route/output");
    });

    test("an action input is bound field by field and only edited keys change", async ({
      page,
    }) => {
      const check = action("onboarding.check-customer", {
        type: "object",
        additionalProperties: false,
        required: ["customerId", "region"],
        properties: {
          customerId: { type: "string", minLength: 1 },
          region: { type: "string" },
          note: { type: "string" },
        },
      });
      await connected(page, {
        catalog: [{ id: "c1", document: check as never }],
      });
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(
        workflow(
          [
            {
              id: "check",
              kind: "action",
              uses: "onboarding.check-customer@1.0.0",
              with: {
                object: {
                  customerId: { ref: "/input/customerId" },
                  region: {
                    op: {
                      name: "coalesce",
                      args: [{ ref: "/input/region" }, { literal: "eu" }],
                    },
                  },
                },
              },
            },
          ],
          {
            type: "object",
            required: ["customerId"],
            properties: {
              customerId: { type: "string" },
              region: { type: "string" },
            },
          },
        ),
      );
      await designer.selectStep("check");
      const form = page.locator(".action-input-form");
      // customerId reads the workflow input: shown as data, not as text.
      const customer = form.getByRole("combobox", { name: /^Customer ID/ });
      await expect(customer).toHaveValue("/input/customerId");
      await expect(
        form
          .locator('[data-path="customerId"]')
          .getByRole("button", { name: "Data" }),
      ).toHaveAttribute("aria-pressed", "true");
      await expect(
        form
          .locator('[data-path="region"] .binding-mode')
          .filter({ hasText: "Formula" }),
      ).toHaveAttribute("aria-pressed", "true");
      // Bound fields count as filled in.
      await expect(page.locator(".integration-issues")).toHaveCount(0);
      await form.getByLabel(/^Note(\s*\(optional\))?$/).fill("checked by hand");
      await apply(page).click();
      const source = await sourceText(page);
      const step = parse(source).spec.steps[0];
      expect(step.with).toEqual({
        object: {
          customerId: { ref: "/input/customerId" },
          region: {
            op: {
              name: "coalesce",
              args: [{ ref: "/input/region" }, { literal: "eu" }],
            },
          },
          note: { literal: "checked by hand" },
        },
      });
      // Note becomes data picked from the suggestions; switching back to
      // Value restores the text.
      await designer.selectStep("check");
      const note = form.locator('[data-path="note"]');
      await note.getByRole("button", { name: "Data" }).click();
      const pointer = note.getByRole("combobox", { name: /^Note/ });
      expect(await suggestions(pointer)).toContain("/input/customerId");
      await pointer.fill("/input/region");
      await note.getByRole("button", { name: "Value" }).click();
      await expect(note.getByLabel(/^Note(\s*\(optional\))?$/)).toHaveValue(
        "checked by hand",
      );
      await note.getByRole("button", { name: "Data" }).click();
      await expect(note.getByRole("combobox", { name: /^Note/ })).toHaveValue(
        "/input/region",
      );
      await apply(page).click();
      const bound = parse(await sourceText(page)).spec.steps[0].with;
      expect(bound.object.note).toEqual({ ref: "/input/region" });
      // What the form wrote compiles against the action's contract.
      const python = resolve(process.cwd(), "../.venv/bin/python");
      if (existsSync(python)) {
        const result = JSON.parse(
          execFileSync(
            python,
            [
              "-c",
              `import json,sys
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
p=json.load(sys.stdin)
a=p['action']
s=a['spec']
c=CatalogSnapshot.from_definitions([load_definition(a)],tasks=[{'taskType':a['metadata']['name'],'taskVersion':'1.0.0','inputSchema':s['inputSchema'],'outputSchema':s['outputSchema'],'sideEffect':'read_only','timeoutSeconds':30}])
print(compile_source(p['source'],format='yaml',catalog=c).to_bytes().decode())`,
            ],
            {
              input: JSON.stringify({
                source: await sourceText(page),
                action: check,
              }),
              encoding: "utf8",
              env: { ...process.env, PYTHONPATH: resolve("../src") },
            },
          ),
        );
        expect(result.validationOk, JSON.stringify(result.diagnostics)).toBe(
          true,
        );
      }
    });

    test("an empty input switched to Formula writes nothing until the formula is edited", async ({
      page,
    }) => {
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      // Angular reports a render loop (NG0103) or a changed binding as errors.
      page.on("console", (message) => {
        if (message.type() === "error" && /NG0\d/.test(message.text()))
          errors.push(message.text());
      });
      await connected(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("action");
      await designer.selectStep("call-action-1");
      await chooseAction(page, "sql.lookup@1.0.0");
      const form = page.locator(".action-input-form");
      await form.getByLabel(/^Customer ID/).fill("c-1");
      await apply(page).click();
      // Applied: no "Not applied yet" chip.
      await expect(page.locator(".apply-state")).toHaveCount(0);
      const mode = form.locator('[data-path="mode"]');
      await mode
        .locator(".binding-mode")
        .filter({ hasText: "Formula" })
        .click();
      const formula = mode.getByRole("group", {
        name: "Mode expression mode",
        exact: true,
      });
      await expect(formula).toBeVisible();
      // It starts by choosing a formula, not with a null value.
      await expect(
        mode
          .getByLabel("Mode operator", { exact: true })
          .locator("option:checked"),
      ).toHaveText("Choose a formula…");
      await page.waitForTimeout(300);
      await expect(page.locator(".apply-state")).toHaveCount(0);
      await formula.getByRole("button", { name: "Data" }).click();
      await expect(page.locator(".apply-state")).toHaveText("Not applied yet");
      expect(errors).toEqual([]);
    });

    test("dates, emails, labelled choices, maps, tables, constants and empty values get their own editors", async ({
      page,
    }) => {
      const order = action("order.create", {
        type: "object",
        required: ["kind"],
        properties: {
          kind: { const: "order" },
          when: { type: "string", format: "date-time", title: "Deliver at" },
          contact: { type: "string", format: "email", title: "Contact" },
          size: {
            title: "Size",
            oneOf: [
              { const: "s", title: "Small" },
              { const: "l", title: "Large" },
            ],
          },
          headers: {
            type: "object",
            title: "Headers",
            additionalProperties: { type: "string" },
          },
          rows: {
            type: "array",
            title: "Rows",
            items: {
              type: "object",
              required: ["sku"],
              properties: { sku: { type: "string", title: "SKU" } },
            },
          },
          nickname: { type: ["string", "null"], title: "Nickname" },
        },
      });
      await connected(page, {
        catalog: [{ id: "o1", document: order as never }],
      });
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("action");
      await designer.selectStep("call-action-1");
      await chooseAction(page);
      const form = page.locator(".action-input-form");
      await expect(form.locator('[data-path="kind"]')).toContainText('"order"');
      await form
        .getByLabel(/^Deliver at(\s*\(optional\))?$/)
        .fill("2026-10-02T09:30");
      const contact = form.getByLabel(/^Contact(\s*\(optional\))?$/);
      await contact.fill("not-an-address");
      await expect(form).toContainText("Contact must be an email address.");
      await expect(apply(page)).toBeDisabled();
      await contact.fill("ana@example.com");
      await form.getByLabel(/^Size(\s*\(optional\))?$/).selectOption("Large");
      await form.getByRole("button", { name: "Add Headers entry" }).click();
      const header = form.getByLabel("Headers name 1", { exact: true });
      await header.fill("X-Trace");
      await header.press("Tab");
      await form.getByLabel("Headers value 1", { exact: true }).fill("abc");
      await form.getByRole("button", { name: "Add Rows item" }).click();
      await form.getByLabel(/^SKU/).fill("A-1");
      await form
        .getByRole("checkbox", { name: "Nickname: no value (null)" })
        .check();
      await expect(apply(page)).toBeEnabled();
      await apply(page).click();
      const input = parse(await sourceText(page)).spec.steps[0].with.literal;
      expect(input).toEqual({
        kind: "order",
        when: new Date("2026-10-02T09:30").toISOString(),
        contact: "ana@example.com",
        size: "l",
        headers: { "X-Trace": "abc" },
        rows: [{ sku: "A-1" }],
        nickname: null,
      });
    });

    test("the workflow input schema is designed field by field", async ({
      page,
    }) => {
      await offline(page);
      const inferred: Record<string, unknown>[] = [];
      await page.route("**/studio/local/http-action", (r) => {
        inferred.push(r.request().postDataJSON());
        return r.fulfill({
          json: {
            ok: true,
            diagnostics: [],
            action: {
              spec: {
                inputSchema: {
                  properties: {
                    body: {
                      type: "array",
                      items: {
                        type: "object",
                        required: ["orderId"],
                        properties: { orderId: { type: "integer" } },
                      },
                    },
                  },
                },
              },
            },
          },
        });
      });
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await closeInspector(page);
      await designer.fit();
      await page
        .getByRole("button", { name: "Start — workflow settings" })
        .click();
      const input = designer.inspector.locator(
        '[data-field="spec/inputSchema"]',
      );
      await input.getByRole("button", { name: "Add field" }).click();
      await input.getByLabel("Field name").fill("customerId");
      await input
        .getByRole("checkbox", { name: /^Required: .customerId.$/ })
        .check();
      await expect(input).toContainText("What people starting a run will see");
      await input.getByRole("button", { name: "Paste sample JSON" }).click();
      const dialog = page.getByRole("dialog", {
        name: "Find fields from an example",
      });
      await dialog.getByLabel("Example 1").fill('{"orderId": 7}');
      await dialog.getByRole("button", { name: "Find fields" }).click();
      await expect(dialog).toHaveCount(0);
      // The example went to the local Studio host only, as a body sample.
      expect(inferred).toHaveLength(1);
      expect(
        (inferred[0]["request"] as Record<string, unknown>)["bodySample"],
      ).toEqual([{ orderId: 7 }]);
      await expect(input.getByLabel("Field name").nth(1)).toHaveValue(
        "orderId",
      );
      await page
        .getByRole("button", { name: "Apply changes", exact: true })
        .click();
      const schema = parse(await sourceText(page)).spec.inputSchema;
      expect(schema.required).toEqual(["customerId", "orderId"]);
      expect(schema.properties.customerId).toEqual({ type: "string" });
      expect(schema.properties.orderId).toEqual({ type: "integer" });
    });

    test("a human task's form is designed with a reviewer preview", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("humanTask");
      await designer.selectStep("approval-1");
      const form = designer.inspector.locator('[data-field="formSchema"]');
      await form.getByRole("button", { name: "Add field" }).click();
      await form.getByLabel("Field name").fill("comment");
      await expect(form).toContainText("What reviewers will see");
      await expect(form.locator(".sd-preview")).toContainText("Comment");
      await apply(page).click();
      const step = parse(await sourceText(page)).spec.steps[0];
      expect(step.formSchema.properties.comment).toEqual({ type: "string" });
    });

    test("map entries keep their errors through renames and removals", async ({
      page,
    }) => {
      const counts = action("count.set", {
        type: "object",
        properties: {
          counts: {
            type: "object",
            title: "Counts",
            additionalProperties: { type: "integer" },
          },
        },
      });
      await connected(page, {
        catalog: [{ id: "c1", document: counts as never }],
      });
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("action");
      await designer.selectStep("call-action-1");
      await chooseAction(page);
      const form = page.locator(".action-input-form");
      // A rejected value stays with its entry when the entry is renamed...
      await form.getByRole("button", { name: "Add Counts entry" }).click();
      await form.getByLabel("Counts value 1", { exact: true }).fill("1.5");
      const whole = form.getByText("Counts value 1 must be a whole number.");
      await expect(whole).toBeVisible();
      const name = form.getByLabel("Counts name 1", { exact: true });
      await name.fill("apples");
      await name.press("Tab");
      await expect(whole).toBeVisible();
      await expect(apply(page)).toBeDisabled();
      await form.getByLabel("Counts value 1", { exact: true }).fill("2");
      await expect(whole).toHaveCount(0);
      await expect(apply(page)).toBeEnabled();
      // ...and a rejected name goes with the entry that is removed.
      await form.getByRole("button", { name: "Add Counts entry" }).click();
      const second = form.getByLabel("Counts name 2", { exact: true });
      await second.fill("apples");
      await second.press("Tab");
      await expect(form).toContainText(
        "There is already an entry named apples.",
      );
      await expect(apply(page)).toBeDisabled();
      await form.getByRole("button", { name: "Remove Counts entry 2" }).click();
      await expect(form).not.toContainText("There is already an entry named");
      await expect(apply(page)).toBeEnabled();
      await apply(page).click();
      const step = parse(await sourceText(page)).spec.steps[0];
      expect(step.with).toEqual({ literal: { counts: { apples: 2 } } });
    });

    test("a list item before a removed one keeps its error", async ({
      page,
    }) => {
      const scored = action("score.lookup", {
        type: "object",
        properties: {
          scores: {
            type: "array",
            title: "Scores",
            items: { type: "integer" },
          },
        },
      });
      await connected(page, {
        catalog: [{ id: "n1", document: scored as never }],
      });
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("action");
      await designer.selectStep("call-action-1");
      await chooseAction(page);
      const form = page.locator(".action-input-form");
      await form.getByRole("button", { name: "Add Scores item" }).click();
      await form.getByRole("button", { name: "Add Scores item" }).click();
      await form.getByLabel("Scores item 1", { exact: true }).fill("1.5");
      await form.getByRole("button", { name: "Remove Scores item 2" }).click();
      // Item 1 still shows 1.5, so it must still say why Apply is off.
      await expect(
        form.getByLabel("Scores item 1", { exact: true }),
      ).toHaveValue("1.5");
      await expect(
        form.getByText("Scores item 1 must be a whole number."),
      ).toBeVisible();
      await expect(apply(page)).toBeDisabled();
    });

    test("switching where a group's value comes from keeps keyboard focus", async ({
      page,
    }) => {
      await connected(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.append("action");
      await designer.selectStep("call-action-1");
      await chooseAction(page, "sql.lookup@1.0.0");
      const form = page.locator(".action-input-form");
      const parameters = form.locator('[data-path="parameters"]');
      const data = parameters.getByRole("button", { name: "Data" }).first();
      // Each source button says which field it belongs to.
      await expect(data).toHaveAccessibleDescription(/^Parameters/);
      await data.focus();
      await page.keyboard.press("Enter");
      // The group is drawn as one data field now; focus stays on its switch.
      const pressed = form
        .locator('[data-path="parameters"]')
        .getByRole("button", { name: "Data" });
      await expect(pressed).toBeFocused();
      await expect(pressed).toHaveAttribute("aria-pressed", "true");
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Enter");
      const value = form
        .locator('[data-path="parameters"]')
        .getByRole("button", { name: "Value" })
        .first();
      await expect(value).toBeFocused();
      await expect(value).toHaveAttribute("aria-pressed", "true");
      await expect(
        form.getByRole("textbox", { name: /^Customer ID/ }),
      ).toBeVisible();
    });
  });

test.describe("360x640", () => {
  test.use({ viewport: { width: 360, height: 640 } });

  test("a rich schema form fits a phone-width dialog without sideways scrolling", async ({
    page,
  }) => {
    await connected(page);
    const versionId = "55555555-5555-4555-8555-555555555555";
    const environment =
      "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";
    await page.route(`${environment}/activations?*`, (r) =>
      r.fulfill({
        json: {
          items: [
            {
              id: "act-1",
              name: "orders",
              revision: 1,
              request: { version_id: versionId },
            },
          ],
          next_cursor: null,
        },
      }),
    );
    await page.route(`**/projects/project/workflows/${versionId}/export`, (r) =>
      r.fulfill({
        json: {
          document: JSON.parse(
            workflow([], {
              type: "object",
              required: ["customerId", "urgent"],
              properties: {
                customerId: { type: "string", format: "uuid" },
                urgent: { type: "boolean" },
                deliverAt: { type: "string", format: "date-time" },
                labels: {
                  type: "object",
                  additionalProperties: { type: "string" },
                },
                lines: {
                  type: "array",
                  items: {
                    type: "object",
                    properties: {
                      sku: { type: "string" },
                      quantity: { type: "integer" },
                    },
                  },
                },
                note: { type: ["string", "null"] },
              },
            }),
          ),
        },
      }),
    );
    await page.getByRole("button", { name: "Runs", exact: true }).click();
    await page.getByRole("button", { name: "Start run", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Start a run" });
    await dialog
      .getByLabel("Version to run", { exact: true })
      .selectOption("act-1");
    await expect(dialog.getByLabel(/^Customer ID/)).toBeVisible();
    await dialog.getByRole("button", { name: "Add Labels entry" }).click();
    await dialog.getByRole("button", { name: "Add Lines item" }).click();
    await expect(dialog.getByLabel(/^Quantity/)).toBeVisible();
    const overflow = await dialog.evaluate((element) => {
      const panel = element.closest(".modal-panel") ?? element;
      return [...panel.querySelectorAll<HTMLElement>("*")]
        .filter((node) => node.getBoundingClientRect().right > 360 + 0.5)
        .map((node) => `${node.tagName}.${node.className}`)
        .slice(0, 5);
    });
    expect(overflow).toEqual([]);
  });
});
