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
// A draft on a platform saves itself: after a pause, at once when step
// details close, and again on request after a failure; the status chip says
// when. Unsaved changes survive a reload as a recovery copy on this computer.
import { expect, test, type Page, type Route } from "@playwright/test";
import { CanvasPage, useNewEditor } from "./canvas-po";
import { StepDetailsPage } from "./step-details-po";
import { box, intersects } from "./step-geometry";

const tenant = "0b9b5a59-6c63-4f3e-9d58-6a3c8f0e2b11";
const project = "5f0c2d4e-7a1b-4c3d-8e9f-0a1b2c3d4e5f";
const draftId = "8d7c6b5a-4f3e-4d2c-9b1a-0f9e8d7c6b5a";
const definition = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "order-intake", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [
      { id: "check-customer", kind: "transform", value: { ref: "/input" } },
    ],
    output: { literal: {} },
  },
};

/** What the platform does with draft saves: answer, fail, or never answer. */
interface Drafts {
  puts: { at: number; body: Record<string, unknown> }[];
  mode: "ok" | "fail" | "hang";
}

async function platform(page: Page, drafts: Drafts) {
  const profile = {
    name: "Test platform",
    baseUrl: "https://weave.invalid",
    tenantId: tenant,
    projectId: project,
    environmentId: "development",
  };
  await page.route("**/studio/session", (route) =>
    route.fulfill({
      json: {
        paired: true,
        csrfToken: "test-csrf",
        version: "1",
        mode: "connected",
        profile,
      },
    }),
  );
  await page.route("**/studio/api/api/v1/**", (route) =>
    route.fulfill({ json: { items: [], next_cursor: null } }),
  );
  await page.route("**/studio/api/api/v1/identity", (route) =>
    route.fulfill({
      json: {
        principal_id: "human",
        kind: "human",
        grants: [
          {
            role: "test",
            scope: {
              tenant_id: tenant,
              project_id: project,
              environment_id: "development",
            },
            resources: [],
            capabilities: ["catalog.read", "definition.write"],
          },
        ],
        workspaces: [],
        truncated: false,
      },
    }),
  );
  let revision = 1;
  await page.route(`**/projects/${project}/drafts/*`, (route: Route) => {
    const request = route.request();
    if (request.method() !== "PUT")
      return route.fulfill({
        json: { id: draftId, revision, document: definition },
        headers: { ETag: `"${revision}"` },
      });
    drafts.puts.push({ at: Date.now(), body: request.postDataJSON() });
    if (drafts.mode === "hang") return undefined;
    if (drafts.mode === "fail")
      return route.fulfill({
        status: 503,
        json: { status: 503, code: "WV-UNAVAILABLE", message: "Try again." },
      });
    revision += 1;
    return route.fulfill({
      json: {
        id: draftId,
        revision,
        document: request.postDataJSON().document,
      },
      headers: { ETag: `"${revision}"` },
    });
  });
  await page.route("**/studio/local/validate", (route) =>
    route.fulfill({
      json: {
        validationOk: true,
        errorCount: 0,
        diagnostics: [],
        partial: true,
      },
    }),
  );
}

async function openDraft(
  page: Page,
  drafts: Drafts,
  size = { width: 1440, height: 900 },
) {
  await page.setViewportSize(size);
  await useNewEditor(page);
  await platform(page, drafts);
  const signedIn = page.waitForResponse("**/studio/api/api/v1/identity");
  await page.goto(`/workflows/${draftId}/designer`);
  await signedIn;
  const view = new CanvasPage(page);
  await view.ready();
  return view;
}

/** A change on the canvas: the step is duplicated. */
async function edit(page: Page) {
  await page.locator('[data-step="check-customer"]').first().click();
  await page.keyboard.press("ControlOrMeta+d");
}
const chip = (page: Page) => page.locator(".editor-identity .status-chip");

test("a platform draft saves itself after a pause and the chip says when", async ({
  page,
}) => {
  const drafts: Drafts = { puts: [], mode: "ok" };
  await openDraft(page, drafts);
  expect(drafts.puts).toHaveLength(0);
  await edit(page);
  await page.waitForTimeout(500);
  await edit(page);
  await expect.poll(() => drafts.puts.length, { timeout: 4000 }).toBe(1);
  await expect(chip(page)).toHaveText(/^Saved \d{2}:\d{2}$/);
  // Both edits went in the one save.
  const steps = (drafts.puts[0].body["document"] as typeof definition).spec
    .steps;
  expect(steps).toHaveLength(3);
});

