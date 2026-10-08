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
// Findings of the Studio design review, each pinned with real clicks:
// toasts announced by live regions that are always there and never drawn
// over a dialog or the navigation rail; a workflow name the editor bar never
// cuts by a pixel; the canvas help that stays readable over steps; disclosures
// that show they open; the run graph centered in its panel; and form fields
// that mark the optional ones instead of starring the required ones.
import { test, expect, type Page } from "@playwright/test";
import {
  allCapabilities,
  closeSheet,
  connected,
  insertStep,
  newWorkflow,
  offline,
} from "./support";
import { DesignerPage } from "./designer-po";
import { chooseAction, openPaletteIntegrations } from "./integrations-po";

const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";
const versionId = "11111111-1111-4111-8111-111111111111";
const run = {
  id: "700587c4-1111-4111-8111-000000000104",
  business_key: "expense-104",
  activation: {
    name: "expense-review",
    revision: 2,
    request: { version_id: versionId },
  },
  created_at: "2026-10-01T09:12:00Z",
  state: { status: "waiting", active: ["review"], steps: { check: {} } },
};
const workflow = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "expense-review", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [
      { id: "check", kind: "transform", value: { literal: true } },
      {
        id: "review",
        kind: "humanTask",
        assignment: "reviewers",
        title: { literal: "Review" },
        context: { literal: {} },
        formSchema: { type: "object" },
        decisions: ["approve", "reject"],
      },
    ],
    output: { literal: {} },
  },
};

/** A platform with one waiting run, opened on its detail. */
async function openRun(page: Page) {
  await connected(page, {
    capabilities: [...allCapabilities, "run.pause", "run.archive"],
  });
  await page.route("**/projects/project/workflows?*", (r) =>
    r.fulfill({
      json: {
        items: [{ id: versionId, name: "expense-review", version: "1.0.0" }],
        next_cursor: null,
      },
    }),
  );
  await page.route(`${environment}/runs?*`, (r) =>
    r.fulfill({ json: { items: [run], next_cursor: null } }),
  );
  await page.route(`${environment}/runs/${run.id}`, (r) =>
    r.fulfill({ json: run }),
  );
  await page.route(`${environment}/runs/${run.id}/lifecycle`, (r) =>
    r.fulfill({ json: { archived: false, purged: false, revision: 1 } }),
  );
  await page.route(`${environment}/runs/${run.id}/history?*`, (r) =>
    r.fulfill({ json: { events: [], next_cursor: null } }),
  );
  await page.route(`**/projects/project/workflows/${versionId}/export`, (r) =>
    r.fulfill({ json: { id: versionId, document: workflow } }),
  );
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  await page.locator(".resource-row").first().click();
  return page.locator(".record-detail");
}

/** A local workflow with a step, its Delete toast showing. */
async function deleteWithToast(page: Page) {
  await offline(page);
  await newWorkflow(page);
  await insertStep(page, "Wait for time");
  await closeSheet(page);
  await page.locator('[data-step="wait-1"] .node-body').focus();
  await page.keyboard.press("Delete");
  const toast = page.locator(".toast");
  await expect(toast).toContainText("Deleted wait-1.");
  return toast;
}

