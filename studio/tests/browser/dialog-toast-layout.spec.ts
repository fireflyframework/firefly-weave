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
// width, only a dialog's own footer holds on to its bottom edge, so a focused
// button in its content is never under it, a toast's dismiss button and
// action are full targets without making the toast taller, a toast over step
// details sits in their panes' corner inside one pane and off the focused
// control, and Save to file delivers its files even when code loaded on
// demand can't load.
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  addCommonStep,
  openStepFixture,
  settingsGear,
  stepFixture,
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
import { command, connected, offline, withClassicDesigner } from "./support";
import { hasPython, stopLocalAuthoring } from "./local-authoring";
import { publishDialog, withOwnedSteps } from "./owned-actions";
import { openActivation, platform } from "./activation-po";

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
  // One footer: nothing else in the panel holds on to its bottom edge.
  const held = await dialog.evaluate((panel) =>
    [...panel.querySelectorAll("*")]
      .filter((element) => {
        const style = getComputedStyle(element);
        return style.position === "sticky" && style.bottom !== "auto";
      })
      .map((element) => element.textContent?.trim()),
  );
  expect(held, "rows held at the panel's bottom").toEqual([
    (await footer.textContent())?.trim(),
  ]);
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

/**
 * The activation dialog of a version whose export Studio can't read: its
 * worker releases and assignment bindings can be added by hand.
 */
async function activationWithAddedPins(page: Page): Promise<Locator> {
  const captured = await platform(page, { exportStatus: 404 });
  const dialog = await openActivation(page, captured.contracts);
  await expect(dialog).toContainText(
    "Studio couldn't read this version's requirements",
  );
  await expect(
    dialog.getByRole("button", { name: "Add worker release" }),
  ).toBeAttached();
  // Measured once its content has stopped growing.
  await expect
    .poll(() =>
      dialog.evaluate(async (panel) => {
        const before = panel.scrollHeight;
        await new Promise((done) =>
          requestAnimationFrame(() => requestAnimationFrame(done)),
        );
        return panel.scrollHeight === before;
      }),
    )
    .toBe(true);
  return dialog;
}

/**
 * Focuses every control of a dialog in turn, each from the top of its
 * scroll, and names those that end up under its title or its footer, or
 * whose center is covered by something else. Answers in the footer only
 * need to be reachable.
 */
