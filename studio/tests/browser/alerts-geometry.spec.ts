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
// How Alerts, the rule editor, the channel sheet, Settings › Administration
// and My alert address lay out: no page scrolls sideways; record lists keep
// their column heads from 1100 px and stack below; Preview sits beside the
// rule's fields only when the page holds both; Administration is two columns
// at 1440 px and one on a phone; every control is 36 px with a fine pointer
// (32 px for small buttons, 24 px for a chip's remove button) and 44 px on
// touch or below 768 px.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { administrator, alertsPlatform } from "./alerts-po";

interface Target {
  label: string;
  height: number;
  width: number;
  kind: "control" | "small" | "chip";
}
/** Every visible control in `root`, by the size class it must meet. */
async function targets(root: Locator): Promise<Target[]> {
  return root.evaluate((element) => {
    const visible = (node: Element) => {
      const box = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return box.width > 0 && box.height > 0 && style.visibility !== "hidden";
    };
    const found: Target[] = [];
    const add = (node: Element, kind: Target["kind"]) => {
      if (!visible(node)) return;
      const box = node.getBoundingClientRect();
      found.push({
        label: (
          node.getAttribute("aria-label") ||
          node.textContent ||
          node.tagName
        )
          .trim()
          .slice(0, 40),
        height: Math.round(box.height),
        width: Math.round(box.width),
        kind,
      });
    };
    for (const node of element.querySelectorAll(
      "button, select, input:not([type=checkbox]):not([type=radio]), textarea",
    )) {
      if (node.closest(".modal-header")) continue;
      add(
        node,
        node.closest(".chip")
          ? "chip"
          : node.classList.contains("sm")
            ? "small"
            : "control",
      );
    }
    for (const node of element.querySelectorAll(
      ".switch, .segments label, .checks .checkbox-field",
    ))
      add(node, "control");
    return found;
  });
}
async function expectTargets(page: Page, root: Locator, touch: boolean) {
  const narrow = (page.viewportSize()?.width ?? 0) < 768;
  const large = touch || narrow;
  const found = await targets(root);
  expect(found.length).toBeGreaterThan(0);
  for (const target of found) {
    const least = large
      ? 44
      : target.kind === "chip"
        ? 24
        : target.kind === "small"
          ? 32
          : 36;
    expect(
      target.height,
      `${target.label} (${target.kind})`,
    ).toBeGreaterThanOrEqual(least);
  }
}
const noSideways = async (page: Page) =>
  expect(
    await page.evaluate(() => {
      const page = document.scrollingElement!.scrollWidth <= innerWidth;
      const clipped = [...document.querySelectorAll(".records, .rec, .card")]
        // Column heads kept only for screen readers are 1 px wide on purpose.
        .filter(
          (node) =>
            node.clientWidth > 1 && node.scrollWidth > node.clientWidth + 1,
        )
        .map((node) => node.className);
      return { page, clipped };
    }),
  ).toEqual({ page: true, clipped: [] });
const box = async (locator: Locator) => (await locator.boundingBox())!;

