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
import { expect, test, type Locator } from "@playwright/test";
import { DesignerPage } from "./designer-po";
import { openNewWorkflow, openWorkflow } from "./canvas-po";
import { newWorkflow, offline } from "./support";

test.describe("the left-to-right canvas", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  test("draws the workflow left to right, from the trigger to End", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    // Too wide to fit at 50%: it opens at 50% from the trigger, names shown.
    expect(await canvas.zoomPercent()).toBe(50);
    await expect(canvas.root).not.toHaveClass(/\blod-/);
    await expect(
      canvas.tile("$trigger:manual").locator(".tile-label strong"),
    ).toBeVisible();
    const area = (await canvas.root.boundingBox())!;
    const trigger = (await canvas.tileBody("$trigger:manual").boundingBox())!;
    expect(trigger.x).toBeGreaterThan(area.x + 48);
    expect(trigger.x).toBeLessThan(area.x + 96);
    await expect(canvas.tileBody("$trigger:manual")).toHaveAccessibleName(
      "Trigger, Manual form · 4 fields",
    );
    await expect(canvas.tileBody("approval")).toHaveAccessibleName(
      "approval, Human task, finance-approvers · approve/reject, step 2 of 4 in Main sequence",
    );
    await expect(canvas.tileBody("$end")).toHaveAccessibleName(
      "End, workflow result",
    );
    const lefts: number[] = [];
    for (const id of [
      "$trigger:manual",
      "prepare-request",
      "approval",
      "route",
      "record-result",
      "$end",
    ])
      lefts.push((await canvas.tileBody(id).boundingBox())!.x);
    expect([...lefts].sort((a, b) => a - b)).toEqual(lefts);
    const top = async (id: string) =>
      (await canvas.tileBody(id).boundingBox())!.y;
    expect(await top("pay-vendor")).toBeCloseTo(await top("route"), 0);
    expect(await top("rejected")).toBeGreaterThan(await top("pay-vendor"));
    await expect(canvas.branchLabel("route/case 1")).toHaveText("Approve");
    await expect(canvas.branchLabel("route/case 2")).toHaveText("Reject");
    await expect(canvas.branchLabel("route/default")).toHaveText("Otherwise");
    await expect(canvas.root.locator(".join-label")).toHaveText(
      "All branches done",
    );
    await expect(
      canvas.root.getByRole("img", { name: "This path ends here" }),
    ).toHaveCount(1);
    await expect(
      canvas.insertTarget("Add a step to path Otherwise of route"),
    ).toBeVisible();
    await expect(
      canvas.insertTarget("Add a step after record-result"),
    ).toBeVisible();
    await expect(page.locator(".graph-node")).toHaveCount(0);
    // The parallel step's icon never covers its input handle.
    const forkIcon = canvas.tile("pay-and-notify").locator(".tile-icon");
    const forkHandle = canvas.tile("pay-and-notify").locator(".handle-in");
    const apart = async () => {
      const [icon, handle] = [
        (await forkIcon.boundingBox())!,
        (await forkHandle.boundingBox())!,
      ];
      return (
        icon.x + icon.width <= handle.x ||
        handle.x + handle.width <= icon.x ||
        icon.y + icon.height <= handle.y ||
        handle.y + handle.height <= icon.y
      );
    };
    expect(await apart()).toBe(true);
    // At 100% a cut-off name, subtitle or path label shows in full on hover.
    await canvas.root
      .getByRole("button", { name: /^Reset zoom to 100%/ })
      .click();
    await expect(canvas.root).not.toHaveClass(/\blod-/);
    expect(await apart()).toBe(true);
    /** Scrolls the canvas (a real wheel) until the element sits in its middle. */
    const centerOn = async (target: Locator) => {
      const box = (await target.boundingBox())!;
      const area = (await canvas.root.boundingBox())!;
      const middle = {
        x: area.x + area.width / 2,
        y: area.y + area.height / 2,
      };
      await page.mouse.move(middle.x, middle.y);
      await page.mouse.wheel(
        box.x + box.width / 2 - middle.x,
        box.y + box.height / 2 - middle.y,
      );
    };
    const name = canvas.tile("approval").locator(".tile-label strong");
    await centerOn(name);
    await name.hover();
    await expect(name).toHaveAttribute("title", "approval");
    const subtitle = canvas.tile("approval").locator(".tile-label span");
    await subtitle.hover();
    await expect(subtitle).toHaveAttribute(
      "title",
      "finance-approvers · approve/reject",
    );
    const path = canvas.branchLabel("route/case 1");
    await centerOn(path);
    await path.hover();
    await expect(path).toHaveAttribute("title", "Approve");
  });

  test("starts an empty workflow with Add first step and adds steps through today's step picker", async ({
    page,
  }) => {
    const canvas = await openNewWorkflow(page);
    const first = canvas.insertTarget("Add first step");
    await expect(first).toBeVisible();
    await expect(
      canvas.root.getByText("Start with what triggers this workflow"),
    ).toBeVisible();
    await expect(
      canvas.root.getByRole("button", {
        name: "Start from a template",
        exact: true,
      }),
    ).toBeVisible();
    await first.click();
    await expect(
      page.getByRole("combobox", { name: "Search steps and actions" }),
    ).toBeFocused();
    await canvas.pick("Transform");
    await expect(canvas.tile("transform-1")).toBeVisible();
    await expect(canvas.tileBody("$trigger:manual")).toBeVisible();
    await expect(canvas.tileBody("$end")).toBeVisible();
    await canvas.insertTarget("Add a step after transform-1").click();
    await canvas.pick("Wait for time");
    expect(await canvas.rootSteps()).toEqual(["transform-1", "wait-1"]);
  });

  test("opens the workflow inputs from the trigger and the result from End", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    const focusedIn = (field: string) =>
      page.evaluate(
        (key) => !!document.activeElement?.closest(`[data-field="${key}"]`),
        field,
      );
    await canvas.tileBody("$trigger:manual").click();
    await expect.poll(() => focusedIn("spec/inputSchema")).toBe(true);
    await canvas.tileBody("$end").click();
    await expect.poll(() => focusedIn("spec/output")).toBe(true);
  });

  test("selects a clicked step, shows it in the inspector and makes it the canvas's Tab stop", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("prepare-request").click();
    await expect(canvas.tileBody("prepare-request")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(
      page
        .getByRole("complementary", { name: "Inspector" })
        .getByRole("heading", { name: "Transform" }),
    ).toBeVisible();
    await expect(canvas.tileBody("prepare-request")).toHaveAttribute(
      "tabindex",
      "0",
    );
    await expect(canvas.tileBody("approval")).toHaveAttribute("tabindex", "-1");
  });

  test("drops labels below 40% and draws plain tiles below 30%, keeping each step's name", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    const name = await canvas.tileBody("approval").getAttribute("aria-label");
    const zoomOut = canvas.root.getByRole("button", {
      name: "Zoom out",
      exact: true,
    });
    const has = (level: string) =>
      canvas.root.evaluate((root, l) => root.classList.contains(l), level);
    for (let i = 0; i < 12 && !(await has("lod-compact")); i++)
      await zoomOut.click();
    await expect(canvas.root).toHaveClass(/\blod-compact\b/);
    await expect(canvas.tile("approval").locator(".tile-label")).toBeHidden();
    await expect(canvas.tile("approval").locator(".tile-icon")).toBeVisible();
    for (let i = 0; i < 12 && !(await has("lod-minimal")); i++)
      await zoomOut.click();
    await expect(canvas.root).toHaveClass(/\blod-minimal\b/);
    await expect(canvas.root.locator(".insert-plus").first()).toBeHidden();
    await expect(canvas.tileBody("approval")).toHaveAttribute(
      "aria-label",
      name!,
    );
  });

  test("keeps Outline and Source working", async ({ page }) => {
    const canvas = await openWorkflow(page);
    await page.getByRole("tab", { name: "Outline", exact: true }).click();
    await page.getByRole("treeitem", { name: /pay-vendor/ }).click();
    await page.getByRole("tab", { name: "Designer", exact: true }).click();
    await canvas.ready();
    await expect(canvas.tileBody("pay-vendor")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await new DesignerPage(page).setSource(`apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: edited, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {type: object}
  steps:
    - {id: first-step, kind: transform, value: {literal: {}}}
    - {id: wrap-up, kind: wait, durationSeconds: 60}
  output: {literal: {}}
`);
    await canvas.ready();
    await expect(canvas.tileBody("wrap-up")).toHaveAccessibleName(
      "wrap-up, Wait for time, 1 min, step 2 of 2 in Main sequence",
    );
  });

  test("keeps the classic designer when the preference is off", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    await expect(page.locator(".canvas-v2")).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Add your first step" }),
    ).toBeVisible();
  });
});

test("collapses the canvas tools into a menu below 768 px, without sideways scroll", async ({
  page,
}) => {
  await page.setViewportSize({ width: 600, height: 500 });
  const canvas = await openWorkflow(page);
  await expect(
    canvas.root.getByRole("toolbar", { name: "Canvas view" }),
  ).toBeHidden();
  await canvas.root.getByRole("button", { name: "Canvas view" }).click();
  await page.getByRole("menuitem", { name: "Fit view" }).click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    ),
  ).toBeLessThanOrEqual(0);
});
