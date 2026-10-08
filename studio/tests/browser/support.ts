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
// Shared route mocks for the integration editor, round-trip and layout specs.
import { expect, Locator, Page } from "@playwright/test";

/** Exercise a native or shared Select through its public keyboard/pointer UI. */
export async function selectChoice(
  field: Locator,
  choice: string | { label?: string; value?: string; index?: number },
) {
  if (await field.evaluate((element) => element.tagName === "SELECT")) {
    await field.selectOption(choice);
    return;
  }
  await field.click();
  const id = await field.getAttribute("aria-controls");
  const list = field.page().locator(`[id=${JSON.stringify(id)}]`);
  await expect(list).toBeVisible();
  const options = list.getByRole("option");
  let option: Locator;
  if (typeof choice !== "string" && choice.index !== undefined) {
    option = options.nth(choice.index);
  } else {
    const value = typeof choice === "string" ? choice : choice.value;
    const label = typeof choice === "string" ? choice : choice.label;
    const byValue = list.locator(
      `[role="option"][data-value=${JSON.stringify(value ?? "")} ]`,
    );
    option =
      value !== undefined && (await byValue.count())
        ? byValue
        : list.getByRole("option", { name: label, exact: true });
  }
  await option.click();
  await expect(list).not.toBeVisible();
}

export const profile = {
  name: "Test platform",
  baseUrl: "https://weave.invalid",
  tenantId: "tenant",
  projectId: "project",
  environmentId: "development",
};
export const allCapabilities = [
  "catalog.read",
  "definition.write",
  "definition.publish",
  "release.activate",
  "connection.manage",
  "run.start",
  "run.read",
  "human_task.read",
  "simulate",
];
export const lookupAction = {
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: "sql.lookup", version: "1.0.0" },
  spec: {
    implementation: {
      kind: "connector",
      uses: "weave-postgresql@1.0.0",
      action: "read",
    },
    connection: { connector: "weave-postgresql@1.0.0" },
    sideEffect: "read_only",
    timeoutSeconds: 30,
    retry: { maxAttempts: 3, initialDelaySeconds: 1, maxDelaySeconds: 30 },
    inputSchema: {
      type: "object",
      required: ["parameters"],
      properties: {
        parameters: {
          type: "object",
          title: "Parameters",
          required: ["customerId"],
          properties: {
            customerId: { type: "string", title: "Customer ID", minLength: 1 },
            region: { type: "string", title: "Region", enum: ["eu", "us"] },
          },
        },
        limit: { type: "integer", title: "Limit", minimum: 1 },
        mode: { type: "string", title: "Mode", enum: ["fast", "safe"] },
        label: { type: "string", title: "Label" },
        tags: { type: "array", title: "Tags", items: { type: "string" } },
        dryRun: { type: "boolean", title: "Dry run" },
        options: { type: "object", title: "Options" },
      },
    },
    outputSchema: {
      type: "object",
      properties: { rows: { type: "array" } },
    },
  },
};
export const workerAction = {
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: "crm.lookup", version: "2.0.0" },
  spec: {
    implementation: {
      kind: "worker",
      taskType: "crm-lookup",
      taskVersion: "1.0.0",
    },
    sideEffect: "idempotent",
    timeoutSeconds: 60,
    inputSchema: {
      type: "object",
      required: ["customer"],
      properties: { customer: { type: "string", title: "Customer" } },
    },
    outputSchema: { type: "object" },
  },
};
export interface CatalogEntry {
  id: string;
  document: typeof lookupAction | typeof workerAction;
}
export const defaultCatalog: CatalogEntry[] = [
  { id: "a1", document: lookupAction },
  { id: "a2", document: workerAction },
];
export interface ConnectedOptions {
  capabilities?: string[];
  catalog?: CatalogEntry[];
  // Milliseconds to hold each successive catalog page request.
  catalogDelays?: number[];
  catalogStatus?: number;
  // Number of initial catalog requests that fail with catalogStatus.
  catalogFailures?: number;
  // Milliseconds to hold the identity answer (a slow platform check).
  identityDelay?: number;
}
export interface Recorder {
  catalogPages: number;
  exports: string[];
}
export async function connected(
  page: Page,
  options: ConnectedOptions = {},
): Promise<Recorder> {
  const recorder: Recorder = { catalogPages: 0, exports: [] };
  const capabilities = options.capabilities ?? allCapabilities;
  const catalog = options.catalog ?? defaultCatalog;
  await page.route("**/studio/session", (r) =>
    r.fulfill({
      json: {
        paired: true,
        csrfToken: "test-csrf",
        version: "1",
        mode: "connected",
        profile,
      },
    }),
  );
  await page.route("**/studio/api/api/v1/**", (r) =>
    r.fulfill({ json: { items: [], next_cursor: null } }),
  );
  // The last registered matching route wins, so specific routes follow the catch-all.
  await page.route("**/studio/api/api/v1/identity", async (r) => {
    if (options.identityDelay)
      await new Promise((resolve) =>
        setTimeout(resolve, options.identityDelay),
      );
    await r.fulfill({
      json: {
        principal_id: "human",
        kind: "human",
        grants: [
          {
            role: "test",
            scope: {
              tenant_id: "tenant",
              project_id: "project",
              environment_id: "development",
            },
            resources: [],
            capabilities,
          },
        ],
        workspaces: [],
        truncated: false,
      },
    });
  });
  await page.route("**/projects/project/actions?*", async (r) => {
    const delay = options.catalogDelays?.[recorder.catalogPages] ?? 0;
    recorder.catalogPages++;
    if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
    if (
      options.catalogStatus &&
      recorder.catalogPages <= (options.catalogFailures ?? Infinity)
    )
      return r.fulfill({
        status: options.catalogStatus,
        json: { code: "WV-UNAVAILABLE", message: "Catalog unavailable" },
      });
    await r
      .fulfill({
        json: {
          items: catalog.map((entry) => ({
            id: entry.id,
            name: entry.document.metadata.name,
            version: entry.document.metadata.version,
          })),
          next_cursor: null,
        },
      })
      .catch(() => undefined);
  });
  for (const entry of catalog)
    await page.route(`**/projects/project/actions/${entry.id}/export`, (r) => {
      recorder.exports.push(entry.id);
      return r.fulfill({
        json: {
          id: entry.id,
          name: entry.document.metadata.name,
          version: entry.document.metadata.version,
          document: entry.document,
        },
      });
    });
  await page.route("**/studio/local/validate", (r) =>
    r.fulfill({
      json: {
        validationOk: true,
        errorCount: 0,
        diagnostics: [],
        partial: true,
      },
    }),
  );
  await page.goto("/");
  return recorder;
}
/**
 * A design token's color as the browser computes colors ("rgb(r, g, b)"),
 * read from the page at run time so tests follow the token, not a copy of it.
 */