for (const size of [
  { width: 1440, height: 900, touch: false },
  { width: 1100, height: 900, touch: false },
  { width: 600, height: 500, touch: false },
  { width: 360, height: 640, touch: false },
  { width: 1440, height: 900, touch: true },
]) {
  const tag = `${size.width}x${size.height}${size.touch ? " touch" : ""}`;
  test.describe(`alerts and administration at ${tag}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
    });
    const main = (page: Page) => page.locator("#main");

    test("Alerts' tabs, records and actions", async ({ page }) => {
      await alertsPlatform(page);
      await expect(
        page.getByRole("table", { name: "Firing alerts" }),
      ).toBeVisible();
      const head = page
        .getByRole("table", { name: "Firing alerts" })
        .locator(".rec.head");
      if (size.width >= 1100) await expect(head).toBeVisible();
      else expect((await box(head)).height).toBeLessThanOrEqual(1);
      await expectTargets(page, main(page), size.touch);
      await noSideways(page);
      for (const tab of ["Rules", "Channels"]) {
        await page.getByRole("tab", { name: new RegExp(`^${tab}`) }).click();
        await expect(
          page.getByRole("tab", { name: new RegExp(`^${tab}`) }),
        ).toHaveAttribute("aria-selected", "true");
        await expectTargets(page, main(page), size.touch);
        await noSideways(page);
      }
      // On a phone the tabs fill rows of 44 px tabs.
      if (size.width < 768)
        for (const tab of await page.getByRole("tab").all())
          expect((await box(tab)).height).toBeGreaterThanOrEqual(44);
    });

    test("the rule editor puts Preview beside the fields only when both fit", async ({
      page,
    }) => {
      await alertsPlatform(page, { path: "/operate/alerts?tab=rules" });
      await page.getByRole("button", { name: "Add rule" }).click();
      const fields = await box(page.locator(".ed-fields"));
      const preview = await box(page.locator(".side"));
      const save = await box(page.getByRole("button", { name: "Save rule" }));
      if (size.width >= 1336) {
        expect(preview.x).toBeGreaterThanOrEqual(fields.x + fields.width);
        expect(fields.width).toBeLessThanOrEqual(720);
        expect(preview.width).toBeGreaterThanOrEqual(320);
      } else {
        expect(preview.y).toBeGreaterThanOrEqual(fields.y + fields.height);
        expect(save.y).toBeGreaterThanOrEqual(preview.y + preview.height);
      }
      await expectTargets(page, main(page), size.touch);
      await noSideways(page);
    });

    test("the channel sheet is 480 px, the whole width on a phone", async ({
      page,
    }) => {
      await alertsPlatform(page, { path: "/operate/alerts?tab=channels" });
      await page.getByRole("button", { name: "Edit Platform team" }).click();
      const sheet = page.getByRole("dialog", { name: "Edit Platform team" });
      const width = (await box(sheet)).width;
      if (size.width < 768)
        expect(width).toBeGreaterThanOrEqual(size.width - 1);
      else expect(Math.round(width)).toBe(480);
      await expectTargets(page, sheet, size.touch);
      const close = await box(sheet.getByRole("button", { name: "Close" }));
      expect(close.height).toBeGreaterThanOrEqual(
        size.touch || size.width < 768 ? 44 : 32,
      );
    });

    test("Administration's cards and My alert address", async ({ page }) => {
      await alertsPlatform(page, {
        capabilities: administrator,
        path: "/settings/administration",
      });
      const cards = page.locator(".card");
      await expect(cards).toHaveCount(7);
      const [platform, compatibility] = [
        await box(cards.nth(0)),
        await box(cards.nth(1)),
      ];
      if (size.width >= 1440) {
        expect(Math.round(compatibility.y)).toBe(Math.round(platform.y));
        expect(Math.round(compatibility.height)).toBe(
          Math.round(platform.height),
        );
      } else if (size.width < 768)
        expect(compatibility.y).toBeGreaterThan(platform.y + platform.height);
      await expectTargets(page, main(page), size.touch);
      await noSideways(page);
      await page.getByRole("tab", { name: "Preferences" }).click();
      const address = page.getByRole("region", { name: "My alert address" });
      await expect(address.getByLabel("Address")).toHaveValue(
        "carla@acme.example",
      );
      await expectTargets(page, address, size.touch);
      // It lines up with the Editor card above it.
      const editor = await box(page.getByRole("heading", { name: "Editor" }));
      const mine = await box(
        address.getByRole("heading", { name: "My alert address" }),
      );
      expect(Math.round(mine.x)).toBe(Math.round(editor.x));
      await noSideways(page);
    });
  });
}

test.describe("alerts and administration at 1920x1080", () => {
  test.use({ viewport: { width: 1920, height: 1080 } });

  test("Alerts keeps to 1200 px", async ({ page }) => {
    await alertsPlatform(page);
    await expect(
      page.getByRole("table", { name: "Firing alerts" }),
    ).toBeVisible();
    expect(
      (await box(page.locator("weave-alerts-page"))).width,
    ).toBeLessThanOrEqual(1200);
  });

  test("Administration keeps to 1200 px in two columns, and an opt-in's address and date stay whole", async ({
    page,
  }) => {
    await alertsPlatform(page, {
      capabilities: administrator,
      path: "/settings/administration",
    });
    const cards = page.locator(".card");
    await expect(cards).toHaveCount(7);
    expect(
      (await box(page.locator("weave-administration-page"))).width,
    ).toBeLessThanOrEqual(1200);
    const [platform, compatibility] = [
      await box(cards.nth(0)),
      await box(cards.nth(1)),
    ];
    expect(Math.round(compatibility.y)).toBe(Math.round(platform.y));
    const optins = page.getByRole("table", { name: "Opt-ins" });
    // Each line is one box: the address and the day never wrap.
    const lines = await optins
      .locator("[role=row]:not(.h) [role=cell]")
      .evaluateAll((cells) =>
        cells
          .slice(0, 3)
          .map((cell) => [...cell.querySelectorAll(".nb, .date")])
          .flat()
          .map((part) => (part as HTMLElement).getClientRects().length),
      );
    expect(lines.length).toBeGreaterThanOrEqual(3);
    expect(lines.every((count) => count === 1)).toBe(true);
    // Stacked, without column heads, the last lines say what they are.
    await expect(
      optins.locator("[role=row]:not(.h) [role=cell]").nth(2),
    ).toHaveText("Recorded by Person b3e5f7a9 on 4 Nov", {
      useInnerText: true,
    });
  });
});

test.describe("Administration at 768x1024", () => {
  test.use({ viewport: { width: 768, height: 1024 } });

  test("a card wide enough keeps opt-ins as a table, Recorded on two lines under its head", async ({
    page,
  }) => {
    await alertsPlatform(page, {
      capabilities: administrator,
      path: "/settings/administration",
    });
    const optins = page.getByRole("table", { name: "Opt-ins" });
    await expect(
      optins.getByRole("columnheader", { name: "Recorded" }),
    ).toBeVisible();
    await expect(
      optins.locator("[role=row]:not(.h) [role=cell]").nth(2),
    ).toHaveText("Person b3e5f7a9 4 Nov", { useInnerText: true });
  });
});

test.describe("the Rules tab at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Now fits its line, with the icon in the muted color: Severity carries the tone", async ({
    page,
  }) => {
    await alertsPlatform(page, { path: "/operate/alerts?tab=rules" });
    const now = page
      .getByRole("table", { name: "Alert rules" })
      .getByRole("cell")
      .filter({ hasText: "acknowledged" });
    await expect(now).toBeVisible();
    const look = await now.evaluate((cell) => {
      const text = cell.querySelector(":scope > span:last-child")!;
      const icon = cell.querySelector("weave-icon")!;
      const muted = getComputedStyle(document.body)
        .getPropertyValue("--muted")
        .trim();
      const probe = document.createElement("span");
      probe.style.color = muted;
      document.body.append(probe);
      const expected = getComputedStyle(probe).color;
      probe.remove();
      return {
        lines: text.getClientRects().length,
        muted: getComputedStyle(icon).color === expected,
      };
    });
    expect(look).toEqual({ lines: 1, muted: true });
  });
});
