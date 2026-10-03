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
// Page object for the workflow designer. It drives the UI the way a person
// does (real clicks, drags and keys, never `force`) and works at desktop and
// narrow layouts, where the palette and inspector become popovers.
import { expect, Locator, Page } from "@playwright/test";
import { closeSheet } from "./support";

export type StepKind =
  | "action"
  | "transform"
  | "switch"
  | "parallel"
  | "wait"
  | "signal"
  | "humanTask"
  | "fail";

/** Palette labels, as authors see them. */
export const stepLabels: Record<StepKind, string> = {
  action: "Call an action",
  transform: "Transform",
  switch: "Decision",
  parallel: "Parallel",
  wait: "Wait for time",
  signal: "Wait for signal",
  humanTask: "Human task",
  fail: "Fail",
};

const exactly = (text: string) =>
  new RegExp(`^\\s*${text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`);

export class DesignerPage {
  constructor(readonly page: Page) {}

  get canvas(): Locator {
    return this.page.getByLabel("Workflow canvas", { exact: true });
  }
  /** A side panel beside the canvas, or a modal sheet where it covers it. */
  get inspector(): Locator {
    return this.page
      .getByRole("complementary", { name: "Inspector" })
      .or(this.page.getByRole("dialog", { name: "Inspector" }));
  }
  get diagnostics(): Locator {
    return this.page.getByRole("region", { name: "Compiler diagnostics" });
  }
  /** The canvas node of a step. */
  node(stepId: string): Locator {
    return this.page.locator(`[data-step="${stepId}"]`);
  }
  /**
   * An insertion "+" by its accessible name, such as "Add a step here, at
   * the start" or "Add a step here, in Case 1 of decision-1". On an empty
   * workflow the start of the main sequence is the "Add your first step"
   * card instead.
   */
  target(label: string): Locator {
    const target = this.page.getByRole("button", { name: label, exact: true });
    return label === "Add a step here, at the start"
      ? target.or(
          this.page.getByRole("button", {
            name: "Add your first step",
            exact: true,
          }),
        )
      : target;
  }

  /** Opens the Designer view of the current workflow. */
  async open() {
    const tab = this.page.getByRole("tab", { name: "Designer", exact: true });
    if (await tab.isVisible()) await tab.click();
    await expect(this.canvas).toBeVisible();
  }

  private paletteItem(kind: StepKind): Locator {
    return this.page
      .locator(".palette-step")
      .filter({ hasText: exactly(stepLabels[kind]) });
  }
  /** Shows the palette item; returns whether the narrow-layout popover was opened. */
  private async showPalette(kind: StepKind) {
    const item = this.paletteItem(kind);
    const opened = !(await item.isVisible());
    if (opened)
      await this.page.getByRole("button", { name: "Insert step" }).click();
    await expect(item).toBeVisible();
    return { item, opened };
  }

  /**
   * Inserts a step at an insertion target. When the "+" opens a step picker
   * (`aria-haspopup`), it picks the kind there; otherwise it drags the
   * palette item onto the target. Returns the inserted node.
   */
  async insertAt(kind: StepKind, targetLabel: string): Promise<Locator> {
    const before = new Set(await this.stepIds());
    await this.fit();
    const target = this.target(targetLabel);
    // A "+" that opens a step picker announces it with aria-haspopup.
    if (await target.getAttribute("aria-haspopup")) {
      await target.click();
      await this.page
        .getByRole("option", { name: exactly(stepLabels[kind]) })
        .filter({ visible: true })
        .first()
        .click();
    } else {
      const { item, opened } = await this.showPalette(kind);
      await item.dragTo(target);
      // A drop keeps the narrow-layout palette (a modal sheet) open; close it.
      if (opened && (await item.isVisible())) await closeSheet(this.page);
    }
    let added: string[] = [];
    await expect
      .poll(async () => {
        added = (await this.stepIds()).filter((id) => !before.has(id));
        return added.length;
      })
      .toBe(1);
    return this.node(added[0]);
  }

  /** Ids of every step node on the canvas. */
  stepIds(): Promise<string[]> {
    return this.page
      .locator("[data-step]")
      .evaluateAll((nodes) =>
        nodes.map((node) => node.getAttribute("data-step") ?? ""),
      );
  }

  /** Inserts after the selected step (or at the end) with a palette click. */
  async append(kind: StepKind) {
    const { item } = await this.showPalette(kind);
    await item.click();
  }

