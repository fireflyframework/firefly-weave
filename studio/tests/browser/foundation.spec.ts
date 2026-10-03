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
// The design foundation (UX plan wave 1): field borders that pass 3:1, flat
// disabled buttons, one focus ring, status pills with a tone border, control
// heights, and one drawing per icon.
import { test, expect, Locator, Page } from "@playwright/test";
import {
  allCapabilities,
  connected,
  insertStep,
  newWorkflow,
  offline,
} from "./support";
import { DesignerPage } from "./designer-po";

/** WCAG contrast of two computed colors ("rgb(r, g, b)"). */
function contrast(a: string, b: string) {
  const luminance = (value: string) => {
    const [r, g, bl] = (value.match(/[\d.]+/g) ?? []).slice(0, 3).map((c) => {
      const s = Number(c) / 255;
      return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}
const css = (locator: Locator, ...names: string[]) =>
  locator.evaluate(
    (element, properties) =>
      Object.fromEntries(
        properties.map((name) => [
          name,
          getComputedStyle(element).getPropertyValue(name),
        ]),
      ),
    names,
  );
/** Tabs until a control matching `selector` has keyboard focus. */
async function tabTo(page: Page, selector: string, limit = 40) {
  for (let i = 0; i < limit; i++) {
    await page.keyboard.press("Tab");
    if (
      await page.evaluate((s) => !!document.activeElement?.matches(s), selector)
    )
      return page.locator(":focus");
  }
  throw Error(`Tab never reached ${selector}`);
}
const white = "rgb(255, 255, 255)";
const mist = "rgb(238, 244, 240)";

test.describe("1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("fields have a 3:1 border and the field focus ring keeps it", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    const name = new DesignerPage(page).inspectorField("Name");
    const rest = await css(name, "border-top-color");
    expect(rest["border-top-color"]).toBe("rgb(115, 140, 128)");
    expect(contrast(rest["border-top-color"], white)).toBeGreaterThanOrEqual(3);
    expect(contrast(rest["border-top-color"], mist)).toBeGreaterThanOrEqual(3);
    await name.click();
    const focused = await css(
      name,
      "outline-style",
      "outline-width",
      "outline-color",
      "outline-offset",
      "border-top-color",
    );
    expect(focused).toEqual({
      "outline-style": "solid",
      "outline-width": "2px",
      "outline-color": "rgb(44, 106, 87)",
      "outline-offset": "1px",
      "border-top-color": "rgb(44, 106, 87)",
    });
  });

  test("a disabled primary is flat and legible, never a pressed slab", async ({
    page,
  }) => {
    // An account that can't save drafts: Save draft (the toolbar's one
    // primary) is unavailable, yet stays focusable to say why.
    await connected(page, {
      capabilities: allCapabilities.filter((c) => c !== "definition.write"),
    });
    await newWorkflow(page);
    const activate = page
      .getByRole("toolbar", { name: "Workflow commands" })
      .getByRole("button", { name: "Save draft", exact: true });
    await expect(activate).toBeDisabled();
    await expect(activate).toHaveAttribute("aria-disabled", "true");
    await expect(activate).toHaveClass(/\bprimary\b/);
    const look = await css(activate, "background-color", "color", "opacity");
    expect(look).toEqual({
      "background-color": "rgb(238, 242, 239)",
      color: "rgb(92, 112, 106)",
      opacity: "1",
    });
    expect(contrast(look.color, look["background-color"])).toBeGreaterThan(4.5);
    // Hovering changes nothing.
    await activate.hover({ force: true });
    expect((await css(activate, "background-color"))["background-color"]).toBe(
      "rgb(238, 242, 239)",
    );
  });

  test("one 2px focus ring, mint in the forest sidebar", async ({ page }) => {
    await offline(page);
    // The first stops are in the sidebar.
    const sidebar = await tabTo(page, ".sidebar button");
    expect(await css(sidebar, "outline-color", "outline-width")).toEqual({
      "outline-color": "rgb(161, 209, 185)",
      "outline-width": "2px",
    });
    const content = await tabTo(page, ".main-shell button");
    expect(
      await css(
        content,
        "outline-style",
        "outline-width",
        "outline-color",
        "outline-offset",
      ),
    ).toEqual({
      "outline-style": "solid",
      "outline-width": "2px",
      "outline-color": "rgb(44, 106, 87)",
      "outline-offset": "2px",
    });
  });

  test("status pills, tags and notices use the tone tokens", async ({
    page,
  }) => {
    await offline(page);
    await expect(page.locator(".dashboard-status")).toBeVisible();
    // Waves 2 and 3 apply these classes; their look is defined now.
    const looks = await page.evaluate(() => {
      const host = document.createElement("div");
      host.innerHTML = `
        <span class="pill" data-tone="danger">Failed</span>
        <span class="pill" data-tone="success">Succeeded</span>
        <span class="pill" data-tone="info">Running</span>
        <span class="pill" data-tone="warning">Waiting</span>
        <span class="status-pill">Queued</span>
        <span class="tag emphasis">Production</span>
        <span class="tag">1.0.0</span>
        <div class="notice" data-tone="success">Created pets.</div>
        <div class="notice error-notice">Not saved.</div>
        <div class="toast">Task claimed.</div>`;
      document.querySelector(".page-content, .home-view")!.append(host);
      const read = (e: Element, pseudo?: string) => {
        const s = getComputedStyle(e, pseudo);
        return {
          color: s.color,
          background: s.backgroundColor,
          border: s.borderTopColor,
          borderWidth: s.borderTopWidth,
          height: s.height,
          font: `${s.fontWeight} ${s.fontSize}`,
          content: s.content,
        };
      };
      const result = [...host.children].map((e) => ({
        text: e.textContent,
        ...read(e),
        dot: read(e, "::before").content,
      }));
      host.remove();
      return result;
    });
    const by = (text: string) => looks.find((l) => l.text === text)!;
    expect(by("Failed")).toMatchObject({
      color: "rgb(150, 37, 49)",
      background: "rgb(251, 232, 233)",
      borderWidth: "1px",
      height: "22px",
      font: "600 12px",
      dot: '""',
    });
    expect(by("Succeeded").color).toBe("rgb(29, 96, 71)");
    expect(by("Running").color).toBe("rgb(36, 90, 107)");
    expect(by("Waiting").color).toBe("rgb(115, 80, 15)");
    // The old class name is the neutral pill.
    expect(by("Queued")).toMatchObject({
      color: "rgb(70, 93, 85)",
      background: "rgb(237, 241, 238)",
    });
    for (const text of ["Failed", "Succeeded", "Running", "Waiting", "Queued"])
      expect(
        contrast(by(text).color, by(text).background),
      ).toBeGreaterThanOrEqual(6);
    // "Production" is a forest outline, never gold.
    expect(by("Production")).toMatchObject({
      color: "rgb(23, 61, 52)",
      border: "rgb(23, 61, 52)",
    });
    expect(by("Created pets.").background).toBe("rgb(226, 241, 233)");
    expect(by("Not saved.").color).toBe("rgb(150, 37, 49)");
    expect(by("Task claimed.").background).toBe("rgb(23, 61, 52)");
  });

  test("each icon has its own drawing, at 16 and 20 px", async ({ page }) => {
    await offline(page);
    await newWorkflow(page);
    await insertStep(page, "Wait for signal");
    await insertStep(page, "Human task");
    const icons = await page
      .locator("weave-icon[data-icon]")
      .evaluateAll((elements) =>
        elements.map((e) => ({
          name: e.getAttribute("data-icon")!,
          d: e.querySelector("path")!.getAttribute("d")!,
          rings: e.querySelectorAll("circle").length,
          size: Math.round(e.getBoundingClientRect().width),
          stroke: getComputedStyle(e.querySelector("svg")!).strokeWidth,
          scaling: getComputedStyle(e.querySelector("path")!).vectorEffect,
        })),
      );
    const drawings = new Map<string, string>();
    for (const icon of icons) {
      const drawing = `${icon.d}|${icon.rings}`;
      const other = drawings.get(drawing);
      if (other)
        expect(other, `${icon.name} draws like ${other}`).toBe(icon.name);
      drawings.set(drawing, icon.name);
    }
    // The gear, the radio waves and a whole head.
    const settings = icons.find((i) => i.name === "settings")!;
    expect(settings.d.startsWith("M12 15a3 3 0 1 0 0-6")).toBe(true);
    expect(settings.rings).toBe(0);
    expect(icons.find((i) => i.name === "signal")!.d).not.toBe(
      icons.find((i) => i.name === "email")!.d,
    );
    // Sizes and strokes: 20 px at 1.75, 16 px at 1.5, never scaled.
    for (const icon of icons) {
      expect(icon.scaling, icon.name).toBe("non-scaling-stroke");
      if (icon.size === 20) expect(icon.stroke, icon.name).toBe("1.75px");
    }
    const chevron = page.locator(".platform-indicator .menu-chevron");
    expect(await chevron.evaluate((e) => e.getBoundingClientRect().width)).toBe(
      16,
    );
    expect(
      await chevron.evaluate(
        (e) => getComputedStyle(e.querySelector("svg")!).strokeWidth,
      ),
    ).toBe("1.5px");
  });
});

for (const [width, height, least] of [
  [1280, 720, 36],
  [360, 740, 44],
] as const)
  test(`page buttons are ${least}px tall at ${width}x${height}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height });
    await offline(page);
    await page.getByRole("button", { name: "Workflows", exact: true }).click();
    for (const name of ["New workflow", "Import workflow"]) {
      const button = page
        .locator(".page-heading")
        .getByRole("button", { name, exact: true });
      const box = (await button.boundingBox())!;
      if (least === 36) expect(Math.round(box.height), name).toBe(36);
      else expect(box.height, name).toBeGreaterThanOrEqual(44);
    }
  });
