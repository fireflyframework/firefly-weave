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
import { expect, test } from "@playwright/test";
import { resolve } from "node:path";
import {
  closeSheet,
  insertStep,
  newWorkflow,
  offline,
  sourceText,
} from "./support";
import { openStepFixture, sizes } from "./step-details-po";

for (const size of sizes)
  test.describe(`step details at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("double-click, Enter and the badge open step details", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("lookup");
      await details.close();
      await expect(details.node("lookup")).toBeFocused();
      await details.openWithKeyboard("check-customer");
      await details.close();
      await insertStep(page, "Call an action");
      const chip = page.locator(".node-chip").first();
      await chip.scrollIntoViewIfNeeded();
      await chip.click();
      await expect(details.dialog).toBeVisible();
    });

    test("close, Escape and the scrim close without asking and focus the tile", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("lookup");
      await expect(
        details.dialog.getByRole("button", { name: "Close", exact: true }),
      ).toHaveAttribute("title", "Close (Esc). Changes are already saved.");
      await details.dialog
        .getByRole("button", { name: "Rename lookup" })
        .focus();
      await page.keyboard.press("Escape");
      await expect(details.dialog).toHaveCount(0);
      await expect(details.node("lookup")).toBeFocused();
      await details.open("lookup");
      if (size.width < 768) {
        const box = await details.dialog.boundingBox();
        expect([box!.x, box!.y, box!.width, box!.height]).toEqual([
          0,
          0,
          size.width,
          size.height,
        ]);
        await details.close();
      } else {
        await page.locator(".sd-scrim").click({ position: { x: 4, y: 4 } });
      }
      await expect(details.dialog).toHaveCount(0);
    });

    test("Escape in the name field reverts and blurs first, a second Escape closes", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("lookup");
      await details.dialog
        .getByRole("button", { name: "Rename lookup" })
        .click();
      const name = details.dialog.getByRole("textbox", { name: "Step name" });
      await name.fill("something else");
      await page.keyboard.press("Escape");
      await expect(name).toHaveCount(0);
      await expect(details.dialogFor("lookup")).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(details.dialog).toHaveCount(0);
    });

    test("Tab wraps inside step details", async ({ page }) => {
      const details = await openStepFixture(page);
      await details.open("lookup");
      for (let i = 0; i < 40; i++) {
        await page.keyboard.press("Tab");
        expect(
          await details.dialog.evaluate((d) =>
            d.contains(document.activeElement),
          ),
        ).toBe(true);
      }
      await details.dialog
        .getByRole("button", { name: "Previous step" })
        .focus();
      for (let i = 0; i < 40; i++) {
        await page.keyboard.press("Shift+Tab");
        expect(
          await details.dialog.evaluate((d) =>
            d.contains(document.activeElement),
          ),
        ).toBe(true);
      }
    });

    test("Previous and Next step and the neighbor buttons move in Outline order", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("check-customer");
      await details.tab("Settings").click();
      await page.keyboard.press("ControlOrMeta+Alt+Shift+ArrowRight");
      await expect(details.dialogFor("lookup")).toBeVisible();
      await expect(details.tab("Settings")).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await details.dialog
        .getByRole("button", { name: "Next step", exact: true })
        .click();
      await expect(details.dialogFor("route-by-value")).toBeVisible();
      if (size.width >= 768) {
        await details.dialog
          .getByRole("button", { name: "Open notify-sales", exact: true })
          .click();
        await expect(details.dialogFor("notify-sales")).toBeVisible();
        await expect(details.dialog).toContainText(
          "Path amount > 1000 of route-by-value · step 1 of 1",
        );
        await details.dialog
          .getByRole("button", { name: "Previous step", exact: true })
          .click();
      } else
        await details.dialog
          .getByRole("button", { name: "Next step", exact: true })
          .click();
      await page.keyboard.press("ControlOrMeta+Alt+Shift+ArrowLeft");
      await expect(details.dialog).toBeVisible();
    });

    test("closing after Next step focuses the new step's tile", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("check-customer");
      await details.dialog
        .getByRole("button", { name: "Next step", exact: true })
        .click();
      await expect(details.dialogFor("lookup")).toBeVisible();
      await details.close();
      await expect(details.node("lookup")).toBeFocused();
    });

    test("renaming to free text stores a normalized ID with references", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("check-customer");
      await details.dialog
        .getByRole("button", { name: "Rename check-customer" })
        .click();
      const name = details.dialog.getByRole("textbox", { name: "Step name" });
      await name.fill("Check the customer!");
      await expect(details.dialog).toContainText("Saved as check-the-customer");
      await page.keyboard.press("Enter");
      await expect(details.dialogFor("check-the-customer")).toBeVisible();
      await expect(page.locator(".toast-region")).toContainText(
        "Renamed to check-the-customer. 2 references updated.",
      );
      await page.keyboard.press("ControlOrMeta+z");
      await expect(details.dialogFor("check-customer")).toBeVisible();
      await details.close();
      const source = await sourceText(page);
      expect(source).toContain("/steps/check-customer/output");
      expect(source).not.toContain("check-the-customer");
    });
  });

test.describe("step details beside the classic inspector", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  // Below 768 px the classic inspector covers the canvas, so a pending
  // inspector edit can't meet a double-click there.
  test("opening step details applies the inspector's pending edit first", async ({
    page,
  }) => {
    const details = await openStepFixture(page);
    await details.node("lookup").click();
    const toggle = page.getByRole("button", { name: "Show inspector" });
    if (await toggle.isVisible()) await toggle.click();
    await page.getByLabel("Step name", { exact: true }).fill("lookup-customer");
    await details.node("lookup").dblclick();
    await expect(details.dialogFor("lookup-customer")).toBeVisible();
  });
});

test.describe("step details stay off without the new editor", () => {
  test("double-click on a step keeps the classic designer", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    await insertStep(page, "Transform");
    await page.locator("[data-step] .node-body").first().dblclick();
    await closeSheet(page);
    await expect(
      page.getByRole("dialog", { name: /^Step details: / }),
    ).toHaveCount(0);
  });
});

for (const size of sizes) {
  test.describe(`dialog integration at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test("opening and navigation produce no browser errors", async ({
      page,
    }) => {
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error") errors.push(message.text());
      });
      const details = await openStepFixture(page);
      await details.open("lookup");
      await details.dialog
        .getByRole("button", { name: "Next step", exact: true })
        .click();
      await expect(details.dialogFor("route-by-value")).toBeVisible();
      expect(errors).toEqual([]);
    });
    test("Undo and Redo follow successive renames without closing", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("check-customer");
      for (const [from, to] of [
        ["check-customer", "check-one"],
        ["check-one", "check-two"],
      ]) {
        await details.dialog
          .getByRole("button", { name: `Rename ${from}` })
          .click();
        await details.dialog
          .getByRole("textbox", { name: "Step name" })
          .fill(to);
        await page.keyboard.press("Enter");
        await expect(details.dialogFor(to)).toBeVisible();
      }
      for (const name of ["check-one", "check-customer"]) {
        await page.keyboard.press("ControlOrMeta+z");
        await expect(details.dialogFor(name)).toBeVisible();
      }
      for (const name of ["check-one", "check-two"]) {
        await page.keyboard.press("ControlOrMeta+Shift+z");
        await expect(details.dialogFor(name)).toBeVisible();
      }
      await details.close();
      await expect(details.node("check-two")).toBeFocused();
    });
    for (const opening of ["double-click", "Enter"] as const)
      test(`Start opens the trigger with ${opening}, owns Escape and returns focus`, async ({
        page,
      }) => {
        const details = await openStepFixture(page);
        if (opening === "double-click") await details.trigger().dblclick();
        else await details.trigger().press("Enter");
        await expect(details.dialogFor("Manual form trigger")).toBeVisible();
        await expect(
          details.dialog.getByRole("button", {
            name: "Manual form trigger",
            exact: true,
          }),
        ).toBeDisabled();
        await expect
          .poll(() =>
            details.dialog.evaluate((dialog) => {
              const active = document.activeElement;
              return (
                !!active &&
                dialog.contains(active) &&
                active.matches(
                  "button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled])",
                )
              );
            }),
          )
          .toBe(true);
        await page.keyboard.press("Escape");
        await expect(details.dialog).toHaveCount(0);
        await expect(details.trigger()).toBeFocused();
      });
    test("Previous from the first step focuses the trigger dialog and Escape returns to Start", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("check-customer");
      await details.dialog
        .getByRole("button", { name: "Previous step", exact: true })
        .click();
      await expect(details.dialogFor("Manual form trigger")).toBeVisible();
      await expect
        .poll(() =>
          details.dialog.evaluate((dialog) => {
            const active = document.activeElement;
            return (
              !!active &&
              dialog.contains(active) &&
              active.matches(
                "button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled])",
              )
            );
          }),
        )
        .toBe(true);
      await page.keyboard.press("Escape");
      await expect(details.dialog).toHaveCount(0);
      await expect(details.trigger()).toBeFocused();
    });
    test("Enter in Outline opens details and closing returns to its row", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await page.getByRole("tab", { name: "Outline", exact: true }).click();
      const row = page.locator('[data-outline="lookup"]');
      await row.focus();
      await row.press("Enter");
      await expect(details.dialogFor("lookup")).toBeVisible();
      await details.close();
      await expect(row).toBeFocused();
    });
    test("background focus returns to the last dialog control", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("lookup");
      const close = details.dialog.getByRole("button", {
        name: "Close",
        exact: true,
      });
      await close.focus();
      await page.getByRole("button", { name: "Validate", exact: true }).focus();
      await expect(close).toBeFocused();
    });
  });
}