  /**
   * "Fit all", then back out of the overview (below 60% the canvas hides its
   * "+" targets): every node on screen, and every target clickable.
   */
  async fit() {
    const fit = this.page.getByRole("button", { name: "Fit all", exact: true });
    if (!(await fit.isVisible())) return;
    await fit.click();
    // A wheel step is smaller on high-density screens: check after each.
    for (let i = 0; i < 8; i++) {
      await this.settled();
      if (!(await this.page.locator(".canvas.overview").count())) break;
      // Ctrl + wheel near the top of the canvas zooms in by 20% around that
      // point: Start stays in view and the rest of the graph grows downward.
      const point = await this.canvas.evaluate((canvas) => {
        const box = canvas.getBoundingClientRect();
        const x = box.left + box.width / 2;
        for (let y = Math.max(box.top, 0) + 24; y < box.bottom; y += 24)
          if (canvas.contains(document.elementFromPoint(x, y))) return { x, y };
        return null;
      });
      if (!point) {
        await this.page
          .getByRole("button", { name: "Zoom in", exact: true })
          .click();
        continue;
      }
      await this.page.mouse.move(point.x, point.y);
      await this.page.keyboard.down("Control");
      await this.page.mouse.wheel(0, -Math.log(1.2) / 0.0015);
      await this.page.keyboard.up("Control");
    }
    await this.settled();
  }

  /** Waits two frames: the canvas applies a pan or zoom on the next one. */
  private settled() {
    return this.page.evaluate(
      () =>
        new Promise((done) =>
          requestAnimationFrame(() => requestAnimationFrame(done)),
        ),
    );
  }

  /** True when a real pointer at the element's center would reach it. */
  private async reachable(locator: Locator): Promise<boolean> {
    const box = await locator.boundingBox();
    const viewport = this.page.viewportSize();
    if (!box || !viewport) return false;
    const x = box.x + box.width / 2;
    const y = box.y + box.height / 2;
    if (x < 0 || y < 0 || x > viewport.width || y > viewport.height)
      return false;
    return locator.evaluate(
      (element, [px, py]) => {
        const hit = document.elementFromPoint(px, py);
        return !!hit && (hit === element || element.contains(hit));
      },
      [x, y] as const,
    );
  }

  /**
   * Selects a step by clicking its node. On narrow layouts it first closes an
   * inspector that covers the node, and fits the graph when the node is off
   * screen.
   */
  async selectStep(stepId: string) {
    const body = this.node(stepId).locator(".node-body");
    if (!(await this.reachable(body))) {
      const close = this.page.getByRole("button", { name: "Close inspector" });
      if (await close.isVisible()) await close.click();
      if (!(await this.reachable(body))) {
        // Focus pans a step into view, as it does for keyboard users.
        await body.focus();
        await this.settled();
      }
      if (!(await this.reachable(body))) {
        await this.fit();
        await this.canvas.focus();
        await body.focus();
        await this.settled();
      }
    }
    await body.focus();
    await this.settled();
    await expect.poll(() => this.reachable(body)).toBe(true);
    await body.click();
    await expect(this.node(stepId)).toHaveClass(/\bselected\b/);
    await expect(this.inspector).toBeVisible();
  }

  /**
   * Returns to workflow settings. Uses the "Workflow settings" control when
   * the designer offers one, otherwise Escape; then waits for the workflow
   * fields.
   */
  async deselect() {
    const settings = this.page.getByRole("button", {
      name: "Workflow settings",
      exact: true,
    });
    const visible = settings.filter({ visible: true });
    if (await visible.count()) await visible.first().click();
    else {
      await this.canvas.focus();
      await this.page.keyboard.press("Escape");
    }
    await expect(
      this.page.locator(".graph-node.selected"),
      "Deselecting needs a Workflow settings control or Escape-to-deselect",
    ).toHaveCount(0);
  }

  /** Reads the workflow source from the Source tab and returns to the Designer. */
  async source(): Promise<string> {
    await this.page.getByRole("tab", { name: "Source", exact: true }).click();
    const text = await this.page
      .getByRole("textbox", { name: "Workflow source" })
      .inputValue();
    await this.open();
    return text;
  }

  /** Replaces the workflow source by typing it in the Source tab and applying it. */
  async setSource(text: string) {
    await this.page.getByRole("tab", { name: "Source", exact: true }).click();
    const box = this.page.getByRole("textbox", { name: "Workflow source" });
    await box.fill(text);
    await this.page
      .getByRole("button", { name: "Apply changes", exact: true })
      .click();
    await this.open();
  }

  /** A labelled field in the inspector (property table or workflow settings). */
  inspectorField(label: string): Locator {
    return this.inspector.getByLabel(label, { exact: true });
  }
}
