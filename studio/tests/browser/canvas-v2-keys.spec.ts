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
// The left-to-right canvas from the keyboard: arrow keys, the step
// commands, zoom keys, Escape, one Tab stop with each step's "+" inside
// it, and the "?" sheet.
import { expect, test, type Page } from "@playwright/test";
import { openNewWorkflow, openWorkflow } from "./canvas-po";

/** Presses Tab up to `limit` times; the names reached inside the steps' layer, until focus leaves it. */
async function tabStops(page: Page, key = "Tab", limit = 8) {
  const inside: string[] = [];
  for (let i = 0; i < limit; i++) {
    await page.keyboard.press(key);
    const where = await page.evaluate(() => {
      const active = document.activeElement;
      // The steps' layer only: the canvas tools come after it.
      return active?.closest(".canvas-v2 .graph-flow")
        ? (active.getAttribute("aria-label") ?? active.textContent ?? "").trim()
        : "";
    });
    if (!where) break;
    inside.push(where);
  }
  return inside;
}

test.describe("the canvas keyboard map", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  test("arrow keys walk the flow, and Enter and F2 open the focused step", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("$trigger:manual").focus();
    for (const [key, id] of [
      ["ArrowRight", "prepare-request"],
      ["ArrowRight", "approval"],
      ["ArrowRight", "route"],
      ["ArrowRight", "pay-vendor"],
      ["ArrowDown", "rejected"],
      ["ArrowUp", "pay-vendor"],
      ["ArrowLeft", "route"],
    ] as const) {
      await page.keyboard.press(key);
      await expect(canvas.tileBody(id)).toBeFocused();
      await expect(canvas.tileBody(id)).toHaveAttribute("aria-pressed", "true");
    }
    await page.keyboard.press("Enter");
    const details = page.getByRole("dialog", {
      name: "Step details: route",
      exact: true,
    });
    await expect(details).toBeVisible();
    await expect
      .poll(() =>
        details.evaluate((dialog) => dialog.contains(document.activeElement)),
      )
      .toBe(true);
    await canvas.closeInspector();
    await canvas.tileBody("approval").focus();
    await page.keyboard.press("F2");
    await expect(
      page
        .getByRole("dialog", { name: "Step details: approval", exact: true })
        .getByRole("textbox", { name: "Step name" }),
    ).toBeFocused();
  });

  test("arrow keys reveal distant steps with their labels and pointer targets intact", async ({
    page,
  }) => {
    const ids = Array.from(
      { length: 40 },
      (_, i) => `review-vendor-ledgers-${i}`,
    );
    const canvas = await openWorkflow(page, {
      name: "long-workflow.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({
          apiVersion: "weave/v1alpha1",
          kind: "Workflow",
          metadata: { name: "long-workflow", version: "1.0.0" },
          spec: {
            inputSchema: { type: "object" },
            steps: ids.map((id) => ({
              id,
              kind: "transform",
              value: { literal: {} },
            })),
            output: { literal: {} },
          },
        }),
      ),
    });
    const last = ids.at(-1)!;
    const viewport = await canvas.root.boundingBox();
    const original = await canvas.tile(last).boundingBox();
    expect(original!.x).toBeGreaterThan(viewport!.x + viewport!.width);
    await canvas.tileBody("$trigger:manual").focus();
    for (const id of ids) {
      await page.keyboard.press("ArrowRight");
      await expect(canvas.tileBody(id)).toBeFocused();
    }
    await expect
      .poll(async () => {
        const tile = await canvas.tile(last).boundingBox();
        return tile!.x + tile!.width;
      })
      .toBeLessThan(viewport!.x + viewport!.width);
    const name = canvas.tile(last).locator(".tile-label strong");
    const box = await name.boundingBox();
    expect(box!.x).toBeGreaterThan(viewport!.x);
    expect(box!.y + box!.height).toBeLessThan(viewport!.y + viewport!.height);
    await expect(name).toHaveText(last);
    expect(
      await name.evaluate((element) => {
        const box = element.getBoundingClientRect();
        return (
          document.elementFromPoint(
            box.x + box.width / 2,
            box.y + box.height / 2,
          ) === element
        );
      }),
    ).toBe(true);
    await name.hover();
    await expect(name).toHaveAttribute("title", last);
    await page.keyboard.press("Enter");
    const details = page.getByRole("dialog", {
      name: `Step details: ${last}`,
      exact: true,
    });
    await expect(details).toBeVisible();
    await expect
      .poll(() =>
        details.evaluate((dialog) => dialog.contains(document.activeElement)),
      )
      .toBe(true);
  });

  test("N and / open the step picker after the focused step; Ctrl or Command+D duplicates and Delete deletes with Undo", async ({
    page,
  }) => {
    const canvas = await openNewWorkflow(page);
    await canvas.addFirstStep("Transform");
    await canvas.closeInspector();
    await canvas.tileBody("transform-1").focus();
    await page.keyboard.press("n");
    const search = page.getByRole("combobox", {
      name: "Search steps and actions",
    });
    await expect(search).toBeFocused();
    await expect(
      page.getByText("Add a step after transform-1").first(),
    ).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(search).toHaveCount(0);
    await canvas.tileBody("transform-1").focus();
    await page.keyboard.press("/");
    await expect(search).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(search).toHaveCount(0);
    await canvas.tileBody("transform-1").focus();
    await page.keyboard.press("ControlOrMeta+d");
    expect(await canvas.rootSteps()).toEqual(["transform-1", "transform-2"]);
    await canvas.tileBody("transform-2").focus();
    await page.keyboard.press("Delete");
    await expect(page.locator(".toast")).toContainText("Deleted transform-2.");
    expect(await canvas.rootSteps()).toEqual(["transform-1"]);
  });

  test("Shift+arrows extend the selection, Ctrl or Command+A selects every step, and Escape hides the toolbar before it clears the selection", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("prepare-request").focus();
    await page.keyboard.press("Shift+ArrowRight");
    const bar = canvas.root.getByRole("toolbar", { name: "Selected steps" });
    await expect(bar).toContainText("2 steps");
    await expect(canvas.tileBody("approval")).toBeFocused();
    await page.keyboard.press("ControlOrMeta+a");
    await expect(bar).toContainText("4 steps");
    await expect(canvas.tileBody("rejected")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    // One meaning per press: first the focused step's toolbar goes...
    const commands = canvas.root.getByRole("group", {
      name: "Commands for approval",
    });
    await expect(commands).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(commands).toHaveCount(0);
    await expect(bar).toContainText("4 steps");
    // ...then the selection.
    await page.keyboard.press("Escape");
    await expect(bar).toHaveCount(0);
    await expect(canvas.tileBody("approval")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    await expect(canvas.tileBody("approval")).toBeFocused();
  });

  test("Escape ends a drag or a move before anything else", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("approval").click();
    await canvas.closeInspector();
    const search = page.getByRole("combobox", {
      name: "Search steps and actions",
    });
    const stillSelected = () =>
      expect(canvas.tileBody("approval")).toHaveAttribute(
        "aria-pressed",
        "true",
      );
    const center = (box: {
      x: number;
      y: number;
      width: number;
      height: number;
    }) => ({
      x: box.x + box.width / 2,
      y: box.y + box.height / 2,
    });

    // An edge drawn from a handle: Escape drops it, and letting go adds nothing.
    const spot = await canvas.emptySpot();
    const handle = center((await canvas.handle("approval>out").boundingBox())!);
    await page.mouse.move(handle.x, handle.y);
    await page.mouse.down();
    await page.mouse.move(spot.x, spot.y, { steps: 12 });
    const band = canvas.root.locator(".rubber-band");
    await expect(band).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(band).toHaveCount(0);
    await page.mouse.up();
    await expect(search).toHaveCount(0);
    await stillSelected();

    // A step dragged over a "+": Escape cancels the move, and letting go
    // there neither moves nor opens the step.
    const from = center(
      (await canvas.tileBody("prepare-request").boundingBox())!,
    );
    await page.mouse.move(from.x, from.y);
    await page.mouse.down();
    await page.mouse.move(from.x + 40, from.y + 12, { steps: 6 });
    await expect(canvas.root).toHaveClass(/\brevealing\b/);
    const plus = canvas
      .insertTarget("Move prepare-request after record-result")
      .or(canvas.insertTarget("Add a step after record-result"));
    const to = center((await plus.boundingBox())!);
    await page.mouse.move(to.x, to.y, { steps: 12 });
    await page.keyboard.press("Escape");
    await expect(canvas.root).not.toHaveClass(/\brevealing\b/);
    await page.mouse.up();
    await stillSelected();
    await expect(canvas.tileBody("prepare-request")).toHaveAttribute(
      "aria-pressed",
      "false",
    );

    // "Move to…" waiting for a "+": Escape stops it and focus returns to the step.
    await canvas.tileBody("record-result").click({ button: "right" });
    await page.getByRole("menuitem", { name: "Move to…" }).click();
    await expect(canvas.root).toHaveClass(/\brevealing\b/);
    await expect(canvas.root.locator(".insert-plus:focus")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator(".toast")).toContainText(
      "Stopped moving record-result.",
    );
    await expect(canvas.root).not.toHaveClass(/\brevealing\b/);
    await expect(canvas.tileBody("record-result")).toBeFocused();
    await stillSelected();
    expect(await canvas.rootSteps()).toEqual([
      "prepare-request",
      "approval",
      "route",
      "record-result",
    ]);
  });

  test("zoom keys change the zoom, 0 resets it and 1 fits the workflow", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("approval").focus();
    const fitted = await canvas.zoomPercent();
    await page.keyboard.press("=");
    await expect.poll(() => canvas.zoomPercent()).toBeGreaterThan(fitted);
    await page.keyboard.press("-");
    await page.keyboard.press("-");
    await expect.poll(() => canvas.zoomPercent()).toBeLessThan(fitted);
    await page.keyboard.press("0");
    await expect.poll(() => canvas.zoomPercent()).toBe(100);
    // Fit view never goes below 50%, like opening the workflow.
    await page.keyboard.press("1");
    await expect.poll(() => canvas.zoomPercent()).toBe(fitted);
    expect(fitted).toBeGreaterThanOrEqual(50);
  });

  test("keys on a + or a canvas tool act on that control, not on the step", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("record-result").click();
    await canvas.closeInspector();
    const plus = canvas.insertTarget("Add a step after record-result");
    await plus.focus();
    await page.keyboard.press("Delete");
    await page.keyboard.press("ControlOrMeta+d");
    await page.keyboard.press("ArrowLeft");
    await expect(plus).toBeFocused();
    const zoomIn = canvas.root.getByRole("button", {
      name: "Zoom in",
      exact: true,
    });
    const before = await canvas.zoomPercent();
    await zoomIn.focus();
    await page.keyboard.press("Delete");
    await page.keyboard.press("Enter");
    await expect.poll(() => canvas.zoomPercent()).toBeGreaterThan(before);
    await expect(zoomIn).toBeFocused();
    expect(await canvas.rootSteps()).toEqual([
      "prepare-request",
      "approval",
      "route",
      "record-result",
    ]);
    await plus.focus();
    await page.keyboard.press("Enter");
    await expect(
      page.getByRole("combobox", { name: "Search steps and actions" }),
    ).toBeFocused();
  });

  test("keeps keyboard focus rings at least two screen pixels while zoom changes", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    const tile = canvas.tileBody("approval");
    await tile.focus();
    for (const key of ["-", "-", "0"]) {
      const before = await canvas.zoomPercent();
      await page.keyboard.press(key);
      await expect.poll(() => canvas.zoomPercent()).not.toBe(before);
      for (const control of [
        tile,
        canvas.insertTarget("Insert a step between approval and route"),
        canvas.root.getByRole("button", {
          name: "More commands for approval",
          exact: true,
        }),
      ]) {
        await tile.focus();
        await control.focus();
        const ring = await control.evaluate((element: HTMLElement) => {
          const style = getComputedStyle(element);
          return {
            visible: element.matches(":focus-visible"),
            style: style.outlineStyle,
            width:
              parseFloat(style.outlineWidth) *
              (element.getBoundingClientRect().width / element.offsetWidth),
          };
        });
        expect(ring.visible).toBe(true);
        expect(ring.style).toBe("solid");
        expect(ring.width).toBeGreaterThanOrEqual(2);
      }
      await tile.focus();
    }
  });

  test("the canvas is one Tab stop, with the focused step's + buttons inside it", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("record-result").click();
    await canvas.closeInspector();
    await canvas.tileBody("record-result").focus();
    const inside = await tabStops(page);
    expect(inside).toContain("Add a step after record-result");
    expect(inside.length).toBeLessThanOrEqual(6);
    expect(inside.some((name) => name.startsWith("approval,"))).toBe(false);
  });

  test("Tab moves forward from a step to the + on the edge after it", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("approval").click();
    await canvas.closeInspector();
    await canvas.tileBody("approval").focus();
    const forward = await tabStops(page);
    expect(forward).toContain("Insert a step between approval and route");
    expect(forward.length).toBeLessThanOrEqual(6);
    await canvas.tileBody("approval").focus();
    // Shift+Tab leaves the steps: nothing of this step comes before it.
    expect(await tabStops(page, "Shift+Tab", 1)).toEqual([]);
  });

  test("? shows the canvas's shortcuts, only those that work", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("approval").focus();
    await page.keyboard.press("?");
    const sheet = page.getByRole("dialog", { name: "Keyboard shortcuts" });
    await expect(sheet).toBeVisible();
    // It opens at the top, though it is taller than the window.
    await expect(
      sheet.getByRole("heading", { name: "Everywhere", exact: true }),
    ).toBeFocused();
    await expect(sheet).toContainText("Fit view");
    await expect(sheet).toContainText("Extend the selection downstream");
    await expect(sheet).not.toContainText("Pin output or unpin");
    await sheet.getByRole("button", { name: "Done", exact: true }).click();
    await expect(sheet).toHaveCount(0);
    await expect(canvas.tileBody("approval")).toBeFocused();
    await canvas.root
      .getByRole("button", { name: "Keyboard shortcuts", exact: true })
      .click();
    await expect(sheet).toBeVisible();
    await sheet.getByRole("button", { name: "Done", exact: true }).click();
    await expect(sheet).toHaveCount(0);
    // Outside the canvas, the sheet still describes the canvas in use.
    await page.evaluate(() => (document.activeElement as HTMLElement).blur());
    await page.keyboard.press("?");
    await expect(sheet).toBeVisible();
    await expect(sheet).toContainText("Extend the selection downstream");
    await expect(sheet).not.toContainText("Reserved for a command bar");
    await expect(sheet).not.toContainText(
      "Not assigned: Weave has no deactivated steps",
    );
  });
});