const screenSizes = [
  { tag: "360x740", width: 360, height: 740 },
  { tag: "600x500", width: 600, height: 500 },
  { tag: "768x1024", width: 768, height: 1024 },
  { tag: "1280x720", width: 1280, height: 720 },
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "1920x1080", width: 1920, height: 1080 },
  { tag: "1280x720-zoom200", width: 640, height: 360, scale: 2 },
];
for (const size of screenSizes) {
  test.describe(`dialog controls at ${size.tag}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      deviceScaleFactor: size.scale ?? 1,
      hasTouch: size.width <= 768,
    });
    test("dialog controls fit and meet pointer target sizes", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("lookup");
      await page.screenshot({
        path: resolve(`../build/editor-m3/task-10/shell-${size.tag}.png`),
      });
      const audit = await details.dialog.evaluate((dialog, touch) => {
        const controls = [
          ...dialog.querySelectorAll<HTMLElement>(
            "button:not([disabled]), a[href], input",
          ),
        ].filter(
          (element) =>
            element.getClientRects().length > 0 &&
            getComputedStyle(element).visibility !== "hidden",
        );
        return {
          width: document.documentElement.scrollWidth,
          viewport: window.innerWidth,
          failures: controls.flatMap((element) => {
            const box = element.getBoundingClientRect();
            const name =
              element.getAttribute("aria-label") ||
              element.textContent?.trim() ||
              element.tagName;
            const target = document.elementFromPoint(
              box.x + box.width / 2,
              box.y + box.height / 2,
            );
            const minimum = touch ? 44 : 24;
            return box.width < minimum ||
              box.height < minimum ||
              box.x < 0 ||
              box.right > window.innerWidth ||
              box.y < 0 ||
              box.bottom > window.innerHeight ||
              !target ||
              !element.contains(target)
              ? [
                  {
                    name,
                    x: box.x,
                    y: box.y,
                    width: box.width,
                    height: box.height,
                    reached: !!target && element.contains(target),
                  },
                ]
              : [];
          }),
        };
      }, size.width <= 768);
      expect(audit.width).toBeLessThanOrEqual(audit.viewport);
      expect(audit.failures).toEqual([]);
      await details.dialog
        .getByRole("button", { name: "Rename lookup" })
        .click();
      const rename = details.dialog.getByRole("textbox", { name: "Step name" });
      const renameBox = await rename.boundingBox();
      expect(renameBox!.height).toBeGreaterThanOrEqual(
        size.width <= 768 ? 44 : 24,
      );
      await rename.press("Escape");
      await details.close();
      await expect(details.node("lookup")).toBeFocused();
    });
  });
}
