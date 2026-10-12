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
// Step details for keyboard and assistive technology: every control has a
// name, a visible focus ring and a 24 px target; toggles, Add option and
// rows are named; drops are announced.
import { expect, test } from "@playwright/test";
import { openStepFixture, sizes } from "./step-details-po";
import { walkFocus } from "./step-focus";
import { formScroller, visibleArea } from "./step-geometry";

for (const size of sizes)
  test.describe(`step details accessibility at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    const sheet = size.width < 768;

    test("every control in step details has a name, a visible focus ring and a 24 px target", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("summarize");
      expect(await walkFocus(page, details.dialog)).toBeGreaterThan(10);
    });

    test("toggles, Add option and rows have their names, and drops are announced", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("summarize");
      const mode = details
        .field("prompt")
        .getByRole("radiogroup", { name: "Value mode for Prompt" });
      await expect(mode.getByRole("radio", { name: "Fixed" })).toHaveAttribute(
        "aria-checked",
        "true",
      );
      await expect(mode.getByRole("radio", { name: "Mapped" })).toHaveAttribute(
        "aria-checked",
        "false",
      );
      const add = details.dialog.getByRole("button", { name: "Add option" });
      await expect(add).toHaveAttribute("aria-haspopup", "menu");
      await expect(add).toHaveAttribute("aria-expanded", "false");
      await add.click();
      await expect(add).toHaveAttribute("aria-expanded", "true");
      await expect(
        page.getByRole("menu", { name: "Add option" }),
      ).toBeVisible();
      await page.keyboard.press("Escape");
      await details.close();
      await details.openWithKeyboard("check-customer");
      await expect(
        details.field("value").getByRole("group", { name: "Row 1 of 2" }),
      ).toBeVisible();
      if (sheet)
        await details.dialog
          .getByRole("tablist", { name: "Step details panes" })
          .getByRole("tab", { name: "Input" })
          .click();
      await details.dialog
        .locator(".sd-input")
        .getByRole("listitem", { name: /^email, / })
        .focus();
      await page.keyboard.press("Enter");
      await page
        .getByRole("menu", { name: "Map email to" })
        .getByRole("menuitem", { name: "Fields", exact: true })
        .click();
      await expect(details.dialog.locator("[aria-live=polite]")).toHaveText(
        "Mapped Input › email to Fields",
      );
    });
  });

// The header sticks under the pane tabs on small screens; a control reached
// with Shift+Tab must scroll into view below both, never under them.
for (const size of [
  { tag: "360x640", width: 360, height: 640 },
  { tag: "640x360", width: 640, height: 360 },
])
  test.describe(`focus below the sticky header at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    for (const [step, tab] of [
      ["route-by-value", "Parameters"],
      ["notify-sales", "Settings"],
    ] as const)
      test(`Shift+Tab keeps the focused control in view in ${step}'s ${tab}`, async ({
        page,
      }) => {
        const details = await openStepFixture(page);
        await details.openWithKeyboard(step);
        if (tab === "Settings") await details.tab("Settings").click();
        const scroller = formScroller(page, details.dialog);
        // Start from the tab's last control, then walk back up.
        await details.dialog
          .locator(".sd-tabpanel:not([hidden])")
          .evaluate((panel) => {
            const controls = [
              ...panel.querySelectorAll<HTMLElement>(
                'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex="0"]',
              ),
            ].filter((element) => element.getClientRects().length > 0);
            controls.at(-1)?.focus();
          });
        let checked = 0;
        for (let i = 0; i < 40; i++) {
          await page.keyboard.press("Shift+Tab");
          await page.evaluate(
            () =>
              new Promise((done) =>
                requestAnimationFrame(() => requestAnimationFrame(done)),
              ),
          );
          const focused = await page.evaluate(() => {
            const element = document.activeElement as HTMLElement | null;
            if (!element) return null;
            const r = element.getBoundingClientRect();
            return {
              key: element.outerHTML.slice(0, 120),
              top: r.top,
              bottom: r.bottom,
              scrolls: !!element.closest(".sd-panes"),
              sticky: !!element.closest(".sd-head, .sd-segments"),
            };
          });
          if (!focused?.scrolls || focused.sticky) continue;
          const view = await visibleArea(scroller);
          expect(focused.top, focused.key).toBeGreaterThanOrEqual(view.y - 0.5);
          expect(focused.bottom, focused.key).toBeLessThanOrEqual(
            view.bottom + 0.5,
          );
          checked++;
        }
        expect(checked).toBeGreaterThan(4);
      });
  });

// A toast moves to the top while the bottom place would cover the focused
// control. On small screens the bottom place is 16 px from the window's
// edge, under the diagnostics strip, wherever the strip is.
for (const size of [
  { tag: "600x500", width: 600, height: 500 },
  { tag: "360x640", width: 360, height: 640 },
  { tag: "640x360", width: 640, height: 360 },
])
  test.describe(`toasts beside the focused control at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test("a toast never covers the control focused at the bottom of the screen", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await expect(page.locator(".diagnostics")).toBeVisible();
      await details.openWithKeyboard("summarize");
      // Execute step explains, in a toast, that it isn't available yet.
      await details.dialog
        .getByRole("button", { name: /^Execute step/ })
        .focus();
      await page.keyboard.press("Enter");
      const toast = page.locator(".toast-region .toast");
      await expect(toast).toBeVisible();
      // The tab's last control, scrolled to the bottom of the screen.
      await details.dialog
        .locator(".sd-tabpanel:not([hidden])")
        .evaluate((panel) => {
          const controls = [
            ...panel.querySelectorAll<HTMLElement>(
              'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex="0"]',
            ),
          ].filter((element) => element.getClientRects().length > 0);
          controls.at(-1)?.focus();
        });
      await page.evaluate(
        () =>
          new Promise((done) =>
            requestAnimationFrame(() => requestAnimationFrame(done)),
          ),
      );
      const focused = await page.evaluate(() => {
        const r = document.activeElement!.getBoundingClientRect();
        return { top: r.top, bottom: r.bottom, left: r.left, right: r.right };
      });
      expect(focused.bottom).toBeGreaterThan(size.height - 120);
      const covered = await toast.evaluate((element, f) => {
        const r = element.getBoundingClientRect();
        return (
          Math.min(r.bottom, f.bottom) - Math.max(r.top, f.top) > 0.5 &&
          Math.min(r.right, f.right) - Math.max(r.left, f.left) > 0.5
        );
      }, focused);
      expect(covered, "the toast covers the focused control").toBe(false);
    });
  });
