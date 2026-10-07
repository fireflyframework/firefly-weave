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
// Layout geometry across desktop-shell, tablet and browser sizes. 640x360 is
// the CSS viewport of a 1280x720 window at 200% browser zoom.
import { test, expect, Page } from "@playwright/test";
import { command, connected, insertStep, newWorkflow } from "./support";
import { chooseAction } from "./integrations-po";

const sizes = [
  { width: 600, height: 500 },
  { width: 640, height: 360 },
  { width: 768, height: 1024 },
  { width: 1024, height: 768 },
  { width: 1280, height: 720 },
  { width: 1440, height: 900 },
  { width: 1920, height: 1080 },
];
const run = {
  id: "00000000-0000-0000-0000-000000000104",
  business_key: "expense-104",
  correlation_key: "batch",
  artifact_digest: "sha256:0123456789abcdef",
  state: { status: "waiting", active: [] },
};

async function expectGeometry(page: Page, name: string) {
  const result = await page.evaluate(() => {
    const width = window.innerWidth;
    const controls = [
      ...document.querySelectorAll<HTMLElement>(
        'button, [role="button"], a[href], select, input:not([type="hidden"]), textarea',
      ),
    ];
    const offscreen = controls
      .filter((element) => {
        // Graph nodes pan freely inside the canvas.
        if (element.closest(".canvas")) return false;
        const box = element.getBoundingClientRect();
        if (box.width === 0 || box.height === 0) return false;
        if (getComputedStyle(element).visibility === "hidden") return false;
        return box.left < -1 || box.right > width + 1;
      })
      .map(
        (element) =>
          `${element.tagName}:${(element.getAttribute("aria-label") || element.textContent || "").trim().slice(0, 40)}`,
      );
    return {
      width,
      scrollWidth: document.scrollingElement!.scrollWidth,
      offscreen,
    };
  });
  expect(result.scrollWidth, `${name} page scroll width`).toBeLessThanOrEqual(
    result.width,
  );
  expect(result.offscreen, `${name} controls outside the viewport`).toEqual([]);
}
// The control is fully inside the viewport and receives a pointer at its center.
async function expectReachable(page: Page, name: string) {
  const control = page.getByRole("button", { name, exact: true });
  await control.scrollIntoViewIfNeeded();
  const box = await control.boundingBox();
  const viewport = page.viewportSize()!;
  expect(box, name).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width);
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height);
  const hit = await control.evaluate((element) => {
    const r = element.getBoundingClientRect();
    const found = document.elementFromPoint(
      r.left + r.width / 2,
      r.top + r.height / 2,
    );
    return found === element || element.contains(found);
  });
  expect(hit, `${name} is not covered`).toBe(true);
}

for (const size of sizes) {
  const tag = `${size.width}x${size.height}`;
  test(`layout geometry at ${tag}`, async ({ page }) => {
    await page.setViewportSize(size);
    await connected(page);
    await page.route("**/environments/development/runs?*", (r) =>
      r.fulfill({ json: { items: [run], next_cursor: null } }),
    );
    await page.route(`**/environments/development/runs/${run.id}`, (r) =>
      r.fulfill({ json: run }),
    );
    await page.route(`**/runs/${run.id}/lifecycle`, (r) =>
      r.fulfill({ json: { archived: false, purged: false, revision: 1 } }),
    );
    await page.route(`**/runs/${run.id}/history?*`, (r) =>
      r.fulfill({ json: { events: [], next_cursor: null } }),
    );
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    await expectGeometry(page, "home");
    await page.screenshot({ path: `test-results/responsive/home-${tag}.png` });

    await newWorkflow(page);
    await insertStep(page, "Call an action");
    // Below 768 px inserting a step leaves the canvas visible; open the inspector from the node.
    if (size.width < 768)
      await page.locator('[data-step="call-action-1"] .node-body').click();
    await chooseAction(page, "sql.lookup@1.0.0");
    await page
      .locator("weave-action-inspector summary", { hasText: "Action details" })
      .click();
    await expect(page.locator(".integration-requirements")).toBeVisible();
    await expectGeometry(page, "designer");
    await expectReachable(page, "Step actions");
    const inspector = (await page.locator(".inspector").boundingBox())!;
    expect(inspector.x + inspector.width).toBeLessThanOrEqual(size.width);
    expect(inspector.y + inspector.height).toBeLessThanOrEqual(size.height);
    await page.screenshot({
      path: `test-results/responsive/designer-${tag}.png`,
    });
    const close = page.getByRole("button", { name: "Close inspector" });
    if (await close.isVisible()) await close.click();
    await command(page, "Publish…");
    await expect(
      page.getByRole("dialog", { name: "Apply your changes?" }),
    ).toHaveCount(0);
    await expect(page.getByRole("dialog")).toBeVisible();
    await expectGeometry(page, "dialog");
    await expectReachable(page, "Publish version");
    await page.screenshot({
      path: `test-results/responsive/dialog-${tag}.png`,
    });
    await page.getByRole("button", { name: "Cancel", exact: true }).click();

    await page.getByRole("button", { name: "Runs", exact: true }).click();
    // The inserted step is unsaved, so leaving asks first.
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Leave designer" })
      .click();
    await page.locator(".resource-row").first().click();
    await expect(page.locator(".record-detail")).toBeVisible();
    await expectGeometry(page, "runs");
    await expectReachable(page, "Close detail");
    await page.screenshot({ path: `test-results/responsive/runs-${tag}.png` });
    // Below 1025 px the detail is a modal sheet over the list: close it first.
    await page.getByRole("button", { name: "Close detail" }).click();

    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "Keyboard controls" }),
    ).toBeAttached();
    await expectGeometry(page, "settings");
    await page.screenshot({
      path: `test-results/responsive/settings-${tag}.png`,
      fullPage: true,
    });
  });
}
