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
// The canvas (W3-1, W3-2, W3-5, W3-9, W3-11): a readable zoom when a
// workflow opens, steady zooming, Delete on the focused step with Undo,
// problems shown on the steps themselves, and a keyboard model with a
// ring that stays visible at any zoom.
import { test, expect, type Page } from "@playwright/test";
import { expectHitTarget, insertStep, newWorkflow, offline } from "./support";
import { DesignerPage } from "./designer-po";
import { iconPaths } from "../../src/app/icon";
/** A workflow of twelve steps in the main sequence. */
const twelve = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: { name: long-flow, version: 1.0.0 }
spec:
  inputSchema: { type: object }
  outputSchema: { type: object }
  steps:
${Array.from(
  { length: 12 },
  (_, i) =>
    `    - { id: wait-${i + 1}, kind: wait, durationSeconds: ${60 * (i + 1)} }`,
).join("\n")}
  output: { literal: {} }
`;
const zoomOf = (page: Page) =>
  page
    .locator(".graph-flow")
    .evaluate((e) => Number(getComputedStyle(e).getPropertyValue("--zoom")));

/** Opens the twelve-step workflow the way an import does, with its readable fit. */
async function openLongFlow(page: Page) {
  await offline(page);
  const chooser = page.getByLabel("Choose a workflow file");
  await chooser.setInputFiles({
    name: "long-flow.yaml",
    mimeType: "application/yaml",
    buffer: Buffer.from(twelve),
  });
  await expect(page.locator('[data-step="wait-12"]')).toBeAttached();
  await expect.poll(() => zoomOf(page)).toBeGreaterThan(0);
}

for (const viewport of [
  { width: 1280, height: 720, scale: 1 },
  { width: 640, height: 360, scale: 2 },
])
  test.describe(`${viewport.width * viewport.scale}x${viewport.height * viewport.scale} at ${viewport.scale * 100}%`, () => {
    test.use({
      viewport: { width: viewport.width, height: viewport.height },
      deviceScaleFactor: viewport.scale,
    });
    test("a twelve-step workflow opens at a readable zoom, Start at the top", async ({
      page,
    }) => {
      await openLongFlow(page);
      const show = page.getByRole("button", { name: "Show canvas" });
      if (await show.isVisible()) await show.click();
      await expect.poll(() => zoomOf(page)).toBeGreaterThanOrEqual(0.75);
      const zoom = await zoomOf(page);
      const title = page.locator('[data-step="wait-1"] .node-title');
      const size = await title.evaluate((e) =>
        parseFloat(getComputedStyle(e).fontSize),
      );
      // On screen: the CSS size times the canvas zoom.
      expect(size * zoom).toBeGreaterThanOrEqual(12);
      const canvas = await page.locator(".canvas").boundingBox();
      const start = await page.locator(".start-node").boundingBox();
      expect(start!.y - canvas!.y).toBeGreaterThanOrEqual(16);
      expect(start!.y - canvas!.y).toBeLessThanOrEqual(48);
    });
    test("keyboard navigation after import keeps the focused step visible without Fit all", async ({
      page,
    }) => {
      await openLongFlow(page);
      const show = page.getByRole("button", { name: "Show canvas" });
      if (await show.isVisible()) await show.click();
      await page
        .getByRole("button", { name: "Start — workflow settings" })
        .focus();
      await page.keyboard.press("Tab");
      await expect(
        page.locator('[data-step="wait-1"] .node-body'),
      ).toBeFocused();
      for (let index = 1; index < 12; index++)
        await page.keyboard.press("ArrowDown");
      const last = page.locator('[data-step="wait-12"] .node-body');
      await expect(last).toBeFocused();
      await expect
        .poll(() =>
          last.evaluate((element) => {
            const box = element.getBoundingClientRect();
            const hit = document.elementFromPoint(
              box.left + box.width / 2,
              box.top + box.height / 2,
            );
            return hit === element || element.contains(hit);
          }),
        )
        .toBe(true);
      await page.keyboard.press("Enter");
      await expect(page.getByLabel("Step name", { exact: true })).toHaveValue(
        "wait-12",
      );
    });
  });

test.describe("1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("canceling Move from an unselected node's menu returns focus to that node", async ({
    page,
  }) => {
    await openLongFlow(page);
    const designer = new DesignerPage(page);
    await designer.selectStep("wait-1");
    await page
      .getByRole("button", { name: "Actions for wait-2", exact: true })
      .click();
    await expect(designer.node("wait-1")).toHaveClass(/\bselected\b/);
    await expect(designer.node("wait-2")).not.toHaveClass(/\bselected\b/);
    await page.getByRole("menuitem", { name: "Move to…", exact: true }).click();
    await expect(page.locator(".insertion-target").first()).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(designer.node("wait-2").locator(".node-body")).toBeFocused();
    await expect(page.locator(".canvas.moving")).toHaveCount(0);
  });

  test("the wheel zooms in small steps around the pointer; Fit all and 100% are one click", async ({
    page,
  }) => {
    await openLongFlow(page);
    const node = page.locator('[data-step="wait-3"] .node-body');
    const before = await node.boundingBox();
    const zoom = await zoomOf(page);
    const x = before!.x + before!.width / 2,
      y = before!.y + before!.height / 2;
    await page.mouse.move(x, y);
    await page.keyboard.down("Control");
    await page.mouse.wheel(0, 100);
    await page.keyboard.up("Control");
    await expect.poll(() => zoomOf(page)).not.toBe(zoom);
    const after = await zoomOf(page);
    expect(Math.abs(after - zoom) / zoom).toBeLessThan(0.2);
    const moved = await node.boundingBox();
    expect(Math.abs(moved!.x + moved!.width / 2 - x)).toBeLessThanOrEqual(2);
    expect(Math.abs(moved!.y + moved!.height / 2 - y)).toBeLessThanOrEqual(2);

    await page.getByRole("button", { name: "Fit all", exact: true }).click();
    await expect.poll(() => zoomOf(page)).toBeLessThan(after);
    expect(await zoomOf(page)).toBeGreaterThan(0);
    const canvas = await page.locator(".canvas").boundingBox();
    await expect
      .poll(async () => {
        const end = await page.locator(".boundary-node.end").boundingBox();
        return end!.y + end!.height <= canvas!.y + canvas!.height;
      })
      .toBe(true);
    // The zoom level is the button back to 100%.
    await page
      .getByRole("button", { name: /^Zoom to 100%, now \d+%$/ })
      .click();
    await expect.poll(() => zoomOf(page)).toBe(1);
    await expect(page.locator(".zoom-level")).toHaveText("100%");
  });

  test("below 60% the canvas keeps readable titles and reachable + targets", async ({
    page,
  }) => {
    await openLongFlow(page);
    const out = page.getByRole("button", { name: "Zoom out", exact: true });
    for (let i = 0; i < 4; i++) await out.click();
    await expect.poll(() => zoomOf(page)).toBeLessThan(0.6);
    await expect(page.locator(".canvas")).toHaveClass(/\boverview\b/);
    await expect(page.locator(".canvas-chip")).toContainText([
      "Overview · zoom in for details",
    ]);
    const target = page.locator(".insertion-target").first();
    await expect(target).toBeVisible();
    await target.focus();
    await expectHitTarget(target);
    await expect(
      page.locator('[data-step="wait-1"] .node-summary'),
    ).toBeHidden();
    const zoom = await zoomOf(page);
    const title = await page
      .locator('[data-step="wait-1"] .node-title')
      .evaluate((e) => parseFloat(getComputedStyle(e).fontSize));
    expect(title * zoom).toBeGreaterThanOrEqual(10);
  });

  test("Delete removes the focused step, not the selected one, and offers Undo", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    await insertStep(page, "Wait for time");
    await insertStep(page, "Human task");
    const designer = new DesignerPage(page);
    await expect(designer.node("approval-1")).toHaveClass(/\bselected\b/);
    // Focus wait-1 by keyboard while approval-1 stays selected.
    await page.locator('[data-step="approval-1"] .node-body').focus();
    await page.keyboard.press("ArrowUp");
    await expect(page.locator('[data-step="wait-1"] .node-body')).toBeFocused();
    await designer.node("approval-1").locator(".node-body").focus();
    await page.keyboard.press("Shift+Tab");
    const focused = await page.evaluate(
      () =>
        document.activeElement
          ?.closest("[data-step]")
          ?.getAttribute("data-step") ?? "",
    );
    expect(focused).not.toBe("approval-1");
    await page.locator('[data-step="wait-1"] .node-body').focus();
    await page.keyboard.press("Delete");
    await expect(designer.node("wait-1")).toHaveCount(0);
    await expect(designer.node("approval-1")).toHaveCount(1);
    const toast = page.locator(".toast");
    await expect(toast).toContainText("Deleted wait-1.");
    // Focus lands on the next step.
    await expect(
      page.locator('[data-step="approval-1"] .node-body'),
    ).toBeFocused();
    await toast.getByRole("button", { name: "Undo" }).click();
    await expect(designer.node("wait-1")).toHaveCount(1);
    await expect(page.locator(".toast")).toContainText("Restored wait-1.");
  });

  test("problems show on the steps and in one honest status line", async ({
    page,
  }) => {
    await page.route("**/studio/session", (r) =>
      r.fulfill({
        json: {
          paired: true,
          csrfToken: "t",
          version: "1",
          mode: "offline",
          profile: null,
        },
      }),
    );
    await page.route("**/studio/local/validate", (r) =>
      r.fulfill({
        json: {
          validationOk: false,
          errorCount: 2,
          partial: true,
          diagnostics: [
            {
              code: "WV-COMP-SCHEMA",
              severity: "error",
              path: "/spec/steps/0/durationSeconds",
              message: "durationSeconds must be greater than or equal to 1.",
            },
            {
              code: "WV-COMP-UNAVAILABLE_REFERENCE",
              severity: "error",
              path: "/spec/steps/1/value/ref",
              message: "Referenced step does not dominate this expression.",
            },
            {
              code: "WV-COMP-UNUSED",
              severity: "warning",
              path: "/spec/steps/1",
              message: "The output of this step is never read.",
            },
          ],
        },
      }),
    );
    await page.goto("/");
    await newWorkflow(page);
    await insertStep(page, "Wait for time");
    await insertStep(page, "Transform");
    const strip = page.getByRole("region", { name: "Compiler diagnostics" });
    await expect(strip).toContainText(
      "2 errors, 1 warning. Fix the errors to publish.",
    );
    // The icon follows the result: a failure mark, never a check.
    await expect(
      strip.locator(`svg path[d="${iconPaths["failCircle"]}"]`).first(),
    ).toBeAttached();
    await expect(
      strip.locator(`.diagnostics-title svg path[d="${iconPaths["check"]}"]`),
    ).toHaveCount(0);
    // The steps say it too.
    await expect(page.locator('[data-step="wait-1"] .node-chip')).toHaveText(
      "1 problem",
    );
    await expect(page.locator('[data-step="wait-1"]')).toHaveClass(
      /\bhas-error\b/,
    );
    // The rows list each problem; none reads as the generic fallback. The
    // strip folds to its one line and opens again.
    const toggle = strip.getByRole("button", { name: "Diagnostics" });
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await toggle.click();
    await expect(strip.locator(".diagnostic-text")).toHaveCount(0);
    expect((await strip.boundingBox())!.height).toBeLessThanOrEqual(37);
    await toggle.click();
    const rows = strip.locator(".diagnostic-text");
    await expect(rows).toHaveCount(3);
    await expect(rows).toContainText(["Duration must be at least 1 second."]);
    for (const text of await rows.allTextContents())
      expect(text.trim()).not.toBe(
        "Something in this definition needs attention.",
      );
    await expect(strip).toContainText("Nothing reads this step's output.");
    // Codes wait behind Details; the place is a link to the field.
    await expect(strip.locator(".support-code").first()).toBeHidden();
    await strip.getByRole("button", { name: "Step wait-1 · Duration" }).click();
    await expect(new DesignerPage(page).node("wait-1")).toHaveClass(
      /\bselected\b/,
    );
    await expect(page.getByLabel("Duration", { exact: true })).toBeFocused();
  });

  test("the canvas keyboard: ports out of the tab order, a zoom-sized ring, and a shortcuts sheet", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    // Inspector rendering must not steal focus between consecutive real keys.
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: 8 });
    await insertStep(page, "Wait for time");
    await insertStep(page, "Transform");
    const canvas = page.getByRole("group", { name: "Workflow canvas" });
    await expect(canvas).toHaveAttribute(
      "aria-roledescription",
      "workflow canvas",
    );
    await expect(page.locator("#canvas-help")).toContainText(
      "Press ? for shortcuts.",
    );
    await expect(page.locator(".port")).toHaveCount(0);
    // The ring stays 2 px on screen: at 75% its CSS width is 2.67 px.
    const box = (await canvas.boundingBox())!;
    await page.mouse.move(box.x + 40, box.y + box.height - 60);
    await page.keyboard.down("Control");
    await page.mouse.wheel(0, Math.log(1 / 0.75) / 0.0015);
    await page.keyboard.up("Control");
    await expect.poll(() => zoomOf(page)).toBeCloseTo(0.75, 2);
    const zoom = await zoomOf(page);
    const node = page.locator('[data-step="wait-1"] .node-body');
    await node.focus();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowUp");
    await expect(node).toBeFocused();
    const width = await node.evaluate((e) =>
      parseFloat(getComputedStyle(e).outlineWidth),
    );
    // The browser snaps outlines to whole pixels.
    expect(Math.abs(width - 2 / zoom)).toBeLessThanOrEqual(0.5);
    expect(width).toBeGreaterThanOrEqual(2.6);
    // Enter edits the focused step: focus moves to its name.
    await page.keyboard.press("Enter");
    await expect(page.getByLabel("Step name")).toBeFocused();
    await page.getByLabel("Step name").press("Escape");
    // "?" opens the keyboard shortcuts.
    await node.focus();
    await page.keyboard.press("Shift+?");
    const sheet = page.getByRole("dialog", { name: "Keyboard shortcuts" });
    await expect(sheet).toContainText("Delete the focused step");
    await sheet.getByRole("button", { name: "Done" }).click();
    await expect(sheet).toHaveCount(0);
    await page
      .getByRole("toolbar", { name: "Canvas view" })
      .getByRole("button", { name: "Keyboard shortcuts" })
      .click();
    await expect(sheet).toBeVisible();
  });

  test("keyboard focus pans an off-screen step or + into view, clear of the view tools", async ({
    page,
  }) => {
    await openLongFlow(page);
    const canvas = page.locator(".canvas");
    const inside = async (selector: string) => {
      const frame = (await canvas.boundingBox())!;
      const box = (await page.locator(selector).boundingBox())!;
      return (
        box.y >= frame.y &&
        box.y + box.height <= frame.y + frame.height - 52 &&
        box.x >= frame.x &&
        box.x + box.width <= frame.x + frame.width
      );
    };
    const scrolled = () =>
      page.evaluate(() =>
        [".canvas", ".graph-flow"].some((selector) => {
          const e = document.querySelector(selector)!;
          return e.scrollTop !== 0 || e.scrollLeft !== 0;
        }),
      );
    // The last step starts below the fold; focus brings it up.
    const last = '[data-step="wait-12"] .node-body';
    expect(await inside(last)).toBe(false);
    await page.locator(last).focus();
    await expect.poll(() => inside(last)).toBe(true);
    // Tab reaches the focused step's actions, then the first insertion slot.
    await page.keyboard.press("Tab");
    await expect(
      page
        .locator('[data-step="wait-12"]')
        .getByRole("button", { name: "Actions for wait-12", exact: true }),
    ).toBeFocused();
    await page.keyboard.press("Tab");
    const first = page.locator(".insertion-target").first();
    await expect(first).toBeFocused();
    await expect(first).toHaveAttribute(
      "aria-label",
      "Add a step here, at the start",
    );
    await expect.poll(() => inside(".insertion-target:focus")).toBe(true);
    expect(await scrolled()).toBe(false);
  });

  test("a new workflow offers its first step and a template; the palette groups steps", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    const first = page.getByRole("button", { name: "Add your first step" });
    await expect(first).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Start from a template" }),
    ).toBeVisible();
    await first.click();
    const search = page.getByRole("combobox", {
      name: "Search steps and actions",
    });
    await expect(search).toBeFocused();
    await search.fill("appro");
    const options = page
      .getByRole("listbox", { name: "Add a step" })
      .getByRole("option");
    await expect(options).toHaveCount(1);
    await expect(options.first()).toContainText("Human task");
    // Nothing in the picker is set in capitals by CSS.
    const upper = await page
      .locator("weave-step-picker *")
      .evaluateAll(
        (all) =>
          all.filter((e) => getComputedStyle(e).textTransform === "uppercase")
            .length,
      );
    expect(upper).toBe(0);
    await page.keyboard.press("Enter");
    await expect(new DesignerPage(page).node("approval-1")).toBeVisible();
    // Palette: sentence-case groups, 40 px rows, descriptions as tooltips.
    const palette = page.getByRole("complementary", { name: "Steps" });
    await expect(palette.getByRole("heading", { level: 3 })).toHaveText([
      "Logic",
      "Data",
      "Waiting",
      "People",
      "Actions",
    ]);
    const decision = palette.getByRole("button", {
      name: "Decision",
      exact: true,
    });
    expect((await decision.boundingBox())!.height).toBeLessThanOrEqual(41);
    await expect(decision).toHaveAttribute(
      "title",
      "Follow a different path based on a condition",
    );
    await expect(palette).toContainText(
      "Click to add after the selected step, or drag onto a + on the canvas.",
    );
    await expect(palette).toContainText(
      "Connect to a platform to use published actions.",
    );
    await expect(palette.locator(".drag-grip").first()).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });
});
