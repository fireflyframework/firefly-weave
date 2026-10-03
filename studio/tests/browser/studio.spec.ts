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
import { test, expect } from "@playwright/test";
async function offline(page: any) {
  await page.route("**/studio/session", async (route: any) =>
    route.fulfill({
      json: {
        paired: true,
        csrfToken: "test-session",
        version: "1",
        mode: "offline",
        profile: null,
      },
    }),
  );
  await page.route("**/studio/local/validate", async (route: any) =>
    route.fulfill({
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
  await page.getByRole("button", { name: "New workflow", exact: true }).click();
}
test("pointer movement changes layout only; keyboard undo and source preserve steps", async ({
  page,
}) => {
  await offline(page);
  await page
    .getByRole("button", {
      name: "Wait for time Wait for an external message",
      exact: true,
    })
    .count();
  await page
    .locator(".palette-step")
    .filter({ hasText: "Wait for time" })
    .click();
  await expect(page.locator('[data-step="wait-1"]')).toBeVisible();
  const node = page.locator('[data-step="wait-1"] .node-body');
  const box = await node.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box!.x + 50, box!.y + 25);
  await page.mouse.down();
  await page.mouse.move(box!.x + 130, box!.y + 70, { steps: 10 });
  await page.mouse.up();
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  const source = await page
    .getByRole("textbox", { name: "Workflow source" })
    .inputValue();
  expect(source).toContain("durationSeconds: 60");
  await page.getByRole("tab", { name: "Designer", exact: true }).click();
  await page.keyboard.press("Control+z");
  await expect(page.locator('[data-step="wait-1"]')).toBeVisible();
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  // A local check never claims the catalog passed; it says what it covered.
  await expect(
    page.getByText(
      "No problems found. Actions and connections are checked when you connect.",
      { exact: true },
    ),
  ).toBeVisible();
});
test("palette drag inserts and invalid source leaves graph intact", async ({
  page,
}) => {
  await offline(page);
  const palette = page
    .locator(".palette-step")
    .filter({ hasText: "Transform" });
  // A new workflow's first "+" is the "Add your first step" card.
  await palette.dragTo(
    page.getByRole("button", {
      name: "Add your first step",
      exact: true,
    }),
  );
  await expect(page.locator('[data-step="transform-1"]')).toBeVisible();
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  await page
    .getByRole("textbox", { name: "Workflow source" })
    .fill("not: [valid");
  await page
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await expect(
    page.getByRole("textbox", { name: "Workflow source" }),
  ).toHaveValue("not: [valid");
  await page.getByRole("tab", { name: "Designer", exact: true }).click();
  await expect(page.locator('[data-step="transform-1"]')).toBeVisible();
  await expect(
    page.getByText("Read-only until the source is fixed"),
  ).toBeVisible();
});
for (const width of [1600, 1440, 1280, 1024, 768, 390])
  test(`layout stays inside viewport at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await offline(page);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({ path: `test-results/designer-${width}.png` });
    await page.getByRole("button", { name: "My tasks", exact: true }).click();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  });

test("Start and End remain visual boundaries for empty and populated workflows", async ({
  page,
}) => {
  await offline(page);
  // Start doubles as the way back to the workflow settings.
  await expect(
    page.getByRole("button", { name: "Start — workflow settings" }),
  ).toBeVisible();
  await expect(
    page.getByRole("img", { name: "Workflow end", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".edges > path")).toHaveCount(1);
  await page.locator(".palette-step").filter({ hasText: "Transform" }).click();
  await expect(page.locator(".edges > path")).toHaveCount(2);
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  const source = await page
    .getByRole("textbox", { name: "Workflow source" })
    .inputValue();
  expect(source).not.toContain("$start");
  expect(source).not.toContain("$end");
});

test("property table edits wait duration and rejects invalid values", async ({
  page,
}) => {
  await offline(page);
  await page
    .locator(".palette-step")
    .filter({ hasText: "Wait for time" })
    .click();
  await expect(page.locator(".property-grid")).toBeVisible();
  const duration = page.getByRole("spinbutton", {
    name: "Duration",
    exact: true,
  });
  await page
    .getByRole("combobox", { name: "Duration unit", exact: true })
    .selectOption("s");
  await duration.fill("0");
  await expect(
    page.getByRole("button", { name: "Apply changes", exact: true }),
  ).toBeDisabled();
  await duration.fill("90");
  await page
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  await expect(
    page.getByRole("textbox", { name: "Workflow source" }),
  ).toHaveValue(/durationSeconds: 90/);
});

test("Home import opens a fresh workflow at a readable zoom, Start near the top", async ({
  page,
}) => {
  await offline(page);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Home", exact: true }).click();
  await expect(page.locator("weave-home-dashboard")).toBeVisible();
  await page.locator("weave-home-dashboard input[type=file]").setInputFiles({
    name: "import.yaml",
    mimeType: "text/yaml",
    buffer: Buffer.from(
      "apiVersion: weave/v1alpha1\nkind: Workflow\nmetadata: {name: imported, version: 1.0.0}\nspec:\n  inputSchema: {type: object}\n  outputSchema: {type: object}\n  steps: []\n  output: {literal: {}}\n",
    ),
  });
  await expect(
    page.getByRole("heading", { name: "imported", exact: true }),
  ).toBeVisible();
  // Centered across, top-aligned: Start sits 32 px below the canvas top.
  await expect
    .poll(async () => {
      const canvas = await page.locator(".canvas").boundingBox();
      const start = await page
        .getByRole("button", { name: "Start — workflow settings" })
        .boundingBox();
      return (
        Math.abs(
          start!.x + start!.width / 2 - (canvas!.x + canvas!.width / 2),
        ) < 20 && Math.abs(start!.y - (canvas!.y + 32)) < 6
      );
    })
    .toBe(true);
  await page.screenshot({ path: "test-results/home-import-centered.png" });
});

test("workflow properties and operation builder serialize supported options", async ({
  page,
}) => {
  await offline(page);
  await page
    .getByRole("button", { name: "Show inspector", exact: true })
    .click();
  await page
    .getByRole("textbox", { name: "Name", exact: true })
    .fill("configured");
  await page
    .getByRole("spinbutton", {
      name: "Workflow timeout",
      exact: true,
    })
    .fill("2");
  await page
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await page.locator(".palette-step").filter({ hasText: "Transform" }).click();
  await page
    .getByRole("group", { name: "Value expression mode", exact: true })
    .getByRole("button", { name: "Formula" })
    .click();
  await page
    .getByLabel("Value operator", { exact: true })
    .selectOption("exists");
  await page
    .getByLabel("Value 0 reference", { exact: true })
    .fill("/input/key");
  await page
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  const source = await page
    .getByRole("textbox", { name: "Workflow source" })
    .inputValue();
  expect(source).toContain("name: configured");
  expect(source).toContain("timeoutSeconds: 120");
  expect(source).toContain("name: exists");
  expect(source).toContain("ref: /input/key");
});

test("parallel branches can be added and renamed while populated deletion is guarded", async ({
  page,
}) => {
  await offline(page);
  await page.locator(".palette-step").filter({ hasText: "Parallel" }).click();
  await page.getByRole("button", { name: "Add branch", exact: true }).click();
  await page
    .getByLabel("Rename branch branch-1", { exact: true })
    .fill("audit");
  await page.getByLabel("Rename branch branch-1", { exact: true }).press("Tab");
  await expect(
    page.getByLabel("Rename branch audit", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Remove branch audit", exact: true })
    .click();
  await expect(
    page.getByLabel("Rename branch audit", { exact: true }),
  ).toHaveCount(0);
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  await expect(
    page.getByRole("textbox", { name: "Workflow source" }),
  ).not.toHaveValue(/audit:/);
});

test("Home disconnected badge remains light and visible with expanded and collapsed navigation", async ({
  page,
}) => {
  await offline(page);
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Home", exact: true }).click();
  for (const width of [1454, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    const badge = page.locator(".dashboard-status");
    await expect(badge).toHaveText("Local authoring");
    await expect(badge).toBeVisible();
    // The neutral status pill (--neutral-bg) with its tone border.
    await expect(badge).toHaveCSS("background-color", "rgb(237, 241, 238)");
    await expect(badge).toHaveCSS("border-top-color", "rgb(195, 208, 201)");
    await expect(badge).not.toHaveCSS("min-height", "64px");
    await page.screenshot({
      path: `test-results/home-${width}.png`,
      fullPage: true,
    });
  }
  await page.setViewportSize({ width: 1454, height: 1000 });
  const toggle = page.getByRole("button", {
    name: /Collapse navigation|Expand navigation/,
  });
  await toggle.click();
  await expect(page.locator(".dashboard-status")).toBeVisible();
});
