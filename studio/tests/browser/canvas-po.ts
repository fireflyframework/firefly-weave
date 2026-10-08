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
// Page object for the left-to-right canvas. It drives Studio the way a
// person does: real clicks, drags, wheels and keys, never `force`.
import { expect, type Locator, type Page } from "@playwright/test";
import { resolve } from "node:path";
import { parse } from "yaml";
import { newWorkflow, offline, sourceText } from "./support";

export const vendorPayment = resolve(
  "tests/fixtures/vendor-payment-approval.yaml",
);

/** Turns on "Try the new editor" for every page this test opens. */
export async function useNewEditor(page: Page) {
  await page.addInitScript(() =>
    localStorage.setItem("ui:weave.editorNext", "true"),
  );
}

export class CanvasPage {
  constructor(readonly page: Page) {}
  get root(): Locator {
    return this.page.locator(".canvas-v2");
  }
  tile(id: string): Locator {
    return this.root.locator(`.tile-node[data-tile="${id}"]`);
  }
  tileBody(id: string): Locator {
    return this.tile(id).locator(".tile-body");
  }
  handle(key: string): Locator {
    return this.root.locator(`.handle[data-handle="${key}"]`);
  }
  insertTarget(name: string): Locator {
    return this.root.getByRole("button", { name, exact: true });
  }
  branchLabel(owner: string): Locator {
    return this.root.locator(`.branch-label[data-owner="${owner}"]`);
  }
  edgeLine(key: string): Locator {
    return this.root.locator(`path.edge[data-edge-line="${key}"]`);
  }
  /**
   * The canvas is on screen and fitted to the workflow. Narrow screens open
   * on the outline: "Show canvas" is clicked only when that is what appeared.
   */
  async ready() {
    const show = this.page.getByRole("button", {
      name: "Show canvas",
      exact: true,
    });
    await expect(this.root.or(show).first()).toBeVisible();
    if (await show.isVisible()) await show.click();
    await expect(this.root).toHaveAttribute("data-ready", "");
  }
  /** Chooses an entry in today's step picker by its exact label ("Decision", not "Decision table"). */
  async pick(label: string) {
    await this.page
      .getByRole("option")
      .filter({
        has: this.page
          .locator(".step-picker-label")
          .getByText(label, { exact: true }),
      })
      .filter({ visible: true })
      .first()
      .click();
  }
  async addFirstStep(label: string) {
    await this.insertTarget("Add first step").click();
    await this.pick(label);
  }
  /**
   * Hides the step details when they are on screen, and waits until they are
   * gone: with their Close button where it shows (narrow windows), else with
   * "Hide inspector" in the editor's toolbar. A narrow window doesn't open
   * them for a step added with the step picker, so there is nothing to hide.
   */
  async closeInspector() {
    const inspector = this.page.getByRole("complementary", {
      name: "Inspector",
    });
    if (!(await inspector.isVisible())) return;
    const close = inspector.getByRole("button", {
      name: "Close inspector",
      exact: true,
    });
    await (
      (await close.isVisible())
        ? close
        : this.page.getByRole("button", { name: "Hide inspector", exact: true })
    ).click();
    await expect(inspector).toBeHidden();
  }
  /** The main sequence's step IDs, read from Source. */
  async rootSteps(): Promise<string[]> {
    const source = parse(await sourceText(this.page)) as {
      spec: { steps: { id: string }[] };
    };
    await this.ready();
    return source.spec.steps.map((step) => step.id);
  }
  /** A point on the canvas at least 80 px from every tile, handle, +, label and tool. */
  async emptySpot(): Promise<{ x: number; y: number }> {
    for (let attempt = 0; attempt < 4; attempt++) {
      const spot = await this.root.evaluate((root) => {
        const area = root.getBoundingClientRect();
        const blockers = [
          ...root.querySelectorAll<Element>(
            ".tile-body, .handle, .insert-plus, .lane-slot, .join-bar, .tile-label, .branch-label, .canvas-v2-tools, .canvas-v2-tools-menu, .selection-toolbar, .tile-toolbar, .empty-start, f-minimap",
          ),
        ]
          .map((element) => element.getBoundingClientRect())
          .filter((box) => box.width && box.height);
        const distance = (x: number, y: number, box: DOMRect) =>
          Math.hypot(
            Math.max(box.left - x, 0, x - box.right),
            Math.max(box.top - y, 0, y - box.bottom),
          );
        for (let y = area.top + 24; y < area.bottom - 24; y += 12)
          for (let x = area.left + 24; x < area.right - 24; x += 12)
            if (blockers.every((box) => distance(x, y, box) >= 80))
              return { x, y };
        return null;
      });
      if (spot) return spot;
      const box = (await this.root.boundingBox())!;
      await this.page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await this.page.keyboard.down("Control");
      await this.page.mouse.wheel(0, 240);
      await this.page.keyboard.up("Control");
    }
    throw new Error("The canvas has no empty spot to drop on.");
  }
  /** Drags from an output handle (or "+") to a point, by default empty canvas. */
  async dragFromHandle(key: string, to?: { x: number; y: number }) {
    const target = to ?? (await this.emptySpot());
    const box = (await this.handle(key).boundingBox())!;
    await this.page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await this.page.mouse.down();
    await this.page.mouse.move(target.x, target.y, { steps: 12 });
    await expect(this.root.locator(".rubber-band")).toBeVisible();
    await this.page.mouse.up();
  }
  /** Moves the pointer onto an edge, 30% of the way along it. */
  async hoverEdge(key: string) {
    const point = await this.root
      .locator(`path.edge-hit[data-edge="${key}"]`)
      .evaluate((path: SVGPathElement) => {
        const at = path.getPointAtLength(path.getTotalLength() * 0.3);
        const m = path.getScreenCTM()!;
        return {
          x: at.x * m.a + at.y * m.c + m.e,
          y: at.x * m.b + at.y * m.d + m.f,
        };
      });
    await this.page.mouse.move(point.x, point.y);
  }
  async zoomPercent(): Promise<number> {
    const text = await this.root
      .locator(".canvas-v2-tools .zoom-level")
      .innerText();
    return Number(text.replace("%", ""));
  }
}

/** Opens a workflow file (a path, or a file made in the test) with the new editor on. */
export async function openWorkflow(
  page: Page,
  file: Parameters<Locator["setInputFiles"]>[0] = vendorPayment,
): Promise<CanvasPage> {
  await useNewEditor(page);
  await offline(page);
  await page.getByLabel("Choose a workflow file").setInputFiles(file);
  await expect(page.locator(".editor-bar")).toBeVisible();
  const canvas = new CanvasPage(page);
  await canvas.ready();
  return canvas;
}

/** Starts a new, empty workflow with the new editor on. */
export async function openNewWorkflow(page: Page): Promise<CanvasPage> {
  await useNewEditor(page);
  await offline(page);
  await newWorkflow(page);
  const canvas = new CanvasPage(page);
  await canvas.ready();
  return canvas;
}