async function controlsUnderFooter(dialog: Locator): Promise<string[]> {
  return dialog.evaluate(async (panel) => {
    const frame = () =>
      new Promise((done) =>
        requestAnimationFrame(() => requestAnimationFrame(done)),
      );
    const footer = [
      ...panel.querySelectorAll<HTMLElement>(".dialog-actions, .dialog-footer"),
    ].at(-1)!;
    const header = panel.querySelector<HTMLElement>(".modal-header")!;
    const controls = [
      ...panel.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])',
      ),
    ].filter(
      (control) => !header.contains(control) && control.getClientRects().length,
    );
    const problems: string[] = [];
    for (const control of controls) {
      panel.scrollTo({ top: 0 });
      await frame();
      control.focus();
      await frame();
      const name =
        control.getAttribute("aria-label") ||
        control.labels?.[0]?.textContent?.trim() ||
        control.textContent?.trim() ||
        control.tagName;
      const r = control.getBoundingClientRect();
      const top = document.elementFromPoint(
        r.x + r.width / 2,
        r.y + r.height / 2,
      );
      if (!top || !control.contains(top))
        problems.push(`${name}: covered by ${top?.className || "nothing"}`);
      if (footer.contains(control)) continue;
      const under = footer.getBoundingClientRect().top;
      const below = header.getBoundingClientRect().bottom;
      if (r.bottom > under + 0.5)
        problems.push(
          `${name}: bottom ${r.bottom} under the footer at ${under}`,
        );
      if (r.top < below - 0.5)
        problems.push(`${name}: top ${r.top} under the title at ${below}`);
    }
    return problems;
  });
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
      // Activating a version whose requirements Studio can't read, so its
      // pins can be added by hand above the footer.
      const activation = await activationWithAddedPins(page);
      footers.push(["Activation", await footerOf(activation)]);
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

    test("a focused button in a dialog's content is never under its footer", async ({
      page,
    }) => {
      test.skip(
        !["1440x900", "600x500", "360x640"].includes(size.tag),
        "measured on a desktop, a short window and a phone",
      );
      const dialog = await activationWithAddedPins(page);
      const footer = dialog.locator(".dialog-footer");
      for (const name of ["Add worker release", "Add assignment binding"]) {
        await dialog.evaluate((panel) => panel.scrollTo({ top: 0 }));
        const add = dialog.getByRole("button", { name });
        await add.focus();
        await expect(add).toBeFocused();
        await expect(add).toBeVisible();
        // Focus scrolls the button into the panel's free band, between the
        // title and the footer, and what is under its center is the button.
        const shown = await box(add);
        expect(shown.bottom, `${name} above the footer`).toBeLessThanOrEqual(
          (await box(footer)).y + 0.5,
        );
        expect(
          await add.evaluate((button) => {
            const r = button.getBoundingClientRect();
            const top = document.elementFromPoint(
              r.x + r.width / 2,
              r.y + r.height / 2,
            );
            return !!top && button.contains(top);
          }),
          `${name} not covered`,
        ).toBe(true);
      }
      // A click reaches it: the new row takes focus.
      await dialog.evaluate((panel) => panel.scrollTo({ top: 0 }));
      await dialog.getByRole("button", { name: "Add worker release" }).click();
      await expect(
        dialog.getByLabel("Worker task type", { exact: true }),
      ).toBeFocused();
    });

    test("on a phone, every control in a dialog scrolls clear of its stacked footer", async ({
      page,
    }) => {
      test.skip(
        !["360x640", "640x360"].includes(size.tag),
        "measured on a phone, upright and on its side",
      );
      const dialog = await activationWithAddedPins(page);
      expect(await controlsUnderFooter(dialog), "activation").toEqual([]);
      // Again with a row of each kind added by hand.
      for (const [name, field] of [
        ["Add worker release", "Worker task type"],
        ["Add assignment binding", "Assignment name"],
      ]) {
        await dialog.getByRole("button", { name }).click();
        await expect(dialog.getByLabel(field, { exact: true })).toBeFocused();
      }
      expect(
        await controlsUnderFooter(dialog),
        "activation with added rows",
      ).toEqual([]);
      await openStepFixture(page);
      const question = await askAboutSource(page);
      expect(await controlsUnderFooter(question), "Source question").toEqual(
        [],
      );
    });

    test("the activation dialog's Add buttons sit on one row or stack full width", async ({
      page,
    }) => {
      const dialog = await activationWithAddedPins(page);
      const adds = await boxes(
        dialog.getByRole("button", { name: /^Add (worker|assignment)/ }),
      );
      expect(adds).toHaveLength(2);
      if (Math.abs(adds[0].middle - adds[1].middle) < 1) return;
      // Stacked: each as wide as the content, 8 px apart, in reading order.
      const content = await box(dialog.locator("weave-activation-pins"));
      expectAligned(
        [content.x, ...adds.map((add) => add.x)],
        "stacked left edge",
      );
      expectAligned(
        [content.right, ...adds.map((add) => add.right)],
        "stacked right edge",
      );
      expectAligned([adds[1].y - adds[0].bottom, 8], "stacked gap");
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
      // Undo's place in the row is a pointer target, 24 px like the dismiss
      // button; for touch it is a 44 px target reaching into the toast's
      // padding, never past its edge.
      const place = 24;
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

    test("on a phone sheet of step details, a toast never covers the focused field", async ({
      page,
    }) => {
      test.skip(size.width >= 768, "Panes have their own check.");
      const details = await openStepFixture(page);
      await details.openWithKeyboard("notify-sales");
      // A toast raised in step details: the rename's, with its Undo, on
      // two lines, so it may cover a field in either of its places.
      const name =
        "notify-sales-team-of-the-regional-office-about-every-new-order";
      await details.dialog
        .getByRole("button", { name: "Rename notify-sales" })
        .click();
      await details.dialog
        .getByRole("textbox", { name: "Step name" })
        .fill(name);
      await page.keyboard.press("Enter");
      const toast = page.locator(".toast-region .toast");
      await expect(toast).toContainText(name);
      await settled(toast);
      const text = await box(toast.locator(".toast-text"));
      expect(text.height, "toast text on two lines").toBeGreaterThan(30);
      const fields = details.dialog
        .locator(
          ".sd-tabpanel:not([hidden]) .param-form :is(input, textarea, [role=combobox])",
        )
        .filter({ visible: true });
      const count = await fields.count();
      expect(count).toBeGreaterThan(1);
      for (let i = 0; i < count; i++) {
        const field = fields.nth(i);
        await field.focus();
        await page.evaluate(
          () =>
            new Promise((done) =>
              requestAnimationFrame(() => requestAnimationFrame(done)),
            ),
        );
        const focused = await box(field);
        const now = await box(toast);
        expect(
          intersects(now, focused),
          `toast ${now.y}–${now.bottom} over field ${i} at ${focused.y}–${focused.bottom}`,
        ).toBe(false);
        // The field stays in the window.
        expect(focused.y, `field ${i} top`).toBeGreaterThanOrEqual(0);
        expect(focused.bottom, `field ${i} bottom`).toBeLessThanOrEqual(
          size.height,
        );
      }
    });
  });

