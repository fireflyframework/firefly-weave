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
// names it and says what it promises, and is the transaction's own target, as
// a loop's is; how each step in it is undone; the point of no return; and
// where a step may not be moved. Runs without a platform.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { parse } from "yaml";
import { CanvasPage, openWorkflow } from "./canvas-po";
import { RUN_IDS, openRunsTab, runFixture } from "./runs-po";
import { yamlFile } from "./step-details-po";
import { showView, sourceText } from "./support";

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
/** Zooms in from Fit view to `percent`, or the closest above it. */
async function zoomTo(page: Page, canvas: CanvasPage, percent: number) {
  const body = await box(canvas.root);
  await page.mouse.move(body.x + body.width / 2, body.y + body.height / 2);
  for (let i = 0; i < 60 && (await canvas.zoomPercent()) < percent; i++) {
    await page.keyboard.down("Control");
    await page.mouse.wheel(0, -20);
    await page.keyboard.up("Control");
  }
}
/** Zooms out to `percent` or the closest the wheel reaches below it. */
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
    await expect(head.locator(".frame-promise")).toHaveText(PROMISE);
    await expect(head.locator("weave-icon").first()).toHaveAttribute(
      "data-icon",
      "transaction",
    );
    await expect(head).toHaveAccessibleName(
      `move, Transaction, ${PROMISE}, step 2 of 3 in Main sequence`,
    );
    // Its name contains the words it shows, as they are said.
    const words = (text: string) => text.replace(/[^\w']+/g, " ").trim();
    const shown = await head
      .locator(".frame-title")
      .evaluate((element) => element.textContent ?? "");
    expect(words(shown)).toBe(`move Transaction ${words(PROMISE)}`);
    expect(words((await head.getAttribute("aria-label"))!)).toContain(
      words(shown),
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

  test("what its failure does comes after the words the header shows", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, GUARDED);
    await expect(canvas.tileBody("move")).toHaveAccessibleName(
      `move, Transaction, ${PROMISE}, continues with error output, step 1 of 2 in Main sequence`,
    );
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

  test("a short name keeps all of its width while the promise still shows", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, MOVE);
    await canvas.closeInspector();
    await zoomOutTo(page, canvas, 35);
    const widths = await canvas
      .tileBody("move")
      .locator(".frame-name")
      .evaluate((element: HTMLElement) => {
        const shown = element.getBoundingClientRect().width;
        // A hair under its natural width draws as an ellipsis.
        element.style.flex = "none";
        const natural = element.getBoundingClientRect().width;
        element.style.flex = "";
        return { shown, natural };
      });
    expect(widths.shown).toBe(widths.natural);
    expect((await headerParts(canvas, "move")).promise.cut).toBe(true);
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

// ------------------------------------- how each step is undone, and where it ends

const REASON =
  "Steps that can be undone must come before the step that can't be undone.";
const NESTED = "A transaction can't contain another transaction.";
const DESCRIPTION =
  "Once submit succeeds, the transaction finishes. Steps after this line retry until they succeed and are never undone.";
const operation = (uses: string) =>
  `{kind: action, uses: ${uses}, with: {literal: {}}}`;
/** A step that calls an action, as YAML, with how it is undone. */
const call = (id: string, undo: string[] = [], by = 8) =>
  [
    `- id: ${id}`,
    "  kind: action",
    `  uses: systems.${id}@1.0.0`,
    "  with: {literal: {}}",
    ...undo.map((line) => `  ${line}`),
  ]
    .map((line) => `${" ".repeat(by)}${line}\n`)
    .join("");
const HOLD = (by?: number) =>
  call(
    "hold",
    [
      `confirm: ${operation("systems.confirm@1.0.0")}`,
      `cancel: ${operation("systems.release@1.0.0")}`,
    ],
    by,
  );
const ADD = (by?: number) =>
  call("add", [`compensate: ${operation("systems.remove@1.0.0")}`], by);
const LAST = (id: string, by?: number) => call(id, ["irreversible: true"], by);
/** move: lookup (says nothing), hold, add, submit (can't be undone), notify; then other (a transaction) and tally. */
const UNDO = workflow(`    - id: move
      kind: transaction
      steps:
${call("lookup")}${HOLD()}${ADD()}${LAST("submit")}${build("notify")}      output: {literal: null}
    - id: other
      kind: transaction
      steps:
${build("check")}      output: {literal: null}
${build("tally", 4)}`);
/** A decision inside move, each of whose paths has its own step that can't be undone. */
const PATHS = workflow(`    - id: move
      kind: transaction
      steps:
${ADD()}        - id: route
          kind: switch
          cases:
            - when: {literal: true}
              steps:
${LAST("submit", 16)}${build("notify", 16)}              output: {literal: null}
          default:
            steps:
${LAST("submit-batch", 14)}            output: {literal: null}
      output: {literal: null}
`);
/** A loop right after the step that can't be undone. */
const THEN_A_LOOP = workflow(`    - id: move
      kind: transaction
      steps:
${ADD()}${LAST("submit")}        - id: each
          kind: forEach
          items: {ref: /input/moves}
          body:
            steps:
${build("note", 14)}            output: {ref: /item}
      output: {literal: null}
`);

const line = (canvas: CanvasPage, step: string) =>
  canvas.root.locator(`.point-line[data-point="${step}"]`);
const pointLabel = (canvas: CanvasPage, step: string) =>
  canvas.root.locator(`.point-label[data-point="${step}"]`);
const undoBadge = (canvas: CanvasPage, id: string) =>
  canvas.tile(id).locator(".undo-badge");
const undoWord = (canvas: CanvasPage, id: string) =>
  canvas.tile(id).locator(".tile-undo");
/** "Move to…" from the step's menu. */
async function startMove(canvas: CanvasPage, id: string) {
  await canvas.tileBody(id).click({ button: "right" });
  await canvas.page.getByRole("menuitem", { name: "Move to…" }).click();
  await expect(canvas.root).toHaveClass(/\brevealing\b/);
}
/** Takes a step by its card, as a person does, and stays over a "+". */
async function dragOver(
  page: Page,
  canvas: CanvasPage,
  id: string,
  target: Locator,
) {
  const from = center(await box(canvas.tileBody(id)));
  await page.mouse.move(from.x, from.y);
  await page.mouse.down();
  await page.mouse.move(from.x + 12, from.y + 12, { steps: 4 });
  await expect(canvas.root).toHaveClass(/\brevealing\b/);
  const to = center(await box(target));
  await page.mouse.move(to.x, to.y, { steps: 10 });
}
const stepsOfMove = async (page: Page) => {
  const { spec } = parse(await sourceText(page)) as {
    spec: { steps: { id: string; steps?: { id: string }[] }[] };
  };
  return spec.steps.find(({ id }) => id === "move")!.steps!.map(({ id }) => id);
};

test.describe("how each step is undone at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("each step that says how it is undone shows it by icon and by word, and in its name", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, UNDO);
    await canvas.closeInspector();
    for (const [id, icon, word, name, token] of [
      [
        "hold",
        "lock",
        "Reserved",
        "is reserved, then confirmed",
        "--neutral-ink",
      ],
      [
        "add",
        "undo",
        "Reversible",
        "can be undone by reversing it",
        "--neutral-ink",
      ],
      [
        "submit",
        "critical",
        "Can't be undone",
        "can't be undone",
        "--warning-ink",
      ],
    ] as const) {
      const badge = undoBadge(canvas, id);
      await expect(badge.locator("weave-icon")).toHaveAttribute(
        "data-icon",
        icon,
      );
      await expect(badge).toHaveAttribute("title", word);
      await expect(undoWord(canvas, id)).toHaveText(word);
      await expect(canvas.tileBody(id)).toHaveAccessibleName(
        new RegExp(`in Transaction move, ${name}$`),
      );
      expect(
        await badge.evaluate((element) => getComputedStyle(element).color),
      ).toBe(
        await page.evaluate((name) => {
          const probe = document.createElement("span");
          probe.style.color = `var(${name})`;
          document.body.append(probe);
          const { color } = getComputedStyle(probe);
          probe.remove();
          return color;
        }, token),
      );
      // On the card's lower left corner: 20 units, 10 out of it, its bottom 10 above the card's.
      const [card, at] = [await box(canvas.tileBody(id)), await box(badge)];
      const zoom = await zoomOf(canvas, id);
      expect(at.width / zoom).toBeCloseTo(20, 0);
      expect((card.left - at.left) / zoom).toBeCloseTo(10, 0);
      expect((card.bottom - at.bottom) / zoom).toBeCloseTo(10, 0);
    }
    for (const id of ["lookup", "notify", "tally"]) {
      await expect(undoBadge(canvas, id)).toHaveCount(0);
      await expect(undoWord(canvas, id)).toHaveCount(0);
    }
    await expect(canvas.tileBody("notify")).toHaveAccessibleName(
      /step 5 of 5 in Transaction move$/,
    );
    await expect(page.locator("weave-icon[data-icon-missing]")).toHaveCount(0);
  });

  test("a step outside a transaction shows no undo, though its document says one", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, workflow(ADD(4)));
    await expect(canvas.tileBody("add")).toBeVisible();
    await expect(undoBadge(canvas, "add")).toHaveCount(0);
    await expect(undoWord(canvas, "add")).toHaveCount(0);
  });

  test("the point of no return is a named line after the step that can't be undone", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, UNDO);
    await canvas.closeInspector();
    await zoomTo(page, canvas, 80);
    const divider = canvas.root.getByRole("img", {
      name: "Point of no return, after submit",
      exact: true,
    });
    await expect(divider).toHaveCount(1);
    await expect(divider).toHaveAccessibleDescription(DESCRIPTION);
    await expect(divider).toHaveAttribute("title", DESCRIPTION);
    // Not a stop: its words are in the step's name and its own.
    await expect(divider).not.toHaveAttribute("tabindex", /.*/);
    const label = pointLabel(canvas, "submit");
    await expect(label).toHaveText("Point of no return");
    await expect(label).toHaveAttribute("title", DESCRIPTION);
    await expect(label.locator("weave-icon")).toHaveAttribute(
      "data-icon",
      "critical",
    );
    const zoom = await zoomOf(canvas, "submit");
    const [submit, notify, at, text, header] = [
      await box(canvas.tileBody("submit")),
      await box(canvas.tileBody("notify")),
      await box(divider),
      await box(label),
      await box(canvas.tileBody("move")),
    ];
    const x = center(at).x;
    expect(Math.abs(x - (submit.right + 76 * zoom))).toBeLessThan(1);
    expect(x).toBeLessThan(notify.left);
    expect(Math.abs(at.top - (submit.top - 8 * zoom))).toBeLessThan(1);
    expect(Math.abs(at.bottom - (submit.top + 184 * zoom))).toBeLessThan(1);
    expect(at.width).toBeCloseTo(12, 0);
    // The label: centered on the line, 12 above the row, clear of the header.
    expect(Math.abs(center(text).x - x)).toBeLessThan(1);
    expect(Math.abs(text.bottom - (submit.top - 12 * zoom))).toBeLessThan(1);
    expect(text.top).toBeGreaterThan(header.bottom);
    // It names the frame's whole sequence: a line of the transaction's own.
    expect(contains(await box(frame(canvas, "move")), at)).toBe(true);
  });

  test("a decision draws one line in each path that has one, with room for its label", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, PATHS);
    await canvas.closeInspector();
    for (const id of ["submit", "submit-batch"])
      await expect(
        canvas.root.getByRole("img", {
          name: `Point of no return, after ${id}`,
          exact: true,
        }),
      ).toHaveCount(1);
    const zoom = await zoomOf(canvas, "submit");
    const [submit, batch] = [
      await box(canvas.tileBody("submit")),
      await box(canvas.tileBody("submit-batch")),
    ];
    // The last step of its path: 20 after it.
    expect(
      Math.abs(
        center(await box(line(canvas, "submit-batch"))).x -
          (batch.right + 20 * zoom),
      ),
    ).toBeLessThan(1);
    expect(Math.abs(batch.top - submit.top - 264 * zoom)).toBeLessThan(1);
  });

  test("the label draws at 11 px or more from 55% up; below, the line alone keeps its name", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, UNDO);
    await canvas.closeInspector();
    const size = () =>
      pointLabel(canvas, "submit").evaluate(
        (element: HTMLElement) =>
          parseFloat(getComputedStyle(element).fontSize) *
          (element.getBoundingClientRect().height / element.offsetHeight),
      );
    await page.getByRole("button", { name: "Reset zoom to 100%" }).click();
    await expect.poll(() => canvas.zoomPercent()).toBe(100);
    expect(await size()).toBeCloseTo(12, 0);
    await zoomOutTo(page, canvas, 60);
    expect(await size()).toBeGreaterThanOrEqual(10.9);
    await zoomOutTo(page, canvas, 50);
    await expect(pointLabel(canvas, "submit")).toHaveCount(0);
    const divider = canvas.root.getByRole("img", {
      name: "Point of no return, after submit",
    });
    await expect(divider).toHaveAccessibleDescription(DESCRIPTION);
    expect((await box(divider)).width).toBeCloseTo(12, 0);
    await expect(undoWord(canvas, "submit")).toBeVisible();
    await expect(undoBadge(canvas, "submit")).toBeVisible();
    await zoomOutTo(page, canvas, 25);
    await expect(undoBadge(canvas, "submit")).toBeHidden();
    await expect(line(canvas, "submit")).toBeVisible();
  });

  test("the label ends before a frame right after the line, at any zoom", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, THEN_A_LOOP);
    await canvas.closeInspector();
    await page.getByRole("button", { name: "Reset zoom to 100%" }).click();
    await expect.poll(() => canvas.zoomPercent()).toBe(100);
    for (const percent of [100, 80, 70, 58]) {
      await zoomOutTo(page, canvas, percent);
      const label = await box(pointLabel(canvas, "submit"));
      const divider = center(await box(line(canvas, "submit")));
      const loop = await box(frame(canvas, "each"));
      expect(label.right, `${percent}%`).toBeLessThan(loop.left);
      // Still over its line.
      expect(label.left, `${percent}%`).toBeLessThan(divider.x);
      expect(label.right, `${percent}%`).toBeGreaterThan(divider.x);
      if (percent === 100)
        expect(Math.abs(center(label).x - divider.x)).toBeLessThan(2);
    }
    // Its header keeps every click.
    const header = await box(canvas.tileBody("each"));
    await page.mouse.click(header.left + 6, center(header).y);
    await expect(frame(canvas, "each")).toHaveClass(/\bselected\b/);
  });

  test("the pointer reaches the line and its label, whose tooltip says what it means", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, UNDO);
    await canvas.closeInspector();
    await zoomTo(page, canvas, 80);
    const under = (x: number, y: number) =>
      page.evaluate(
        ([left, top]) =>
          document
            .elementFromPoint(left, top)
            ?.closest("[title]")
            ?.getAttribute("title") ?? null,
        [x, y],
      );
    const at = await box(line(canvas, "submit"));
    // Anywhere in its 12 px, above the edge it crosses and below it.
    for (const x of [at.left + 1, center(at).x, at.right - 1])
      for (const y of [at.top + 20, at.bottom - 20])
        expect(await under(x, y), `${x} ${y}`).toBe(DESCRIPTION);
    const label = center(await box(pointLabel(canvas, "submit")));
    expect(await under(label.x, label.y)).toBe(DESCRIPTION);
    // So does each step's undo badge.
    const badge = center(await box(undoBadge(canvas, "hold")));
    expect(await under(badge.x, badge.y)).toBe("Reserved");
  });

  test("the edge under the line keeps its +, which inserts there", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, UNDO);
    await canvas.closeInspector();
    const at = center(await box(line(canvas, "submit")));
    const row = center(await box(canvas.tileBody("submit"))).y;
    await page.mouse.move(at.x, row);
    const plus = canvas.insertTarget("Insert a step between submit and notify");
    await expect(plus).toHaveCSS("opacity", "1");
    await plus.click();
    await expect(page.locator(".add-step-panel")).toBeVisible();
  });

  test("the canvas opens under a moving pointer without an error", async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    // Its first event may come before its data: more than one opening, as
    // none is sure to.
    for (let opening = 0; opening < 5; opening++) {
      let moving = true;
      const pointer = (async () => {
        for (let at = 0; moving; at++)
          await page.mouse.move(500 + (at % 60) * 4, 450 + (at % 40) * 3);
      })();
      const canvas = await openWorkflow(page, UNDO);
      await expect(canvas.tileBody("add")).toBeVisible();
      moving = false;
      await pointer;
    }
    expect(errors).toEqual([]);
  });

  test("a step that can be undone can't be dragged past the point of no return", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, UNDO);
    await canvas.closeInspector();
    const refused = canvas.insertTarget(
      "Move add at the end of transaction move",
    );
    await dragOver(page, canvas, "add", refused);
    await expect(refused).toHaveAttribute("aria-disabled", "true");
    await expect(refused).toHaveAccessibleDescription(REASON);
    await expect(refused.locator("weave-icon")).toHaveAttribute(
      "data-icon",
      "revoke",
    );
    const tip = canvas.root.locator(".move-reason");
    await expect(tip).toHaveText(REASON);
    const [plus, at] = [await box(refused), await box(tip)];
    expect(at.top).toBeGreaterThanOrEqual(plus.bottom);
    // The one before it is offered, and so is the next path's.
    await expect(
      canvas.insertTarget("Move add at the start of transaction move"),
    ).not.toHaveAttribute("aria-disabled", /.*/);
    await expect(
      canvas.insertTarget("Move add between submit and notify"),
    ).toHaveAttribute("aria-disabled", "true");
    await page.mouse.up();
    await expect(page.locator(".toast-region")).toContainText(REASON);
    await expect(tip).toHaveCount(0);
    expect(await stepsOfMove(page)).toEqual([
      "lookup",
      "hold",
      "add",
      "submit",
      "notify",
    ]);
  });

  test("a step that changes nothing goes anywhere, and the reason goes as the pointer leaves", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, UNDO);
    await canvas.closeInspector();
    await dragOver(
      page,
      canvas,
      "add",
      canvas.insertTarget("Move add at the end of transaction move"),
    );
    const tip = canvas.root.locator(".move-reason");
    await expect(tip).toBeVisible();
    const open = center(
      await box(
        canvas.insertTarget("Move add at the start of transaction move"),
      ),
    );
    await page.mouse.move(open.x, open.y, { steps: 6 });
    await expect(tip).toHaveCount(0);
    await page.keyboard.press("Escape");
    await page.mouse.up();
    await startMove(canvas, "lookup");
    const last = canvas.insertTarget(
      "Move lookup at the end of transaction move",
    );
    await expect(last).not.toHaveAttribute("aria-disabled", /.*/);
    await last.click();
    expect(await stepsOfMove(page)).toEqual([
      "hold",
      "add",
      "submit",
      "notify",
      "lookup",
    ]);
  });

  test("by keyboard, Move to… starts at a place it offers and a refused one says why", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, UNDO);
    await canvas.closeInspector();
    await startMove(canvas, "add");
    const focused = page.locator(":focus");
    await expect(focused).toHaveAttribute("data-insert", "");
    await expect(focused).not.toHaveAttribute("aria-disabled", /.*/);
    const refused = canvas.insertTarget(
      "Move add at the end of transaction move",
    );
    await refused.focus();
    await expect(refused).toBeFocused();
    await expect(refused).toHaveAccessibleDescription(REASON);
    await expect(canvas.root.locator(".move-reason")).toHaveText(REASON);
    await page.keyboard.press("Enter");
    await expect(page.locator(".toast-region")).toContainText(REASON);
    // Still moving: another place takes it.
    await expect(canvas.root).toHaveClass(/\brevealing\b/);
    // The reason goes as focus goes on to a place that is offered.
    await canvas
      .insertTarget("Move add at the start of transaction move")
      .focus();
    await expect(canvas.root.locator(".move-reason")).toHaveCount(0);
    await canvas
      .insertTarget("Move add at the start of transaction move")
      .click();
    expect(await stepsOfMove(page)).toEqual([
      "add",
      "lookup",
      "hold",
      "submit",
      "notify",
    ]);
  });

  test("N and / on a refused place say why, as Enter does", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, UNDO);
    await canvas.closeInspector();
    await startMove(canvas, "add");
    for (const key of ["n", "/"]) {
      await canvas
        .insertTarget("Move add at the end of transaction move")
        .focus();
      await page.keyboard.press(key);
      const toast = page.locator(".toast", { hasText: REASON });
      await expect(toast).toBeVisible();
      await toast.getByRole("button", { name: "Dismiss notification" }).click();
      await expect(page.locator(".add-step-panel")).toHaveCount(0);
      // Still moving.
      await expect(canvas.root).toHaveClass(/\brevealing\b/);
    }
  });

  test("a transaction can't be moved or pasted into another, and the reason says so", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, UNDO);
    await canvas.closeInspector();
    await startMove(canvas, "other");
    const inside = canvas.insertTarget(
      "Move other at the end of transaction move",
    );
    await expect(inside).toHaveAttribute("aria-disabled", "true");
    await expect(inside).toHaveAccessibleDescription(NESTED);
    await inside.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator(".toast-region")).toContainText(NESTED);
    await page.keyboard.press("Escape");
    const before = await sourceText(page);
    await showView(page, "Designer");
    await canvas.ready();
    await canvas.tileBody("hold").click();
    await canvas.root.evaluate(
      (root, text) => {
        const data = new DataTransfer();
        data.setData("text/plain", text);
        root.querySelector('[data-tile="hold"] .tile-body')!.dispatchEvent(
          new ClipboardEvent("paste", {
            clipboardData: data,
            bubbles: true,
            cancelable: true,
          }),
        );
      },
      JSON.stringify({
        weaveClipboard: 1,
        steps: [
          {
            id: "again",
            kind: "transaction",
            steps: [],
            output: { literal: null },
          },
        ],
      }),
    );
    await expect(page.locator(".toast-region")).toContainText(NESTED);
    expect(await sourceText(page)).toBe(before);
  });

  test("step details and Outline name a step's place in its transaction", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, UNDO);
    await canvas.closeInspector();
    await canvas.tileBody("hold").dblclick();
    const details = page.getByRole("dialog", { name: "Step details: hold" });
    await expect(details).toContainText("Transaction move · step 2 of 5");
    await details.getByRole("button", { name: "Close", exact: true }).click();
    await showView(page, "Outline");
    await expect(
      page.getByRole("treeitem", { name: /^hold\s*Transaction move$/ }),
    ).toBeVisible();
  });
});

