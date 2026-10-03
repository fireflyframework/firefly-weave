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
// WP-06: the designer checks the workflow as it changes, explains each
// problem in plain words, opens the step and field a problem belongs to, and
// publishes only what passed against the project catalog. Real clicks and
// keys at 1440x900 and 600x500.
import { test, expect, Locator, Page, Route } from "@playwright/test";
import {
  allCapabilities,
  command,
  connected,
  newWorkflow,
  offline,
  sourceText,
} from "./support";
import { DesignerPage } from "./designer-po";

const project = "**/studio/api/api/v1/tenants/tenant/projects/project";
const workflow = (steps: unknown[], extra: Record<string, unknown> = {}) =>
  JSON.stringify({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "checked", version: "1.0.0" },
    spec: {
      inputSchema: { type: "object" },
      outputSchema: { type: "object" },
      steps,
      output: { literal: {} },
      ...extra,
    },
  });
/** b reads a, which runs in another branch of the decision. */
const siblingRead = workflow([
  {
    id: "route",
    kind: "switch",
    cases: [
      {
        when: { literal: true },
        steps: [{ id: "a", kind: "transform", value: { literal: 1 } }],
      },
    ],
    default: {
      steps: [
        { id: "b", kind: "transform", value: { ref: "/steps/a/output" } },
      ],
    },
  },
]);
const clean = { validationOk: true, errorCount: 0, diagnostics: [] };
const panel = (page: Page) =>
  page.getByRole("region", { name: "Compiler diagnostics" });
const toolbar = (page: Page, name: string) =>
  page.getByRole("button", { name, exact: true });
