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
// Text typed in Source that isn't applied yet: leaving Source any way
// (another tab, a step's details, another page, the browser's Back) asks
// first, and so does an edit or a command started from Source. Apply
// changes applies the text as the button beside it does, Discard changes
// returns to the applied source, and Keep editing (or Escape) stays with the
// caret where it was. Nothing else ever edits the workflow under it.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { openStepFixture } from "./step-details-po";
import { box, boxes, clippedText, expectAligned } from "./step-geometry";
import { parse } from "yaml";
import { command, sourceText } from "./support";

const QUESTION = "Apply the changes typed in Source?";
/** The text selected in Source, from its end back to its start. */
const CARET = 42;
const SELECTED_TO = 58;
/** How far Source is scrolled down while the question is asked. */
let scrolled = 0;

/** The question asked on leaving Source with text that isn't applied. */
const question = (page: Page): Locator =>
  page.getByRole("dialog", { name: QUESTION });
const sourceBox = (page: Page): Locator =>
  page.getByRole("textbox", { name: "Workflow source" });
const viewTab = (page: Page, name: string): Locator =>
  page.getByRole("tab", { name, exact: true });

/**
 * Types in Source without applying: the workflow's version becomes 9.9.9,
 * or the text stops parsing. A few characters in the middle of the text are
 * left selected backward, and the text is scrolled down when it can be.
 */
async function typeInSource(page: Page, parses = true) {
  await viewTab(page, "Source").click();
  const source = sourceBox(page);
  const original = await source.inputValue();
  const typed = parses
    ? original.replace("version: 1.0.0", "version: 9.9.9")
    : `${original}\n  broken: [`;
  expect(typed).not.toBe(original);
  await source.fill(typed);
  scrolled = await source.evaluate(
    (element: HTMLTextAreaElement, [from, to]) => {
      element.setSelectionRange(from, to, "backward");
      element.scrollTop = Math.min(
        40,
        element.scrollHeight - element.clientHeight,
      );
      return element.scrollTop;
    },
    [CARET, SELECTED_TO],
  );
  expect(scrolled, "Source scrolled down").toBeGreaterThan(0);
  return { source, original, typed };
}

/** The workflow's version as the applied source says it. */
async function appliedVersion(page: Page): Promise<string> {
  const text = await sourceText(page);
  return /version: ([\d.]+)/.exec(text)?.[1] ?? "";
}

/** Answers the question by its button, or by Escape. */
async function answer(page: Page, choice: Choice) {
  const dialog = question(page);
  await expect(dialog).toBeVisible();
  if (choice === "Escape") await page.keyboard.press("Escape");
  else await dialog.getByRole("button", { name: choice, exact: true }).click();
  await expect(dialog).toHaveCount(0);
}

/** Still in Source, with the text as typed, selected and scrolled as it was. */
async function stillTyping(page: Page, typed: string) {
  await expect(viewTab(page, "Source")).toHaveAttribute(
    "aria-selected",
    "true",
  );
  const source = sourceBox(page);
  await expect(source).toHaveValue(typed);
  await expect(source).toBeFocused();
  expect(
    await source.evaluate((element: HTMLTextAreaElement) => [
      element.selectionStart,
      element.selectionEnd,
      element.selectionDirection,
      element.scrollTop,
    ]),
  ).toEqual([CARET, SELECTED_TO, "backward", scrolled]);
}

type Choice = "Apply changes" | "Discard changes" | "Keep editing" | "Escape";

/** Makes the next check report one error on check-customer's value. */
async function reportOnCheckCustomer(page: Page) {
  await page.route("**/studio/local/validate", (route) =>
    route.fulfill({
      json: {
        validationOk: false,
        errorCount: 1,
        diagnostics: [
          {
            code: "WV-COMP-TYPE_MISMATCH",
            severity: "error",
            message: "The value doesn't fit the step's output",
            path: "/spec/steps/0/value",
          },
        ],
        partial: true,
      },
    }),
  );
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  const dismiss = page.getByRole("button", { name: "Dismiss notification" });
  await expect(dismiss.first()).toBeVisible();
  await dismiss.first().click();
}
const problemsStrip = (page: Page): Locator =>
  page.getByRole("region", { name: "Compiler diagnostics" });

