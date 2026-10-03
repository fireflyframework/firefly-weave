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

    test("unapplied inspector edits are applied, discarded or kept before Validate", async ({
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
      // The designer checks each change on its own; count from that check.
      await expect
        .poll(() => bodies.some((b) => b.source.includes("id: fail-1")))
        .toBe(true);
      const live = bodies.length;
      const designer = new DesignerPage(page);
      await designer.selectStep("fail-1");
      await designer.inspectorField("Message").fill("Customer not found");
      await press(page, toolbar(page, "Validate"));
      const dialog = page.getByRole("dialog", { name: "Apply your changes?" });
      await expect(dialog).toBeVisible();
      await expect(dialog).toContainText("fail-1");
      // Keep editing: nothing is validated and the edit stays in the field.
      await dialog.getByRole("button", { name: "Keep editing" }).click();
      await expect(dialog).toHaveCount(0);
      await page.waitForTimeout(900);
      expect(bodies).toHaveLength(live);
      await press(page, toolbar(page, "Validate"));
      await dialog.getByRole("button", { name: "Apply and continue" }).click();
      await expect.poll(() => bodies.length).toBe(live + 1);
      expect(bodies[live].source).toContain("message: Customer not found");
      // Discard: the stored value is validated and the field shows it again.
      await designer.selectStep("fail-1");
      await designer.inspectorField("Message").fill("Something else");
      await press(page, toolbar(page, "Validate"));
      await dialog.getByRole("button", { name: "Discard changes" }).click();
      await expect.poll(() => bodies.length).toBe(live + 2);
      expect(bodies[live + 1].source).toContain("message: Customer not found");
      expect(bodies[live + 1].source).not.toContain("Something else");
    });

    test("an invalid unapplied edit stops the command and focuses the field", async ({
      page,
    }) => {
      await offline(page);
      let validations = 0;
      await page.route("**/studio/local/validate", (r) => {
        validations++;
        return r.fulfill({
          json: { validationOk: true, errorCount: 0, diagnostics: [] },
        });
      });
      await newWorkflow(page);
      await insertStep(page, "Fail");
      // The automatic check of the new step comes first.
      await expect.poll(() => validations).toBeGreaterThan(0);
      await page.waitForTimeout(900);
      const live = validations;
      const designer = new DesignerPage(page);
      await designer.selectStep("fail-1");
      await designer.inspectorField("Error code").fill("not valid!");
      await press(page, toolbar(page, "Validate"));
      await page
        .getByRole("dialog", { name: "Apply your changes?" })
        .getByRole("button", { name: "Apply and continue" })
        .click();
      await expect(designer.inspectorField("Error code")).toBeFocused();
      expect(validations).toBe(live);
    });

    test("a step stored with an invalid value has no unapplied edits until one is made", async ({
      page,
    }) => {
      await offline(page);
      let validations = 0;
      await page.route("**/studio/local/validate", (r) => {
        validations++;
        return r.fulfill({
          json: { validationOk: true, errorCount: 0, diagnostics: [] },
        });
      });
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
      // The automatic check of the new source comes first.
      await expect.poll(() => validations).toBeGreaterThan(0);
      await page.waitForTimeout(900);
      const live = validations;
      await designer.selectStep("stop");
      await expect(designer.inspector).toContainText(
        "Use letters, numbers, dots, underscores or hyphens.",
      );
      const dialog = page.getByRole("dialog", { name: "Apply your changes?" });
      await press(page, toolbar(page, "Validate"));
      await expect.poll(() => validations).toBe(live + 1);
      await expect(dialog).toHaveCount(0);
      // Once the person edits the step, the same invalid value is theirs.
      await designer.selectStep("stop");
      await designer.inspectorField("Message").fill("Changed");
      await press(page, toolbar(page, "Validate"));
      await expect(dialog).toBeVisible();
      await dialog.getByRole("button", { name: "Apply and continue" }).click();
      await expect(designer.inspectorField("Error code")).toBeFocused();
      expect(validations).toBe(live + 1);
    });

    test("dropping a palette step settles unapplied edits first", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      await insertStep(page, "Fail");
      const designer = new DesignerPage(page);
      await designer.selectStep("fail-1");
      await designer.inspectorField("Message").fill("Customer not found");
      // A narrow-layout inspector covers the canvas; wide layouts keep it.
      const close = page.getByRole("button", { name: "Close inspector" });
      if (await close.isVisible()) await close.click();
      await designer.fit();
      const item = page
        .locator(".palette-step")
        .filter({ hasText: "Transform" });
      if (!(await item.isVisible()))
        await page.getByRole("button", { name: "Insert step" }).click();
      await item.dragTo(designer.target("Add a step here, after fail-1"));
      const dialog = page.getByRole("dialog", { name: "Apply your changes?" });
      await expect(dialog).toBeVisible();
      await dialog.getByRole("button", { name: "Apply and continue" }).click();
      await expect(page.locator('[data-step="transform-1"]')).toHaveCount(1);
      const text = await sourceText(page);
      expect(text).toContain("message: Customer not found");
      expect(text).toContain("id: transform-1");
    });

    test("Keep editing stops a publish, not only its validation", async ({
      page,
    }) => {
      await connected(page);
      const posts: Request[] = [];
      await page.route(`${project}/workflows`, (r) => {
        posts.push(r.request());
        return r.fulfill({ status: 201, json: { id: versionId } });
      });
      await newWorkflow(page);
      await insertStep(page, "Fail");
      await press(page, toolbar(page, "Validate"));
      await expect(
        page.getByRole("region", { name: "Compiler diagnostics" }),
      ).toContainText("No problems found");
      const designer = new DesignerPage(page);
      await designer.selectStep("fail-1");
      await designer.inspectorField("Message").fill("Not published yet");
      // Publish waits in More until the draft is saved.
      await closeSheet(page);
      await command(page, "Publish…");
      const dialog = page.getByRole("dialog", { name: "Apply your changes?" });
      await dialog.getByRole("button", { name: "Keep editing" }).click();
      await expect(dialog).toHaveCount(0);
      await expect(page.getByRole("dialog", { name: /^Publish / })).toHaveCount(
        0,
      );
      expect(posts).toHaveLength(0);
      await expect(designer.inspectorField("Message")).toHaveValue(
        "Not published yet",
      );
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
      // Saving to a file leads; the rest needs a platform, and says so.
      await expect(
        page.getByRole("toolbar", { name: "Workflow commands" }),
      ).toContainText("Saving, publishing and runs need a platform.");
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
