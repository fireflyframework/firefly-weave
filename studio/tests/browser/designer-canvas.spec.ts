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
// WP-07: every "+" inserts through a step picker (pointer and keyboard), empty
// branches have their own reachable targets, workflow settings are always one
// click away, and large workflows keep every edge inside the drawing.
import { test, expect, Page } from "@playwright/test";
import { parse } from "yaml";
import {
  command,
  connected,
  insertStep,
  newWorkflow,
  offline,
  sourceText,
} from "./support";
import { DesignerPage } from "./designer-po";

type Doc = {
  spec: {
    steps: {
      id: string;
      uses?: string;
      cases?: { steps: { id: string }[] }[];
    }[];
  };
};
const steps = async (page: Page) =>
  (parse(await sourceText(page)) as Doc).spec.steps;
const picker = (page: Page) =>
  page.getByRole("listbox", { name: "Add a step" });
const option = (page: Page, name: string) =>
  page.getByRole("option", { name: new RegExp(`^${name}`) });

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("clicking a + opens a step picker that inserts at that position", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      await insertStep(page, "Transform");
      const designer = new DesignerPage(page);
      await designer.fit();
      const target = designer.target("Add a step here, at the start");
      await expect(target).toHaveAttribute("aria-haspopup", "dialog");
      await target.click();
      await expect(target).toHaveAttribute("aria-expanded", "true");
      await expect(picker(page)).toBeVisible();
      await expect(
        page.getByRole("combobox", { name: "Search steps and actions" }),
      ).toBeFocused();
      await option(page, "Wait for time").click();
      await expect(picker(page)).toHaveCount(0);
      await expect(page.locator('[data-step="wait-1"]')).toBeVisible();
      expect((await steps(page)).map((s) => s.id)).toEqual([
        "wait-1",
        "transform-1",
      ]);
    });

    test("the keyboard alone can open the picker, insert, or close it", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const start = page.getByRole("button", {
        name: "Start — workflow settings",
      });
      await start.focus();
      await page.keyboard.press("Tab");
      // A new workflow's first "+" is the "Add your first step" card.
      const target = page.getByRole("button", {
        name: "Add your first step",
      });
      await expect(target).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(picker(page)).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(picker(page)).toHaveCount(0);
      await expect(target).toBeFocused();
      await page.keyboard.press("Enter");
      await expect(
        page.getByRole("combobox", { name: "Search steps and actions" }),
      ).toBeFocused();
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("Enter");
      await expect(page.locator('[data-step="transform-1"]')).toBeVisible();
      await expect(
        page.locator('[data-step="transform-1"] .node-body'),
      ).toBeFocused();
      expect((await steps(page)).map((s) => s.id)).toEqual(["transform-1"]);
    });

    test("each empty branch has its own target next to its group", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      await insertStep(page, "Decision");
      const designer = new DesignerPage(page);
      await designer.fit();
      const first = designer.target("Add a step here, in Case 1 of decision-1");
      const fallback = designer.target(
        "Add a step here, in Otherwise of decision-1",
      );
      await expect(first).toBeVisible();
      // Cards name the path by its condition, not "Case 1" or "Default".
      await expect(first).toContainText("Condition not set");
      await expect(fallback).toContainText("Otherwise");
      const a = (await first.boundingBox())!;
      const b = (await fallback.boundingBox())!;
      expect(
        a.y + a.height <= b.y ||
          b.y + b.height <= a.y ||
          a.x + a.width <= b.x ||
          b.x + b.width <= a.x,
      ).toBe(true);
      await first.click();
      await option(page, "Transform").click();
      await expect(page.locator('[data-step="transform-1"]')).toBeVisible();
      const [decision] = await steps(page);
      expect(decision.cases?.[0].steps.map((s) => s.id)).toEqual([
        "transform-1",
      ]);
      await expect(
        designer.target("Add a step here, in Case 1 of decision-1"),
      ).toHaveCount(0);
      await expect(
        designer.target("Add a step here, after transform-1"),
      ).toHaveCount(1);
    });

    test("workflow settings are reachable after selecting a step", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      await insertStep(page, "Wait for time");
      const designer = new DesignerPage(page);
      const name = page.getByRole("textbox", { name: "Name", exact: true });
      // A click on empty canvas returns to the workflow settings.
      await designer.selectStep("wait-1");
      const close = page.getByRole("button", { name: "Close inspector" });
      const narrow = viewport.width < 768;
      const hideInspector = async () => {
        await close.click();
        await expect(designer.inspector).toBeHidden();
      };
      if (narrow) await hideInspector();
      const canvas = (await designer.canvas.boundingBox())!;
      await page.mouse.click(canvas.x + canvas.width - 24, canvas.y + 56);
      await expect(page.locator(".graph-node.selected")).toHaveCount(0);
      if (narrow) await command(page, "Show inspector");
      await expect(name).toBeVisible();
      // The inspector header offers the same.
      await designer.selectStep("wait-1");
      await expect(name).toHaveCount(0);
      await designer.inspector
        .getByRole("button", { name: "Workflow settings" })
        .click();
      await expect(name).toBeVisible();
      await expect(page.locator(".graph-node.selected")).toHaveCount(0);
      // The Start node opens them too.
      await designer.selectStep("wait-1");
      if (narrow) await hideInspector();
      await page
        .getByRole("button", { name: "Start — workflow settings" })
        .click();
      await expect(name).toBeVisible();
      await designer.selectStep("wait-1");
      if (narrow) {
        // The inspector covers the canvas as a modal sheet: Escape closes it
        // and returns focus to the step; Escape there returns to the
        // workflow settings.
        await page.keyboard.press("Escape");
        await expect(designer.inspector).toBeHidden();
        const step = page.locator('[data-step="wait-1"] .node-body');
        await expect(step).toBeFocused();
        await page.keyboard.press("Escape");
        await expect(page.locator(".graph-node.selected")).toHaveCount(0);
        return;
      }
      // Escape first returns to the workflow settings; the inspector stays a
      // column beside the canvas.
      await page.locator('[data-step="wait-1"] .node-body').focus();
      await page.keyboard.press("Escape");
      await expect(page.locator(".graph-node.selected")).toHaveCount(0);
      await expect(name).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(name).toBeVisible();
    });

    test("a long workflow keeps every edge inside the drawing and zooms out further", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(
        JSON.stringify({
          apiVersion: "weave/v1alpha1",
          kind: "Workflow",
          metadata: { name: "long", version: "1.0.0" },
          spec: {
            inputSchema: { type: "object" },
            outputSchema: { type: "object" },
            steps: Array.from({ length: 130 }, (_, i) => ({
              id: `step-${i}`,
              kind: "transform",
              value: { literal: {} },
            })),
            output: { literal: {} },
          },
        }),
      );
      await expect(page.locator("[data-step]")).toHaveCount(130);
      const fits = await page.locator("svg.edges").evaluate((svg) => {
        const paths = [...svg.querySelectorAll("path[d]")] as SVGPathElement[];
        const width = Number(svg.getAttribute("width"));
        const height = Number(svg.getAttribute("height"));
        return paths.every((path) => {
          const box = path.getBBox();
          return box.x + box.width <= width && box.y + box.height <= height;
        });
      });
      expect(fits).toBe(true);
      // It opens at a readable zoom and scrolls; Fit all shows the whole
      // flow down to 40%, and zooming steps by 20% down to 25%.
      const zoom = page.locator(".zoom-level");
      const level = async () =>
        Number((await zoom.textContent())?.replace("%", ""));
      await expect.poll(level).toBeGreaterThanOrEqual(75);
      await page.getByRole("button", { name: "Fit all", exact: true }).click();
      await expect.poll(level).toBe(40);
      // That's an overview: titles only, no "+" targets.
      await expect(page.locator(".canvas")).toHaveClass(/\boverview\b/);
      await page.getByRole("button", { name: "Zoom in" }).click();
      await expect.poll(level).toBe(48);
      await page.getByRole("button", { name: "Zoom out" }).click();
      await expect.poll(level).toBe(40);
      for (let i = 0; i < 6; i++)
        await page.getByRole("button", { name: "Zoom out" }).click();
      await expect.poll(level).toBe(25);
    });

    test("published actions can be inserted from the picker", async ({
      page,
    }) => {
      await connected(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.fit();
      await designer.target("Add a step here, at the start").click();
      await page
        .getByRole("combobox", { name: "Search steps and actions" })
        .fill("lookup");
      await option(page, "sql.lookup").click();
      await expect(page.locator('[data-step="call-action-1"]')).toBeVisible();
      const [step] = await steps(page);
      expect(step.uses).toBe("sql.lookup@1.0.0");
    });
  });