/**
 * The ways to leave Source: each starts leaving, then checks where the
 * person is once leaving went on, and reads the workflow's version there.
 */
const paths: {
  name: string;
  prepare?: (page: Page) => Promise<void>;
  leave: (page: Page) => Promise<void>;
  left: (page: Page, choice: Choice) => Promise<string>;
}[] = [
  {
    name: "the Designer tab",
    leave: (page) => viewTab(page, "Designer").click(),
    left: async (page) => {
      await expect(viewTab(page, "Designer")).toHaveAttribute(
        "aria-selected",
        "true",
      );
      return appliedVersion(page);
    },
  },
  {
    name: "a problem's step details",
    prepare: reportOnCheckCustomer,
    leave: async (page) => {
      const strip = problemsStrip(page);
      const toggle = strip.getByRole("button", { name: "Diagnostics" });
      if ((await toggle.getAttribute("aria-expanded")) !== "true")
        await toggle.click();
      await strip.locator(".diagnostic-row .diagnostic-where").first().click();
    },
    left: async (page) => {
      const details = page.getByRole("dialog", {
        name: /^Step details: check-customer/,
      });
      await expect(details).toBeVisible();
      await details.getByRole("button", { name: "Close", exact: true }).click();
      await expect(details).toHaveCount(0);
      return appliedVersion(page);
    },
  },
  {
    name: "another page",
    leave: (page) =>
      page.getByRole("button", { name: "Home", exact: true }).click(),
    left: async (page, choice) => {
      // Applied text is an edit kept on this computer: leaving says so.
      if (choice === "Apply changes")
        await page.getByRole("button", { name: "Leave", exact: true }).click();
      await expect(page.getByLabel("Choose a workflow file")).toBeAttached();
      await page.goBack();
      await expect(page.locator(".editor-bar")).toBeVisible();
      return appliedVersion(page);
    },
  },
  {
    name: "Connect to a platform",
    leave: async (page) => {
      await page
        .getByRole("button", { name: "Platform: Local authoring" })
        .click();
      await page
        .getByRole("button", { name: "Connect to a platform", exact: true })
        .click();
    },
    left: async (page) => {
      await expect(page.locator("#wizard-heading")).toHaveText(
        "Connect to a platform",
      );
      await page.goBack();
      await expect(page.locator(".editor-bar")).toBeVisible();
      return appliedVersion(page);
    },
  },
  {
    name: "the browser's Back",
    leave: (page) => page.evaluate(() => history.back()),
    left: async (page) => {
      await expect(page.getByLabel("Choose a workflow file")).toBeAttached();
      await expect(page.locator(".editor-bar")).toHaveCount(0);
      await page.goForward();
      await expect(page.locator(".editor-bar")).toBeVisible();
      return appliedVersion(page);
    },
  },
];