export async function tokenColor(page: Page, token: string): Promise<string> {
  return page.evaluate((name) => {
    const probe = document.createElement("i");
    probe.style.color = `var(${name})`;
    document.body.append(probe);
    const color = getComputedStyle(probe).color;
    probe.remove();
    return color;
  }, token);
}
export async function offline(page: Page) {
  await page.route("**/studio/session", (r) =>
    r.fulfill({
      json: {
        paired: true,
        csrfToken: "test-session",
        version: "1",
        mode: "offline",
        profile: null,
      },
    }),
  );
  await page.route("**/studio/local/validate", (r) =>
    r.fulfill({
      json: {
        validationOk: true,
        errorCount: 0,
        diagnostics: [],
        partial: true,
        artifact: null,
      },
    }),
  );
  await page.goto("/");
}
export async function newWorkflow(page: Page) {
  await page.getByRole("button", { name: "New workflow", exact: true }).click();
  // The designer loads with its first use: wait until it is on screen,
  // its lists rendered too.
  await expect(page.locator(".editor-bar")).toBeVisible();
  await expect(page.locator(".palette-step").first()).toBeAttached();
  // Below 600 px a workflow opens on its outline; these specs use the canvas.
  const canvas = page.getByRole("button", { name: "Show canvas" });
  if (await canvas.isVisible()) await canvas.click();
}
// Inserts a palette step, opening the palette popover on narrow layouts. The
// popover is a modal sheet there: the insert closes it before anything else.
export async function insertStep(page: Page, label: string) {
  await expect(page.locator(".palette-step").first()).toBeAttached();
  const step = page
    .locator(".palette")
    .getByRole("button", { name: label, exact: true });
  if (!(await step.isVisible()))
    await page.getByRole("button", { name: "Insert step" }).click();
  await step.click();
  await expect(page.locator(".palette.popover-visible")).toHaveCount(0);
}
/**
 * Closes a side panel that covers the page as a modal sheet (narrow layouts,
 * zoom) with a real click on the dimmed page around it.
 */
export async function closeSheet(page: Page) {
  const sheet = page.locator('.sheet-modal[aria-modal="true"]');
  if (!(await sheet.count())) return;
  await page.locator(".sheet-scrim").click({ position: { x: 4, y: 4 } });
  await expect(sheet).toHaveCount(0);
}
// Asserts that a real pointer at the control's center reaches the control itself.
export async function expectHitTarget(locator: Locator) {
  await locator.scrollIntoViewIfNeeded();
  const hit = await locator.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const found = document.elementFromPoint(
      box.left + box.width / 2,
      box.top + box.height / 2,
    );
    return found === element || element.contains(found)
      ? "self"
      : `${found?.tagName}.${found?.className}`;
  });
  expect(hit).toBe("self");
}
export async function sourceText(page: Page) {
  await closeSheet(page);
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  const value = await page
    .getByRole("textbox", { name: "Workflow source" })
    .inputValue();
  await page.getByRole("tab", { name: "Designer", exact: true }).click();
  return value;
}
/**
 * Runs a designer command by its name: the toolbar's button when it shows
 * there (the lifecycle's one primary, Validate, Simulate), otherwise the item
 * in the toolbar's "More" menu.
 */
export async function command(page: Page, name: string) {
  const toolbar = page.getByRole("toolbar", { name: "Workflow commands" });
  const button = toolbar.getByRole("button", { name, exact: true });
  // A command that just finished may still be relabelling its button.
  const shown = await button.waitFor({ state: "visible", timeout: 2000 }).then(
    () => true,
    () => false,
  );
  if (shown) {
    // A blocked command is aria-disabled but still takes a click (it says
    // why); press it with the pointer where Playwright would wait instead.
    if ((await button.getAttribute("aria-disabled")) === "true") {
      const box = (await button.boundingBox())!;
      return page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    }
    return button.click();
  }
  await toolbar.getByRole("button", { name: "More", exact: true }).click();
  await page
    .getByRole("menuitem", {
      name: new RegExp(`^${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`),
    })
    .click();
}
/** Whether Undo can run: the toolbar's button, or More's item on phones. */
export async function canUndo(page: Page) {
  const toolbar = page.getByRole("toolbar", { name: "Workflow commands" });
  const button = toolbar.getByRole("button", { name: "Undo", exact: true });
  if (await button.isVisible()) return button.isEnabled();
  await toolbar.getByRole("button", { name: "More", exact: true }).click();
  const item = page.getByRole("menuitem", { name: "Undo" });
  const enabled = (await item.getAttribute("aria-disabled")) !== "true";
  await page.keyboard.press("Escape");
  return enabled;
}