/** Clicks a toolbar control, closing a narrow-layout inspector that covers it. */
async function press(page: Page, name: string) {
  await reach(page, toolbar(page, name));
}
/** Clicks a control, closing a narrow-layout inspector that covers it. */
async function reach(page: Page, control: Locator) {
  await control.scrollIntoViewIfNeeded();
  const covered = await control.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const hit = document.elementFromPoint(
      box.left + box.width / 2,
      box.top + box.height / 2,
    );
    return !(hit === element || element.contains(hit));
  });
  if (covered)
    await page.getByRole("button", { name: "Close inspector" }).click();
  await control.click();
}
/** Local checks answer with `answer(source)` and are recorded. */
async function localChecks(
  page: Page,
  answer: (source: string) => unknown = () => ({ ...clean, partial: true }),
) {
  const sources: string[] = [];
  await page.route("**/studio/local/validate", (r: Route) => {
    const source = String(r.request().postDataJSON().source);
    sources.push(source);
    return r.fulfill({ json: answer(source) });
  });
  return sources;
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("a sibling-branch reference is reported while typing, without Validate", async ({
      page,
    }) => {
      await offline(page);
      const sources = await localChecks(page, (source) =>
        source.includes("/steps/a/output")
          ? {
              validationOk: false,
              errorCount: 1,
              partial: true,
              diagnostics: [
                {
                  code: "WV-COMP-UNAVAILABLE_REFERENCE",
                  severity: "error",
                  stage: "semantic",
                  path: "/spec/steps/0/default/steps/0/value/ref",
                  message:
                    "Referenced step does not dominate this expression in its lexical scope.",
                },
              ],
            }
          : { ...clean, partial: true },
      );
      await newWorkflow(page);
      await expect(panel(page)).toContainText(
        "No problems found. Actions and connections are checked when you connect.",
      );
      await page.getByRole("tab", { name: "Source", exact: true }).click();
      const started = Date.now();
      await page
        .getByRole("textbox", { name: "Workflow source" })
        .fill(siblingRead);
      // No Validate and no Apply: the typed source is checked on its own.
      await expect(panel(page)).toContainText(
        "1 error. Fix the errors to publish.",
        { timeout: 1500 },
      );
      expect(Date.now() - started).toBeLessThan(1500);
      await expect(panel(page)).toContainText(
        "This uses data from a step that hasn't finished at this point in the workflow.",
      );
      await expect(panel(page)).not.toContainText("Passed");
      await page
        .getByRole("button", { name: "Apply changes", exact: true })
        .click();
      await page.getByRole("tab", { name: "Designer", exact: true }).click();
      await expect(page.locator('[data-step="b"]')).toHaveClass(
        /\bhas-error\b/,
      );
      await expect(page.locator('[data-step="b"] .node-chip')).toHaveText(
        "1 problem",
      );
      await expect(
        page.locator('[data-step="b"] .node-body'),
      ).toHaveAccessibleName(/1 error/);
      // Applying the source that was just checked does not check it again.
      expect(sources.filter((s) => s === siblingRead)).toHaveLength(1);
    });

    test("typing in Source checks once per pause, not on every key", async ({
      page,
    }) => {
      await offline(page);
      const sources = await localChecks(page);
      await newWorkflow(page);
      await expect.poll(() => sources.length).toBe(1);
      await page.getByRole("tab", { name: "Source", exact: true }).click();
      const box = page.getByRole("textbox", { name: "Workflow source" });
      await box.click();
      await page.keyboard.press("ControlOrMeta+End");
      // Twelve keys, one every 100 ms: no check while the person types.
      await box.pressSequentially("# a comment!", { delay: 100 });
      expect(sources).toHaveLength(1);
      await expect.poll(() => sources.length, { timeout: 2000 }).toBe(2);
      expect(sources[1]).toContain("# a comment!");
      await page.waitForTimeout(900);
      expect(sources).toHaveLength(2);
    });

    test("a connected Validate compiles against the project catalog", async ({
      page,
    }) => {
      await connected(page, { capabilities: [...allCapabilities, "compile"] });
      const local = await localChecks(page);
      const compiles: { source: string; format: string }[] = [];
      await page.route(`${project}/compiler/compile`, (r) => {
        compiles.push(r.request().postDataJSON());
        return r.fulfill({
          json: { ...clean, ok: true, partial: false, artifact: {} },
        });
      });
      await newWorkflow(page);
      await expect.poll(() => local.length).toBe(1);
      // A local check never claims the project catalog passed.
      await expect(panel(page)).toContainText(
        "Validate to check actions and connections against the project.",
      );
      await expect(panel(page)).not.toContainText("Passed");
      await press(page, "Validate");
      await expect.poll(() => compiles.length).toBe(1);
      expect(compiles[0].format).toBe("yaml");
      expect(compiles[0].source).toContain("name: untitled-workflow");
      expect(local).toHaveLength(1);
      await expect(panel(page)).toContainText("No problems found.");
    });

    test("clicking a problem opens its step and focuses the field", async ({
      page,
    }) => {
      await offline(page);
      await localChecks(page, (source) =>
        source.includes('"id":"b"')
          ? {
              validationOk: false,
              errorCount: 1,
              partial: true,
              diagnostics: [
                {
                  code: "WV-COMP-TYPE_MISMATCH",
                  severity: "error",
                  path: "/spec/steps/0/cases/0/steps/1/value",
                  message: "Expression type does not match.",
                },
              ],
            }
          : { ...clean, partial: true },
      );
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
                steps: [
                  { id: "a", kind: "transform", value: { literal: 1 } },
                  { id: "b", kind: "transform", value: { literal: 2 } },
                ],
              },
            ],
            default: { steps: [] },
          },
        ]),
      );
      // The row says what's wrong; its place is a link to the field.
      const place = panel(page).getByRole("button", { name: /Step b · Value/ });
      await expect(
        panel(page)
          .locator(".diagnostic-row")
          .filter({ hasText: "Step b · Value" }),
      ).toContainText(
        "This value's type doesn't match what the field expects.",
      );
      await place.click();
      await expect(designer.node("b")).toHaveClass(/\bselected\b/);
      await expect(designer.inspectorField("Step name")).toHaveValue("b");
      await expect(
        designer.inspector.locator('[data-field="value"] :focus'),
      ).toHaveCount(1);
    });

    test("a workflow problem opens the workflow settings", async ({ page }) => {
      await offline(page);
      await localChecks(page, () => ({
        validationOk: true,
        errorCount: 0,
        partial: true,
        diagnostics: [
          {
            code: "WV-COMP-UNKNOWN_COMPATIBILITY",
            severity: "warning",
            path: "/spec/output",
            message: "Output compatibility is unknown.",
          },
        ],
      }));
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(
        workflow([{ id: "wait-1", kind: "wait", durationSeconds: 60 }]),
      );
      await designer.selectStep("wait-1");
      await reach(
        page,
        panel(page).getByRole("button", {
          name: /Workflow settings · Workflow output/,
        }),
      );
      await expect(page.locator(".graph-node.selected")).toHaveCount(0);
      await expect(designer.inspectorField("Name")).toBeVisible();
      await expect(
        designer.inspector.locator('[data-field="spec/output"] :focus'),
      ).toHaveCount(1);
      await expect(panel(page)).toContainText("No errors, 1 warning.");
    });

    test("a suggested edit is applied as one undoable change", async ({
      page,
    }) => {
      await offline(page);
      await localChecks(page, (source) =>
        source.includes("Customer not found")
          ? { ...clean, partial: true }
          : {
              validationOk: false,
              errorCount: 1,
              partial: true,
              diagnostics: [
                {
                  code: "WV-COMP-EXPRESSION",
                  severity: "error",
                  path: "/spec/steps/0/message",
                  message: "Message is empty.",
                  hint: "Say what went wrong.",
                  suggestedEdit: {
                    path: "/spec/steps/0/message",
                    value: "Customer not found",
                  },
                },
              ],
            },
      );
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(
        workflow([{ id: "stop", kind: "fail", code: "missing", message: "x" }]),
      );
      await expect(panel(page)).toContainText("Say what went wrong.");
      await panel(page)
        .getByRole("button", { name: "Apply suggested edit" })
        .click();
      await expect(panel(page)).toContainText("No problems found");
      expect(await sourceText(page)).toContain("Customer not found");
      await command(page, "Undo");
      expect(await sourceText(page)).not.toContain("Customer not found");
    });

    test("Publish needs a passing compile against the project catalog", async ({
      page,
    }) => {
      await connected(page, { capabilities: [...allCapabilities, "compile"] });
      await localChecks(page);
      let failing = true;
      await page.route(`${project}/compiler/compile`, (r) =>
        r.fulfill({
          json: failing
            ? {
                ok: false,
                validationOk: false,
                partial: false,
                errorCount: 1,
                diagnostics: [
                  {
                    code: "WV-COMP-UNKNOWN_ACTION",
                    severity: "error",
                    path: "/spec/steps/0/uses",
                    message: "Unknown action.",
                  },
                ],
              }
            : { ...clean, ok: true, partial: false, artifact: {} },
        }),
      );
      const posts: unknown[] = [];
      await page.route(`${project}/workflows`, (r) => {
        posts.push(r.request().postDataJSON());
        return r.fulfill({
          status: 201,
          json: { id: "22222222-2222-4222-8222-222222222222" },
        });
      });
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(
        workflow([
          {
            id: "call",
            kind: "action",
            uses: "crm.lookup@9.9.9",
            with: { literal: {} },
          },
        ]),
      );
      await command(page, "Publish…");
      await expect(panel(page)).toContainText("1 error");
      await expect(panel(page)).toContainText("Step call · Action version");
      await expect(page.getByRole("dialog", { name: /^Publish / })).toHaveCount(
        0,
      );
      expect(posts).toHaveLength(0);
      failing = false;
      await command(page, "Publish…");
      await page.getByRole("button", { name: "Publish version" }).click();
      await expect.poll(() => posts.length).toBe(1);
    });

    test("a check of the previous workflow never reports on the next one", async ({
      page,
    }) => {
      await offline(page);
      let release = () => {};
      const held = new Promise<void>((resolve) => (release = resolve));
      await localChecks(page, (source) =>
        source.includes("old-workflow")
          ? {
              validationOk: false,
              errorCount: 1,
              partial: true,
              diagnostics: [
                {
                  code: "WV-COMP-EXPRESSION",
                  severity: "error",
                  path: "/spec/steps/0/message",
                  message: "A problem in the old workflow.",
                },
              ],
            }
          : { ...clean, partial: true },
      );
      // The old workflow's check answers only after the next one opened.
      await page.route("**/studio/local/validate", async (r) => {
        if (String(r.request().postDataJSON().source).includes("old-workflow"))
          await held;
        return r.fallback();
      });
      await newWorkflow(page);
      await new DesignerPage(page).setSource(
        workflow([
          { id: "stop", kind: "fail", code: "x", message: "old-workflow" },
        ]),
      );
      // Let the check of the typed source start.
      await page.waitForTimeout(900);
      await page
        .getByRole("button", { name: "Workflows", exact: true })
        .click();
      // A local workflow is kept on this computer; leaving says so.
      await page
        .getByRole("dialog")
        .getByRole("button", { name: /^Leave/ })
        .click();
      await newWorkflow(page);
      release();
      const shown: string[] = [];
      for (let i = 0; i < 15; i++) {
        shown.push(await panel(page).innerText());
        await page.waitForTimeout(100);
      }
      expect(shown.join("\n")).not.toContain("old workflow");
      expect(shown.join("\n")).not.toContain("1 error");
      await expect(panel(page)).toContainText("No problems found");
    });

    test("a busy Studio host is asked again instead of reporting a failure", async ({
      page,
    }) => {
      await offline(page);
      let calls = 0;
      await page.route("**/studio/local/validate", (r) => {
        calls++;
        // Both analysis slots are taken (say, by an OpenAPI import) once.
        return calls === 1
          ? r.fulfill({
              status: 503,
              json: {
                code: "WV-STUDIO-BUSY",
                message:
                  "Studio is still analyzing an earlier document; try again shortly",
              },
            })
          : r.fulfill({ json: { ...clean, partial: true } });
      });
      await newWorkflow(page);
      await expect(panel(page)).toContainText("No problems found", {
        timeout: 4000,
      });
      await expect(panel(page)).not.toContainText("couldn't check");
      expect(calls).toBe(2);
    });

    test("a suggested edit on the Source tab applies the typed source with it", async ({
      page,
    }) => {
      await offline(page);
      await localChecks(page, (source) =>
        source.includes("Customer not found")
          ? { ...clean, partial: true }
          : {
              validationOk: false,
              errorCount: 1,
              partial: true,
              diagnostics: [
                {
                  code: "WV-COMP-EXPRESSION",
                  severity: "error",
                  path: "/spec/steps/0/message",
                  message: "Message is empty.",
                  suggestedEdit: {
                    path: "/spec/steps/0/message",
                    value: "Customer not found",
                  },
                },
              ],
            },
      );
      await newWorkflow(page);
      await page.getByRole("tab", { name: "Source", exact: true }).click();
      // Typed, never applied: the suggestion belongs to this text.
      await page.getByRole("textbox", { name: "Workflow source" }).fill(
        workflow([
          { id: "stop", kind: "fail", code: "missing", message: "x" },
          { id: "typed-only", kind: "wait", durationSeconds: 5 },
        ]),
      );
      await reach(
        page,
        panel(page).getByRole("button", { name: "Apply suggested edit" }),
      );
      await expect(panel(page)).toContainText("No problems found");
      const text = await sourceText(page);
      expect(text).toContain("Customer not found");
      expect(text).toContain("typed-only");
    });

    test("a suggestion made before the latest edit is refused, not applied over it", async ({
      page,
    }) => {
      await offline(page);
      await page.route("**/studio/local/validate", async (r) => {
        const source = String(r.request().postDataJSON().source);
        // The edited workflow is still being checked when the button is used.
        if (source.includes("edited")) return;
        return r.fulfill({
          json: {
            validationOk: false,
            errorCount: 1,
            partial: true,
            diagnostics: [
              {
                code: "WV-COMP-EXPRESSION",
                severity: "error",
                path: "/spec/steps/0/message",
                message: "Message is empty.",
                suggestedEdit: {
                  path: "/spec/steps/0/message",
                  value: "Customer not found",
                },
              },
            ],
          },
        });
      });
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(
        workflow([{ id: "stop", kind: "fail", code: "missing", message: "x" }]),
      );
      await expect(panel(page)).toContainText("1 error");
      await designer.setSource(
        workflow([{ id: "stop", kind: "fail", code: "edited", message: "x" }]),
      );
      await reach(
        page,
        panel(page).getByRole("button", { name: "Apply suggested edit" }),
      );
      await expect(
        page.getByText("The workflow changed after this suggestion was made."),
      ).toBeVisible();
      const text = await sourceText(page);
      expect(text).toContain("edited");
      expect(text).not.toContain("Customer not found");
    });

    test("a problem in one action input focuses that input field", async ({
      page,
    }) => {
      await connected(page);
      await localChecks(page, () => ({
        validationOk: false,
        errorCount: 1,
        partial: true,
        diagnostics: [
          {
            code: "WV-COMP-TYPE_MISMATCH",
            severity: "error",
            path: "/spec/steps/0/with/literal/limit",
            message: "Expression type does not match.",
          },
        ],
      }));
      await newWorkflow(page);
      await new DesignerPage(page).setSource(
        workflow([
          {
            id: "lookup",
            kind: "action",
            uses: "sql.lookup@1.0.0",
            with: { literal: { parameters: { customerId: "c" }, limit: 0 } },
          },
        ]),
      );
      await reach(
        page,
        panel(page).getByRole("button", { name: /Step lookup · Input/ }),
      );
      await expect(
        page
          .locator(".action-input-form")
          .getByLabel(/^Limit(\s*\(optional\))?$/),
      ).toBeFocused();
    });
  });
