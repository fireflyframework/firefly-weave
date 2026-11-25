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
// The screens of a step's error path, measured at a desktop, a narrow window
// and a phone, and on touch: the Success and Error outputs, the lane and its
// slot on the canvas; On error and Result on error in Settings; the Simulate
// a failure dialog; the question before Execute step inside a path its step
// didn't take; and the toast when the path goes. A fine pointer from 768 px
// gets the desktop sizes; a coarse pointer or a narrower window gets 44 px
// targets, the canvas's outputs too, whatever the zoom.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { repository } from "../python-path";
import { openWorkflow, type CanvasPage } from "./canvas-po";
import {
  StepDetailsPage,
  openStepFixture,
  withLanguageFeatures,
  yamlFile,
} from "./step-details-po";
import { connected, defaultCatalog } from "./support";
import {
  box,
  boxes,
  clippedText,
  density,
  showParameters,
  type Box,
} from "./step-geometry";
import { PROJECT, TENANT, outputPane } from "./test-data-po";

const SIZES = [
  { tag: "1440x900", use: { viewport: { width: 1440, height: 900 } } },
  { tag: "600x500", use: { viewport: { width: 600, height: 500 } } },
  { tag: "360x640", use: { viewport: { width: 360, height: 640 } } },
  {
    tag: "600x500 touch",
    use: {
      viewport: { width: 600, height: 500 },
      hasTouch: true,
      isMobile: true,
    },
  },
];

/** The language manifest the Studio host serves, as the repository's Python writes it. */
const manifest = JSON.parse(
  readFileSync(
    join(repository, "studio/tests/fixtures/language/manifest.json"),
    "utf8",
  ),
).manifest as { features: string[] };

/** review's error path is empty; submit's holds notify-team; summarize follows. */
const WORKFLOW = yamlFile(
  "request-intake.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: request-intake, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - id: review
      kind: transform
      value: {object: {checked: {literal: true}}}
      onError: errorOutput
      errorPath:
        steps: []
        output: {literal: null}
    - id: submit
      kind: transform
      value: {object: {sent: {literal: true}}}
      onError: errorOutput
      errorPath:
        steps:
          - id: notify-team
            kind: transform
            value: {ref: /steps/submit/error/code}
        output: {literal: null}
    - id: summarize
      kind: transform
      value: {literal: done}
  output: {literal: null}
`,
);

/** An action that changes data: its output can be pinned and its failure simulated. */
const SUBMIT = {
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: "requests.submit", version: "1.0.0" },
  spec: {
    implementation: {
      kind: "worker",
      taskType: "requests.submit",
      taskVersion: "1.0.0",
    },
    sideEffect: "non_idempotent",
    timeoutSeconds: 30,
    inputSchema: { type: "object" },
    outputSchema: { type: "object", properties: { id: { type: "string" } } },
  },
};
const INTAKE = yamlFile(
  "request-intake.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: request-intake, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - id: submit
      kind: action
      uses: requests.submit@1.0.0
      with: {literal: {}}
      onError: continue
  output: {literal: null}
`,
);
/** request-intake, its action in the catalog, with step details open. */
const openIntake = (page: Page) =>
  openStepFixture(page, INTAKE, async (p) => {
    const result = await connected(p, {
      catalog: [...defaultCatalog, { id: "a9", document: SUBMIT }],
      tenantId: TENANT,
      projectId: PROJECT,
    });
    await withLanguageFeatures(p, ["text.concat", "text.join", "flow.onError"]);
    return result;
  });

async function open(page: Page): Promise<CanvasPage> {
  await page.route(
    /\/studio\/(?:contracts\/language|api\/api\/v1\/tenants\/[^/]+\/projects\/[^/]+\/language)$/,
    (route) => route.fulfill({ json: manifest }),
  );
  return openWorkflow(page, WORKFLOW);
}

/**
 * A dialog's two answers share a row, the main one last; on a phone, where
 * the panel is narrower than three answers need, they stack at its full
 * width, the main one last, as Studio's dialogs all do.
 */
function expectAnswers(cancel: Box, main: Box, width: number) {
  if (width >= 600) {
    near(cancel.y, main.y, "answers on one row");
    expect(main.x).toBeGreaterThan(cancel.x);
  } else {
    expect(main.y).toBeGreaterThanOrEqual(cancel.bottom);
    near(main.width, cancel.width, "answers as wide");
  }
}

