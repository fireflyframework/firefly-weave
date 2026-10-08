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
import { fileURLToPath } from "node:url";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

const source = (path: string) =>
  readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");
const appDir = fileURLToPath(new URL("../src/app", import.meta.url));
/** Every component stylesheet, template and script under src/app. */
function appFiles(dir = appDir) {
  const found: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) found.push(...appFiles(path));
    else if (/\.(ts|css|html)$/.test(name)) found.push(path);
  }
  return found;
}
const styles = source("styles.css");

/**
 * The string literals that start at `from`: one literal, or the literals of an
 * array (the shape of a component's `styles`).
 */
function literals(text: string, from: number): string[] {
  const found: string[] = [];
  let i = from;
  const skip = () => {
    while (/[\s,]/.test(text[i] ?? "")) i++;
  };
  const read = () => {
    const quote = text[i++];
    let value = "";
    while (i < text.length && text[i] !== quote) {
      if (text[i] === "\\") i++;
      value += text[i++];
    }
    i++;
    return value;
  };
  skip();
  if (/[`'"]/.test(text[i] ?? "")) found.push(read());
  else if (text[i] === "[") {
    i++;
    for (skip(); i < text.length && text[i] !== "]"; skip()) {
      if (/[`'"]/.test(text[i])) found.push(read());
      else i++;
    }
  }
  return found;
}
/**
 * The CSS a component file carries: a .css file whole; a .ts file's `styles`
 * blocks and its `const …Styles = \`…\`` stylesheets.
 */
function componentCss(file: string): string[] {
  const text = readFileSync(file, "utf8");
  if (file.endsWith(".css")) return [text];
  if (!file.endsWith(".ts")) return [];
  return [...text.matchAll(/\bstyles\s*:|\bconst\s+\w*[sS]tyles\s*=/g)].flatMap(
    (m) => literals(text, m.index + m[0].length),
  );
}
/** Every declaration in the component styles under src/app, by file. */
const appDeclarations = appFiles().flatMap((file) =>
  componentCss(file).flatMap((css) =>
    declarations(css).map((d) => ({ file, ...d })),
  ),
);

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
/** Contrast of two tokens that hold #rrggbb colors. */
const ratio = (a: string, b: string) => contrast(token(a), token(b));
/** The color-scheme :root declares (it is not a custom property). */
const colorScheme = () =>
  all.find((d) => d.selector === ":root" && d.property === "color-scheme")
    ?.value;

