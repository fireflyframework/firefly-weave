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
// WP-05: the designer never saves over another draft, never sends stale
// inspector edits, never undoes into another workflow, and reports what the
// platform said when publishing fails. Real clicks and keys at 1440x900 and
// 600x500.
import { test, expect, Locator, Page, Request } from "@playwright/test";
import {
  allCapabilities,
  closeSheet,
  command,
  connected,
  insertStep,
  newWorkflow,
  offline,
  sourceText,
} from "./support";
import { DesignerPage } from "./designer-po";

const project = "**/studio/api/api/v1/tenants/tenant/projects/project";
const workflow = (name: string, steps: unknown[] = []) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name, version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps,
    output: { literal: {} },
  },
});
const draftId = "11111111-1111-4111-8111-111111111111";
const versionId = "22222222-2222-4222-8222-222222222222";

/** Clicks a toolbar control, closing a narrow-layout inspector that covers it. */
async function press(page: Page, control: Locator) {
  const covered = await control.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const hit = document.elementFromPoint(
      box.left + box.width / 2,
      box.top + box.height / 2,
    );
    return !(hit === element || element.contains(hit));
  });
  // A narrow-layout inspector is a modal sheet over the toolbar.
  if (covered) await closeSheet(page);
  await control.click();
}
const toolbar = (page: Page, name: string) =>
  page.getByRole("button", { name, exact: true });

