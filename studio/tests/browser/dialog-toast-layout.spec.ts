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
// The shared dialog frame and toasts at every density: a dialog's heading
// sits on its close button's center, a phone stacks a dialog's answers full
// width, a toast's dismiss button and action are full targets without making
// the toast taller, a toast over step details sits in their panes' corner
// inside one pane and off the focused control, and Save to file delivers its
// files even when code loaded on demand can't load.
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  addCommonStep,
  openStepFixture,
  settingsGear,
} from "./step-details-po";
import {
  box,
  boxes,
  density,
  expectAligned,
  intersects,
  showParameters,
  type Box,
} from "./step-geometry";
import { command, connected } from "./support";
import { hasPython, stopLocalAuthoring } from "./local-authoring";
import { publishDialog, withOwnedSteps } from "./owned-actions";

// Desktop with a fine pointer, then the touch density: a coarse pointer, or
// a window under 768 px.
const sizes = [
  { tag: "1440x900", width: 1440, height: 900, touch: false },
  { tag: "1280x720", width: 1280, height: 720, touch: false },
  { tag: "768x1024", width: 768, height: 1024, touch: true },
  { tag: "600x500", width: 600, height: 500, touch: false },
  { tag: "640x360", width: 640, height: 360, touch: true },
  { tag: "360x640", width: 360, height: 640, touch: true },
  { tag: "360x740", width: 360, height: 740, touch: true },
];

/** The question Studio asks on leaving Source with typed text. */
async function askAboutSource(page: Page): Promise<Locator> {
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  const source = page.getByRole("textbox", { name: "Workflow source" });
  await source.fill(
    (await source.inputValue()).replace("version: 1.0.0", "version: 1.0.1"),
  );
  await page.getByRole("tab", { name: "Designer", exact: true }).click();
  const dialog = page.getByRole("dialog", {
    name: "Apply the changes typed in Source?",
  });
  await expect(dialog).toBeVisible();
  return dialog;
}

/** How a dialog's footer sits in its panel, in CSS pixels. */
interface Footer {
  /** The footer's rule: width, style and color. */
  rule: string;
  /** The rule's color, as the --line token paints it. */
  line: string;
  /** From the panel's left, right and bottom edges to the footer's. */
  left: number;
  right: number;
  bottom: number;
  /** From the footer's top to its first answer, and from its last answer to its bottom. */
  above: number;
  below: number;
  /** From the main answer's right edge to the panel's. */
  inset: number;
}

/** Measures the footer of a dialog: its answers and the rule above them. */
async function footerOf(dialog: Locator): Promise<Footer> {
  const footer = dialog.locator(".dialog-actions, .dialog-footer").last();
  const panel = await box(dialog);
  const shown = await box(footer);
  const answers = await boxes(footer.locator(":scope > button"));
  expect(answers.length).toBeGreaterThan(0);
  const { rule, line } = await footer.evaluate((element) => {
    const style = getComputedStyle(element);
    const probe = document.createElement("span");
    probe.style.color = "var(--line)";
    element.append(probe);
    const line = getComputedStyle(probe).color;
    probe.remove();
    return {
      rule: `${style.borderTopWidth} ${style.borderTopStyle} ${style.borderTopColor}`,
      line,
    };
  });
  return {
    rule,
    line,
    left: shown.x - panel.x,
    right: panel.right - shown.right,
    bottom: panel.bottom - shown.bottom,
    above: Math.min(...answers.map((answer) => answer.y)) - shown.y,
    below: shown.bottom - Math.max(...answers.map((answer) => answer.bottom)),
    inset: panel.right - Math.max(...answers.map((answer) => answer.right)),
  };
}

/**
 * Step details for a new Call an API step, with the Undo toast GET shows on
 * removing the Body typed for POST, once it has slid into place.
 */
async function removedBodyToast(page: Page) {
  const details = await openStepFixture(page);
  await addCommonStep(page, "Call an API");
  await expect(details.dialogFor("call-api")).toBeVisible();
  const method = details
    .field("method")
    .getByRole("combobox", { name: "Method" });
  await method.click();
  await page.getByRole("option", { name: /^POST/ }).click();
  const row = details
    .field("body")
    .getByRole("textbox", { name: "Name of row 1" });
  await row.fill("note");
  await row.press("Tab");
  await method.click();
  await page.getByRole("option", { name: /^GET/ }).click();
  const toast = page.locator(".toast-region .toast");
  await expect(toast).toContainText("Removed Body.");
  await settled(toast);
  return { details, toast };
}