describe("design tokens", () => {
  const surfaces = [
    "--bg",
    "--sunken",
    "--surface",
    "--raised",
    "--hover",
    "--selected",
  ];
  const tones = ["success", "info", "warning", "danger"];

  it("declares the dark theme and its anchor values", () => {
    expect(colorScheme()).toBe("dark");
    expect(Object.fromEntries(root)).toMatchObject({
      "--bg": "#10110f",
      "--surface": "#1a1b17",
      "--text": "#f3f1eb",
      "--muted": "#bfb8ab",
      "--accent": "#ffb34a",
      "--on-accent": "#10110f",
      "--field-border": "#767672",
      "--flow-line": "#767672",
      "--focus": "#ffb34a",
      "--live": "#ffb34a",
      "--control-md": "36px",
      "--control-touch": "44px",
      "--radius-lg": "12px",
    });
  });

  it("declares every token lanes build on (contract C1)", () => {
    const names = [
      ...["--bg", "--sunken", "--surface", "--raised", "--hover", "--selected"],
      ...[
        "--accent-soft",
        "--disabled-bg",
        "--canvas",
        "--canvas-dot",
        "--scrim",
      ],
      ...["--text", "--muted", "--subtle", "--link", "--disabled-ink"],
      ...[
        "--accent",
        "--accent-hover",
        "--accent-press",
        "--on-accent",
        "--accent-glow",
      ],
      ...[
        "--line",
        "--border",
        "--border-hover",
        "--field-border",
        "--field-border-hover",
      ],
      ...tones.flatMap((t) => [
        `--${t}-ink`,
        `--${t}-bg`,
        `--${t}-bd`,
        `--${t}`,
      ]),
      ...["--neutral-ink", "--neutral-bg", "--neutral-bd", "--danger-hover"],
      ...["--focus", "--selection"],
      ...["--flow-line", "--flow-line-dim", "--node-bg", "--node-border"],
      ...["--live", "--live-glow", "--marquee-bg"],
      ...[
        "--series-1",
        "--series-2",
        "--series-3",
        "--series-4",
        "--series-other",
      ],
      ...["yellow", "blue", "green", "pink", "purple", "gray"].flatMap((n) => [
        `--note-${n}-bg`,
        `--note-${n}-bd`,
      ]),
      ...["--syntax-keyword", "--syntax-string", "--syntax-number"],
      ...["--diff-add-bg", "--diff-add-ink", "--diff-del-bg", "--diff-del-ink"],
      "--skeleton",
      ...["--font-sans", "--font-mono"],
      ...[
        "caption",
        "small",
        "body",
        "label",
        "button",
        "title-sm",
        "title-md",
      ].map((t) => `--type-${t}`),
      ...["--type-title-lg", "--type-display", "--type-mono"],
      ...["--shadow-1", "--shadow-2", "--shadow-3"],
      ...["--ease", "--ease-move", "--ease-exit"],
      ...["--dur-fast", "--dur", "--dur-panel", "--dur-expressive"],
      ...["--control-sm", "--control-md", "--control-touch", "--panel-w"],
    ];
    expect(names.filter((name) => !root.has(name))).toEqual([]);
    expect(token("--font-sans")).toMatch(/^"Manrope", system-ui/);
    expect(token("--type-title-lg")).toMatch(/^600 24px\/32px /);
    expect(token("--type-display")).toMatch(/^500 32px\/40px /);
    expect(token("--dur-expressive")).toBe("1200ms");
  });

  it("drops the light-theme brand tokens everywhere under studio/src", () => {
    const removed =
      /--(forest|forest-hover|forest-press|forest-active|jade|jade-strong|mint|mist|gold|gold-soft|on-dark|on-dark-muted|on-dark-accent|focus-on-dark)\b/;
    for (const file of [
      fileURLToPath(new URL("../src/styles.css", import.meta.url)),
      ...appFiles(),
    ])
      expect(readFileSync(file, "utf8"), file).not.toMatch(removed);
    expect(styles).not.toContain(".forest-surface");
  });

  it("writes colors only in :root: no literal in styles.css or src/app", () => {
    const literal = /#[0-9a-f]{3,8}\b|\brgba?\(|\bhsla?\(/i;
    const raw = all.filter(
      (d) =>
        d.selector !== ":root" &&
        !d.media.includes("forced-colors") &&
        literal.test(d.value),
    );
    expect(
      raw.map((d) => `${d.selector} { ${d.property}: ${d.value} }`),
    ).toEqual([]);
    for (const file of appFiles()) {
      const text = readFileSync(file, "utf8").replace(/href="#[^"]*"/g, "");
      const lines = text.split("\n").filter((line) => literal.test(line));
      expect(lines, file).toEqual([]);
    }
  });

  it("never redefines a theme token outside :root (one value set per theme)", () => {
    const local = all.filter(
      (d) =>
        d.selector !== ":root" &&
        d.property.startsWith("--") &&
        root.has(d.property),
    );
    expect(local.map((d) => `${d.selector} { ${d.property} }`)).toEqual([]);
    for (const file of appFiles())
      for (const [, name] of readFileSync(file, "utf8").matchAll(
        /^\s*(--[a-z0-9-]+)\s*:/gm,
      ))
        expect(root.has(name), `${file} redefines ${name}`).toBe(false);
  });

  it("reads the component styles under src/app (the rules below scan them)", () => {
    const files = new Set(appDeclarations.map((d) => d.file));
    // A .css file, a `styles: [...]` array, a `styles: \`...\`` literal and a
    // shared `const …Styles` stylesheet: all four shapes must be found.
    for (const name of [
      "operate/clusters/clusters.css",
      "workspace-picker.ts",
      "integrations/ai-setup-wizard.ts",
      "integrations/http-action-styles.ts",
    ])
      expect([...files].some((f) => f.replace(/\\/g, "/").endsWith(name))).toBe(
        true,
      );
    expect(appDeclarations.length).toBeGreaterThan(1500);
  });

  it("uses only the weights Studio uses", () => {
    const weights = ["400", "500", "600", "700"];
    const faces = all.filter((d) => d.selector !== "@font-face");
    for (const d of faces.filter((d) => d.property === "font-weight"))
      expect([...weights, "inherit"]).toContain(d.value);
    // Shorthands and type tokens follow the same rule.
    for (const d of faces.filter((d) => d.property.startsWith("--type-")))
      expect(d.value).toMatch(/^(400|500|600|700) /);
    // Component styles follow it too: a weight off the scale (650, 550) would
    // fall between the variable font's instances.
    const component = appDeclarations.filter(
      (d) => d.property === "font-weight",
    );
    expect(component.length).toBeGreaterThan(50);
    expect(
      component
        .filter((d) => ![...weights, "inherit"].includes(d.value))
        .map((d) => `${d.file}: ${d.selector} { font-weight: ${d.value} }`),
    ).toEqual([]);
    const shorthand = [...faces, ...appDeclarations].filter(
      (d) => d.property === "font" && /^\d{3}\s/.test(d.value),
    );
    expect(
      shorthand
        .filter((d) => !weights.includes(d.value.slice(0, 3)))
        .map((d) => `${d.selector} { font: ${d.value} }`),
    ).toEqual([]);
    // The variable font covers the whole range it declares.
    for (const d of all.filter(
      (d) => d.selector === "@font-face" && d.property === "font-weight",
    ))
      expect(d.value).toBe("200 800");
  });

  it("sets no text below 12px outside the canvas (wave 3 owns it)", () => {
    const px = (d: { property: string; value: string }) => {
      const size =
        d.property === "font-size"
          ? /^([\d.]+)px$/.exec(d.value)
          : d.property === "font"
            ? /^(?:\d{3}\s+)?([\d.]+)px\b/.exec(d.value)
            : null;
      return size ? Number(size[1]) : Infinity;
    };
    const canvas = (selector: string) =>
      /graph-flow|node-|canvas-chip/.test(selector);
    const small = all.filter((d) => px(d) < 12 && !canvas(d.selector));
    expect(small.map((d) => `${d.selector}: ${d.value}`)).toEqual([]);
    // Component styles follow the rule too. These four 11px declarations
    // predate the rule and are product sizes this change does not own: the
    // list may only shrink, so a new one fails and a fixed one must leave it.
    const known = [
      "forms/ui/schema-designer.ts: .sd-json pre",
      "task-form.ts: .schema-map-row button, .schema-remove",
      "task-form.ts: .schema-null",
      "templates/new-menu.ts: .new-menu-list small",
    ];
    const name = (d: { file: string; selector: string }) =>
      `${relative(appDir, d.file).replace(/\\/g, "/")}: ${d.selector.replace(/\s+/g, " ")}`;
    const component = appDeclarations
      .filter((d) => px(d) < 12 && !canvas(d.selector))
      .map(name);
    expect(component.filter((n) => !known.includes(n))).toEqual([]);
    expect(known.filter((n) => !component.includes(n))).toEqual([]);
  });

  it("keeps text at 4.5:1 and boundaries at 3:1 on every surface", () => {
    for (const surface of surfaces) {
      for (const ink of ["--text", "--muted", "--subtle", "--link"])
        expect(
          ratio(ink, surface),
          `${ink} on ${surface}`,
        ).toBeGreaterThanOrEqual(4.5);
      for (const line of ["--field-border", "--focus"])
        expect(
          ratio(line, surface),
          `${line} on ${surface}`,
        ).toBeGreaterThanOrEqual(3);
    }
    expect(ratio("--disabled-ink", "--disabled-bg")).toBeGreaterThanOrEqual(
      4.5,
    );
  });

  it("draws the canvas and charts at 3:1", () => {
    expect(ratio("--flow-line", "--canvas")).toBeGreaterThan(3.7);
    expect(ratio("--node-border", "--canvas")).toBeGreaterThan(3.7);
    expect(ratio("--flow-line-dim", "--canvas")).toBeGreaterThanOrEqual(3);
    for (const series of [
      "--series-1",
      "--series-2",
      "--series-3",
      "--series-4",
      "--series-other",
    ])
      expect(ratio(series, "--surface"), series).toBeGreaterThanOrEqual(3);
  });

  it("puts 7:1 text on every solid fill", () => {
    for (const fill of [
      "--accent",
      "--accent-hover",
      "--accent-press",
      "--danger-hover",
      ...tones.map((t) => `--${t}`),
    ])
      expect(ratio("--on-accent", fill), fill).toBeGreaterThanOrEqual(7);
  });

  it("keeps every status tone readable and visible on a selected row", () => {
    for (const tone of [...tones, "neutral"]) {
      expect(
        ratio(`--${tone}-ink`, `--${tone}-bg`),
        `${tone} ink`,
      ).toBeGreaterThanOrEqual(6);
      for (const surface of ["--surface", "--raised", "--selected"])
        expect(
          ratio(`--${tone}-ink`, surface),
          `${tone} on ${surface}`,
        ).toBeGreaterThanOrEqual(4.5);
      // A pill on a selected row keeps an edge.
      expect(
        ratio(`--${tone}-bd`, "--selected"),
        `${tone} border`,
      ).toBeGreaterThanOrEqual(1.3);
    }
  });

  it("keeps notes, code and diffs readable", () => {
    for (const note of ["yellow", "blue", "green", "pink", "purple", "gray"])
      expect(ratio("--text", `--note-${note}-bg`), note).toBeGreaterThanOrEqual(
        4.5,
      );
    for (const syntax of [
      "--syntax-keyword",
      "--syntax-string",
      "--syntax-number",
    ])
      expect(ratio(syntax, "--sunken"), syntax).toBeGreaterThanOrEqual(4.5);
    expect(ratio("--diff-add-ink", "--diff-add-bg")).toBeGreaterThanOrEqual(6);
    expect(ratio("--diff-del-ink", "--diff-del-bg")).toBeGreaterThanOrEqual(6);
  });

  it("draws every step kind alike, the canvas dots in --canvas-dot, and a disabled segment at 7:1", () => {
    // Spec §2.1: no role colors. A Human task differs by icon and shape only.
    const human = /\.human(?![\w-])/;
    expect(all.filter((d) => human.test(d.selector))).toEqual([]);
    expect(appDeclarations.filter((d) => human.test(d.selector))).toEqual([]);
    // Spec §2.2: the dot grid is its own decorative token, not a border.
    expect(
      all.find(
        (d) => d.selector === ".canvas" && d.property === "background-image",
      )?.value,
    ).toContain("var(--canvas-dot)");
    // A checked segment of a disabled mode switch keeps 7:1 text on its fill.
    const segment = appDeclarations.find(
      (d) =>
        d.file.endsWith("property-grid.css") &&
        d.selector === '.mode-switch button:disabled[aria-checked="true"]' &&
        d.property === "background",
    );
    expect(segment?.value).toMatch(/^var\(--muted\)/);
    expect(ratio("--on-accent", "--muted")).toBeGreaterThanOrEqual(7);
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
    for (const file of appFiles().filter((f) => /\.(ts|css)$/.test(f))) {
      const text = readFileSync(file, "utf8");
      for (const rule of text.matchAll(/([^{}]*)\{([^{}]*)\}/g))
        if (/:disabled|aria-disabled|\.disabled\b/.test(rule[1]))
          expect(rule[2], `${file}: ${rule[1].trim()}`).not.toMatch(
            /opacity\s*:\s*0?\.\d/,
          );
    }
  });

  it("draws one 2px amber focus ring on every surface", () => {
    const ring = all.find(
      (d) =>
        /^:is\(\s*button,\s*a,/.test(d.selector) &&
        d.selector.includes(":focus-visible") &&
        d.property === "outline",
    );
    expect(ring?.value).toBe("2px solid var(--focus)");
    // Every surface is dark: no sidebar or toast override remains.
    expect(all.filter((d) => d.property === "outline-color")).toEqual([]);
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

  it("selects text in translucent amber with paper text", () => {
    const selection = all.filter((d) => d.selector === "::selection");
    expect(
      Object.fromEntries(selection.map((d) => [d.property, d.value])),
    ).toEqual({
      background: "var(--selection)",
      color: "var(--text)",
    });
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
