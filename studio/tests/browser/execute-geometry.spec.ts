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
// Alignment of the execution screens, measured: the Execute split button and
// Stop, the gate dialogs, the run log, the Table view, a decision's paths and
// the Input pane's empty state, at desktop and narrow sizes and on touch.
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  EXECUTE_ASKING,
  EXECUTE_CHAIN,
  EXECUTE_ROUTED,
  EXECUTE_ROWS,
  executeWorkflowIn,
  gateDialog,
  moreWays,
  openExecuteFixture,
  openExecuteWorkflow,
  pinSample,
  shownExecute,
  stopButton,
  validateWith,
} from "./execute-po";
import { hasPython } from "./local-authoring";
import {
  box,
  boxes,
  clippedText,
  density,
  expectAligned,
  intersects,
  type Box,
} from "./step-geometry";
import { outputPane } from "./test-data-po";

test.skip(!hasPython, "Create the Python development environment first.");

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

/** No two of `items` overlap. */
function expectApart(items: Box[], what: string) {
  for (let i = 0; i < items.length; i++)
    for (let j = i + 1; j < items.length; j++)
      expect(
        intersects(items[i], items[j]),
        `${what} ${i} and ${j} overlap`,
      ).toBe(false);
}

/** The page never scrolls sideways. */
async function expectNoPageScroll(page: Page) {
  const [scroll, width] = await page.evaluate(() => [
    document.scrollingElement!.scrollWidth,
    innerWidth,
  ]);
  expect(scroll).toBeLessThanOrEqual(width);
}

/** Every box sits inside the window. */
async function expectInWindow(page: Page, items: Box[], what: string) {
  const { width, height } = page.viewportSize()!;
  for (const item of items) {
    expect(item.x, `${what} left`).toBeGreaterThanOrEqual(-0.5);
    expect(item.y, `${what} top`).toBeGreaterThanOrEqual(-0.5);
    expect(item.right, `${what} right`).toBeLessThanOrEqual(width + 0.5);
    expect(item.bottom, `${what} bottom`).toBeLessThanOrEqual(height + 0.5);
  }
}

/** Holds the host's answer to Execute until `release` is called. */
async function holdExecute(page: Page): Promise<() => void> {
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/studio/local/debug/execute", async (route) => {
    await held;
    await route.fallback().catch(() => undefined);
  });
  return release;
}

const centers = (items: Box[]) => items.map((b) => b.middle);