test.describe("1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("toasts are announced by live regions that are always in the page", async ({
    page,
  }) => {
    await offline(page);
    // The polite region exists, empty, before anything happened; an alert
    // appears only with a danger toast.
    const polite = page.locator(".toast-region > [role='status']");
    const assertive = page.locator(".toast-region > [role='alert']");
    await expect(polite).toHaveCount(1);
    await expect(polite).toBeEmpty();
    await expect(assertive).toHaveCount(0);
    await newWorkflow(page);
    await insertStep(page, "Wait for time");
    await page.locator('[data-step="wait-1"] .node-body').focus();
    await page.keyboard.press("Delete");
    const toast = polite.locator(".toast");
    await expect(toast).toContainText("Deleted wait-1.");
    await expect(polite).toHaveAttribute("aria-atomic", "true");
    await expect(assertive).toHaveCount(0);
    // The same words again are a new announcement: a new element.
    await toast.evaluate((e) => e.setAttribute("data-seen", "1"));
    await toast.getByRole("button", { name: "Undo" }).click();
    await expect(polite.locator(".toast")).toContainText("Restored wait-1.");
    await page.locator('[data-step="wait-1"] .node-body').focus();
    await page.keyboard.press("Delete");
    await expect(polite.locator(".toast")).toContainText("Deleted wait-1.");
    await expect(polite.locator(".toast[data-seen]")).toHaveCount(0);
  });

  test("a toast never sits over an open dialog", async ({ page }) => {
    const toast = await deleteWithToast(page);
    await page
      .getByRole("button", { name: "Keyboard shortcuts", exact: true })
      .click();
    const dialog = page.getByRole("dialog", { name: "Keyboard shortcuts" });
    await expect(dialog).toBeVisible();
    // A real pointer over the toast lands on the dialog's backdrop.
    const box = (await toast.boundingBox())!;
    const hit = await page.evaluate(
      ([x, y]) => document.elementFromPoint(x, y)?.className ?? "",
      [box.x + box.width / 2, box.y + box.height / 2],
    );
    expect(hit).toContain("modal-backdrop");
  });

  test("the canvas help reads as a quiet chip over the steps it overlaps", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    const help = page.locator(".canvas-key-help");
    await expect(help).toHaveText(
      "Scroll to pan, Ctrl + scroll to zoom. Press ? for shortcuts.",
    );
    const style = await help.evaluate((e) => {
      const s = getComputedStyle(e);
      return { background: s.backgroundColor, z: s.zIndex };
    });
    expect(style.background).not.toMatch(/rgba\(0, 0, 0, 0\)|transparent/);
    expect(Number(style.z)).toBeGreaterThan(0);
  });

  test("a disclosure shows a chevron that turns when it opens", async ({
    page,
  }) => {
    const detail = await openRun(page);
    const summary = detail.getByText("Technical details", { exact: true });
    const chevron = () =>
      summary.evaluate((e) => {
        const s = getComputedStyle(e, "::before");
        return { width: s.borderRightWidth, transform: s.transform };
      });
    const closed = await chevron();
    expect(closed.width).toBe("2px");
    await summary.click();
    await expect(detail.locator(".technical pre")).toBeVisible();
    await expect
      .poll(async () => (await chevron()).transform)
      .not.toBe(closed.transform);
  });

  test("an action's optional inputs say so; required ones carry no asterisk", async ({
    page,
  }) => {
    await connected(page);
    await newWorkflow(page);
    const designer = new DesignerPage(page);
    await designer.append("action");
    await designer.selectStep("call-action-1");
    await chooseAction(page, "sql.lookup@1.0.0");
    const form = page.locator(".action-input-form");
    await expect(form.locator('[data-path="limit"] .optional')).toHaveText(
      "(optional)",
    );
    const required = form
      .locator("label, .schema-label, legend")
      .filter({ hasText: /^Customer ID/ });
    await expect(required).toHaveCount(1);
    await expect(required.locator(".optional")).toHaveCount(0);
    await expect(form).not.toContainText("*");
    // Everyone gets the same words: the marker is part of the field's name.
    await expect(
      form.getByLabel("Limit (optional)", { exact: true }),
    ).toBeVisible();
  });
});

for (const viewport of [
  { width: 1280, height: 720 },
  { width: 600, height: 500 },
])
  test(`the API action builder shows Publish action before it can run, and says why (${viewport.width})`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await connected(page);
    await newWorkflow(page);
    const palette = await openPaletteIntegrations(page);
    await palette.getByRole("button", { name: "New API action" }).click();
    const builder = page.getByRole("dialog", { name: "New API action" });
    const publish = builder.getByRole("button", { name: "Publish action" });
    // In the footer, in view, blocked but focusable.
    await expect(publish).toHaveAttribute("aria-disabled", "true");
    const box = (await publish.boundingBox())!;
    expect(box.y + box.height).toBeLessThanOrEqual(viewport.height);
    await expect(builder.locator(".hb-footer [role='status']")).toBeEmpty();
    // A real pointer press (Playwright waits on aria-disabled controls).
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    const why = builder.locator(".hb-footer [role='status']");
    await expect(why).toHaveText(/to see the action/);
    await expect(publish).toHaveAttribute(
      "aria-describedby",
      (await why.getAttribute("id"))!,
    );
    await expect(publish).toBeFocused();
  });

for (const width of [1440, 1920])
  test(`the run's graph is centered in its panel at ${width}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    const detail = await openRun(page);
    const graph = detail.getByRole("group", {
      name: "Workflow version for this run",
    });
    await expect(graph.locator(".run-node.is-live")).toBeVisible();
    const [frame, world] = await Promise.all([
      graph.boundingBox(),
      graph.locator(".run-graph-world").boundingBox(),
    ]);
    const center = (b: { x: number; width: number }) => b.x + b.width / 2;
    expect(Math.abs(center(frame!) - center(world!))).toBeLessThanOrEqual(2);
  });

for (const viewport of [
  { width: 1280, height: 720 },
  { width: 1440, height: 900 },
])
  test(`the editor bar keeps the workflow name and status readable at ${viewport.width}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await offline(page);
    await newWorkflow(page);
    for (const step of ["Decision", "Wait for time", "Human task"])
      await insertStep(page, step);
    await expect(page.locator(".editor-identity .status-chip")).toHaveText(
      /Draft saved \d{2}:\d{2}/,
    );
    const name = page.locator(".editor-identity h1");
    await expect(name).toHaveText("untitled-workflow");
    const cut = await name.evaluate((e) => e.scrollWidth > e.clientWidth);
    expect(cut).toBe(false);
    expect(
      await page
        .locator(".editor-identity .status-chip")
        .evaluate((e) => e.scrollWidth <= e.clientWidth),
    ).toBe(true);
  });

test.describe("600x500", () => {
  test.use({ viewport: { width: 600, height: 500 } });

  test("a toast sits beside the navigation rail, not over it", async ({
    page,
  }) => {
    const toast = await deleteWithToast(page);
    const rail = (await page.locator(".sidebar").boundingBox())!;
    const box = (await toast.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(rail.x + rail.width + 8);
    expect(box.x + box.width).toBeLessThanOrEqual(600);
  });
});