/** A dialog's content never scrolls sideways: its footer stays inside its padding. */
async function expectNoSidewaysScroll(dialog: Locator) {
  const [scroll, width] = await dialog.evaluate((node) => [
    node.scrollWidth,
    node.clientWidth,
  ]);
  expect(scroll, "the dialog scrolls sideways").toBeLessThanOrEqual(width);
}

/** The page never scrolls sideways. */
async function expectNoPageScroll(page: Page) {
  const [scroll, width] = await page.evaluate(() => [
    document.scrollingElement!.scrollWidth,
    innerWidth,
  ]);
  expect(scroll).toBeLessThanOrEqual(width);
}
const near = (actual: number, expected: number, what: string, by = 0.5) =>
  expect(Math.abs(actual - expected), `${what}: ${actual}`).toBeLessThanOrEqual(
    by,
  );

/**
 * The points around the target's center, as [left, right, above, below]
 * offsets, that a tap or click doesn't reach: what its hit area takes,
 * whatever draws it. A control that inserts at the same place counts (the
 * "+" on the Error output's edge adds at the start of its path too).
 */
async function missed(
  target: Locator,
  [left, right, above, below]: number[],
): Promise<string[]> {
  return target.evaluate(
    (element, points) => {
      const r = element.getBoundingClientRect();
      const [x, y] = [r.x + r.width / 2, r.y + r.height / 2];
      return points.flatMap(([dx, dy, side]) => {
        const hit = document.elementFromPoint(x + dx, y + dy);
        if (hit && element.contains(hit)) return [];
        const insert = (node: Element | null | undefined) =>
          node?.getAttribute("data-insert-owner") === null
            ? null
            : `${node?.getAttribute("data-insert-owner")}#${node?.getAttribute("data-insert-index")}`;
        const same = insert(hit?.closest("[data-insert-owner]"));
        if (same && same === insert(element)) return [];
        const what = hit
          ? `${hit.tagName.toLowerCase()}.${[...hit.classList].join(".")}`
          : "nothing";
        return [`${side}: ${what}`];
      });
    },
    [
      [-left, 0, "left"],
      [right, 0, "right"],
      [0, -above, "above"],
      [0, below, "below"],
    ] as [number, number, string][],
  );
}

/** Pans the canvas with the wheel, as a person does, until the target is in its middle. */
async function panIntoView(page: Page, canvas: CanvasPage, target: Locator) {
  const area = await box(canvas.root);
  const at = await box(target);
  const by = Math.round(at.x + at.width / 2 - (area.x + area.width / 2));
  await page.mouse.move(area.x + area.width / 2, area.middle);
  await page.mouse.wheel(by, 0);
  await expect
    .poll(async () => Math.abs((await box(target)).x - (at.x - by)))
    .toBeLessThan(3);
}

/** Pans the canvas with the wheel until the target is at its center. */
async function centerOn(page: Page, canvas: CanvasPage, target: Locator) {
  const area = await box(canvas.root);
  const at = await box(target);
  const dx = Math.round(at.x + at.width / 2 - (area.x + area.width / 2));
  const dy = Math.round(at.middle - area.middle);
  await page.mouse.move(area.x + area.width / 2, area.middle);
  await page.mouse.wheel(dx, dy);
  await expect
    .poll(async () => {
      const now = await box(target);
      return Math.abs(now.x - (at.x - dx)) + Math.abs(now.y - (at.y - dy));
    })
    .toBeLessThan(3);
}

/** Zooms with Ctrl and the wheel over the canvas's center, as a person does. */
async function zoomTo(page: Page, canvas: CanvasPage, percent: number) {
  const area = await box(canvas.root);
  const now = await canvas.zoomPercent();
  await page.mouse.move(area.x + area.width / 2, area.middle);
  await page.keyboard.down("Control");
  await page.mouse.wheel(0, -Math.log(percent / now) / 0.0015);
  await page.keyboard.up("Control");
  await expect
    .poll(async () => Math.abs((await canvas.zoomPercent()) - percent))
    .toBeLessThanOrEqual(1);
}