for (const size of SIZES)
  test.describe(`Execution screens at ${size.tag}`, () => {
    test.use(size.use);

    test("the Execute split button and Stop share one height, edge and baseline", async ({
      page,
    }) => {
      const { details } = await openExecuteWorkflow(page, EXECUTE_CHAIN);
      const { control } = await density(page);
      await details.open("second");
      const execute = await shownExecute(details);
      const menu = moreWays(details.dialog);
      const [run, more] = [await box(execute), await box(menu)];
      expect(run.height).toBeCloseTo(control, 0);
      expectAligned([run.y, more.y], "split tops");
      expectAligned([run.bottom, more.bottom], "split bottoms");
      expect(run.right, "Execute comes first").toBeLessThanOrEqual(more.x + 1);
      expect(intersects(run, more)).toBe(false);

      const release = await holdExecute(page);
      await execute.click();
      const stop = stopButton(details.dialog);
      await expect(stop).toBeVisible();
      const halt = await box(stop);
      expect(halt.height).toBeCloseTo(control, 0);
      expectAligned([halt.x, run.x], "Stop's left edge");
      expectAligned([halt.y, run.y], "Stop's top");
      release();
      await expect(stop).toBeHidden();
      await expectNoPageScroll(page);
    });

    test("a gate dialog fits the window, its answers sized and the main one focused", async ({
      page,
    }) => {
      const { details } = await openExecuteFixture(page);
      const { control } = await density(page);
      await pinSample(details, "lookup");
      await details.open("lookup");
      await (await shownExecute(details)).click();
      const dialog = gateDialog(page);
      await expect(dialog).toContainText("Unpin and execute?");
      await expectInWindow(page, [await box(dialog)], "dialog");
      expect(await clippedText(dialog, "")).toEqual([]);
      const answers = dialog.locator(".dialog-actions button");
      const sized = await boxes(answers);
      expect(sized).toHaveLength(2);
      for (const answer of sized) expect(answer.height).toBeCloseTo(control, 0);
      expectApart(sized, "answers");
      const primary = dialog.getByRole("button", { name: "Unpin and execute" });
      await expect(primary).toBeFocused();
      // The main answer comes last in reading order, as in every Studio dialog.
      const [cancel, main] = sized;
      expect(
        main.x >= cancel.right - 1 || main.y >= cancel.bottom - 1,
        "the main answer follows Cancel",
      ).toBe(true);
    });

    test("the Fix dialog separates each step from its message, with Fix on the row's center", async ({
      page,
    }) => {
      const { details } = await openExecuteFixture(page);
      const { control } = await density(page);
      await validateWith(page, [
        {
          code: "WV-SCHEMA-REQUIRED",
          severity: "error",
          stage: "schema",
          path: "/spec/steps/0/with",
          message: "Customer ID is required.",
        },
      ]);
      await details.open("score");
      await (await shownExecute(details)).click();
      const dialog = gateDialog(page);
      await expect(dialog).toContainText("Fix 1 issue first");
      await expectInWindow(page, [await box(dialog)], "dialog");
      expect(await clippedText(dialog, "")).toEqual([]);
      const row = dialog.locator(".execute-issue").first();
      await expect(row).toHaveCSS("list-style-type", "none");
      const step = await box(row.locator(".execute-issue-step"));
      const message = await box(row.locator(".execute-issue-message"));
      expect(intersects(step, message)).toBe(false);
      expect(
        message.y >= step.bottom - 0.5 || message.x >= step.right + 4,
        "the step and its message are apart",
      ).toBe(true);
      const fix = await box(row.getByRole("button", { name: /^Fix / }));
      const line = await box(row);
      expect(fix.height).toBeCloseTo(control, 0);
      expect(Math.abs(fix.middle - line.middle)).toBeLessThanOrEqual(1);
      expect(fix.right).toBeLessThanOrEqual(line.right);
      expect(intersects(fix, message)).toBe(false);
      const close = await box(
        dialog.getByRole("button", { name: "Close", exact: true }),
      );
      expect(close.height).toBeCloseTo(control, 0);
      await expectNoPageScroll(page);
    });

    test("the run log's question form keeps Send on the density, inside the log", async ({
      page,
    }) => {
      await openExecuteWorkflow(page, EXECUTE_ASKING);
      const { touch } = await density(page);
      await executeWorkflowIn(page);
      const log = page.locator("weave-run-log");
      await expect(log.locator(".run-log-status")).toHaveText(
        "Waiting · Send signal order-paid",
      );
      const send = log.getByRole("button", { name: "Send", exact: true });
      await send.scrollIntoViewIfNeeded();
      const sent = await box(send);
      expect(sent.height).toBeCloseTo(touch ? 44 : 32, 0);
      const sheet = await box(log);
      expect(sent.x).toBeGreaterThanOrEqual(sheet.x);
      expect(sent.right).toBeLessThanOrEqual(sheet.right + 0.5);
      expect(await clippedText(log, "")).toEqual([]);
      await expectNoPageScroll(page);
    });

    test("run log rows, icons and controls line up", async ({ page }) => {
      await openExecuteWorkflow(page, EXECUTE_CHAIN);
      const { row, touch } = await density(page);
      await page.locator('[data-step="third"] .tile-body').first().click();
      await page.keyboard.press("F9");
      await executeWorkflowIn(page);
      const log = page.locator("weave-run-log");
      await expect(log.locator(".run-log-status")).toHaveText(
        "Paused before third",
      );
      const entries = log.locator(".run-log-entry");
      await expect(entries).toHaveCount(3);
      const rows = await boxes(entries);
      for (const entry of rows) expect(entry.height).toBeCloseTo(row, 0);
      expectApart(rows, "run log rows");
      const icons = await boxes(log.locator(".run-log-entry .run-log-icon"));
      for (const [i, icon] of icons.entries())
        expect(Math.abs(icon.middle - rows[i].middle)).toBeLessThanOrEqual(1);
      expectAligned(
        icons.map((icon) => icon.x),
        "run log icons' left edges",
      );
      const controls = await boxes(log.locator(".run-log-controls button"));
      expect(controls.length).toBeGreaterThan(0);
      expectAligned(
        controls.map((c) => c.y),
        "run log controls' tops",
      );
      expectAligned(
        controls.map((c) => c.height),
        "run log controls' heights",
      );
      for (const control of controls)
        expect(control.height).toBeGreaterThanOrEqual(touch ? 44 : 32);
      expectApart(controls, "run log controls");
      const close = await box(
        log.getByRole("button", { name: "Close the run log" }),
      );
      expect(close.height).toBeCloseTo(touch ? 44 : 32, 0);
      const head = await box(log.locator(".run-log-head"));
      expect(Math.abs(close.middle - head.middle)).toBeLessThanOrEqual(1);
      await expectInWindow(page, [close, ...controls], "run log controls");
      await expectNoPageScroll(page);
    });

    test("the Table view keeps its rows on the density and scrolls inside its pane", async ({
      page,
    }) => {
      const { details } = await openExecuteWorkflow(page, EXECUTE_ROWS);
      const { row } = await density(page);
      await details.open("rows");
      await (await shownExecute(details)).click();
      const pane = await outputPane(details);
      const table = pane.getByRole("table", { name: "Output data" });
      await expect(table).toBeVisible();
      const cells = await boxes(table.locator("tr"));
      expect(cells).toHaveLength(4);
      for (const line of cells) expect(line.height).toBeCloseTo(row, 0);
      expectApart(cells, "table rows");
      const scroller = pane.locator(".data-table-scroll");
      const frame = await box(scroller);
      const panes = await box(pane);
      expect(frame.right).toBeLessThanOrEqual(panes.right + 0.5);
      await expectNoPageScroll(page);
    });

    test("a decision's path rows sit on the density with Taken centered", async ({
      page,
    }) => {
      const { details } = await openExecuteWorkflow(page, EXECUTE_ROUTED);
      const { row } = await density(page);
      await details.open("route");
      await (await shownExecute(details)).click();
      const pane = await outputPane(details);
      await expect(pane.locator(".pane-count")).toHaveText("Took path 1");
      const rows = await boxes(pane.locator(".path-row"));
      expect(rows.length).toBe(3);
      for (const line of rows) expect(line.height).toBeCloseTo(row, 0);
      expectApart(rows, "path rows");
      const taken = await box(pane.locator(".path-row .pill"));
      expect(Math.abs(taken.middle - rows[0].middle)).toBeLessThanOrEqual(1);
      expect(taken.right).toBeLessThanOrEqual(rows[0].right);
      expectAligned(
        centers(await boxes(pane.locator(".path-row-label"))).map(
          (m, i) => m - rows[i].middle,
        ),
        "path labels on their rows",
      );
      await expectNoPageScroll(page);
    });

    test("the Input pane's empty state keeps its two answers together", async ({
      page,
    }) => {
      const { details } = await openExecuteFixture(page);
      const { control, touch } = await density(page);
      await details.open("score");
      const tabs = details.dialog
        .getByRole("tablist", { name: /^(Step details panes|Data)$/ })
        .getByRole("tab", { name: "Input" });
      if (await tabs.count()) await tabs.first().click();
      const input = details.dialog.locator("weave-input-pane");
      await input.getByLabel("Input source").selectOption({ index: 1 });
      await expect(input).toContainText("No input data yet");
      const answers = await boxes(
        input.locator(".pane-empty-actions > button"),
      );
      expect(answers).toHaveLength(2);
      expectApart(answers, "empty-state answers");
      for (const answer of answers)
        expect(answer.height).toBeGreaterThanOrEqual(touch ? 44 : control - 8);
      expectAligned(
        answers.map((a) => a.height),
        "empty-state answers' heights",
      );
      const [first, second] = answers;
      if (size.tag === "1440x900")
        expectAligned([first.middle, second.middle], "answers on one row");
      if (size.tag === "360x640") {
        expect(second.y).toBeGreaterThanOrEqual(first.bottom - 0.5);
        expectAligned([first.width, second.width], "stacked answers' widths");
        expectAligned([first.x, second.x], "stacked answers' left edges");
      }
      expect(await clippedText(input as Locator, "")).toEqual([]);
      await expectNoPageScroll(page);
    });
  });
