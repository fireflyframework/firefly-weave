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
// The screens of reacting to a platform event, measured at a desktop, a tablet
// and a phone width and on touch: Add a step's entry, the tile, Parameters,
// Test event, the activation dialog's rows and questions, Starts, and the two
// questions about the input and the removal. A fine pointer from 768 px gets
// the desktop sizes; a coarse pointer or a narrower window gets 44 px targets.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { requested, startedRuns } from "../event-platform";
import { AddStepPanel, languageFeatures } from "./add-step-po";
import { openNewWorkflow, openStoredWorkflow } from "./canvas-po";
import {
  activate,
  activationDialog,
  eventRows,
  openConnected,
  openForActivation,
  showStarts,
  startsTab,
  STORED,
  WIDER,
} from "./event-activation-po";
import {
  eventTile,
  FORM,
  openEventTrigger,
  openNotifyTeam,
} from "./event-trigger-po";
import { hasPython } from "./local-authoring";
import { box, boxes, clippedText, density, type Box } from "./step-geometry";
import { addCommonStep, type StepDetailsPage } from "./step-details-po";

const SIZES = [
  { tag: "1440x900", use: { viewport: { width: 1440, height: 900 } } },
  { tag: "768x1024", use: { viewport: { width: 768, height: 1024 } } },
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
const near = (actual: number, expected: number, what: string) =>
  expect(Math.abs(actual - expected), `${what}: ${actual}`).toBeLessThanOrEqual(
    0.5,
  );
/** The page never scrolls sideways. */
async function expectNoPageScroll(page: Page) {
  const [scroll, width] = await page.evaluate(() => [
    document.scrollingElement!.scrollWidth,
    innerWidth,
  ]);
  expect(scroll).toBeLessThanOrEqual(width);
}
/** A pane of step details, where the window folds the panes into tabs. */
async function pane(details: StepDetailsPage, name: string) {
  const tab = details.dialog
    .getByRole("tablist", { name: "Step details panes" })
    .getByRole("tab", { name, exact: true });
  if (await tab.count()) await tab.click();
}
const inside = (part: Box, whole: Box, what: string) => {
  expect(part.x, `${what} left`).toBeGreaterThanOrEqual(whole.x - 0.5);
  expect(part.right, `${what} right`).toBeLessThanOrEqual(whole.right + 0.5);
};
/** The focus ring around Add a workflow: 2 px, 1 px out. */
const RING = 3;
/** Where the pane shows its content: below the sticky bars, to its bottom edge. */
const paneRoom = (dialog: Locator) =>
  dialog.locator(".et-field").evaluate((field) => {
    let pane = field.parentElement!;
    while (!/auto|scroll/.test(getComputedStyle(pane).overflowY))
      pane = pane.parentElement!;
    const { top } = pane.getBoundingClientRect();
    const head = field
      .closest(".sd-pane")!
      .querySelector(".sd-head")!
      .getBoundingClientRect();
    return {
      top: Math.max(top, head.bottom),
      bottom: top + pane.clientHeight,
    };
  });
/** Add a workflow and its ring, whole: below the sticky bars and above `end`. */
async function expectWholeField(page: Page, dialog: Locator, end: number) {
  const field = await box(dialog.locator(".et-field"));
  const { top } = await paneRoom(dialog);
  expect(field.y - RING, "the ring's top").toBeGreaterThanOrEqual(top - 0.5);
  expect(field.bottom + RING, "the ring's bottom").toBeLessThanOrEqual(
    end + 0.5,
  );
  // What is typed is what is drawn there.
  expect(
    await page.evaluate(
      ([x, y]) =>
        document.elementFromPoint(x!, y!)?.closest(".et-field") !== null,
      [field.x + field.width / 2, field.y + field.height / 2],
    ),
  ).toBe(true);
}
const ONLY = {
  eventTrigger: {
    events: ["transaction.in_doubt"],
    from: ["move-between-systems", "notify-team"],
  },
};
const CURRENT = {
  id: "t-2",
  name: "notify-team-2",
  activation: "a-old",
  revision: 1,
};
const OLD = {
  id: "t-1",
  name: "notify-team",
  activation: "a-older",
  revision: 3,
};

// A 1280×720 window at 200% zoom: a sheet, under two sticky bars.
test("at 200% zoom the sheet leaves the field it belongs to in view, and scrolls", async ({
  page,
}) => {
  test.skip(!hasPython, "needs the repository's Python");
  await page.setViewportSize({ width: 640, height: 360 });
  const details = await openNotifyTeam(page, {
    spec: ONLY,
    drafts: ["move-archive"],
  });
  const dialog = await openEventTrigger(page, details);
  await pane(details, "Parameters");
  const field = dialog.getByRole("combobox", { name: "Add a workflow" });
  for (const open of [
    () => field.fill("mo"),
    async () => {
      // Closed and scrolled away, then opened with the arrow.
      await page.keyboard.press("Escape");
      await expect(page.locator(".et-list")).toBeHidden();
      await page.keyboard.press("ArrowDown");
    },
  ]) {
    await open();
    const sheet = page.locator(".et-list");
    await expect(sheet).toBeVisible();
    await expect(sheet).toHaveAttribute("data-side", "sheet");
    await expect
      .poll(async () => (await box(sheet)).y)
      .toBeGreaterThan((await box(dialog.locator(".et-field"))).bottom);
    const shape = await box(sheet);
    near(shape.bottom, 360, "the sheet's bottom");
    // Room for one suggestion at least; the rest scrolls into view.
    expect(shape.height).toBeGreaterThanOrEqual(48);
    await expectWholeField(page, dialog, shape.y);
    expect(
      await sheet.evaluate((list) => list.scrollHeight > list.clientHeight),
    ).toBe(true);
    await page.keyboard.press("ArrowUp");
    await expect(
      sheet.getByRole("option", { name: "move-archive" }),
    ).toBeInViewport({ ratio: 0.9 });
  }
});

for (const size of SIZES)
  test.describe(`Reacting to a platform event at ${size.tag}`, () => {
    test.skip(!hasPython, "needs the repository's Python");
    test.use(size.use);
    const narrow = size.use.viewport.width < 768;

    test("Add a step's entry and the tile", async ({ page }) => {
      await languageFeatures(page, ["flow.eventTrigger"], []);
      const canvas = await openNewWorkflow(page);
      await canvas.insertTarget("Add first step").click();
      const panel = new AddStepPanel(page);
      const entry = panel.option("Platform event");
      await entry.scrollIntoViewIfNeeded();
      expect((await box(entry)).height, "entry").toBeGreaterThanOrEqual(56);
      const art = await box(entry.locator(".entry-tile"));
      near(art.width, 36, "entry tile width");
      near(art.height, 36, "entry tile height");
      await panel.choose("Platform event");
      await page.getByRole("dialog", { name: /^Step details: / }).waitFor();
      await page.keyboard.press("Escape");
      // 96 units at any zoom, with its subtitle whole.
      const tile = eventTile(page);
      const body = await box(tile.locator(".tile-body"));
      near(body.width, body.height, "tile is square");
      const zoom = body.width / 96;
      expect(zoom).toBeGreaterThan(0);
      const subtitle = tile.getByText("Needs attention · any workflow");
      expect(
        await subtitle.evaluate((item) => item.scrollWidth <= item.clientWidth),
        "the tile's subtitle",
      ).toBe(true);
      await expectNoPageScroll(page);
    });

    test("Parameters: the events, their error, the workflows and the input", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page, { spec: ONLY });
      const dialog = await openEventTrigger(page, details);
      const { touch } = await density(page);
      await pane(details, "Parameters");
      const rows = await boxes(dialog.locator(".et-row"));
      expect(rows).toHaveLength(6);
      for (const row of rows)
        expect(row.height, "event row").toBeGreaterThanOrEqual(52);
      for (const tick of await boxes(
        dialog.locator(".et-row input[type=checkbox]"),
      )) {
        near(tick.width, 16, "event box width");
        near(tick.height, 16, "event box height");
      }
      // The whole row is the label: its far end ticks the box.
      const second = dialog.locator(".et-row").nth(1);
      const at = await box(second);
      await second.click({ position: { x: at.width - 6, y: at.height - 6 } });
      await expect(second.getByRole("checkbox")).toBeChecked();
      await second.getByRole("checkbox").uncheck();
      for (const card of await boxes(dialog.locator(".et-card")))
        expect(card.height, "option").toBeGreaterThanOrEqual(touch ? 44 : 36);
      for (const chip of await boxes(dialog.locator(".et-chip")))
        near(chip.height, touch ? 44 : 28, "workflow chip height");
      for (const remove of await boxes(dialog.locator(".et-remove"))) {
        near(remove.height, touch ? 44 : 24, "Remove height");
        near(remove.width, touch ? 44 : 24, "Remove width");
      }
      // The field with its border, around the text.
      const add = await box(dialog.locator(".et-field"));
      near(add.height, touch ? 44 : 28, "Add a workflow height");
      const list = await box(dialog.locator(".et-chips"));
      if (touch) near(add.width, list.width, "Add a workflow takes its line");
      else expect(add.width).toBeGreaterThanOrEqual(150);
      inside(add, list, "Add a workflow");
      near(
        (await box(dialog.locator(".et-box-head"))).height,
        36,
        "input header",
      );
      for (const row of await boxes(dialog.locator(".et-fields > li")))
        expect(row.height, "input field").toBeGreaterThanOrEqual(32);
      // No event: the error 6 px under the list, whose border turns danger.
      await dialog.locator(".et-row").first().getByRole("checkbox").uncheck();
      const problem = dialog.getByText("Choose at least one event.");
      await expect(problem).toBeVisible();
      const events = await box(dialog.locator(".et-rows"));
      near(
        (await box(dialog.locator(".et-problem").first())).y - events.bottom,
        6,
        "error gap",
      );
      await expect(problem).toHaveCSS("font-size", "13px");
      const ink = (name: string) =>
        page.evaluate((token) => {
          const probe = document.body.appendChild(
            document.createElement("span"),
          );
          probe.style.color = `var(${token})`;
          const { color } = getComputedStyle(probe);
          probe.remove();
          return color;
        }, name);
      await expect(dialog.locator(".et-rows")).toHaveCSS(
        "border-top-color",
        await ink("--danger-bd"),
      );
      expect(await clippedText(dialog)).toEqual([]);
      await expectNoPageScroll(page);
    });

    test("the suggestions: a popover under the field, or a sheet on touch", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page, { spec: ONLY });
      const dialog = await openEventTrigger(page, details);
      const { touch } = await density(page);
      await pane(details, "Parameters");
      await dialog.getByRole("combobox", { name: "Add a workflow" }).fill("mo");
      const panel = page.locator(".et-list");
      await expect(panel).toBeVisible();
      const shape = await box(panel);
      const { width, height } = page.viewportSize()!;
      if (touch) {
        near(shape.bottom, height, "the sheet's bottom");
        expect(shape.x).toBeLessThanOrEqual(0.5);
        expect(shape.right).toBeGreaterThanOrEqual(width - 0.5);
      } else near(shape.width, 320, "popover width");
      for (const option of await boxes(panel.getByRole("option")))
        expect(option.height, "suggestion").toBeGreaterThanOrEqual(
          touch ? 48 : 36,
        );
      expect(shape.x).toBeGreaterThanOrEqual(-0.5);
      expect(shape.right).toBeLessThanOrEqual(width + 0.5);
      await expectWholeField(
        page,
        dialog,
        touch ? shape.y : (await paneRoom(dialog)).bottom,
      );
      await expectNoPageScroll(page);
    });

    test("Add a workflow shows whole with its error when Only these workflows is chosen", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page);
      const dialog = await openEventTrigger(page, details);
      await pane(details, "Parameters");
      await dialog.getByRole("radio", { name: "Only these workflows" }).check();
      const field = dialog.getByRole("combobox", { name: "Add a workflow" });
      await expect(field).toBeFocused();
      const end = (await paneRoom(dialog)).bottom;
      const shows = async (problem: string) => {
        const line = dialog.locator(".et-problem").filter({ hasText: problem });
        await expect
          .poll(async () => (await box(line)).bottom)
          .toBeLessThanOrEqual(end + 0.5);
        await expectWholeField(page, dialog, end);
      };
      await shows("Add at least one workflow");
      // A name that isn't one, with two lines of what is wrong on a phone.
      await dialog.locator(".et-field").evaluate((item) => {
        const at = item.getBoundingClientRect();
        for (let up = item.parentElement; up; up = up.parentElement)
          if (/auto|scroll/.test(getComputedStyle(up).overflowY)) {
            up.scrollBy({
              top: at.bottom - up.getBoundingClientRect().bottom + 3,
            });
            break;
          }
      });
      await field.fill("not one");
      await field.press("Enter");
      await shows("“not one” isn't a workflow name.");
    });

    test("an input that refuses the event comes first, with Reset the input", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page, {
        spec: { inputSchema: FORM },
      });
      const dialog = await openEventTrigger(page, details);
      const { touch } = await density(page);
      await pane(details, "Parameters");
      const reset = dialog.getByRole("button", { name: "Reset the input" });
      await expect(reset).toBeVisible();
      near((await box(reset)).height, touch ? 44 : 32, "Reset the input");
      expect((await box(dialog.locator(".et-callout"))).bottom).toBeLessThan(
        (await box(dialog.locator(".et-rows"))).y,
      );
      expect(await clippedText(dialog)).toEqual([]);
      await expectNoPageScroll(page);
    });

    test("Test event: the event, its buttons and the sample's summary", async ({
      page,
    }) => {
      const details = await openNotifyTeam(page);
      const dialog = await openEventTrigger(page, details);
      const { touch } = await density(page);
      await pane(details, "Test event");
      const region = dialog.getByRole("region", { name: "Input data" });
      const generate = region.getByRole("button", { name: "Generate sample" });
      const paste = region.getByRole("button", {
        name: "Paste a sample event",
      });
      const control = touch ? 44 : 36;
      for (const [locator, what] of [
        [region.getByRole("combobox", { name: "Event" }), "Event"],
        [generate, "Generate sample"],
        [paste, "Paste a sample event"],
      ] as const) {
        const shape = await box(locator);
        near(shape.height, control, what);
        inside(shape, await box(region), what);
      }
      await generate.click();
      const summary = region.getByRole("group", { name: "The test event" });
      await expect(summary).toBeVisible();
      await expect(summary.getByRole("term")).toHaveCount(3);
      for (const line of await boxes(summary.locator("dl > div")))
        expect(line.height, "summary line").toBeGreaterThanOrEqual(32);
      const labels = await boxes(summary.getByRole("term"));
      expect(new Set(labels.map((label) => Math.round(label.x))).size).toBe(1);
      const values = await boxes(summary.getByRole("definition"));
      expect(new Set(values.map((value) => Math.round(value.x))).size).toBe(1);
      expect(await clippedText(dialog)).toEqual([]);
      await expectNoPageScroll(page);
    });

    test("the tabs, and the dot of a stop still owed", async ({ page }) => {
      const opened = await openConnected(page, {
        declaration: WIDER,
        seeds: [CURRENT, OLD],
        stored: "t-2",
        pendingStops: [
          { triggerId: "t-1", revision: 3, activationId: "a-older" },
        ],
      });
      opened.platform.starts.set("t-2", startedRuns(30));
      const { dialog, panel } = await showStarts(page, async (tab) => {
        if (narrow) return;
        await expect(tab).toBeVisible();
      });
      const { touch } = await density(page);
      const tabs = await boxes(
        dialog
          .getByRole("tablist", { name: "Step details sections" })
          .getByRole("tab"),
      );
      expect(tabs).toHaveLength(3);
      // As every step's tabs are.
      for (const tab of tabs)
        expect(tab.height, "tab").toBeGreaterThanOrEqual(touch ? 44 : 36);
      const dot = await startsTab(dialog).evaluate((tab) => {
        const mark = tab.querySelector<HTMLElement>(".sd-dot")!;
        const { width, height } = mark.getBoundingClientRect();
        return [width, height];
      });
      expect(dot).toEqual([8, 8]);
      const stop = await box(
        panel.locator(".es-callout").getByRole("button", { name: "Stop it" }),
      );
      near(stop.height, touch ? 44 : 32, "Stop it");
      for (const row of (await boxes(panel.locator(".es-row"))).slice(0, 8))
        expect(row.height, "start").toBeGreaterThanOrEqual(touch ? 64 : 56);
      const open = await box(
        panel.getByRole("button", { name: /^Open run/ }).first(),
      );
      near(open.height, touch ? 44 : 32, "Open run");
      const more = panel.getByRole("button", { name: "Show more" });
      near((await box(more)).height, touch ? 44 : 36, "Show more");
      near(
        (await box(panel.getByRole("button", { name: "Refresh the starts" })))
          .height,
        touch ? 44 : 32,
        "Refresh the starts",
      );
      expect(await clippedText(dialog)).toEqual([]);
      await expectNoPageScroll(page);
    });

    test("the activation dialog's rows and the question before a stop", async ({
      page,
    }) => {
      const { platform } = await openForActivation(page, {
        declaration: WIDER,
        seeds: [
          { ...STORED, mayReconcile: true },
          {
            id: "t-cli",
            name: "notify-old",
            activation: "a-older",
            events: ["transaction.completed"],
          },
        ],
      });
      platform.capabilities.add("transaction.reconcile");
      platform.starts.set("t-1", [requested("s-1"), requested("s-2")]);
      const dialog = await activationDialog(page);
      const { touch } = await density(page);
      const panel = await box(dialog);
      expect(panel.x).toBeGreaterThanOrEqual(0);
      expect(panel.right).toBeLessThanOrEqual(page.viewportSize()!.width);
      for (const row of await boxes(eventRows(dialog))) {
        expect(row.height, "row").toBeGreaterThanOrEqual(44);
        inside(row, panel, "row");
      }
      const label = await box(dialog.locator(".activation-stop-it"));
      expect(label.height, "Stop it").toBeGreaterThanOrEqual(44);
      await activate(dialog);
      const keep = dialog.getByRole("button", { name: "Keep it for now" });
      await expect(keep).toBeFocused();
      for (const button of [
        keep,
        dialog.getByRole("button", { name: "Stop now" }),
      ]) {
        const shape = await box(button);
        expect(shape.height, "answer").toBeGreaterThanOrEqual(touch ? 44 : 32);
        inside(shape, panel, "answer");
      }
      const done = await box(
        dialog.getByRole("button", { name: "Done", exact: true }),
      );
      expect(done.height, "Done").toBeGreaterThanOrEqual(touch ? 44 : 36);
      expect(await clippedText(dialog, "")).toEqual([]);
      await expectNoPageScroll(page);
    });

    test("the questions about the input and the removal", async ({ page }) => {
      await languageFeatures(page, ["flow.eventTrigger"], []);
      await openStoredWorkflow(page, {
        id: `event-geometry-${size.use.viewport.width}`,
        source: stringify({
          apiVersion: "weave/v1alpha1",
          kind: "Workflow",
          metadata: { name: "notify-team", version: "1.2.0" },
          spec: {
            inputSchema: FORM,
            outputSchema: { type: "object" },
            steps: [
              { id: "send", kind: "transform", value: { ref: "/input" } },
            ],
            output: { literal: {} },
          },
        }),
      });
      await addCommonStep(page, "Platform event", "Add a step after send");
      const { touch } = await density(page);
      const question = page.getByRole("dialog", {
        name: "Replace this workflow's input?",
      });
      const { width } = page.viewportSize()!;
      const panel = await box(question);
      expect(panel.x).toBeGreaterThanOrEqual(0);
      expect(panel.right).toBeLessThanOrEqual(width);
      const answers = await boxes(
        question.getByRole("button", { name: /the input$/ }),
      );
      expect(answers).toHaveLength(2);
      for (const answer of answers) {
        expect(answer.height, "answer").toBeGreaterThanOrEqual(touch ? 44 : 36);
        inside(answer, panel, "answer");
      }
      expect(await clippedText(question, "")).toEqual([]);
      await expectNoPageScroll(page);
    });
  });