/** Library routes: one draft and one published version. */
async function library(page: Page) {
  await page.route(`${project}/drafts?*`, (r) =>
    r.fulfill({
      json: {
        items: [{ id: draftId, revision: 3, name: "draft-one" }],
        next_cursor: null,
      },
    }),
  );
  await page.route(`${project}/workflows?*`, (r) =>
    r.fulfill({
      json: {
        items: [{ id: versionId, name: "published-one", version: "1.0.0" }],
        next_cursor: null,
      },
    }),
  );
  await page.route(`${project}/drafts/${draftId}`, (r) =>
    r.request().method() === "GET"
      ? r.fulfill({
          json: {
            id: draftId,
            revision: 3,
            definition: workflow("draft-one", [
              { id: "keep", kind: "wait", durationSeconds: 60 },
            ]),
          },
        })
      : r.fallback(),
  );
  await page.route(`${project}/workflows/${versionId}/export`, (r) =>
    r.fulfill({ json: { document: workflow("published-one") } }),
  );
}
async function openFromLibrary(page: Page, collection: string, name: string) {
  await page.getByRole("button", { name: "Workflows", exact: true }).click();
  await page.getByRole("button", { name: collection, exact: true }).click();
  await page.locator(".resource-row").filter({ hasText: name }).click();
  await expect(
    page.getByRole("heading", { name, exact: true, level: 1 }),
  ).toBeVisible();
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("a published version opened after a draft saves as a new draft", async ({
      page,
    }) => {
      await connected(page);
      await library(page);
      const saves: Request[] = [];
      await page.route(`${project}/drafts/*`, (r) => {
        if (r.request().method() !== "PUT") return r.fallback();
        saves.push(r.request());
        return r.fulfill({
          json: { id: "new", revision: 1, document: {} },
        });
      });
      await openFromLibrary(page, "Drafts", "draft-one");
      await expect(page).toHaveURL(
        new RegExp(`/workflows/${draftId}/designer`),
      );
      await openFromLibrary(page, "Published", "published-one");
      await expect(page).not.toHaveURL(new RegExp(draftId));
      await insertStep(page, "Transform");
      await press(page, toolbar(page, "Save draft"));
      await expect.poll(() => saves.length).toBe(1);
      expect(saves[0].url()).not.toContain(draftId);
      expect(saves[0].headers()["if-match"]).toBeUndefined();
      expect(saves[0].postDataJSON().document.metadata.name).toBe(
        "published-one",
      );
      await expect(page).toHaveURL(
        new RegExp(
          `/workflows/${new URL(saves[0].url()).pathname.split("/").at(-1)}/designer`,
        ),
      );
    });

    test("undo after opening a workflow never restores the previous one", async ({
      page,
    }) => {
      await connected(page);
      await library(page);
      await newWorkflow(page);
      await insertStep(page, "Transform");
      await page
        .getByRole("button", { name: "Workflows", exact: true })
        .click();
      await page.getByRole("button", { name: "Leave designer" }).click();
      await openFromLibrary(page, "Published", "published-one");
      // Undo sits in the toolbar, or in More on narrow screens.
      const undo = page.getByRole("button", { name: "Undo" });
      if (await undo.isVisible()) await expect(undo).toBeDisabled();
      else {
        await page.getByRole("button", { name: "More", exact: true }).click();
        await expect(
          page.getByRole("menuitem", { name: "Undo" }),
        ).toHaveAttribute("aria-disabled", "true");
        await page.keyboard.press("Escape");
      }
      await page.getByLabel("Workflow canvas", { exact: true }).focus();
      await page.keyboard.press("ControlOrMeta+z");
      await expect(
        page.getByRole("heading", { name: "published-one", level: 1 }),
      ).toBeVisible();
      await expect(page.locator('[data-step="transform-1"]')).toHaveCount(0);
    });

    test("Validate flushes valid fields and never opens an inspector confirmation", async ({
      page,
    }) => {
      await offline(page);
      const bodies: { source: string }[] = [];
      await page.route("**/studio/local/validate", (r) => {
        bodies.push(r.request().postDataJSON());
        return r.fulfill({
          json: {
            validationOk: true,
            errorCount: 0,
            diagnostics: [],
            partial: true,
          },
        });
      });
      await newWorkflow(page);
      await insertStep(page, "Fail");
      const designer = new DesignerPage(page);
      await designer.selectStep("fail-1");
      await designer.inspectorField("Message").fill("Customer not found");
      await press(page, toolbar(page, "Validate"));
      await expect
        .poll(() =>
          bodies.some((body) =>
            body.source.includes("message: Customer not found"),
          ),
        )
        .toBe(true);
      await expect(
        page.getByRole("dialog", { name: "Apply your changes?" }),
      ).toHaveCount(0);
    });

    test("Validate blocks invalid visible edits and preserves them when the inspector closes", async ({
      page,
    }) => {
      await offline(page);
      const bodies: { source: string }[] = [];
      await page.route("**/studio/local/validate", (r) => {
        bodies.push(r.request().postDataJSON());
        return r.fulfill({
          json: { validationOk: true, errorCount: 0, diagnostics: [] },
        });
      });
      await newWorkflow(page);
      await insertStep(page, "Fail");
      const designer = new DesignerPage(page);
      await designer.selectStep("fail-1");
      await designer.inspectorField("Error code").fill("not valid!");
      await press(page, toolbar(page, "Validate"));
      await expect(
        page.getByText(
          "Fix the invalid fields before continuing. Your edits are still here.",
          { exact: true },
        ),
      ).toBeVisible();
      expect(bodies).toHaveLength(0);
      await designer.selectStep("fail-1");
      await expect(designer.inspectorField("Error code")).toHaveValue(
        "not valid!",
      );
      await designer.inspectorField("Error code").fill("corrected-error");
      await press(page, toolbar(page, "Validate"));
      await expect.poll(() => bodies.length).toBeGreaterThan(0);
      expect(bodies.at(-1)!.source).toContain("corrected-error");
    });

    test("Save and Publish refuse a visible invalid edit in a saved draft", async ({
      page,
    }) => {
      await connected(page);
      await library(page);
      await openFromLibrary(page, "Drafts", "draft-one");
      const requests: Request[] = [];
      await page.route(`${project}/drafts/*`, (route) => {
        if (route.request().method() !== "PUT") return route.fallback();
        requests.push(route.request());
        return route.fulfill({
          json: { id: draftId, revision: 4, document: {} },
        });
      });
      await page.route(`${project}/workflows`, (route) => {
        requests.push(route.request());
        return route.fulfill({ json: { id: versionId } });
      });
      const designer = new DesignerPage(page);
      await designer.selectStep("keep");
      await designer.inspectorField("Duration").fill("0");
      await expect(page.locator(".editor-identity .status-chip")).toHaveText(
        "Unsaved",
      );
      for (const name of ["Save draft", "Publish…"]) {
        const close = page.getByRole("button", {
          name: "Close inspector",
          exact: true,
        });
        if (await close.isVisible()) await close.click();
        await command(page, name);
        await expect(
          page.getByText(
            "Fix the invalid fields before continuing. Your edits are still here.",
            { exact: true },
          ),
        ).toBeVisible();
        await expect(designer.inspectorField("Duration")).toHaveValue("0");
        expect(requests).toHaveLength(0);
      }
    });

    test("an existing invalid field does not prevent another valid field from updating", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(
        JSON.stringify({
          ...workflow("stored-invalid"),
          spec: {
            ...workflow("stored-invalid").spec,
            steps: [
              { id: "stop", kind: "fail", code: "not valid!", message: "x" },
            ],
          },
        }),
      );
      await designer.selectStep("stop");
      await designer.inspectorField("Error code").focus();
      await designer.inspectorField("Error code").press("Tab");
      await expect(designer.inspector).toContainText(
        "Use letters, numbers, dots, underscores or hyphens.",
      );
      await designer.inspectorField("Message").fill("Changed");
      const source = await sourceText(page);
      expect(source).toContain("Changed");
      expect(source).toContain("not valid!");
      await expect(
        page.getByRole("dialog", { name: "Apply your changes?" }),
      ).toHaveCount(0);
    });

    test("dropping a palette step keeps the latest valid inspector fields", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      await insertStep(page, "Wait for time");
      const designer = new DesignerPage(page);
      await designer.selectStep("wait-1");
      await designer.inspectorField("Duration").fill("2");
      const close = page.getByRole("button", { name: "Close inspector" });
      if (await close.isVisible()) await close.click();
      await designer.fit();
      const item = page
        .locator(".palette-step")
        .filter({ hasText: "Transform" });
      if (!(await item.isVisible()))
        await page.getByRole("button", { name: "Insert step" }).click();
      await item.dragTo(designer.target("Add a step here, after wait-1"));
      await expect(page.locator('[data-step="transform-1"]')).toHaveCount(1);
      const source = await sourceText(page);
      expect(source).toContain("durationSeconds: 120");
      await expect(
        page.getByRole("dialog", { name: "Apply your changes?" }),
      ).toHaveCount(0);
    });

    test("Publish opens its own review directly after a valid field edit", async ({
      page,
    }) => {
      await connected(page);
      const posts: Request[] = [];
      await page.route(project + "/workflows", (r) => {
        posts.push(r.request());
        return r.fulfill({ status: 201, json: { id: versionId } });
      });
      await newWorkflow(page);
      await insertStep(page, "Fail");
      const designer = new DesignerPage(page);
      await designer.selectStep("fail-1");
      await designer.inspectorField("Message").fill("Ready to review");
      await closeSheet(page);
      await command(page, "Publish…");
      await expect(
        page.getByRole("dialog", { name: "Apply your changes?" }),
      ).toHaveCount(0);
      await expect(
        page.getByRole("dialog", { name: /^Publish / }),
      ).toBeVisible();
      expect(posts).toHaveLength(0);
    });

    test("a rejected publish shows the platform's diagnostics", async ({
      page,
    }) => {
      await connected(page);
      await page.route(`${project}/workflows`, (r) =>
        r.fulfill({
          status: 422,
          json: {
            code: "WV-COMPILE",
            message: "Definition compilation failed",
            result: {
              ok: false,
              validationOk: false,
              partial: false,
              errorCount: 1,
              diagnostics: [
                {
                  code: "WV-COMP-UNAVAILABLE_REFERENCE",
                  severity: "error",
                  path: "/spec/steps/0/value/ref",
                  message:
                    "Referenced step does not dominate this expression in its lexical scope.",
                },
              ],
            },
          },
        }),
      );
      await newWorkflow(page);
      await insertStep(page, "Transform");
      await closeSheet(page);
      await command(page, "Publish…");
      await page.getByRole("button", { name: "Publish version" }).click();
      const panel = page.getByRole("region", { name: "Compiler diagnostics" });
      await expect(panel).toContainText("WV-COMP-UNAVAILABLE_REFERENCE");
      await expect(panel).toContainText("1 error");
      await expect(page.getByRole("alert")).toContainText(
        "The platform found problems",
      );
    });

    test("a taken version offers the next patch version with a new request", async ({
      page,
    }) => {
      await connected(page);
      const posts: Request[] = [];
      await page.route(`${project}/workflows`, (r) => {
        posts.push(r.request());
        return posts.length === 1
          ? r.fulfill({
              status: 409,
              json: {
                code: "WV-VERSION-CONFLICT",
                message: "An immutable version already has different content",
                result: { ok: false, errorCount: 1, diagnostics: [] },
              },
            })
          : r.fulfill({
              status: 201,
              json: {
                id: versionId,
                name: "untitled-workflow",
                version: "1.0.1",
                digest: "sha256:" + "a".repeat(64),
              },
            });
      });
      await newWorkflow(page);
      await insertStep(page, "Transform");
      await closeSheet(page);
      await command(page, "Publish…");
      await page.getByRole("button", { name: "Publish version" }).click();
      const conflict = page.getByRole("dialog", {
        name: "Version 1.0.0 is already published",
      });
      await expect(conflict).toBeVisible();
      await conflict.getByRole("button", { name: "Publish as 1.0.1" }).click();
      await expect.poll(() => posts.length).toBe(2);
      expect(posts[1].postDataJSON().source).toContain("version: 1.0.1");
      expect(posts[1].headers()["idempotency-key"]).not.toBe(
        posts[0].headers()["idempotency-key"],
      );
      await expect(page.locator(".version-badge")).toHaveText("1.0.1");
      expect(await sourceText(page)).toContain("version: 1.0.1");
    });

    test("Save shortcut needs the save permission and disabled buttons say why", async ({
      page,
    }) => {
      await connected(page, {
        capabilities: allCapabilities.filter((c) => c !== "definition.write"),
      });
      let saves = 0;
      await page.route(`${project}/drafts/*`, (r) => {
        saves++;
        return r.fulfill({ json: { id: "x", revision: 1, document: {} } });
      });
      await newWorkflow(page);
      await insertStep(page, "Transform");
      await page.getByLabel("Workflow canvas", { exact: true }).focus();
      await page.keyboard.press("ControlOrMeta+s");
      await page.waitForTimeout(300);
      expect(saves).toBe(0);
      const save = toolbar(page, "Save draft");
      await expect(save).toBeDisabled();
      await expect(save).toHaveAccessibleDescription(
        "Your account can't save drafts in this workspace.",
      );
      // The other commands wait in More, each with its reason.
      await closeSheet(page);
      await page.getByRole("button", { name: "More", exact: true }).click();
      await expect(
        page.getByRole("menuitem", { name: /^Activate…/ }),
      ).toContainText("Publish this version before activating it.");
    });

    test("offline, the platform commands explain that a connection is needed", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const localHelp = page.locator(".toolbar-help");
      await localHelp.locator(":scope > summary").click();
      await expect(localHelp).toContainText("Save to file downloads a copy.");
      await expect(localHelp).toContainText(
        "Connect to a platform to publish, activate and run it.",
      );
      await localHelp.locator(":scope > summary").press("Escape");
      const simulate = toolbar(page, "Simulate");
      if (await simulate.isVisible())
        await expect(simulate).toHaveAccessibleDescription(
          /Connect to a platform/,
        );
      else {
        await page.getByRole("button", { name: "More", exact: true }).click();
        await expect(
          page.getByRole("menuitem", { name: /^Simulate/ }),
        ).toContainText("Connect to a platform to run simulations.");
        await page.keyboard.press("Escape");
      }
      await expect(toolbar(page, "Save draft")).toHaveCount(0);
      await page.getByLabel("Workflow canvas", { exact: true }).focus();
      await page.keyboard.press("ControlOrMeta+s");
      await expect(page.getByRole("alert")).toHaveCount(0);
    });

    test("a connection without destinations says it cannot reach any host", async ({
      page,
    }) => {
      await connected(page);
      const connection = {
        id: "33333333-3333-4333-8333-333333333333",
        name: "crm",
        connector: "weave-http@2.0.0",
        adapter: "weave-http-v2",
        revision: 1,
        allowed_destinations: [],
      };
      await page.route("**/environments/development/connections?*", (r) =>
        r.fulfill({ json: { items: [connection], next_cursor: null } }),
      );
      await page.route(
        `**/environments/development/connections/${connection.id}`,
        (r) => r.fulfill({ json: connection }),
      );
      await page
        .getByRole("button", { name: "Connections", exact: true })
        .click();
      await page.locator(".resource-row").click();
      await expect(page.locator(".record-detail")).toContainText(
        "None — this connection can't reach any host",
      );
    });
  });