test.describe("leaving Source with text that isn't applied", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  for (const path of paths)
    for (const choice of [
      "Apply changes",
      "Discard changes",
      "Keep editing",
    ] as const)
      test(`by ${path.name}: ${choice}`, async ({ page }) => {
        await openStepFixture(page);
        await path.prepare?.(page);
        const { typed } = await typeInSource(page);
        const url = page.url();
        await path.leave(page);
        await answer(page, choice);
        if (choice === "Keep editing") {
          await stillTyping(page, typed);
          expect(page.url()).toBe(url);
          await expect(page.getByRole("dialog")).toHaveCount(0);
          return;
        }
        expect(await path.left(page, choice)).toBe(
          choice === "Apply changes" ? "9.9.9" : "1.0.0",
        );
      });

  test("Escape keeps editing", async ({ page }) => {
    await openStepFixture(page);
    const { typed } = await typeInSource(page);
    await viewTab(page, "Outline").click();
    await answer(page, "Escape");
    await stillTyping(page, typed);
  });

  test("the arrow keys between the views ask too", async ({ page }) => {
    await openStepFixture(page);
    const { typed } = await typeInSource(page);
    await viewTab(page, "Source").focus();
    await page.keyboard.press("ArrowRight");
    await answer(page, "Keep editing");
    await stillTyping(page, typed);
    await viewTab(page, "Source").focus();
    await page.keyboard.press("ArrowRight");
    await answer(page, "Discard changes");
    await expect(viewTab(page, "Outline")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(viewTab(page, "Outline")).toBeFocused();
  });

  test("text that doesn't parse stays in Source with its problem", async ({
    page,
  }) => {
    await openStepFixture(page);
    const { typed } = await typeInSource(page, false);
    await viewTab(page, "Designer").click();
    await answer(page, "Apply changes");
    await expect(page.locator(".source-error")).toBeVisible();
    await stillTyping(page, typed);
  });

  test("nothing typed, or a source error already applied, leaves without asking", async ({
    page,
  }) => {
    await openStepFixture(page);
    await viewTab(page, "Source").click();
    await viewTab(page, "Designer").click();
    await expect(viewTab(page, "Designer")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await typeInSource(page, false);
    await page
      .getByRole("button", { name: "Apply changes", exact: true })
      .click();
    await expect(page.locator(".source-error")).toBeVisible();
    await viewTab(page, "Designer").click();
    await expect(viewTab(page, "Designer")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(question(page)).toHaveCount(0);
  });

  test("an edit or a command started from Source asks first", async ({
    page,
  }) => {
    await openStepFixture(page);
    const { typed } = await typeInSource(page);
    // A command: Keep editing validates nothing.
    const toast = page.locator(".toast-region");
    await page.getByRole("button", { name: "Validate", exact: true }).click();
    await answer(page, "Keep editing");
    await stillTyping(page, typed);
    await expect(toast).not.toContainText("No problems found.");
    // An edit: Undo applies nothing and undoes nothing until answered.
    const before = await page.evaluate(() => location.href);
    await page
      .getByRole("toolbar", { name: "Workflow commands" })
      .getByRole("button", { name: "Undo", exact: true })
      .click();
    await answer(page, "Keep editing");
    await stillTyping(page, typed);
    expect(page.url()).toBe(before);
    // Applied, the command goes on with the text in the workflow.
    await page.getByRole("button", { name: "Validate", exact: true }).click();
    await answer(page, "Apply changes");
    await expect(toast).toContainText("No problems found.");
    expect(await appliedVersion(page)).toBe("9.9.9");
  });

  test("Undo from Source settles the text first, then undoes", async ({
    page,
  }) => {
    await openStepFixture(page);
    // One edit to undo: the version, applied from Source.
    await typeInSource(page);
    await page
      .getByRole("button", { name: "Apply changes", exact: true })
      .click();
    const source = sourceBox(page);
    const applied = await source.inputValue();
    await source.fill(applied.replace("name: order-intake", "name: typed"));
    await page
      .getByRole("toolbar", { name: "Workflow commands" })
      .getByRole("button", { name: "Undo", exact: true })
      .click();
    await answer(page, "Discard changes");
    // The typed name is gone, and Undo took back the applied version.
    await expect(source).toHaveValue(/version: 1\.0\.0/);
    await expect(source).not.toHaveValue(/name: typed/);
  });

  test("a new source format asks first, and Keep editing keeps the format", async ({
    page,
  }) => {
    await openStepFixture(page);
    const { typed } = await typeInSource(page);
    const format = page.getByRole("combobox", { name: "Source format" });
    await format.selectOption("json");
    await answer(page, "Keep editing");
    await expect(format).toHaveValue("yaml");
    await stillTyping(page, typed);
    await format.selectOption("json");
    await answer(page, "Apply changes");
    // JSON can't keep the file's comments: that question comes next.
    await page
      .getByRole("dialog", { name: "Convert to JSON?" })
      .getByRole("button", { name: "Convert and remove comments" })
      .click();
    await expect(format).toHaveValue("json");
    await expect(sourceBox(page)).toHaveValue(/"version": "9\.9\.9"/);
  });

  test("closing or reloading the window asks while text isn't applied", async ({
    page,
  }) => {
    await openStepFixture(page);
    await typeInSource(page);
    const asked = new Promise<string>((done) => {
      page.once("dialog", (dialog) => {
        done(dialog.type());
        void dialog.dismiss();
      });
      page.once("close", () => done("closed without asking"));
    });
    await page.close({ runBeforeUnload: true });
    expect(await asked).toBe("beforeunload");
  });

  test("the question starts on Keep editing and keeps Tab inside", async ({
    page,
  }) => {
    await openStepFixture(page);
    await typeInSource(page);
    await viewTab(page, "Designer").click();
    const dialog = question(page);
    await expect(
      dialog.getByRole("button", { name: "Keep editing", exact: true }),
    ).toBeFocused();
    const names: string[] = [];
    for (let i = 0; i < 4; i++) {
      await page.keyboard.press("Tab");
      names.push(
        await page.evaluate(
          () =>
            (document.activeElement as HTMLElement | null)?.getAttribute(
              "aria-label",
            ) ??
            document.activeElement?.textContent?.trim() ??
            "",
        ),
      );
    }
    expect(names).toEqual([
      "Discard changes",
      "Apply changes",
      "Close dialog",
      "Keep editing",
    ]);
    await page.keyboard.press("Shift+Tab");
    await expect(
      dialog.getByRole("button", { name: "Close dialog" }),
    ).toBeFocused();
    await expect(dialog).toContainText(
      "They aren't in the workflow yet. Discard them to go back to the workflow as it is.",
    );
  });
});

// A toast's Undo pressed from Source with typed text asks the same
// question, and says only what happened: Keep editing restores nothing and
// says nothing, Discard restores the step, and Apply keeps the applied text
// and says the step can't come back from there.
test.describe("a toast's Undo from Source with typed text", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  for (const choice of [
    "Keep editing",
    "Discard changes",
    "Apply changes",
  ] as const)
    test(`asks first: ${choice}`, async ({ page }) => {
      const details = await openStepFixture(page);
      await details.node("reject-order").focus();
      await page.keyboard.press("Delete");
      const toast = page.locator(".toast-region");
      await expect(toast).toContainText("Deleted reject-order.");
      const { typed } = await typeInSource(page);
      await toast.getByRole("button", { name: "Undo", exact: true }).click();
      await answer(page, choice);
      const steps = async () => parse(await sourceText(page)).spec.steps;
      const ids = async () =>
        (await steps()).map((step: { id: string }) => step.id);
      if (choice === "Keep editing") {
        await stillTyping(page, typed);
        await expect(toast).not.toContainText("Restored");
        expect(await ids()).not.toContain("reject-order");
        return;
      }
      if (choice === "Discard changes") {
        await expect(toast).toContainText("Restored reject-order.");
        expect(await ids()).toContain("reject-order");
        expect(await appliedVersion(page)).toBe("1.0.0");
        return;
      }
      // Applied: the typed text stays, the step stays deleted, and the
      // toast says so.
      await expect(toast).toContainText(
        "reject-order can't come back from here: the workflow changed since. Use Undo in the toolbar.",
      );
      await expect(toast).not.toContainText("Restored");
      expect(await appliedVersion(page)).toBe("9.9.9");
      expect(await ids()).not.toContain("reject-order");
    });
});

// Back pressed again while the question is open, the way a phone's back
// gesture tries to close a dialog: the question stays, and once answered
// Studio goes back one entry, not one per press.
for (const size of [
  { tag: "1440x900", width: 1440, height: 900, touch: false },
  { tag: "360x640", width: 360, height: 640, touch: true },
])
  test.describe(`Back pressed while asking at ${size.tag}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
    });

    for (const choice of ["Discard changes", "Apply changes"] as const)
      test(`goes back one entry: ${choice}`, async ({ page }) => {
        await openStepFixture(page);
        await typeInSource(page);
        const designer = page.url();
        await page.evaluate(() => history.back());
        await expect(question(page)).toBeVisible();
        await expect.poll(() => page.url()).toBe(designer);
        await page.evaluate(() => history.back());
        // Taken back again, and still one question.
        await expect.poll(() => page.url()).toBe(designer);
        await expect(page.getByRole("dialog")).toHaveCount(1);
        await answer(page, choice);
        await expect(page.getByLabel("Choose a workflow file")).toBeAttached();
        const home = page.url();
        expect(home).toMatch(/\/home$/);
        // No second move follows: Studio stays one entry back.
        await page.waitForTimeout(500);
        expect(page.url()).toBe(home);
        await page.goForward();
        await expect(page.locator(".editor-bar")).toBeVisible();
        expect(page.url()).toBe(designer);
      });
  });

// The question's layout: one left edge and one right edge, answers of the
// density's height inside them, in one row on a desktop with the main one
// at the right edge.
for (const size of [
  { tag: "1440x900", width: 1440, height: 900, touch: false },
  { tag: "360x640", width: 360, height: 640, touch: true },
])
  test.describe(`the question at ${size.tag}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
    });

    test("lines up and fits the window", async ({ page }) => {
      await openStepFixture(page);
      await typeInSource(page);
      await viewTab(page, "Designer").click();
      const dialog = question(page);
      await expect(dialog).toBeVisible();
      const panel = await box(dialog);
      expect(panel.x).toBeGreaterThanOrEqual(0);
      expect(panel.right).toBeLessThanOrEqual(size.width);
      expect(panel.bottom).toBeLessThanOrEqual(size.height);
      const heading = await box(
        dialog.getByRole("heading", { name: QUESTION }),
      );
      const close = await box(
        dialog.getByRole("button", { name: "Close dialog" }),
      );
      const message = await box(dialog.locator(".dialog-message"));
      const buttons = await boxes(dialog.locator(".dialog-actions button"));
      expect(buttons).toHaveLength(3);
      expectAligned([heading.x, message.x], "left edge");
      expectAligned([close.right, message.right], "right edge");
      const target = size.touch ? 44 : 36;
      expectAligned(
        [...buttons.map((b) => b.height), target],
        "answer heights",
      );
      for (const answer of buttons) {
        expect(answer.x).toBeGreaterThanOrEqual(message.x - 1);
        expect(answer.right).toBeLessThanOrEqual(message.right + 1);
      }
      if (!size.touch) {
        expectAligned([heading.middle, close.middle], "heading and close");
        expectAligned(
          buttons.map((b) => b.middle),
          "answers on one row",
        );
        expectAligned(
          [buttons[2].right, message.right],
          "main answer at the right edge",
        );
      }
      expect(await clippedText(dialog, "")).toEqual([]);
      // The message's last line holds more than its last word.
      const lastTwo = await dialog
        .locator(".dialog-message")
        .evaluate((message) => {
          const node = message.firstChild!;
          const text = node.textContent!.trimEnd();
          const last = text.lastIndexOf(" ");
          const before = text.lastIndexOf(" ", last - 1);
          const top = (from: number, to: number) => {
            const range = document.createRange();
            range.setStart(node, from);
            range.setEnd(node, to);
            return range.getClientRects()[0].top;
          };
          return [top(before + 1, last), top(last + 1, text.length)];
        });
      expectAligned(lastTwo, "the message's last two words on one line");
    });
  });