/** Waits until the toast has slid in and no longer moves. */
async function settled(toast: Locator) {
  await expect
    .poll(() => toast.evaluate((element) => element.getAnimations().length))
    .toBe(0);
}

/** The shown pane under the panes' bottom-left corner. */
async function cornerPane(dialog: Locator): Promise<Box> {
  const panes = await boxes(dialog.locator(".sd-panes > .sd-pane"));
  return panes.reduce((left, pane) => (pane.x < left.x ? pane : left));
}

/** A dialog's heading and close button share one vertical center. */
async function expectHeadingOnClose(dialog: Locator, what: string) {
  const header = dialog.locator(".modal-header").first();
  const heading = await box(header.locator("h2"));
  const close = await box(header.getByRole("button"));
  const line = await header
    .locator("h2")
    .evaluate((h2) => parseFloat(getComputedStyle(h2).lineHeight));
  // The first line of the heading, which a long heading wraps after.
  expectAligned(
    [heading.y + line / 2, close.middle],
    `${what}: heading and close centers`,
  );
}

test.afterAll(() => stopLocalAuthoring());

for (const size of sizes)
  test.describe(`dialogs and toasts at ${size.tag}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
    });

    test("a dialog's heading sits on its close button's center", async ({
      page,
    }) => {
      await openStepFixture(page);
      const question = await askAboutSource(page);
      await expectHeadingOnClose(question, "a question");
      await question
        .getByRole("button", { name: "Discard changes", exact: true })
        .click();
      await expect(question).toHaveCount(0);
      await settingsGear(page).click();
      const settings = page.getByRole("dialog", { name: "Workflow settings" });
      await expect(settings).toBeVisible();
      await expectHeadingOnClose(settings, "workflow settings");
    });

    test("a dialog's answers sit in a row, or stack full width on a phone", async ({
      page,
    }) => {
      await openStepFixture(page);
      const question = await askAboutSource(page);
      const message = await box(question.locator(".dialog-message"));
      const answers = await boxes(question.locator(".dialog-actions button"));
      expectAligned(
        [
          ...answers.map((answer) => answer.height),
          (await density(page)).control,
        ],
        "answer heights",
      );
      if (size.width <= 480) {
        // One column in reading order, each the message's width, 8 px apart,
        // the main answer last.
        expectAligned(
          [message.x, ...answers.map((answer) => answer.x)],
          "stacked left edge",
        );
        expectAligned(
          [message.right, ...answers.map((answer) => answer.right)],
          "stacked right edge",
        );
        for (let i = 1; i < answers.length; i++)
          expectAligned(
            [answers[i].y - answers[i - 1].bottom, 8],
            "stacked gap",
          );
      } else {
        expectAligned(
          answers.map((answer) => answer.middle),
          "answers on one row",
        );
        expectAligned(
          [answers.at(-1)!.right, message.right],
          "main answer on the right edge",
        );
      }
      await expect(
        question.locator(".dialog-actions button").last(),
      ).toHaveText("Apply changes");
    });

    test("every dialog ends in one footer: a rule across the panel, flush with its bottom", async ({
      page,
    }) => {
      const footers: [string, Footer][] = [];
      const details = await openStepFixture(page);
      // A question asked through the shared dialog.
      const question = await askAboutSource(page);
      footers.push(["Source question", await footerOf(question)]);
      await question
        .getByRole("button", { name: "Discard changes", exact: true })
        .click();
      await expect(question).toHaveCount(0);
      // The canvas's keyboard shortcuts, a sheet taller than most windows.
      await page.evaluate(() => (document.activeElement as HTMLElement).blur());
      await page.keyboard.press("?");
      const sheet = page.getByRole("dialog", { name: "Keyboard shortcuts" });
      await expect(sheet).toBeVisible();
      footers.push(["Keyboard shortcuts", await footerOf(sheet)]);
      await sheet.getByRole("button", { name: "Done", exact: true }).click();
      await expect(sheet).toHaveCount(0);
      // An example for the trigger's input fields.
      await details.trigger().dblclick();
      await showParameters(page, details.dialog);
      await details
        .field("inputSchema")
        .getByRole("button", { name: "Paste sample JSON" })
        .click();
      const example = page.getByRole("dialog", {
        name: "Find fields from an example",
      });
      footers.push(["Example", await footerOf(example)]);
      await example
        .getByRole("button", { name: "Cancel", exact: true })
        .click();
      await expect(example).toHaveCount(0);
      await details.close();
      // A step's YAML, from step details.
      await details.open("wait-for-payment");
      await details.dialog
        .getByRole("button", { name: "More actions for wait-for-payment" })
        .click();
      await page.getByRole("menuitem", { name: "Edit as YAML" }).click();
      const yaml = page.getByRole("dialog", {
        name: "Edit wait-for-payment as YAML",
      });
      footers.push(["Step YAML", await footerOf(yaml)]);
      await yaml.getByRole("button", { name: "Cancel", exact: true }).click();
      await expect(yaml).toHaveCount(0);
      await details.close();
      // A new connection, on the Connections page.
      await connected(page);
      await page.route("**/projects/project/capabilities", (route) =>
        route.fulfill({ json: { connectors: ["weave-http-v2"] } }),
      );
      await page
        .getByRole("button", { name: "Connections", exact: true })
        .click();
      await page.getByRole("button", { name: "New connection" }).click();
      const connection = page.getByRole("dialog", {
        name: "New API connection",
      });
      await expect(
        connection.getByLabel("Connection name", { exact: true }),
      ).toBeVisible();
      footers.push(["New connection", await footerOf(connection)]);
      if (hasPython) {
        // Publishing the workflow's own actions.
        await withOwnedSteps(page);
        await command(page, "Publish…");
        const publish = publishDialog(page);
        // Measured once both actions are checked and nothing is on its way.
        await expect(publish.locator(".owned-state")).toHaveText([
          "Ready",
          "Ready",
        ]);
        footers.push(["Publish", await footerOf(publish)]);
      }
      const [first] = footers;
      for (const [name, footer] of footers) {
        expect(footer.rule, `${name}: the rule`).toBe(
          `1px solid ${footer.line}`,
        );
        expectAligned(
          [footer.left, footer.right, footer.bottom, 0],
          `${name}: flush`,
        );
        expectAligned(
          [footer.above, first[1].above, 1 + 12],
          `${name}: above the answers`,
        );
        expectAligned(
          [footer.below, first[1].below, 12],
          `${name}: below the answers`,
        );
        expectAligned(
          [footer.inset, first[1].inset],
          `${name}: main answer's edge`,
        );
      }
    });

    test("a toast's dismiss button is a full target, and the toast keeps its height", async ({
      page,
    }) => {
      await openStepFixture(page);
      await command(page, "Validate");
      const toast = page.locator(".toast-region .toast");
      await expect(toast).toContainText("No problems found.");
      // Measured once it has slid into place.
      await expect
        .poll(() => toast.evaluate((element) => element.getAnimations().length))
        .toBe(0);
      const dismiss = toast.getByRole("button", {
        name: "Dismiss notification",
      });
      const button = await box(dismiss);
      const { target } = await density(page);
      expectAligned([button.width, target], "dismiss width");
      expectAligned([button.height, target], "dismiss height");
      const shown = await box(toast);
      const text = await box(toast.locator(".toast-text"));
      expectAligned([button.middle, text.middle], "dismiss and text centers");
      // The target reaches into the toast's padding, never past its edge,
      // and doesn't make the toast taller than its text.
      expect(button.y).toBeGreaterThanOrEqual(shown.y);
      expect(button.bottom).toBeLessThanOrEqual(shown.bottom);
      expect(button.right).toBeLessThanOrEqual(shown.right);
      expectAligned(
        [shown.height, Math.max(text.height, 24) + 2 * 12 + 2],
        "toast height",
      );
      await dismiss.click();
      await expect(toast).toHaveCount(0);
    });

    test("over step details, a toast sits in their panes' corner inside one pane, and its action is a full target", async ({
      page,
    }) => {
      const { details, toast } = await removedBodyToast(page);
      // Its place while focus is far from it, on Close in the top strip.
      await details.dialog
        .getByRole("button", { name: "Close", exact: true })
        .focus();
      await expect(page.locator(".toast-region")).not.toHaveClass(/\btop\b/);
      await settled(toast);
      const shown = await box(toast);
      if (size.width < 768) {
        // A phone sheet: the toast spans the sheet's content like its
        // labels, that far above the window's bottom.
        const content = (
          await box(details.field("method").locator(".param-label"))
        ).x;
        expectAligned([shown.x, content], "toast left edge");
        expectAligned([size.width - shown.right, content], "toast right edge");
        expectAligned(
          [size.height - shown.bottom, content],
          "toast bottom edge",
        );
      } else {
        // Step details cover the navigation and the diagnostics strip: the
        // toast's corner is the panes' bottom-left corner, and it never
        // reaches past the pane under that corner.
        const panes = await box(details.dialog.locator(".sd-panes"));
        const pane = await cornerPane(details.dialog);
        expectAligned([shown.x, panes.x, pane.x], "toast left edge");
        expectAligned([shown.bottom, panes.bottom], "toast bottom edge");
        expect(shown.right, "toast inside its pane").toBeLessThanOrEqual(
          pane.right + 0.5,
        );
      }
      // Undo keeps its 28 px place in the row; for touch it is a 44 px
      // target reaching into the toast's padding, never past its edge.
      const place = 28;
      const action = await box(toast.getByRole("button", { name: "Undo" }));
      const text = await box(toast.locator(".toast-text"));
      const { touch, target } = await density(page);
      expectAligned([action.height, touch ? target : place], "Undo height");
      expect(action.width).toBeGreaterThanOrEqual(touch ? target : 24);
      expectAligned([action.middle, text.middle], "Undo and text centers");
      expect(action.y).toBeGreaterThanOrEqual(shown.y);
      expect(action.bottom).toBeLessThanOrEqual(shown.bottom);
      expectAligned(
        [shown.height, Math.max(text.height, place) + 2 * 12 + 2],
        "toast height",
      );
    });

    test("over step details, a toast leaves its corner only while it would cover the focused control", async ({
      page,
    }) => {
      test.skip(size.width < 768, "A phone sheet has its own check.");
      const { details, toast } = await removedBodyToast(page);
      // Where the toast sits while focus is elsewhere: the panes' corner.
      const corner = await box(toast);
      // Every control in the toast's pane: Input's rows beside Parameters,
      // or Parameters' own controls where Parameters is the corner's pane.
      const pane = await cornerPane(details.dialog);
      const controls = details.dialog.locator(
        pane.x === (await box(details.dialog.locator(".sd-input"))).x
          ? ".sd-input .data-row"
          : ".sd-tabpanel:not([hidden]) :is(input, button, [role=combobox])",
      );
      const count = await controls.count();
      expect(count).toBeGreaterThan(0);
      for (let i = 0; i < count; i++) {
        const control = controls.nth(i);
        if (!(await control.isVisible())) continue;
        await control.focus();
        await page.evaluate(
          () =>
            new Promise((done) =>
              requestAnimationFrame(() => requestAnimationFrame(done)),
            ),
        );
        const focused = await box(control);
        const now = await box(toast);
        expect(
          intersects(now, focused),
          `toast ${Math.round(now.y)}–${Math.round(now.bottom)} over control ${i} at ${Math.round(focused.y)}–${Math.round(focused.bottom)}`,
        ).toBe(false);
        // It stays in the corner unless the corner would cover the control.
        if (!intersects(corner, focused))
          expectAligned([now.bottom, corner.bottom], `toast for control ${i}`);
      }
    });
  });

test.describe("Save to file", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("downloads the workflow even when code it loads on demand can't load", async ({
    page,
  }) => {
    await openStepFixture(page);
    // The code that words the note about unpublished actions fails to
    // load, as after Studio was updated on the server.
    let failed = () => undefined as void;
    const chunkFailed = new Promise<void>((done) => (failed = done));
    await page.route(/\/chunk-[^/]+\.js(?:\?.*)?$/, async (route) => {
      try {
        const response = await route.fetch();
        const body = await response.text();
        if (!body.includes("Open the workflow in the designer to publish"))
          return await route.fulfill({ response, body });
        failed();
        return await route.abort();
      } catch {
        // The page closed while a chunk was on its way.
      }
    });
    const download = page.waitForEvent("download");
    await command(page, "Save to file");
    expect((await download).suggestedFilename()).toBe("order-intake.yaml");
    // The note's code did fail to load.
    await chunkFailed;
  });
});