test("closing step details saves at once", async ({ page }) => {
  const drafts: Drafts = { puts: [], mode: "ok" };
  await openDraft(page, drafts);
  const details = new StepDetailsPage(page);
  await details.open("check-customer");
  await details.dialog
    .getByRole("button", { name: "Rename check-customer" })
    .click();
  await details.dialog
    .getByRole("textbox", { name: "Step name" })
    .fill("check-account");
  await page.keyboard.press("Enter");
  await expect(details.dialogFor("check-account")).toBeVisible();
  const closing = Date.now();
  await details.close();
  await expect.poll(() => drafts.puts.length, { timeout: 2000 }).toBe(1);
  expect(drafts.puts[0].at - closing).toBeLessThan(1000);
});

test("a failed save says so and retries on request", async ({ page }) => {
  const drafts: Drafts = { puts: [], mode: "fail" };
  await openDraft(page, drafts);
  await edit(page);
  const retry = page.getByRole("button", { name: "Not saved: retry" });
  await expect(retry).toBeVisible({ timeout: 5000 });
  // The chip is a button at its target size, centered with the workflow's name.
  const [button, title] = [
    await box(retry),
    await box(page.locator(".editor-identity h1")),
  ];
  expect(button.height).toBeGreaterThanOrEqual(24);
  expect(
    Math.abs(button.y + button.height / 2 - (title.y + title.height / 2)),
  ).toBeLessThanOrEqual(1);
  drafts.mode = "ok";
  await retry.click();
  await expect(chip(page)).toHaveText(/^Saved \d{2}:\d{2}$/);
});

test("unsaved changes come back after a reload", async ({ page }) => {
  const drafts: Drafts = { puts: [], mode: "hang" };
  await openDraft(page, drafts);
  await edit(page);
  // The recovery copy is kept 800 ms after the change.
  await page.waitForTimeout(1200);
  await page.reload();
  await new CanvasPage(page).ready();
  const offer = page.locator(".recovery-note");
  await expect(offer).toContainText(
    "This computer has changes to this draft that weren't saved. Restore them?",
  );
  // One row: icon, text and buttons share a center line.
  const [restoreBox, textBox] = [
    await box(offer.getByRole("button", { name: "Restore" })),
    await box(offer.locator(".recovery-text")),
  ];
  expect(
    Math.abs(
      restoreBox.y + restoreBox.height / 2 - (textBox.y + textBox.height / 2),
    ),
  ).toBeLessThanOrEqual(1);
  await offer.getByRole("button", { name: "Restore" }).click();
  await expect(offer).toHaveCount(0);
  await expect(page.locator('[data-step^="check-customer"]')).toHaveCount(2);
});

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
  { width: 360, height: 640 },
])
  test(`the retry chip and the recovery notice fit and align at ${size.width}x${size.height}`, async ({
    page,
  }) => {
    const drafts: Drafts = { puts: [], mode: "hang" };
    await openDraft(page, drafts, size);
    await edit(page);
    await page.waitForTimeout(1200);
    await page.reload();
    await new CanvasPage(page).ready();
    const offer = page.locator(".recovery-note");
    await expect(offer).toBeVisible();
    const touch = size.width < 768;
    const note = await box(offer);
    const text = await box(offer.locator(".recovery-text"));
    const restore = await box(offer.getByRole("button", { name: "Restore" }));
    const discard = await box(offer.getByRole("button", { name: "Discard" }));
    // Inside the window, buttons at their target size, one shared center line, nothing overlapping.
    expect(note.x).toBeGreaterThanOrEqual(0);
    expect(note.right).toBeLessThanOrEqual(size.width + 0.5);
    for (const button of [restore, discard]) {
      expect(button.height).toBeGreaterThanOrEqual(touch ? 44 : 32);
      expect(button.right).toBeLessThanOrEqual(note.right - 15.5);
    }
    expect(Math.abs(restore.y - discard.y)).toBeLessThanOrEqual(0.5);
    expect(intersects(restore, discard)).toBe(false);
    expect(intersects(text, restore)).toBe(false);
    if (restore.y < text.bottom)
      expect(
        Math.abs(restore.y + restore.height / 2 - (text.y + text.height / 2)),
      ).toBeLessThanOrEqual(1);
    expect(text.x - note.x).toBeGreaterThanOrEqual(15.5);
    // The chip as a button: its target, centered with the workflow's name.
    drafts.mode = "fail";
    await offer.getByRole("button", { name: "Discard" }).click();
    await edit(page);
    const retry = page.getByRole("button", { name: "Not saved: retry" });
    await expect(retry).toBeVisible({ timeout: 6000 });
    const button = await box(retry);
    const title = await box(page.locator(".editor-identity h1"));
    expect(button.height).toBeGreaterThanOrEqual(touch ? 44 : 24);
    expect(button.width).toBeGreaterThanOrEqual(touch ? 44 : 24);
    expect(
      Math.abs(button.y + button.height / 2 - (title.y + title.height / 2)),
    ).toBeLessThanOrEqual(1);
    expect(intersects(button, title)).toBe(false);
  });
