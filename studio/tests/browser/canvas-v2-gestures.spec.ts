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
// Gestures on the left-to-right canvas: dragging from a handle, moving a
// step by drag or "Move to…", the hover toolbar and palette drops.
import { expect, test } from "@playwright/test";
import { openNewWorkflow, openWorkflow } from "./canvas-po";
import { sourceText } from "./support";

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`at ${size.width}x${size.height}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize(size);
    });

    test("dragging from a handle to empty canvas adds a Decision whose labeled outputs each offer a +", async ({
      page,
    }) => {
      const canvas = await openNewWorkflow(page);
      await canvas.addFirstStep("Transform");
      await canvas.closeInspector();
      await canvas.dragFromHandle("transform-1:out");
      await expect(
        page.getByRole("combobox", { name: "Search steps and actions" }),
      ).toBeFocused();
      await canvas.pick("Decision");
      await expect(canvas.tile("decision-1")).toBeVisible();
      await expect(canvas.branchLabel("decision-1/case 1")).toHaveText(
        "Case 1",
      );
      await expect(canvas.branchLabel("decision-1/default")).toHaveText(
        "Otherwise",
      );
      await canvas.closeInspector();
      await canvas
        .insertTarget("Add a step to path Otherwise of decision-1")
        .click();
      await canvas.pick("Wait for time");
      expect(await sourceText(page)).toMatch(
        /default:\s+steps:\s+- id: wait-1/,
      );
    });

    test("an edge's + inserts between two steps, and the menu's Move to… moves a step", async ({
      page,
    }) => {
      const canvas = await openNewWorkflow(page);
      await canvas.addFirstStep("Transform");
      await canvas.closeInspector();
      await canvas.insertTarget("Add a step after transform-1").click();
      await canvas.pick("Wait for time");
      await canvas.closeInspector();
      const plus = canvas.insertTarget(
        "Insert a step between transform-1 and wait-1",
      );
      await expect(plus).toHaveCSS("opacity", "0");
      await canvas.hoverEdge("transform-1>wait-1");
      await expect(plus).toHaveCSS("opacity", "1");
      await plus.click();
      await canvas.pick("Transform");
      expect(await canvas.rootSteps()).toEqual([
        "transform-1",
        "transform-2",
        "wait-1",
      ]);
      await canvas.closeInspector();
      await canvas.tileBody("wait-1").click({ button: "right" });
      await page.getByRole("menuitem", { name: "Move to…" }).click();
      await canvas
        .insertTarget("Move wait-1 between transform-1 and transform-2")
        .click();
      expect(await canvas.rootSteps()).toEqual([
        "transform-1",
        "wait-1",
        "transform-2",
      ]);
    });
  });

test.describe("canvas gestures", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  test("a drop near a step is refused and says why", async ({ page }) => {
    const canvas = await openWorkflow(page);
    const near = (await canvas.tileBody("approval").boundingBox())!;
    await canvas.dragFromHandle("prepare-request:out", {
      x: near.x + near.width / 2,
      y: near.y + near.height + 20,
    });
    await expect(page.locator(".toast")).toContainText(
      "Steps run in order. Use + to insert.",
    );
    await expect(
      page.getByRole("combobox", { name: "Search steps and actions" }),
    ).toHaveCount(0);
  });

  test("dragging a step onto a highlighted + moves it there", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    // The workflow is wider than the canvas: hide the step details and
    // scroll (a real wheel) until the step and the end of the main sequence
    // are both in view.
    await canvas.closeInspector();
    const area = (await canvas.root.boundingBox())!;
    const step = (await canvas.tileBody("approval").boundingBox())!;
    const end = (await canvas
      .insertTarget("Add a step after record-result")
      .boundingBox())!;
    const middle = { x: area.x + area.width / 2, y: area.y + area.height / 2 };
    await page.mouse.move(middle.x, middle.y);
    await page.mouse.wheel((step.x + end.x + end.width) / 2 - middle.x, 0);
    await expect
      .poll(async () => {
        const box = (await canvas
          .insertTarget("Add a step after record-result")
          .boundingBox())!;
        return box.x + box.width < area.x + area.width;
      })
      .toBe(true);
    const from = (await canvas.tileBody("approval").boundingBox())!;
    expect(from.x).toBeGreaterThan(area.x);
    await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
    await page.mouse.down();
    await page.mouse.move(
      from.x + from.width / 2 + 12,
      from.y + from.height / 2 + 12,
      { steps: 4 },
    );
    await expect(canvas.root).toHaveClass(/\brevealing\b/);
    const target = canvas.insertTarget("Move approval after record-result");
    const box = (await target.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, {
      steps: 10,
    });
    await page.mouse.up();
    expect(await canvas.rootSteps()).toEqual([
      "prepare-request",
      "route",
      "record-result",
      "approval",
    ]);
  });

  test("a palette step dropped on a + is inserted there", async ({ page }) => {
    const canvas = await openNewWorkflow(page);
    await canvas.addFirstStep("Transform");
    await canvas.closeInspector();
    await page
      .locator(".palette-step")
      .filter({ hasText: /^\s*Wait for time/ })
      .dragTo(canvas.insertTarget("Insert a step before transform-1"));
    expect(await canvas.rootSteps()).toEqual(["wait-1", "transform-1"]);
  });

  test("hovering a step shows Delete and More, and Delete offers Undo", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("prepare-request").hover();
    const bar = canvas.root.getByRole("group", {
      name: "Commands for prepare-request",
    });
    await expect(bar).toBeVisible();
    // At the 50% the workflow opens with, its buttons still draw 24 px or more.
    for (const button of await bar.getByRole("button").all()) {
      const box = (await button.boundingBox())!;
      expect(Math.min(box.width, box.height)).toBeGreaterThanOrEqual(24);
    }
    await bar
      .getByRole("button", { name: "More commands for prepare-request" })
      .click();
    await expect(
      page.getByRole("menuitem", { name: "Duplicate" }),
    ).toBeVisible();
    await expect(
      page.getByRole("menuitem", { name: "Move to…" }),
    ).toBeVisible();
    await page.keyboard.press("Escape");
    await canvas.tileBody("pay-vendor").hover();
    await canvas.root
      .getByRole("button", { name: "Delete pay-vendor", exact: true })
      .click();
    await expect(page.locator(".toast")).toContainText("Deleted pay-vendor.");
    await expect(canvas.tile("pay-vendor")).toHaveCount(0);
    await page
      .locator(".toast")
      .getByRole("button", { name: "Undo", exact: true })
      .click();
    await expect(page.locator(".toast")).toContainText("Restored pay-vendor.");
    await expect(canvas.tile("pay-vendor")).toHaveCount(1);
  });

  test("below 30% Move to… still shows where a step can go and focuses one of them", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    const zoomOut = canvas.root.getByRole("button", {
      name: "Zoom out",
      exact: true,
    });
    const minimal = () =>
      canvas.root.evaluate((root) => root.classList.contains("lod-minimal"));
    for (let i = 0; i < 12 && !(await minimal()); i++) await zoomOut.click();
    await expect(canvas.root).toHaveClass(/\blod-minimal\b/);
    await expect(canvas.root.locator(".insert-plus").first()).toBeHidden();
    await canvas.tileBody("record-result").click({ button: "right" });
    await page.getByRole("menuitem", { name: "Move to…" }).click();
    await expect(canvas.root).toHaveClass(/\brevealing\b/);
    const focused = canvas.root.locator(".insert-plus:focus");
    await expect(focused).toBeVisible();
    await expect(focused).toHaveAccessibleName(/^Move record-result /);
    const area = (await canvas.root.boundingBox())!;
    const box = (await focused.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(area.x);
    expect(box.y).toBeGreaterThanOrEqual(area.y);
    expect(box.x + box.width).toBeLessThanOrEqual(area.x + area.width);
    expect(box.y + box.height).toBeLessThanOrEqual(area.y + area.height);
    await page.keyboard.press("Enter");
    await expect(page.locator(".toast")).toContainText("Moved record-result.");
    await expect(canvas.root).not.toHaveClass(/\brevealing\b/);
  });
});
