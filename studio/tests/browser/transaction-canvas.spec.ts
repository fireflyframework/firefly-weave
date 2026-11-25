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
// A transaction on the canvas: a frame around its own steps, whose header
// names it and says what it promises. The header is the transaction's own
// target, as a loop's is. Runs without a platform.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { openWorkflow, type CanvasPage } from "./canvas-po";
import { yamlFile } from "./step-details-po";

const PROMISE = "All of these succeed, or what's done is undone";
const build = (id: string, by = 8) =>
  `- id: ${id}\n  kind: transform\n  value: {object: {done: {literal: true}}}\n`.replace(
    /^(?=.)/gm,
    " ".repeat(by),
  );
const workflow = (steps: string) =>
  yamlFile(
    "move-between-systems.yaml",
    `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: move-between-systems, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      moves: {type: array, title: Moves, items: {type: object}}
  outputSchema: {}
  steps:
${steps}  output: {literal: null}
`,
  );
/** lookup, move (hold, add, notify), summarize. */
const MOVE = workflow(`${build("lookup", 4)}    - id: move
      kind: transaction
      steps:
${build("hold")}${build("add")}${build("notify")}      output: {literal: null}
${build("summarize", 4)}`);
/** lookup, then an empty transaction. */
const EMPTY = workflow(`${build("lookup", 4)}    - id: transaction-1
      kind: transaction
      steps: []
      output: {literal: null}
`);
/** lookup, an empty transaction with an empty error path, summarize. */
const EMPTY_PATHS = workflow(`${build("lookup", 4)}    - id: move
      kind: transaction
      onError: errorOutput
      errorPath: {steps: [], output: {literal: null}}
      steps: []
      output: {literal: null}
${build("summarize", 4)}`);
/** move continues with error output to report; summarize follows. */
const GUARDED = workflow(`    - id: move
      kind: transaction
      onError: errorOutput
      errorPath:
        steps:
${build("report", 10)}        output: {literal: null}
      steps:
${build("hold")}${build("add")}${build("notify")}      output: {literal: null}
${build("summarize", 4)}`);
/** A loop over moves whose body is check, then the transaction move. */
const IN_LOOP = workflow(`    - id: moves
      kind: forEach
      items: {ref: /input/moves}
      concurrency: 1
      maxItems: 100
      collect: all
      body:
        steps:
${build("check", 10)}          - id: move
            kind: transaction
            steps:
${build("hold", 14)}${build("add", 14)}            output: {literal: null}
        output: {ref: /item}
${build("summarize", 4)}`);

const frame = (canvas: CanvasPage, id: string) =>
  canvas.root.locator(`.loop-frame[data-frame="${id}"]`);
const box = (locator: Locator) =>
  locator.evaluate((element) =>
    element.getBoundingClientRect().toJSON(),
  ) as Promise<DOMRect>;
const contains = (outer: DOMRect, inner: DOMRect) =>
  inner.left >= outer.left &&
  inner.right <= outer.right &&
  inner.top >= outer.top &&
  inner.bottom <= outer.bottom;
const center = (rect: DOMRect) => ({
  x: rect.left + rect.width / 2,
  y: rect.top + rect.height / 2,
});
/** The canvas's zoom, read from a step's card. */
const zoomOf = (canvas: CanvasPage, id: string) =>
  canvas
    .tileBody(id)
    .evaluate(
      (element: HTMLElement) =>
        element.getBoundingClientRect().width / element.offsetWidth,
    );
/** Zooms out to `percent` or the closest the buttons reach below it. */
async function zoomOutTo(page: Page, canvas: CanvasPage, percent: number) {
  const body = await box(canvas.root);
  await page.mouse.move(body.x + body.width / 2, body.y + body.height / 2);
  for (let i = 0; i < 40 && (await canvas.zoomPercent()) > percent; i++) {
    await page.keyboard.down("Control");
    await page.mouse.wheel(0, 20);
    await page.keyboard.up("Control");
  }
}
/** What of the header's text is cut: its name, "Transaction" and its promise. */
const headerParts = (canvas: CanvasPage, id: string) =>
  canvas.tileBody(id).evaluate((element) => {
    const zoom =
      element.getBoundingClientRect().width /
      (element as HTMLElement).offsetWidth;
    const part = (name: string) => {
      const found = element.querySelector<HTMLElement>(`.frame-${name}`)!;
      return {
        cut: found.scrollWidth > found.clientWidth + 1,
        size: parseFloat(getComputedStyle(found).fontSize) * zoom,
      };
    };
    return {
      name: part("name"),
      kind: part("kind"),
      promise: part("promise"),
    };
  });