/**
 * The points of a 9 × 9 grid over a step's card that a tap gives to an
 * output: whatever an output takes beyond its own 24-unit circle, the area a
 * fine pointer has, it takes from the card.
 */
async function takenFromCard(card: Locator): Promise<string[]> {
  return card.evaluate((body) => {
    const r = body.getBoundingClientRect();
    const taken: string[] = [];
    for (let i = 0; i < 9; i++)
      for (let j = 0; j < 9; j++) {
        const x = r.x + 1 + ((r.width - 2) * i) / 8;
        const y = r.y + 1 + ((r.height - 2) * j) / 8;
        if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) continue;
        const output = document.elementFromPoint(x, y)?.closest(".handle");
        if (!output) continue;
        // Drawn 12 units across: its circle's radius is its drawn width.
        const o = output.getBoundingClientRect();
        const center = [o.x + o.width / 2, o.y + o.height / 2];
        if (Math.hypot(x - center[0], y - center[1]) <= o.width + 0.5) continue;
        taken.push(`${output.getAttribute("data-handle")} at ${i},${j}`);
      }
    return taken;
  });
}

/** What a tap on the target's center reaches instead of it; null when it reaches the target. */
const takerOfCenter = (target: Locator) =>
  target.evaluate((element) => {
    const r = element.getBoundingClientRect();
    const hit = document.elementFromPoint(
      r.x + r.width / 2,
      r.y + r.height / 2,
    );
    if (!hit || element.contains(hit)) return null;
    const owner = hit.closest("[data-handle], [data-tile]");
    return `${hit.className} ${owner?.getAttribute("data-handle") ?? owner?.getAttribute("data-tile") ?? ""}`.trim();
  });

/** The text's size on screen: its font size times the scale it is drawn at. */
const screenFont = (label: Locator) =>
  label.evaluate((element: HTMLElement) => {
    const drawn = element.getBoundingClientRect().height / element.offsetHeight;
    return parseFloat(getComputedStyle(element).fontSize) * drawn;
  });