/** The lines as drawn, in canvas units from the first step: their step, place and size. */
const drawnLines = (canvas: CanvasPage, from: string) =>
  canvas.root.evaluate((root, id) => {
    const card = root.querySelector<HTMLElement>(
      `[data-tile="${id}"] .tile-body`,
    )!;
    const at = card.getBoundingClientRect();
    const zoom = at.width / card.offsetWidth;
    return [...root.querySelectorAll<HTMLElement>(".point-line")].map(
      (line) => {
        const box = line.getBoundingClientRect();
        return [
          line.dataset["point"],
          line.getAttribute("aria-label"),
          Math.round((box.left + box.width / 2 - at.left) / zoom),
          Math.round((box.top - at.top) / zoom),
          Math.round(box.height / zoom),
        ];
      },
    );
  }, from);

test.describe("the point of no return in runs", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  /** hold, add and submit's columns, less the rest of submit's edge. */
  const LINE = [
    "submit",
    "Point of no return, after submit",
    2 * 248 + 96 + 76,
    -8,
    192,
  ];

  test("a simulated run keeps the line and hides how steps are undone", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    const canvas = await openWorkflow(page, UNDO);
    await canvas.closeInspector();
    expect(await drawnLines(canvas, "hold")).toEqual([LINE]);
    await page.route("**/studio/local/debug/execute", (route) =>
      route.fulfill({
        json: {
          status: "completed",
          compile: { ok: true, sliced: false, diagnostics: [], stubs: [] },
          trace: [
            ["lookup", "action"],
            ["hold", "action"],
            ["add", "action"],
            ["submit", "action"],
            ["notify", "transform"],
            ["move", "transaction"],
            ["check", "transform"],
            ["other", "transaction"],
            ["tally", "transform"],
          ].map(([nodeId, kind], index) => ({
            index,
            nodeId,
            instanceKey: "",
            kind,
            status: "completed",
            output: {},
          })),
        },
      }),
    );
    await page
      .locator(".editor-bar")
      .getByRole("button", { name: "Execute workflow" })
      .click();
    await expect(canvas.tile("submit").locator(".tile-badge")).toHaveAttribute(
      "data-badge",
      "done",
    );
    expect(await drawnLines(canvas, "hold")).toEqual([LINE]);
    for (const id of ["hold", "add", "submit"]) {
      await expect(undoBadge(canvas, id)).toHaveCount(0);
      await expect(undoWord(canvas, id)).toHaveCount(0);
      await expect(canvas.tileBody(id)).toHaveAccessibleName(
        /of 5 in Transaction move$/,
      );
    }
  });

  test("a loop inside a transaction counts its own items", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    const canvas = await openWorkflow(
      page,
      workflow(`    - id: move
      kind: transaction
      steps:
        - id: each
          kind: forEach
          items: {ref: /input/moves}
          concurrency: 1
          maxItems: 100
          collect: all
          body:
            steps:
${build("send", 14)}            output: {ref: /item}
      output: {literal: null}
`),
    );
    await canvas.closeInspector();
    const entry = (nodeId: string, kind: string, instanceKey = "") => ({
      nodeId,
      instanceKey,
      kind,
      status: "completed",
      output: {},
    });
    await page.route("**/studio/local/debug/execute", (route) =>
      route.fulfill({
        json: {
          status: "completed",
          compile: { ok: true, sliced: false, diagnostics: [], stubs: [] },
          trace: [
            entry("send", "transform", "send[0]"),
            entry("send", "transform", "send[1]"),
            { ...entry("each", "forEach"), output: [{}, {}] },
            entry("move", "transaction"),
          ].map((step, index) => ({ index, ...step })),
          loops: [{ nodeId: "each", instanceKey: "", count: 2, completed: 2 }],
        },
      }),
    );
    await page
      .locator(".editor-bar")
      .getByRole("button", { name: "Execute workflow" })
      .click();
    await expect(canvas.tileBody("each").locator(".frame-run")).toContainText(
      "2 of 2",
    );
    // The transaction isn't a loop: its header keeps its promise.
    await expect(canvas.tileBody("move").locator(".frame-run")).toHaveCount(0);
    await expect(
      canvas.tileBody("move").locator(".frame-promise"),
    ).toBeVisible();
  });

  test("a recorded run keeps it too, and offers no place to drop a step", async ({
    page,
  }) => {
    const { spec } = parse(UNDO.buffer.toString()) as { spec: unknown };
    const document = {
      apiVersion: "weave/v1alpha1",
      kind: "Workflow",
      metadata: { name: "invoice-approval", version: "1.3.0" },
      spec,
    };
    const base = runFixture("succeeded");
    const { runs } = await openRunsTab(page, {
      runs: [base],
      workflow: document,
      before: async () => {
        await page.route("**/projects/*/workflows/*/export", (route) =>
          route.fulfill({ json: { id: "version", document } }),
        );
      },
    });
    await runs.row(RUN_IDS.succeeded).click();
    const canvas = new CanvasPage(page);
    await expect(
      canvas.root.getByRole("img", {
        name: "Point of no return, after submit",
        exact: true,
      }),
    ).toHaveCount(1);
    expect(await drawnLines(canvas, "hold")).toEqual([LINE]);
    await expect(undoBadge(canvas, "submit")).toHaveCount(0);
    await expect(canvas.root.locator("[data-insert]")).toHaveCount(0);
  });
});

test.describe("how each step is undone on a phone", () => {
  test.use({ viewport: { width: 360, height: 640 } });

  test("the label shows above the line at 55% and up, and its tooltip area is 12 px", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page, UNDO);
    await canvas.closeInspector();
    const body = await box(canvas.root);
    await expect(async () => {
      await page.mouse.move(body.x + body.width / 2, body.y + body.height / 2);
      await page.keyboard.down("Control");
      await page.mouse.wheel(0, -40);
      await page.keyboard.up("Control");
      await expect(pointLabel(canvas, "submit")).toHaveCount(1, {
        timeout: 500,
      });
    }).toPass({ timeout: 15_000 });
    await expect(pointLabel(canvas, "submit")).toHaveCount(1);
    expect((await box(line(canvas, "submit"))).width).toBeCloseTo(12, 0);
    expect((await box(pointLabel(canvas, "submit"))).bottom).toBeLessThan(
      (await box(canvas.tileBody("submit"))).top,
    );
  });
});
