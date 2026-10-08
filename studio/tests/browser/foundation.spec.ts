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
// The design foundation (brand PR 1, contract C1): field borders that pass
// 3:1, flat disabled buttons, one amber focus ring, status pills in the tone
// tokens, Lucide icons at a 1.5px stroke, Manrope from Studio's own origin,
// and the Firefly Weave lockup, mark and favicons. Colors are read from the
// tokens at run time; only the amber accent is a fixed brand anchor.
import { test, expect, Locator, Page } from "@playwright/test";
import {
  allCapabilities,
  connected,
  insertStep,
  newWorkflow,
  offline,
  tokenColor,
} from "./support";
import { DesignerPage } from "./designer-po";

/** The brand amber (--accent, --focus): a fixed anchor, not read from CSS. */
const amber = "rgb(255, 179, 74)";

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

test.describe("1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("fields have a 3:1 border and the field focus ring keeps it", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    const name = new DesignerPage(page).inspectorField("Name");
    const rest = (await css(name, "border-top-color"))["border-top-color"];
    expect(rest).toBe(await tokenColor(page, "--field-border"));
    for (const surface of ["--sunken", "--surface", "--raised", "--hover"])
      expect(
        contrast(rest, await tokenColor(page, surface)),
        surface,
      ).toBeGreaterThanOrEqual(3);
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
      "outline-color": amber,
      "outline-offset": "1px",
      "border-top-color": amber,
    });
  });

  test("placeholders are drawn in --subtle at 4.5:1, not Chromium's grey", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    const search = page.getByPlaceholder("Search steps");
    await expect(search).toBeVisible();
    const look = await search.evaluate((input) => ({
      color: getComputedStyle(input, "::placeholder").color,
      opacity: getComputedStyle(input, "::placeholder").opacity,
      background: getComputedStyle(input).backgroundColor,
    }));
    expect(look.color).toBe(await tokenColor(page, "--subtle"));
    expect(look.opacity).toBe("1");
    expect(contrast(look.color, look.background)).toBeGreaterThanOrEqual(4.5);
  });

  test("a misspelled token fails the test instead of passing by inheritance", async ({
    page,
  }) => {
    await offline(page);
    await expect(tokenColor(page, "--sutble")).rejects.toThrow(
      "Design token --sutble is not defined on :root",
    );
    await expect(tokenColor(page, "--subtle")).resolves.toMatch(/^rgb\(/);
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
    const flat = {
      "background-color": await tokenColor(page, "--disabled-bg"),
      color: await tokenColor(page, "--disabled-ink"),
      opacity: "1",
    };
    const look = await css(activate, "background-color", "color", "opacity");
    expect(look).toEqual(flat);
    expect(contrast(look.color, look["background-color"])).toBeGreaterThan(4.5);
    // Hovering changes nothing.
    await activate.hover({ force: true });
    expect((await css(activate, "background-color"))["background-color"]).toBe(
      flat["background-color"],
    );
  });

  test("one 2px amber focus ring, in the sidebar too", async ({ page }) => {
    await offline(page);
    // The first stops are in the sidebar.
    const sidebar = await tabTo(page, ".sidebar button");
    expect(await css(sidebar, "outline-color", "outline-width")).toEqual({
      "outline-color": amber,
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
      "outline-color": amber,
      "outline-offset": "2px",
    });
  });

  test("status pills, tags and notices use the tone tokens", async ({
    page,
  }) => {
    await offline(page);
    await expect(page.locator(".dashboard-status")).toBeVisible();
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
    const token = (name: string) => tokenColor(page, name);
    expect(by("Failed")).toMatchObject({
      color: await token("--danger-ink"),
      background: await token("--danger-bg"),
      borderWidth: "1px",
      height: "22px",
      font: "600 12px",
      dot: '""',
    });
    expect(by("Succeeded").color).toBe(await token("--success-ink"));
    expect(by("Running").color).toBe(await token("--info-ink"));
    expect(by("Waiting").color).toBe(await token("--warning-ink"));
    // The old class name is the neutral pill.
    expect(by("Queued")).toMatchObject({
      color: await token("--neutral-ink"),
      background: await token("--neutral-bg"),
    });
    for (const text of ["Failed", "Succeeded", "Running", "Waiting", "Queued"])
      expect(
        contrast(by(text).color, by(text).background),
      ).toBeGreaterThanOrEqual(6);
    // "Production" is a paper outline: never amber, never a status tone.
    expect(by("Production")).toMatchObject({
      color: await token("--text"),
      border: await token("--text"),
    });
    expect(by("Created pets.").background).toBe(await token("--success-bg"));
    expect(by("Not saved.").color).toBe(await token("--danger-ink"));
    // Toasts are raised surfaces with paper text and a graphite edge.
    expect(by("Task claimed.")).toMatchObject({
      background: await token("--raised"),
      color: await token("--text"),
      border: await token("--border"),
    });
  });

  test("icons draw Lucide geometry at a 1.5px stroke, 16 and 20 px", async ({
    page,
  }) => {
    await offline(page);
    await newWorkflow(page);
    await insertStep(page, "Wait for signal");
    await insertStep(page, "Human task");
    const icons = await page
      .locator("weave-icon[data-icon]")
      .evaluateAll((elements) =>
        elements.map((e) => {
          const svg = e.querySelector("svg")!;
          return {
            name: e.getAttribute("data-icon")!,
            missing: e.hasAttribute("data-icon-missing"),
            // Angular's control-flow anchors are comments; the drawing is the rest.
            drawing: svg.innerHTML.replace(/<!--[\s\S]*?-->/g, ""),
            first: svg.firstElementChild?.getAttribute("d") ?? "",
            size: Math.round(e.getBoundingClientRect().width),
            stroke: getComputedStyle(svg).strokeWidth,
            scaling: [...svg.children].map(
              (c) => getComputedStyle(c).vectorEffect,
            ),
          };
        }),
      );
    expect(icons.filter((i) => i.missing).map((i) => i.name)).toEqual([]);
    const drawings = new Map<string, string>();
    for (const icon of icons) {
      const other = drawings.get(icon.drawing);
      if (other)
        expect(other, `${icon.name} draws like ${other}`).toBe(icon.name);
      drawings.set(icon.drawing, icon.name);
    }
    // Lucide's gear; the signal's radio tower is not the email envelope.
    const named = (name: string) => icons.find((i) => i.name === name)!;
    expect(named("settings").first).toMatch(/^M9\.671 4\.136/);
    expect(named("signal").drawing).not.toBe(named("email").drawing);
    // One stroke at every size (16, 20 and CSS-sized), never scaled with the drawing.
    expect(icons.some((i) => i.size === 16)).toBe(true);
    expect(icons.some((i) => i.size === 20)).toBe(true);
    for (const icon of icons) {
      expect(icon.stroke, icon.name).toBe("1.5px");
      expect(new Set(icon.scaling), icon.name).toEqual(
        new Set(["non-scaling-stroke"]),
      );
    }
  });

  test("renders Manrope from Studio's own origin", async ({ page }) => {
    const fonts: string[] = [];
    page.on("request", (request) => {
      if (request.resourceType() === "font") fonts.push(request.url());
    });
    await offline(page);
    await page.evaluate(() => document.fonts.ready);
    expect(
      await page.evaluate(() => document.fonts.check("14px Manrope")),
    ).toBe(true);
    expect(
      await page.evaluate(() => getComputedStyle(document.body).fontFamily),
    ).toMatch(/^"?Manrope"?,/);
    const origin = new URL(page.url()).origin;
    expect(fonts.length).toBeGreaterThan(0);
    for (const url of fonts)
      expect(url.startsWith(`${origin}/fonts/manrope/`), url).toBe(true);
    const latin = await page.request.get(
      "/fonts/manrope/manrope-latin-wght-normal.woff2",
    );
    expect(latin.headers()["content-type"]).toMatch(/^font\/woff2/);
  });

  test("asks the browser for dark native controls", async ({ page }) => {
    await offline(page);
    expect(
      await page.evaluate(() => ({
        root: getComputedStyle(document.documentElement).colorScheme,
        meta: document
          .querySelector('meta[name="color-scheme"]')
          ?.getAttribute("content"),
      })),
    ).toEqual({ root: "dark", meta: "dark" });
  });

  test("loads the brand files on a nested route", async ({ page }) => {
    await offline(page);
    await page.goto("/operations/targets/example");
    const lockup = page
      .getByRole("link", { name: "Firefly Weave Studio home" })
      .locator("img.brand-lockup");
    await expect(lockup).toBeVisible();
    expect(
      await lockup.evaluate(
        (e: HTMLImageElement) => e.complete && e.naturalWidth > 0,
      ),
    ).toBe(true);
    await page.evaluate(() => document.fonts.ready);
    expect(
      await page.evaluate(() => document.fonts.check("14px Manrope")),
    ).toBe(true);
  });

  test("shows the lockup, the mark when narrow, and the favicon set", async ({
    page,
  }) => {
    await offline(page);
    const home = page.getByRole("link", { name: "Firefly Weave Studio home" });
    const lockup = home.locator("img.brand-lockup");
    const mark = home.locator("img.brand-mark");
    const drawn = (image: Locator) =>
      image.evaluate((e: HTMLImageElement) => ({
        loaded: e.complete && e.naturalWidth > 0,
        width: e.getBoundingClientRect().width,
        padding: getComputedStyle(e).paddingLeft,
      }));
    await expect(lockup).toBeVisible();
    await expect(mark).toBeHidden();
    expect(await drawn(lockup)).toMatchObject({ loaded: true, width: 200 });
    // The box is reserved before the SVG loads (200 x 248.25/1427.4 = 34.8).
    await expect(lockup).toHaveAttribute("width", "200");
    await expect(lockup).toHaveAttribute("height", "35");
    // At 1280px and below the sidebar narrows: the 32px mark in a 44px box.
    await page.setViewportSize({ width: 1280, height: 720 });
    await expect(lockup).toBeHidden();
    await expect(mark).toBeVisible();
    expect(await drawn(mark)).toEqual({
      loaded: true,
      width: 44,
      padding: "6px",
    });
    const head = await page.evaluate(() => ({
      icons: [
        ...document.querySelectorAll(
          'link[rel="icon"], link[rel="apple-touch-icon"]',
        ),
      ].map((link) => link.getAttribute("href")),
      manifests: document.querySelectorAll('link[rel="manifest"]').length,
      theme: document
        .querySelector('meta[name="theme-color"]')
        ?.getAttribute("content"),
      scheme: document
        .querySelector('meta[name="color-scheme"]')
        ?.getAttribute("content"),
    }));
    expect(head).toEqual({
      icons: ["favicon.ico", "favicon.svg", "apple-touch-icon.png"],
      manifests: 0,
      theme: "#10110f",
      scheme: "dark",
    });
    for (const file of [
      "/favicon.ico",
      "/favicon.svg",
      "/apple-touch-icon.png",
    ])
      expect(
        (await page.request.get(file)).headers()["content-type"],
        file,
      ).toMatch(/^image\//);
  });

  test("pairing shows the lockup on charcoal", async ({ page }) => {
    await page.route("**/studio/session", (r) =>
      r.fulfill({
        json: { paired: false, version: "1", mode: "offline", profile: null },
      }),
    );
    await page.goto("/");
    const lockup = page.getByRole("img", {
      name: "Firefly Weave",
      exact: true,
    });
    await expect(lockup).toBeVisible();
    expect(
      await lockup.evaluate((e: HTMLImageElement) => ({
        src: new URL(e.src).pathname,
        loaded: e.complete && e.naturalWidth > 0,
        width: e.getBoundingClientRect().width,
      })),
    ).toEqual({
      src: "/assets/weave-lockup-reversed.svg",
      loaded: true,
      width: 240,
    });
    expect(
      await page.evaluate(
        () => getComputedStyle(document.documentElement).backgroundColor,
      ),
    ).toBe(await tokenColor(page, "--bg"));
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