for (const size of SIZES)
  test.describe(`a step's error path at ${size.tag}`, () => {
    test.use(size.use);

    test("the Success and Error outputs, the lane and its slot", async ({
      page,
    }) => {
      const canvas = await open(page);
      const { touch } = await density(page);
      const zoom = (await canvas.zoomPercent()) / 100;
      // review's empty path: its slot 64 units, at least 44 px on touch.
      const slot = canvas.insertTarget(
        "Add a step to the error path of review",
      );
      await panIntoView(page, canvas, slot);
      near((await box(slot)).width, 64 * zoom, "slot", 1);
      if (touch)
        expect(await missed(slot, [21, 21, 21, 21]), `slot at ${zoom}`).toEqual(
          [],
        );
      const success = canvas.handle("submit:path:success");
      const error = canvas.handle("submit:path:errorPath");
      await panIntoView(page, canvas, success);
      // Each output drawn 12 units across; Error with a 2 px danger border.
      for (const handle of [success, error])
        near((await box(handle)).width, 12 * zoom, "output drawn", 1);
      expect(await error.getAttribute("data-tone")).toBe("danger");
      expect(
        await error.evaluate((node) => getComputedStyle(node).borderTopWidth),
      ).toBe("2px");
      // The hit area: 24 units with a fine pointer. On touch and below
      // 768 px, 44 px on screen whatever the zoom: from the step's card (20
      // units left of the output's center) to the right; Error's 22 px up
      // and down, Success's 11 px up and 33 px down, which leaves the step's
      // issue badge above it its own center.
      const fine = 12 * zoom - 2;
      for (const [handle, above] of [
        [success, 10],
        [error, 21],
      ] as const)
        expect(
          await missed(
            handle,
            touch
              ? [20 * zoom - 1, 43 - 20 * zoom, above, 42 - above]
              : [fine, fine, fine, fine],
          ),
          `${await handle.getAttribute("data-handle")} at ${zoom}`,
        ).toEqual([]);
      // The lane: 224 units under the step's row; its first step 152 units
      // after the step's right edge.
      near(
        (await box(error)).middle - (await box(success)).middle,
        224 * zoom,
        "lane row",
        1,
      );
      near(
        (await box(canvas.tileBody("notify-team"))).x -
          (await box(canvas.tileBody("submit"))).right,
        152 * zoom,
        "first step of the lane",
        1,
      );
      // Path labels never under 10 px on screen; Success and Error apart.
      const labels = [
        canvas.branchLabel("submit/success"),
        canvas.branchLabel("submit/errorPath"),
      ];
      for (const label of labels)
        expect(await screenFont(label)).toBeGreaterThanOrEqual(9.9);
      const [above, below] = await Promise.all(
        labels.map((label) => box(label)),
      );
      expect(below.y).toBeGreaterThanOrEqual(above.bottom);
      await expectNoPageScroll(page);
    });

    test("each step's card and issue badge keep their taps beside its outputs", async ({
      page,
    }) => {
      const canvas = await open(page);
      // An issue on every step: review and submit have Success and Error
      // outputs, notify-team and summarize a plain one.
      await page.route("**/studio/local/validate", (route) =>
        route.fulfill({
          json: {
            validationOk: false,
            errorCount: 4,
            partial: true,
            diagnostics: [
              "/spec/steps/0/value",
              "/spec/steps/1/value",
              "/spec/steps/1/errorPath/steps/0/value",
              "/spec/steps/2/value",
            ].map((path) => ({
              code: "WV-COMP-TYPE_MISMATCH",
              severity: "error",
              message: "Choose a value",
              path,
            })),
          },
        }),
      );
      const validate = page.getByRole("button", {
        name: "Validate",
        exact: true,
      });
      if (await validate.isVisible()) await validate.click();
      else {
        await page
          .getByRole("button", { name: "More workflow commands" })
          .click();
        await page
          .getByRole("menuitem", { name: "Validate", exact: true })
          .click();
      }
      await page.getByRole("button", { name: "Dismiss notification" }).click();
      await expect(canvas.root.locator(".tile-issue")).toHaveCount(4);
      // Down to the canvas's smallest zoom; issue badges hide below 30%.
      for (const percent of [100, 46, 40, 31, 25]) {
        await zoomTo(page, canvas, percent);
        for (const id of ["review", "submit", "notify-team", "summarize"]) {
          const card = canvas.tileBody(id);
          await centerOn(page, canvas, card);
          expect(
            await takenFromCard(card),
            `${id}'s card at ${percent}%`,
          ).toEqual([]);
          // A tile just panned into view paints, and takes taps, from the
          // next frame on (content-visibility): a person's tap comes later.
          if (percent >= 30)
            await expect
              .poll(
                () => takerOfCenter(canvas.tile(id).locator(".tile-issue")),
                { message: `${id}'s issue badge at ${percent}%` },
              )
              .toBeNull();
        }
      }
    });

    test("On error and Result on error in Settings", async ({ page }) => {
      await open(page);
      const { control } = await density(page);
      const details = new StepDetailsPage(page);
      await details.openWithKeyboard("submit");
      await showParameters(page, details.dialog);
      await details.tab("Settings").click();
      const field = details.field("onError");
      const select = field.getByRole("combobox", { name: "On error" });
      await select.scrollIntoViewIfNeeded();
      await expect(select).toHaveValue("Continue with error output");
      near((await box(select)).height, control, "On error height");
      // As wide as the form: the control spans its field.
      const whole = await box(field.locator(".param-control"));
      near(
        (await box(field.locator("weave-select"))).width,
        whole.width,
        "On error width",
        1,
      );
      // Result on error: on a 2 px rule, 12 px in, its value control as tall as the others.
      const result = field.locator(".settings-on-error-result");
      const rule = await result.evaluate((node) => {
        const style = getComputedStyle(node);
        return [style.borderLeftWidth, style.paddingLeft];
      });
      expect(rule).toEqual(["2px", "12px"]);
      for (const mode of await boxes(
        result.getByRole("radiogroup").getByRole("radio"),
      ))
        expect(mode.height, "Fixed and Mapped").toBeGreaterThanOrEqual(
          control === 44 ? 44 : 24,
        );
      expect(await clippedText(details.dialog)).toEqual([]);
      await expectNoPageScroll(page);
    });

    test("the Simulate a failure dialog", async ({ page }) => {
      await open(page);
      const { touch, control } = await density(page);
      const details = new StepDetailsPage(page);
      await details.openWithKeyboard("review");
      const pane = await outputPane(details);
      const more = pane.getByRole("button", { name: "More output actions" });
      near((await box(more)).height, touch ? 44 : 32, "More output actions");
      await more.click();
      await page.getByRole("menuitem", { name: "Simulate a failure…" }).click();
      const dialog = page.getByRole("dialog", {
        name: "Simulate a failure of review",
      });
      await expect(dialog.getByLabel("Code")).toBeFocused();
      const { width, height } = page.viewportSize()!;
      const panel = await box(dialog);
      if (touch) {
        // A sheet along the bottom, the window's width.
        near(panel.x, 0, "sheet left");
        near(panel.right, width, "sheet right");
        near(panel.bottom, height, "sheet bottom");
      } else {
        near(panel.width, 520, "dialog width");
        expect(panel.bottom).toBeLessThanOrEqual(height);
      }
      near((await box(dialog.getByLabel("Code"))).height, control, "Code");
      // Studio's chevron ends the Code field, in place of the browser's
      // triangle: the field looks the same with the triangle taken out.
      const code = dialog.getByLabel("Code");
      await page.addStyleTag({
        content:
          ".failure-code-chevron { visibility: hidden } .failure-code-field > input { caret-color: transparent }",
      });
      const kept = await code.screenshot();
      await page.addStyleTag({
        content:
          ".failure-code-field > input::-webkit-calendar-picker-indicator { display: none !important }",
      });
      expect(
        (await code.screenshot()).equals(kept),
        "the browser's triangle",
      ).toBe(true);
      await page.addStyleTag({
        content: ".failure-code-chevron { visibility: visible }",
      });
      const field = await box(code);
      const chevron = await box(dialog.locator(".failure-code-chevron"));
      near(field.right - chevron.right, 8, "chevron at the field's end", 1);
      near(
        chevron.y + chevron.height / 2,
        field.y + field.height / 2,
        "chevron centered",
        1,
      );
      // Each suggestion names its group: the browser's list shows none.
      expect(
        await dialog.locator("datalist option").first().getAttribute("label"),
      ).toBe("Platform failure · A value the step reads is missing.");
      // A transform's failure didn't start: no outcome to choose.
      await expect(dialog.locator(".failure-choice")).toHaveCount(0);
      const [cancel, simulate] = await boxes(
        dialog.locator(".dialog-actions button"),
      );
      for (const button of [cancel, simulate])
        near(button.height, control, "button height");
      expectAnswers(cancel, simulate, width);
      expect(await clippedText(dialog, "")).toEqual([]);
      await expectNoSidewaysScroll(dialog);
      await expectNoPageScroll(page);
    });

    test("the Outcome choices of an action's failure", async ({ page }) => {
      const details = await openIntake(page);
      await details.open("submit");
      const pane = await outputPane(details);
      await pane.getByRole("button", { name: "More output actions" }).click();
      await page.getByRole("menuitem", { name: "Simulate a failure…" }).click();
      const dialog = page.getByRole("dialog", {
        name: "Simulate a failure of submit",
      });
      await expect(dialog.getByLabel("Code")).toBeFocused();
      const cards = await boxes(
        dialog.getByRole("group", { name: "Outcome" }).locator("label"),
      );
      expect(cards).toHaveLength(2);
      for (const card of cards)
        expect(card.height, "Outcome").toBeGreaterThanOrEqual(52);
      expect(await clippedText(dialog, "")).toEqual([]);
      await expectNoSidewaysScroll(dialog);
    });

    test("More output actions ends the Output toolbar's first row, after Pin and Edit", async ({
      page,
    }) => {
      const details = await openIntake(page);
      await details.open("submit");
      const pane = await outputPane(details);
      // Pinned output, then a simulated failure over it: a pill and the count before the actions.
      await pane.getByRole("button", { name: "Set test output" }).click();
      await pane
        .locator(".output-editor")
        .getByRole("textbox")
        .first()
        .fill(JSON.stringify({ id: "PINNED-42", checked: true }));
      await pane.getByRole("button", { name: "Save", exact: true }).click();
      await expect(pane.locator(".pinned-banner")).toBeVisible();
      const more = pane.getByRole("button", { name: "More output actions" });
      await more.click();
      await page.getByRole("menuitem", { name: "Simulate a failure…" }).click();
      await page
        .getByRole("dialog", { name: "Simulate a failure of submit" })
        .getByRole("button", { name: "Simulate failure" })
        .click();
      await expect(pane.locator("weave-failure-banner")).toBeVisible();
      const pin = await box(pane.getByRole("button", { name: "Unpin output" }));
      const edit = await box(pane.getByRole("button", { name: "Edit output" }));
      const last = await box(more);
      const pill = await box(pane.locator(".pane-sub .pill").first());
      const sub = await box(pane.locator(".pane-sub"));
      // Pin, Edit, then More output actions, on the first row with the first pill.
      expect(edit.x).toBeGreaterThan(pin.x);
      expect(last.x).toBeGreaterThan(edit.x);
      for (const button of [pin, edit, last])
        near(button.y + button.height / 2, pill.y + pill.height / 2, "row", 1);
      near(last.right, sub.right - 16, "the row's end", 1);
      // One group for the three, named for a screen reader.
      await expect(
        pane.getByRole("group", { name: "Output actions" }).getByRole("button"),
      ).toHaveText(["", "", ""]);
      await expectNoPageScroll(page);
    });

    test("the question before Execute step inside a path its step didn't take", async ({
      page,
    }) => {
      await open(page);
      const { touch, control } = await density(page);
      const details = new StepDetailsPage(page);
      await details.openWithKeyboard("notify-team");
      await details.dialog.locator(".sd-execute > button").first().click();
      const asked = page.getByRole("dialog", {
        name: "Simulate a failure of submit first",
      });
      await expect(asked).toBeVisible();
      const { width, height } = page.viewportSize()!;
      const panel = await box(asked);
      expect(panel.x).toBeGreaterThanOrEqual(0);
      expect(panel.right).toBeLessThanOrEqual(width);
      expect(panel.bottom).toBeLessThanOrEqual(height);
      if (touch) near(panel.bottom, height, "sheet bottom");
      for (const chip of await boxes(
        asked
          .getByRole("list", { name: "Where notify-team is" })
          .getByRole("listitem"),
      ))
        near(chip.height, 26, "path chip", 1);
      const cancel = await box(asked.getByRole("button", { name: "Cancel" }));
      const simulate = await box(
        asked.getByRole("button", { name: "Simulate a failure" }),
      );
      for (const button of [cancel, simulate])
        near(button.height, control, "answer height");
      expectAnswers(cancel, simulate, width);
      expect(await clippedText(asked, "")).toEqual([]);
      await expectNoSidewaysScroll(asked);
      await expectNoPageScroll(page);
    });

    test("the toast when the error path goes", async ({ page }) => {
      await open(page);
      const { touch } = await density(page);
      const details = new StepDetailsPage(page);
      await details.openWithKeyboard("submit");
      await showParameters(page, details.dialog);
      await details.tab("Settings").click();
      const select = details
        .field("onError")
        .getByRole("combobox", { name: "On error" });
      await select.click();
      await page
        .getByRole("option", { name: /^Continue\s*The run goes on/ })
        .click();
      const toast = page.locator(".toast");
      await expect(toast.locator("strong")).toHaveText(
        "Removed the error path (1 step)",
      );
      const { width, height } = page.viewportSize()!;
      const whole = await box(toast);
      expect(whole.bottom).toBeLessThanOrEqual(height);
      const undo = await box(toast.getByRole("button", { name: "Undo" }));
      if (touch) {
        // At least 12 px from both sides; Undo 44 px.
        expect(whole.x).toBeGreaterThanOrEqual(12);
        expect(whole.right).toBeLessThanOrEqual(width - 12);
        near(undo.height, 44, "Undo");
      } else {
        // Up to 560 px wide; Undo at least 24 px, Studio's toasts' size.
        expect(whole.width).toBeLessThanOrEqual(560);
        expect(undo.height).toBeGreaterThanOrEqual(24);
      }
      expect(await clippedText(toast, "")).toEqual([]);
      await expectNoPageScroll(page);
    });
  });