test.describe("transaction frames at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a transaction draws as a frame whose header names it and what it promises", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, MOVE);
    await canvas.closeInspector();
    const head = canvas.tileBody("move");
    await expect(head.locator(".frame-name")).toHaveText("move");
    await expect(head.locator(".frame-kind")).toHaveText("Transaction");
    await expect(head.locator(".frame-promise")).toHaveText(`· ${PROMISE}`);
    await expect(head.locator("weave-icon").first()).toHaveAttribute(
      "data-icon",
      "transaction",
    );
    await expect(head).toHaveAccessibleName(
      `move, Transaction, ${PROMISE}, step 2 of 3 in Main sequence`,
    );
    const outer = await box(frame(canvas, "move"));
    const band = await box(head);
    expect(Math.abs(band.left - outer.left)).toBeLessThan(1);
    expect(Math.abs(band.right - outer.right)).toBeLessThan(1);
    expect(Math.abs(band.top - outer.top)).toBeLessThan(1);
    for (const [index, id] of ["hold", "add", "notify"].entries()) {
      expect(contains(outer, await box(canvas.tileBody(id))), id).toBe(true);
      await expect(canvas.tileBody(id)).toHaveAccessibleName(
        new RegExp(`, step ${index + 1} of 3 in Transaction move$`),
      );
    }
    // Arrowheads point at steps, never at the frame.
    const arrow = (key: string) =>
      canvas.edgeLine(key).getAttribute("marker-end");
    expect(await arrow("lookup>move")).toBeNull();
    expect(await arrow("move>hold")).toContain("canvas-arrow-");
    expect(await arrow("notify>$exit:move")).toBeNull();
    // The whole sentence shows on hover.
    await head.hover();
    await expect(
      canvas.root.locator('.frame-tip[data-frame-tip="move"]'),
    ).toHaveText(`move: Transaction · ${PROMISE}`);
  });

  test("a click on the header selects the transaction, and Enter opens its step details", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, MOVE);
    const head = canvas.tileBody("move");
    await canvas.tileBody("lookup").click();
    await page.keyboard.press("ArrowRight");
    await expect(head).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect(canvas.tileBody("hold")).toBeFocused();
    await head.click();
    await expect(frame(canvas, "move")).toHaveClass(/\bselected\b/);
    // The frame says its steps come with it: they draw as they are.
    await expect(canvas.tileBody("hold")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    await page.keyboard.press("Enter");
    await expect(
      page.getByRole("dialog", { name: "Step details: move" }),
    ).toBeVisible();
  });

  test("the header's promise shortens first, then its name; Transaction never does", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, EMPTY);
    await canvas.closeInspector();
    // A new transaction's whole header shows at 100%.
    await page.getByRole("button", { name: "Reset zoom to 100%" }).click();
    await expect.poll(() => canvas.zoomPercent()).toBe(100);
    expect(await headerParts(canvas, "transaction-1")).toMatchObject({
      name: { cut: false },
      kind: { cut: false },
      promise: { cut: false },
    });
    await zoomOutTo(page, canvas, 60);
    expect(await headerParts(canvas, "transaction-1")).toMatchObject({
      name: { cut: false },
      kind: { cut: false },
      promise: { cut: true },
    });
    await zoomOutTo(page, canvas, 30);
    const small = await headerParts(canvas, "transaction-1");
    expect(small).toMatchObject({ name: { cut: true }, kind: { cut: false } });
    // Never under 11 px on screen.
    expect(small.name.size).toBeGreaterThanOrEqual(11);
    expect(small.kind.size).toBeGreaterThanOrEqual(11);
  });

  test("an empty transaction asks for a step on its frame, as setup", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, EMPTY);
    await canvas.closeInspector();
    const tile = canvas.tile("transaction-1");
    // Setup, not a failure: Diagnostics counts the error.
    await expect(tile.locator(".tile-badge")).toHaveAttribute(
      "data-badge",
      "setup",
    );
    await expect(
      tile.getByRole("button", {
        name: "Show issues for transaction-1: Setup needed: Add a step to the transaction.",
      }),
    ).toBeVisible();
    await expect(frame(canvas, "transaction-1")).not.toHaveClass(/\berror\b/);
  });

  test("arrow keys go through an empty transaction's slot, and leave its error path's alone", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, EMPTY_PATHS);
    const slot = canvas.insertTarget("Add a step to transaction move");
    const errors = canvas.insertTarget("Add a step to the error path of move");
    await canvas.tileBody("lookup").click();
    await page.keyboard.press("ArrowRight");
    await expect(canvas.tileBody("move")).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect(slot).toBeFocused();
    await page.keyboard.press("ArrowLeft");
    await expect(canvas.tileBody("move")).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect(slot).toBeFocused();
    await page.keyboard.press("ArrowRight");
    await expect(canvas.tileBody("summarize")).toBeFocused();
    await errors.focus();
    await page.keyboard.press("ArrowLeft");
    await expect(errors).toBeFocused();
    // Enter on the body's slot adds inside the transaction.
    await slot.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator(".add-step-panel")).toContainText(
      "Add a step to transaction move",
    );
  });

  test("a transaction inside a loop draws a frame inside the loop's", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, IN_LOOP);
    const outer = await box(frame(canvas, "moves"));
    const inner = await box(frame(canvas, "move"));
    expect(contains(outer, inner)).toBe(true);
    for (const id of ["hold", "add"])
      expect(contains(inner, await box(canvas.tileBody(id))), id).toBe(true);
    // The loop's header and the transaction's don't touch.
    expect((await box(canvas.tileBody("move"))).top).toBeGreaterThan(
      (await box(canvas.tileBody("moves"))).bottom,
    );
    expect((await box(canvas.tileBody("check"))).right).toBeLessThan(
      inner.left,
    );
  });
});

