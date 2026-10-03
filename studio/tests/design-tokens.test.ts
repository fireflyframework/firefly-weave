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
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const source = (path: string) =>
  readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");
const styles = source("styles.css");

interface Declaration {
  /** Enclosing at-rules, outermost first. */
  media: string;
  selector: string;
  property: string;
  value: string;
}

/** Every declaration in a stylesheet, with its selector and at-rules. */
function declarations(css: string): Declaration[] {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const found: Declaration[] = [];
  const stack: string[] = [];
  let buffer = "";
  for (const char of text) {
    if (char === "{") {
      stack.push(buffer.trim());
      buffer = "";
    } else if (char === "}") {
      const selector = stack.pop() ?? "";
      for (const part of buffer.split(";")) {
        const colon = part.indexOf(":");
        if (colon < 0 || !part.trim()) continue;
        found.push({
          media: stack.join(" / "),
          selector,
          property: part.slice(0, colon).trim(),
          value: part.slice(colon + 1).trim(),
        });
      }
      buffer = "";
    } else buffer += char;
  }
  return found;
}
const all = declarations(styles);
const root = new Map(
  all
    .filter((d) => d.selector === ":root" && d.property.startsWith("--"))
    .map((d) => [d.property, d.value]),
);

/** WCAG 2.x contrast ratio of two #rrggbb colors. */
function contrast(a: string, b: string) {
  const luminance = (hex: string) => {
    const n = parseInt(hex.slice(1), 16);
    const [r, g, bl] = [n >> 16, (n >> 8) & 255, n & 255].map((c) => {
      const s = c / 255;
      return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}
const token = (name: string) => {
  const value = root.get(name);
  if (!value) throw Error(`missing token ${name}`);
  return value;
};

describe("design tokens", () => {
  it("keeps the brand and declares the plan's tokens", () => {
    expect(Object.fromEntries(root)).toMatchObject({
      "--forest": "#173d34",
      "--jade": "#367d68",
      "--mint": "#a1d1b9",
      "--mist": "#eef4f0",
      "--gold": "#b88322",
      "--field-border": "#738c80",
      "--flow-line": "#5f8a76",
      "--disabled-bg": "#eef2ef",
      "--disabled-ink": "#5c706a",
      "--focus": "#2c6a57",
      "--focus-on-dark": "#a1d1b9",
      "--live": "#b88322",
      "--control-md": "36px",
      "--control-touch": "44px",
      "--radius-lg": "12px",
    });
    for (const name of [
      "--type-caption",
      "--type-small",
      "--type-body",
      "--type-label",
      "--type-button",
      "--type-title-sm",
      "--type-title-md",
      "--type-title-lg",
      "--type-display",
      "--type-mono",
      "--shadow-1",
      "--shadow-2",
      "--shadow-3",
      "--info-ink",
      "--info-bg",
      "--info-bd",
      "--panel-w",
    ])
      expect(root.has(name), name).toBe(true);
  });

  it("has no raw hex color outside :root and forced-colors", () => {
    const raw = all.filter(
      (d) =>
        d.selector !== ":root" &&
        !d.media.includes("forced-colors") &&
        /#[0-9a-f]{3,8}\b/i.test(d.value),
    );
    expect(
      raw.map((d) => `${d.selector} { ${d.property}: ${d.value} }`),
    ).toEqual([]);
  });

  it("uses only the four font weights Avenir Next has", () => {
    const weights = all
      .filter((d) => d.property === "font-weight")
      .map((d) => d.value);
    for (const weight of weights)
      expect(["400", "500", "600", "700", "inherit"]).toContain(weight);
    // Shorthands and type tokens follow the same rule.
    for (const d of all.filter((d) => d.property.startsWith("--type-")))
      expect(d.value).toMatch(/^(400|500|600|700) /);
  });

  it("sets no text below 12px outside the canvas (wave 3 owns it)", () => {
    const small = all.filter((d) => {
      const size = /^([\d.]+)px$/.exec(d.value);
      return (
        d.property === "font-size" &&
        !!size &&
        Number(size[1]) < 12 &&
        !/graph-flow|node-|canvas-chip/.test(d.selector)
      );
    });
    expect(small.map((d) => `${d.selector}: ${d.value}`)).toEqual([]);
  });

  it("meets the contrast the plan measured", () => {
    const white =
      token("--surface") === "#fff" ? "#ffffff" : token("--surface");
    // Field borders and canvas lines: 3:1 against what they sit on (1.4.11).
    for (const surface of [white, token("--bg"), token("--mist")])
      expect(contrast(token("--field-border"), surface)).toBeGreaterThanOrEqual(
        3,
      );
    expect(contrast(token("--flow-line"), token("--canvas"))).toBeGreaterThan(
      3.7,
    );
    // Disabled text stays legible.
    expect(
      contrast(token("--disabled-ink"), token("--disabled-bg")),
    ).toBeGreaterThanOrEqual(4.5);
    // The focus ring is visible on every light surface, mint on forest.
    for (const surface of [white, token("--bg"), token("--mist")])
      expect(contrast(token("--focus"), surface)).toBeGreaterThanOrEqual(3);
    expect(
      contrast(token("--focus-on-dark"), token("--forest")),
    ).toBeGreaterThanOrEqual(7);
    expect(
      contrast(token("--focus-on-dark"), token("--forest-active")),
    ).toBeGreaterThanOrEqual(3);
    // Links on mist, text on a selected row.
    expect(contrast(token("--link"), token("--mist"))).toBeGreaterThan(4.5);
    expect(contrast(token("--muted"), token("--selected"))).toBeGreaterThan(
      4.5,
    );
    expect(
      contrast(token("--on-dark-muted"), token("--forest-active")),
    ).toBeGreaterThan(4.5);
  });

  it("keeps every status tone readable and visible on a selected row", () => {
    for (const tone of ["success", "info", "warning", "danger", "neutral"]) {
      const ink = token(`--${tone}-ink`);
      const bg = token(`--${tone}-bg`);
      const border = token(`--${tone}-bd`);
      expect(contrast(ink, bg), `${tone} ink`).toBeGreaterThanOrEqual(6);
      // A pill on a selected row keeps an edge (C15 in the plan).
      expect(
        contrast(border, token("--selected")),
        `${tone} border`,
      ).toBeGreaterThanOrEqual(1.3);
    }
    expect(contrast("#ffffff", token("--danger"))).toBeGreaterThan(7);
  });

  it("styles disabled buttons with tokens, never opacity", () => {
    const disabled = all.filter((d) =>
      /:disabled|aria-disabled/.test(d.selector),
    );
    expect(disabled.some((d) => d.value === "var(--disabled-bg)")).toBe(true);
    expect(
      disabled.filter((d) => d.property === "opacity" && d.value !== "1"),
    ).toEqual([]);
    // Component styles follow the same rule.
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) walk(path);
        else if (/\.(ts|css)$/.test(name)) files.push(path);
      }
    };
    walk(new URL("../src/app", import.meta.url).pathname);
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      for (const rule of text.matchAll(/([^{}]*)\{([^{}]*)\}/g))
        if (/:disabled|aria-disabled|\.disabled\b/.test(rule[1]))
          expect(rule[2], `${file}: ${rule[1].trim()}`).not.toMatch(
            /opacity\s*:\s*0?\.\d/,
          );
    }
  });

  it("draws one focus ring: 2px in --focus, mint in the sidebar", () => {
    const ring = all.find(
      (d) =>
        /^:is\(\s*button,\s*a,/.test(d.selector) &&
        d.selector.includes(":focus-visible") &&
        d.property === "outline",
    );
    expect(ring?.value).toBe("2px solid var(--focus)");
    const dark = all.find(
      (d) =>
        d.selector.startsWith(":is(.sidebar") && d.property === "outline-color",
    );
    expect(dark?.value).toBe("var(--focus-on-dark)");
    // No component keeps its own 3px ring.
    for (const file of [
      "app/home-dashboard.css",
      "app/property-grid.css",
      "app/designer/step-picker.ts",
      "app/integrations/action-picker.ts",
      "app/forms/ui/reference-combobox.ts",
      "app/workspace-picker.ts",
    ])
      expect(source(file), file).not.toMatch(/outline:\s*3px/);
  });

  it("writes group labels in sentence case", () => {
    for (const file of [
      "app/designer/step-picker.ts",
      "app/integrations/action-picker.ts",
      "app/home-dashboard.css",
    ])
      expect(source(file), file).not.toMatch(
        /text-transform:\s*(uppercase|capitalize)/,
      );
    expect(source("app/property-grid.css")).not.toContain("--secondary");
  });
});
