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
import { openNewWorkflow, openWorkflow, type CanvasPage } from "./canvas-po";
import { newWorkflow, offline } from "./support";

/** How each step's label block draws on screen at the canvas's zoom. */
async function labelBlocks(canvas: CanvasPage) {
  return canvas.root.evaluate((root) => {
    const flow = root.querySelector("f-flow")!;
    const zoom = Number(getComputedStyle(flow).getPropertyValue("--zoom"));
    const line = (element: HTMLElement) => {
      const box = element.getBoundingClientRect();
      return {
        // The font size times the scale the line is drawn at.
        px:
          parseFloat(getComputedStyle(element).fontSize) *
          (box.height / element.offsetHeight),
        width: box.width,
      };
    };
    const blocks = [...root.querySelectorAll<HTMLElement>(".tile-label")].map(
      (block) => {
        const tile = block.closest<HTMLElement>("[data-tile]")!;
        const card = tile.querySelector(".tile-body")!.getBoundingClientRect();
        const box = block.getBoundingClientRect();
        return {
          id: tile.dataset["tile"]!,
          name: line(block.querySelector("strong")!),
          subtitle: line(block.querySelector("span")!),
          width: box.width,
          offset: box.x + box.width / 2 - (card.x + card.width / 2),
        };
      },
    );
    return { zoom, blocks };
  });
}

/**
 * Names draw at 12 px or more on screen and subtitles at 11 px or more, and
 * each label block keeps to its 168-unit column centered under its tile.
 */
async function expectReadableLabels(canvas: CanvasPage) {
  const { zoom, blocks } = await labelBlocks(canvas);
  expect(blocks.length).toBeGreaterThan(8);
  for (const block of blocks) {
    expect(block.name.px, `${block.id}: name`).toBeGreaterThanOrEqual(11.99);
    expect(block.subtitle.px, `${block.id}: subtitle`).toBeGreaterThan(11);
    expect(block.width, `${block.id}: block`).toBeLessThanOrEqual(
      168 * zoom + 1,
    );
    expect(block.name.width, `${block.id}: name`).toBeLessThanOrEqual(
      168 * zoom + 1,
    );
    expect(Math.abs(block.offset), `${block.id}: centered`).toBeLessThan(1);
  }
}

/**
 * Every label under a tile or an empty-path slot that overlaps a step, a
 * slot, a handle, a "+", a path label, a join or another such label; and
 * every slot label an edge line runs through.
 */