// A dialog's answers sit on one row, or each on its own full-width row:
// never two on one row and one under them, at any window width.
for (const touch of [false, true])
  test.describe(`answers as the window narrows, ${touch ? "touch" : "fine pointer"}`, () => {
    test.use({ viewport: { width: 900, height: 800 }, hasTouch: touch });

    test("stay on one row or stack, never in uneven rows", async ({ page }) => {
      await openStepFixture(page);
      /** Narrows the window a pixel at a time with the dialog open. */
      const narrow = async (name: string, dialog: Locator) => {
        await expect(dialog).toBeVisible();
        const answers = dialog.locator(".dialog-actions button");
        for (let width = 900; width >= 320; width -= 1) {
          await page.setViewportSize({ width, height: 800 });
          const shown = await boxes(answers);
          const rows = new Set(shown.map((answer) => Math.round(answer.y)));
          if (rows.size === 1) continue;
          // Stacked: one answer per row, all as wide as each other.
          expect(rows.size, `${name} at ${width} px: rows`).toBe(shown.length);
          expectAligned(
            shown.map((answer) => answer.width),
            `${name} at ${width} px: stacked widths`,
          );
        }
        await page.setViewportSize({ width: 900, height: 800 });
      };
      // Three answers: the question about text typed in Source.
      const question = await askAboutSource(page);
      await narrow("Source question", question);
      await question
        .getByRole("button", { name: "Keep editing", exact: true })
        .click();
      // Two answers: converting the source to JSON, once nothing is typed.
      const source = page.getByRole("textbox", { name: "Workflow source" });
      await source.fill(
        (await source.inputValue()).replace("version: 1.0.1", "version: 1.0.0"),
      );
      await page
        .getByRole("combobox", { name: "Source format" })
        .selectOption("json");
      await narrow(
        "Convert to JSON?",
        page.getByRole("dialog", { name: "Convert to JSON?" }),
      );
    });
  });

test.describe("Save to file", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("downloads the workflow even when code it loads on demand can't load", async ({
    page,
  }) => {
    // Only the classic editor words that note: the new editor's canvas file
    // keeps the steps' action settings.
    await withClassicDesigner(page);
    await offline(page);
    await page.getByLabel("Choose a workflow file").setInputFiles(stepFixture);
    await expect(page.locator(".editor-bar")).toBeVisible();
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
