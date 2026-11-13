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
// The workflow's Runs view beside Designer, Source and Outline: a fourth
// view tab, on which the workflow is read-only and nothing executes.
import { expect, test, type Page } from "@playwright/test";
import { useNewEditor } from "./canvas-po";
import { DesignerPage } from "./designer-po";
import { sizes } from "./step-details-po";
import { allCapabilities, connected, newWorkflow } from "./support";

const source = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: invoice-approval, version: 1.3.0}
spec:
  inputSchema:
    type: object
    properties:
      total: {type: number, title: Total}
  outputSchema: {type: object}
  steps:
    - {id: check, kind: transform, value: {ref: /input}}
    - {id: record, kind: transform, value: {literal: {}}}
  output: {literal: {}}
`;

/** A connected workflow open in the new editor, at `size`. */
async function openWorkflow(page: Page, size = sizes[0]) {
  await page.setViewportSize({ width: size.width, height: size.height });
  await useNewEditor(page);
  await connected(page, { capabilities: [...allCapabilities, "compile"] });
  await newWorkflow(page);
  await new DesignerPage(page).setSource(source);
  return page.getByRole("tablist", { name: "Workflow views" });
}

const workflowText = (page: Page) =>
  page.getByRole("textbox", { name: "Workflow source" }).inputValue();

test("the workflow views include Runs", async ({ page }) => {
  const tabs = await openWorkflow(page);
  await expect(tabs.getByRole("tab")).toHaveText([
    "Designer",
    "Source",
    "Outline",
    "Runs",
  ]);
  const designer = tabs.getByRole("tab", { name: "Designer" });
  const runs = tabs.getByRole("tab", { name: "Runs" });
  await designer.click();
  await designer.focus();
  for (let i = 0; i < 3; i++) await page.keyboard.press("ArrowRight");
  await expect(runs).toBeFocused();
  await expect(runs).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("ArrowRight");
  await expect(designer).toBeFocused();
  await page.keyboard.press("End");
  await expect(runs).toBeFocused();
  await expect(runs).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("#editor-view")).toHaveAttribute(
    "aria-labelledby",
    "view-tab-runs",
  );
  // The Runs view draws the workflow read-only.
  await expect(page.locator(".canvas-v2")).toHaveClass(/\bviewing-runs\b/);
  await page.keyboard.press("Home");
  await expect(designer).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".canvas-v2")).toBeVisible();
});

test("nothing edits the workflow on the Runs tab", async ({ page }) => {
  const tabs = await openWorkflow(page);
  const toolbar = page.getByRole("toolbar", { name: "Workflow commands" });
  const undo = toolbar.getByRole("button", { name: "Undo", exact: true });
  // Applying the source was an edit: Undo can take it back.
  await expect(undo).toBeEnabled();
  await tabs.getByRole("tab", { name: "Source" }).click();
  const before = await workflowText(page);
  await tabs.getByRole("tab", { name: "Source" }).focus();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowRight");
  const runs = tabs.getByRole("tab", { name: "Runs" });
  await expect(runs).toHaveAttribute("aria-selected", "true");
  // The Runs tab draws none of the draft's commands.
  await expect(toolbar).toHaveCount(0);
  // Undo's key works anywhere on the page, so it meets the lock from the
  // tab itself; the canvas's own keys meet it on the run's read-only canvas.
  await page.keyboard.press("ControlOrMeta+z");
  await tabs.getByRole("tab", { name: "Source" }).click();
  expect(await workflowText(page)).toBe(before);
  // The edit made before the Runs tab is still the one Undo takes back.
  await tabs.getByRole("tab", { name: "Designer" }).click();
  await expect(undo).toBeEnabled();
});

test("Execute workflow isn't offered on the Runs tab", async ({ page }) => {
  const tabs = await openWorkflow(page);
  const bar = page.locator(".editor-bar");
  const execute = bar.getByRole("button", { name: "Execute workflow" });
  // Nor the classic designer's Simulate in its place.
  const simulate = bar.getByRole("button", { name: /^Simulat/ });
  await expect(execute).toBeVisible();
  await tabs.getByRole("tab", { name: "Runs" }).click();
  await expect(tabs.getByRole("tab", { name: "Runs" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(execute).toHaveCount(0);
  await expect(simulate).toHaveCount(0);
  await tabs.getByRole("tab", { name: "Designer" }).click();
  await expect(execute).toBeVisible();
});

test("on a phone, the Runs tab gives the view tabs their own row and offers no Execute workflow", async ({
  page,
}) => {
  const tabs = await openWorkflow(page, {
    tag: "390x844",
    width: 390,
    height: 844,
  });
  const toolbar = page.getByRole("toolbar", { name: "Workflow commands" });
  await toolbar.getByRole("button", { name: "More", exact: true }).click();
  await expect(
    page.getByRole("menuitem", { name: /^Execute workflow/ }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await tabs.getByRole("tab", { name: "Runs" }).click();
  await expect(toolbar).toHaveCount(0);
  await expect(
    page.getByRole("menuitem", { name: /^Execute workflow/ }),
  ).toHaveCount(0);
  // Four 44 px tabs on a row of their own, as wide as the bar.
  const bar = await page.locator(".editor-bar").boundingBox();
  const list = await tabs.boundingBox();
  expect(list!.width).toBeGreaterThan(bar!.width - 40);
  for (const tab of await tabs.getByRole("tab").all())
    expect((await tab.boundingBox())!.height).toBeGreaterThanOrEqual(44);
});
