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
import { CanvasPage } from "./canvas-po";
import { openStepFixture, sizes } from "./step-details-po";

for (const size of sizes)
  test.describe(`canvas details at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test("single click selects; double-click, Enter and Space each open once and return focus", async ({
      page,
    }) => {
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      const details = await openStepFixture(page);
      const canvas = new CanvasPage(page);
      await canvas.ready();
      await canvas.closeInspector();
      await canvas.tileBody("lookup").click();
      await expect(details.dialog).toHaveCount(0);
      await expect(canvas.tileBody("lookup")).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      await expect(
        page.getByRole("complementary", { name: "Inspector" }),
      ).toBeHidden();
      await canvas.tileBody("lookup").dblclick();
      await expect(details.dialogFor("lookup")).toBeVisible();
      await details.close();
      await expect(canvas.tileBody("lookup")).toBeFocused();
      for (const key of ["Enter", "Space"]) {
        await page.keyboard.press(key);
        await expect(details.dialogFor("lookup")).toBeVisible();
        await details.close();
        await expect(canvas.tileBody("lookup")).toBeFocused();
      }
      expect(errors).toEqual([]);
    });
    for (const target of [
      { id: "$trigger:manual", title: "Manual form trigger" },
      { id: "$end", title: "End" },
    ]) {
      test(`${target.title} opens once and returns focus to its current tile after resizing`, async ({
        page,
      }) => {
        const details = await openStepFixture(page);
        const canvas = new CanvasPage(page);
        await canvas.ready();
        const tile = canvas.tileBody(target.id);
        await tile.focus();
        await page.keyboard.press("Enter");
        await expect(details.dialogFor(target.title)).toBeVisible();
        await page.setViewportSize({ width: 600, height: 650 });
        await details.close();
        await expect(tile).toBeFocused();
        const box = (await tile.boundingBox())!;
        expect(box.x).toBeGreaterThanOrEqual(0);
        expect(box.x + box.width).toBeLessThanOrEqual(600);
        await tile.dblclick();
        await expect(details.dialogFor(target.title)).toBeVisible();
      });
    }
    test("Open and Rename in the tile menu open the dialog with the right focus", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      const canvas = new CanvasPage(page);
      await canvas.tileBody("lookup").click({ button: "right" });
      await page.getByRole("menuitem", { name: "Open", exact: true }).click();
      await expect(details.dialogFor("lookup")).toBeVisible();
      await details.close();
      await canvas.tileBody("lookup").click({ button: "right" });
      await page.getByRole("menuitem", { name: /^Rename/ }).click();
      await expect(
        details.dialog.getByRole("textbox", { name: "Step name" }),
      ).toBeFocused();
    });
  });

test("an issue badge opens on the reported field rather than the first field", async ({
  page,
}) => {
  const details = await openStepFixture(page);
  await page.route("**/studio/local/validate", (route) =>
    route.fulfill({
      json: {
        validationOk: false,
        errorCount: 1,
        partial: true,
        diagnostics: [
          {
            code: "WV-COMP-TYPE_MISMATCH",
            severity: "error",
            message: "Choose an action input",
            path: "/spec/steps/6/message",
          },
        ],
      },
    }),
  );
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  const badge = page.locator(
    '.tile-node[data-tile="reject-order"] .tile-badge',
  );
  await expect(badge).toBeVisible();
  await badge.click();
  await expect(details.dialogFor("reject-order")).toBeVisible();
  await expect
    .poll(() =>
      details.dialog.evaluate((dialog) =>
        document.activeElement
          ?.closest("[data-param]")
          ?.getAttribute("data-param"),
      ),
    )
    .toBe("message");
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 360, height: 740 },
  { width: 640, height: 360 },
])
  test(`issue targets keep 44px and leave the tile and output usable at ${viewport.width}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const details = await openStepFixture(page);
    const canvas = new CanvasPage(page);
    await canvas.closeInspector();
    await page.route("**/studio/local/validate", (route) =>
      route.fulfill({
        json: {
          validationOk: false,
          errorCount: 1,
          partial: true,
          diagnostics: [
            {
              code: "WV-COMP-TYPE_MISMATCH",
              severity: "error",
              message: "Choose a duration",
              path: "/spec/steps/2/default/steps/0/branches/ledger/steps/0/durationSeconds",
            },
          ],
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
    await canvas.tileBody("wait-a-minute").focus();
    await page.keyboard.press("Enter");
    await expect(details.dialogFor("wait-a-minute")).toBeVisible();
    await details.close();
    for (const zoom of [
      31, 30.01, 32, 40, 45.8, 45.9, 50, 100, 200, 29.99, 25,
    ]) {
      const body = canvas.tileBody("wait-a-minute");
      await expect(body).toBeFocused();
      await page.keyboard.press("0");
      await page.keyboard.press("Enter");
      await expect(details.dialogFor("wait-a-minute")).toBeVisible();
      await details.close();
      await expect(body).toBeFocused();
      await body.hover();
      await page.keyboard.down("Control");
      await page.mouse.wheel(0, -Math.log(zoom / 100) / 0.0015);
      await page.keyboard.up("Control");
      await expect.poll(() => canvas.zoomPercent()).toBe(Math.round(zoom));
      const clickCenter = async () => {
        const box = (await body.boundingBox())!;
        await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
        await expect(details.dialog).toHaveCount(0);
        await expect(body).toHaveAttribute("aria-pressed", "true");
      };
      if (zoom < 30) {
        await expect(
          canvas.tile("wait-a-minute").locator(".tile-issue"),
        ).toBeHidden();
        await clickCenter();
        continue;
      }
      const target = canvas
        .tile("wait-a-minute")
        .locator(".tile-issue, .tile-badge:not(.tile-issue .tile-badge)");
      const box = (await target.boundingBox())!;
      expect(box.width).toBeGreaterThanOrEqual(44);
      expect(box.height).toBeGreaterThanOrEqual(44);
      await expect(target).toHaveRole("button");
      for (const control of [
        target,
        canvas.tileBody("wait-a-minute"),
        canvas.handle("wait-a-minute>out"),
      ]) {
        expect(
          await control.evaluate((element) => {
            const box = element.getBoundingClientRect();
            return element.contains(
              document.elementFromPoint(
                box.x + box.width / 2,
                box.y + box.height / 2,
              ),
            );
          }),
        ).toBe(true);
      }
      const coveredNeighbors = await target.evaluate((button) => {
        const root = button.closest(".canvas-v2")!;
        return Array.from(
          root.querySelectorAll<HTMLElement>(
            ".tile-body, [data-handle], .branch-label, .join-label, .insert-plus, .tile-issue",
          ),
        )
          .filter((element) => {
            if (element === button || !element.getClientRects().length)
              return false;
            const bounds = element.getBoundingClientRect();
            return button.contains(
              document.elementFromPoint(
                bounds.x + bounds.width / 2,
                bounds.y + bounds.height / 2,
              ),
            );
          })
          .map(
            (element) =>
              element.getAttribute("aria-label") || element.textContent?.trim(),
          );
      });
      expect(coveredNeighbors).toEqual([]);
      await clickCenter();
      if (zoom === 31) {
        if (viewport.width === 1440) {
          await canvas.tileBody("notify-sales").click({ modifiers: ["Shift"] });
          await body.click({ modifiers: ["ControlOrMeta"] });
          await expect(body).toHaveAttribute("aria-pressed", "false");
          await body.click({ modifiers: ["Shift"] });
          await expect(body).toHaveAttribute("aria-pressed", "true");
          await expect(canvas.tileBody("notify-sales")).toHaveAttribute(
            "aria-pressed",
            "true",
          );
        }
        const start = (await body.boundingBox())!;
        await page.mouse.move(
          start.x + start.width / 2,
          start.y + start.height / 2,
        );
        await page.mouse.down();
        await page.mouse.move(
          start.x + start.width / 2 + 12,
          start.y + start.height / 2 + 12,
          { steps: 4 },
        );
        await expect(canvas.root).toHaveClass(/\brevealing\b/);
        await page.keyboard.press("Escape");
        await page.mouse.up();
        await expect(canvas.root).not.toHaveClass(/\brevealing\b/);
        await expect(details.dialog).toHaveCount(0);
        await expect(body).toHaveAttribute("aria-pressed", "true");
        if (viewport.width === 1440)
          await expect(canvas.tileBody("notify-sales")).toHaveAttribute(
            "aria-pressed",
            "true",
          );
      }
      await canvas.tileBody("wait-a-minute").focus();
      await page.keyboard.press("Tab");
      await expect(target).toBeFocused();
      await target.click();
      await expect(details.dialogFor("wait-a-minute")).toBeVisible();
      await details.close();
    }
  });

test("closing and switching to Source keeps focus in the selected view", async ({
  page,
}) => {
  const details = await openStepFixture(page);
  await details.open("lookup");
  await details.close();
  const source = page.getByRole("tab", { name: "Source", exact: true });
  await source.click();
  await expect(source).toHaveAttribute("aria-selected", "true");
  await expect(source).toBeFocused();
  await expect(page.locator(".canvas-v2")).toHaveCount(0);
  await expect(details.dialog).toHaveCount(0);
});

test("an issue in a pending Settings field falls back to a visible parameter", async ({
  page,
}) => {
  const details = await openStepFixture(page);
  await page.route("**/studio/local/validate", (route) =>
    route.fulfill({
      json: {
        validationOk: false,
        errorCount: 1,
        partial: true,
        diagnostics: [
          {
            code: "WV-COMP-TYPE_MISMATCH",
            severity: "error",
            message: "Choose a timeout",
            path: "/spec/steps/5/timeoutSeconds",
          },
        ],
      },
    }),
  );
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  await page
    .locator('.tile-node[data-tile="wait-for-payment"] .tile-issue')
    .click();
  await expect(details.dialogFor("wait-for-payment")).toBeVisible();
  await expect(details.tab("Parameters")).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(details.field("name").getByRole("textbox")).toBeFocused();
});