for (const size of [
  { tag: "1440x900", use: { viewport: { width: 1440, height: 900 } } },
  { tag: "360x640", use: { viewport: { width: 360, height: 640 } } },
  {
    tag: "600x500 touch",
    use: {
      viewport: { width: 600, height: 500 },
      hasTouch: true,
      isMobile: true,
    },
  },
])
  test.describe(`a transaction with error output at ${size.tag}`, () => {
    test.use(size.use);

    test("Success and Error sit on the frame's right border, and the error path runs below it", async ({
      page,
    }) => {
      const canvas = await openWorkflow(page, GUARDED);
      await canvas.closeInspector();
      const outer = await box(frame(canvas, "move"));
      const zoom = await zoomOf(canvas, "hold");
      const success = canvas.handle("move:path:success");
      const error = canvas.handle("move:path:errorPath");
      const at = {
        success: center(await box(success)),
        error: center(await box(error)),
      };
      for (const point of [at.success, at.error])
        expect(Math.abs(point.x - outer.right)).toBeLessThanOrEqual(1);
      const row = center(await box(canvas.tileBody("hold"))).y;
      expect(Math.abs(at.success.y - row)).toBeLessThanOrEqual(1);
      expect(Math.abs(at.error.y - (outer.bottom - 24 * zoom))).toBeLessThan(1);
      await expect(canvas.branchLabel("move/success")).toHaveText("Success");
      await expect(canvas.branchLabel("move/errorPath")).toContainText("Error");
      // The labels are clear of the border and of the next step.
      for (const owner of ["move/success", "move/errorPath"]) {
        const label = await box(canvas.branchLabel(owner));
        expect(label.left).toBeGreaterThanOrEqual(outer.right);
        expect(label.right).toBeLessThanOrEqual(
          (await box(canvas.tileBody("report"))).left,
        );
      }
      const report = await box(canvas.tileBody("report"));
      expect(Math.abs(report.top - (outer.bottom + 48 * zoom))).toBeLessThan(1);
      expect(Math.abs(report.left - (outer.right + 152 * zoom))).toBeLessThan(
        1,
      );
      // The "+" after the last step in the frame is clear of Success.
      const plus = await box(
        canvas.insertTarget("Add a step at the end of transaction move"),
      );
      expect(plus.right).toBeLessThanOrEqual((await box(success)).left);
      if (size.use.viewport.width < 768)
        for (const output of [success, error]) {
          const tap = await output.evaluate((element) => {
            const style = getComputedStyle(element, "::before");
            const scale =
              element.getBoundingClientRect().width /
              (element as HTMLElement).offsetWidth;
            return {
              left:
                element.getBoundingClientRect().left +
                (parseFloat(getComputedStyle(element).borderLeftWidth) +
                  parseFloat(style.left)) *
                  scale,
              width: parseFloat(style.width) * scale,
              height: parseFloat(style.height) * scale,
            };
          });
          expect(Math.min(tap.width, tap.height)).toBeGreaterThanOrEqual(43.5);
          // The larger tap leaves the last step's "+" all of its own.
          expect(tap.left).toBeGreaterThanOrEqual(plus.right - 0.5);
        }
    });
  });