async function labelCollisions(canvas: CanvasPage): Promise<string[]> {
  return canvas.root.evaluate((root) => {
    const describe = (element: Element) =>
      `${element.className} ${
        element.closest("[data-tile]")?.getAttribute("data-tile") ??
        element.getAttribute("data-handle") ??
        element.getAttribute("data-owner") ??
        element
          .closest("[data-insert-owner]")
          ?.getAttribute("data-insert-owner") ??
        ""
      }`;
    const overlap = (a: DOMRect, b: DOMRect) =>
      a.left < b.right - 0.5 &&
      b.left < a.right - 0.5 &&
      a.top < b.bottom - 0.5 &&
      b.top < a.bottom - 0.5;
    const labels = [...root.querySelectorAll(".tile-label, .lane-slot-label")];
    const others = [
      ...root.querySelectorAll(
        ".tile-body, .lane-slot, .handle, .insert-plus, .branch-label, .join-bar, .join-label, .lane-end",
      ),
    ];
    const found: string[] = [];
    labels.forEach((label, i) => {
      const box = label.getBoundingClientRect();
      for (const other of [...labels.slice(i + 1), ...others])
        if (overlap(box, other.getBoundingClientRect()))
          found.push(`${describe(label)} overlaps ${describe(other)}`);
    });
    for (const label of root.querySelectorAll(".lane-slot-label")) {
      const box = label.getBoundingClientRect();
      for (const path of root.querySelectorAll<SVGPathElement>("path.edge")) {
        const m = path.getScreenCTM()!;
        const length = path.getTotalLength();
        for (let at = 0; at <= length; at += 2) {
          const p = path.getPointAtLength(at);
          const x = p.x * m.a + p.y * m.c + m.e;
          const y = p.x * m.b + p.y * m.d + m.f;
          if (x > box.left && x < box.right && y > box.top && y < box.bottom) {
            found.push(
              `${describe(label)} touches ${path.getAttribute("data-edge-line")}`,
            );
            break;
          }
        }
      }
    }
    return found;
  });
}

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

  test("keeps step names and subtitles readable at every zoom the canvas chooses", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    expect(await canvas.zoomPercent()).toBe(50);
    await expectReadableLabels(canvas);
    // A name too long for its column ends in an ellipsis; its tooltip and
    // the step's accessible name keep it whole.
    const long = canvas.tile("post-ledger-entry").locator(".tile-label strong");
    expect(
      await long.evaluate((name) => name.scrollWidth > name.clientWidth),
    ).toBe(true);
    await expect(long).toHaveAttribute("title", "post-ledger-entry");
    await expect(canvas.tileBody("post-ledger-entry")).toHaveAccessibleName(
      /^post-ledger-entry, /,
    );
    // An empty path's "Add a step" shows whole, at 11 px or more.
    const slot = canvas.root.locator(".lane-slot-label");
    const drawn = await slot.evaluate((label: HTMLElement) => ({
      px:
        parseFloat(getComputedStyle(label).fontSize) *
        (label.getBoundingClientRect().height / label.offsetHeight),
      cut: label.scrollWidth > label.clientWidth,
    }));
    expect(drawn.px).toBeGreaterThan(11);
    expect(drawn.cut).toBe(false);
    // Fit view chooses no less than 50% either: this workflow doesn't fit
    // above it, so Fit view brings back the view it opened with.
    const zoomOut = canvas.root.getByRole("button", {
      name: "Zoom out",
      exact: true,
    });
    await zoomOut.click();
    await expect.poll(() => canvas.zoomPercent()).toBe(42);
    await canvas.root
      .getByRole("button", { name: "Fit view", exact: true })
      .click();
    await expect.poll(() => canvas.zoomPercent()).toBe(50);
    await expectReadableLabels(canvas);
    // At 100% the text draws at its own size: 13 px names, 12 px subtitles.
    await canvas.root
      .getByRole("button", { name: /^Reset zoom to 100%/ })
      .click();
    await expect.poll(() => canvas.zoomPercent()).toBe(100);
    for (const block of (await labelBlocks(canvas)).blocks) {
      expect(block.name.px).toBeCloseTo(13, 1);
      expect(block.subtitle.px).toBeCloseTo(12, 1);
    }
    // Zoomed out on purpose, names still show at 40% and drop below it.
    for (let i = 0; i < 5; i++) await zoomOut.click();
    await expect.poll(() => canvas.zoomPercent()).toBe(40);
    await expect(canvas.tile("approval").locator(".tile-label")).toBeVisible();
    await expectReadableLabels(canvas);
    await zoomOut.click();
    await expect.poll(() => canvas.zoomPercent()).toBe(33);
    await expect(canvas.tile("approval").locator(".tile-label")).toBeHidden();
  });

  test("keeps each label clear of other steps, labels, handles and + at 50% and 40%", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    expect(await canvas.zoomPercent()).toBe(50);
    expect(await labelCollisions(canvas)).toEqual([]);
    await canvas.root
      .getByRole("button", { name: /^Reset zoom to 100%/ })
      .click();
    const zoomOut = canvas.root.getByRole("button", {
      name: "Zoom out",
      exact: true,
    });
    for (let i = 0; i < 5; i++) await zoomOut.click();
    await expect.poll(() => canvas.zoomPercent()).toBe(40);
    expect(await labelCollisions(canvas)).toEqual([]);
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
