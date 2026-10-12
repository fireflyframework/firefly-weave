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
// the toast taller, a toast over step details lines up with them, and Save
// to file delivers its files even when code loaded on demand can't load.
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  addCommonStep,
  openStepFixture,
  settingsGear,
} from "./step-details-po";
import { box, boxes, density, expectAligned } from "./step-geometry";
import { command } from "./support";

// Desktop with a fine pointer, then the touch density: a coarse pointer, or
// a window under 768 px.
const sizes = [
  { tag: "1440x900", width: 1440, height: 900, touch: false },
  { tag: "1280x720", width: 1280, height: 720, touch: false },
  { tag: "768x1024", width: 768, height: 1024, touch: true },
  { tag: "600x500", width: 600, height: 500, touch: false },
  { tag: "640x360", width: 640, height: 360, touch: true },
  { tag: "360x640", width: 360, height: 640, touch: true },
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

    test("over step details, a toast lines up with them, and its action is a full target", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await addCommonStep(page, "Call an API");
      await expect(details.dialogFor("call-api")).toBeVisible();
      // GET removes the Body typed for POST, with an Undo toast.
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
      await expect
        .poll(() => toast.evaluate((element) => element.getAnimations().length))
        .toBe(0);
      const shown = await box(toast);
      // Step details cover the navigation: the toast starts at their panes'
      // outer edge, or spans a phone sheet's content like its labels.
      if (size.width < 768) {
        const content = (
          await box(details.field("method").locator(".param-label"))
        ).x;
        expectAligned([shown.x, content], "toast left edge");
        expectAligned([size.width - shown.right, content], "toast right edge");
      } else
        expectAligned(
          [shown.x, (await box(details.dialog.locator(".sd-panes"))).x],
          "toast left edge",
        );
      // Undo keeps its 28 px place in the row; for touch it is a 44 px
      // target reaching into the toast's padding, never past its edge.
      const place = 28;
      const action = await box(toast.getByRole("button", { name: "Undo" }));
      const text = await box(toast.locator(".toast-text"));
      const { touch, target } = await density(page);
      expectAligned([action.height, touch ? target : place], "Undo height");
      expectAligned([action.middle, text.middle], "Undo and text centers");
      expect(action.y).toBeGreaterThanOrEqual(shown.y);
      expect(action.bottom).toBeLessThanOrEqual(shown.bottom);
      expectAligned(
        [shown.height, Math.max(text.height, place) + 2 * 12 + 2],
        "toast height",
      );
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
