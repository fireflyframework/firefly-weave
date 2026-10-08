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
// A visual tour of every important Studio screen and state at phone, desktop
// minimum, tablet, laptop, desktop and wide sizes, plus a 1280x720 window at
// 200% zoom (a 640x360 CSS viewport at twice the pixel density). Each state is
// saved as a screenshot under test-results/visual-tour/<size>/ (and copied to
// STUDIO_VISUAL_TOUR_COPY when it names a folder) and checked: no sideways
// page scroll, no control cut off by the viewport or its container, no
// control covered by another element, text contrast of at least 4.5:1 (3:1
// for large text) measured against the page's own dark ground, a visible focus
// indicator on Tab whose outline reaches 3:1, Manrope rendering the text, and
// no icon name without a drawing.
// The platform and the local host are mocked; the local authoring endpoints
// and the simulation artifact use the repository's real Python code.
import { selectChoice } from "./support";
import { test, expect, Page, Request, Route } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { parse } from "yaml";
import {
  allCapabilities,
  command,
  connected,
  newWorkflow,
  offline,
  workerAction,
  type CatalogEntry,
} from "./support";
import {
  HostOptions,
  PlatformHost,
  acmeDiscovery,
  acmeOption,
  acmePlatform,
  globexPlatform,
} from "./platform-host";
import { DesignerPage, StepKind, stepLabels } from "./designer-po";
import { openWorkflow, type CanvasPage } from "./canvas-po";
import {
  chooseAction,
  openPaletteIntegrations,
  openYaml,
} from "./integrations-po";
import {
  hasPython,
  localAuthoring,
  python,
  stopLocalAuthoring,
} from "./local-authoring";
import { crossingOf } from "./woven-crossing";

interface Size {
  tag: string;
  width: number;
  height: number;
  scale?: number;
}
const sizes: Size[] = [
  { tag: "360x740", width: 360, height: 740 },
  { tag: "600x500", width: 600, height: 500 },
  { tag: "768x1024", width: 768, height: 1024 },
  { tag: "1280x720", width: 1280, height: 720 },
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "1920x1080", width: 1920, height: 1080 },
  // A 1280x720 window at 200% browser zoom.
  { tag: "1280x720-zoom200", width: 640, height: 360, scale: 2 },
];
const folder = resolve("test-results/visual-tour");
const copies = process.env["STUDIO_VISUAL_TOUR_COPY"]?.trim();
const repository = resolve("..");
const project = "**/studio/api/api/v1/tenants/tenant/projects/project";
const environment = `${project}/environments/development`;
const digest =
  "eddfa829184f8505fd0e1bc7a84b490fc57b555a39495b9724b2728b277133d8";
const connectorVersion = "7a8cbeef-7d5c-483a-b926-4157ad4286d0";
const httpRelease = "3f2a9b1c-0000-4000-8000-000000000001";
const workerRelease = "3f2a9b1c-0000-4000-8000-000000000002";
const revisionId = "0f8fad5b-d9cb-469f-a165-70867728950e";
const versionId = "11111111-1111-4111-8111-111111111111";

/** Floating surfaces that cover the page on purpose while they are open. */
const overlays =
  '[aria-modal="true"], .modal-backdrop, [role="dialog"], [role="menu"], [role="listbox"], .platform-menu, .inspector, .record-detail, .palette.popover-visible, .sim-panel, .toast';

test.describe.configure({ mode: "parallel" });
test.afterAll(() => stopLocalAuthoring());

// ---------------------------------------------------------------- checks

/** What the in-page audit found; `truncated` is informational only. */
interface Audit {
  scrollWidth: number;
  width: number;
  /** How far the document itself scrolls down inside the app shell. */
  scrollDown: number;
  /** What sticks out below the window when the document scrolls. */
  below: string[];
  scrollers: string[];
  offscreen: string[];
  clipped: string[];
  covered: string[];
  truncated: string[];
}

/**
 * Runs in the page. Checks the page and each visible control: no sideways page
 * scroll, no control partly outside the viewport or cut by a clipping
 * container, and a real pointer at each control's center reaches it (scrolling
 * it into view first). An open modal limits the check to the modal; controls
 * under an open popover, menu, drawer or overlay inspector are skipped.
 */
function auditPage(overlays: string): Audit {
  const width = window.innerWidth;
  const height = window.innerHeight;
  const label = (e: Element) =>
    (
      e.getAttribute("aria-label") ||
      (e as HTMLElement).innerText ||
      e.getAttribute("placeholder") ||
      e.getAttribute("title") ||
      (e as HTMLInputElement).value ||
      ""
    )
      .trim()
      .replace(/\s+/g, " ")
      .slice(0, 48);
  const describe = (e: Element | null) => {
    if (!e) return "nothing";
    const classes =
      typeof e.className === "string"
        ? e.className.trim().split(/\s+/).filter(Boolean).slice(0, 2)
        : [];
    const head = [e.tagName.toLowerCase(), ...classes].join(".");
    const text =
      label(e) ||
      (e.getAttribute("aria-labelledby") &&
        document.getElementById(e.getAttribute("aria-labelledby")!)
          ?.textContent) ||
      `in ${e.parentElement?.className || e.parentElement?.tagName}`;
    return `${head} "${String(text).trim().slice(0, 48)}"`;
  };
  const style = (e: Element) => getComputedStyle(e);
  const hidden = (e: Element) => {
    // Closed <details> content and content-visibility subtrees are not shown.
    if (
      !e.checkVisibility({
        contentVisibilityAuto: true,
        opacityProperty: true,
        visibilityProperty: true,
      })
    )
      return true;
    for (let n: Element | null = e; n; n = n.parentElement) {
      const s = style(n);
      if (s.display === "none" || s.visibility === "hidden") return true;
      if (Number(s.opacity) === 0) return true;
      if (s.clip !== "auto" && s.clip !== "") return true;
      if (/inset\(50%|circle\(0/.test(s.clipPath)) return true;
    }
    const box = e.getBoundingClientRect();
    return box.width <= 1 || box.height <= 1;
  };
  const scrollWidth = document.scrollingElement!.scrollWidth;
  // The app shell fits the window; only its panes scroll.
  const scrollDown = document.querySelector(".shell")
    ? document.scrollingElement!.scrollHeight - height
    : 0;
  // Elements below the window that no pane clips: in normal flow outside
  // every clipping pane, or absolutely positioned against the document
  // itself (no positioned ancestor), which the panes don't clip either.
  const below = !scrollDown
    ? []
    : [...document.querySelectorAll<HTMLElement>("body *")]
        .filter((e) => {
          if (e.getBoundingClientRect().bottom <= height + 1) return false;
          const absolute = style(e).position === "absolute";
          for (
            let a = e.parentElement;
            a && a !== document.body;
            a = a.parentElement
          ) {
            const s = style(a);
            if (absolute && (s.position !== "static" || s.transform !== "none"))
              return false;
            if (!absolute && s.overflowY !== "visible") return false;
          }
          return true;
        })
        .slice(0, 5)
        .map(
          (e) =>
            `${describe(e)} bottom ${Math.round(e.getBoundingClientRect().bottom)} ${style(e).position}`,
        );
  // Scrolling controls into view must not change the next capture.
  const offsets = [...document.querySelectorAll<HTMLElement>("*")]
    .filter(
      (e) => e.scrollHeight > e.clientHeight || e.scrollWidth > e.clientWidth,
    )
    .map((e) => [e, e.scrollTop, e.scrollLeft] as const);
  // Containers that scroll sideways by design.
  const sideways =
    ".table-scroll, pre, code, textarea, .canvas, f-flow, f-canvas, .run-graph, .source-editor, .hb-yaml, .hb-preview";
  const scrollers = [...document.querySelectorAll<HTMLElement>("body *")]
    .filter((e) => {
      const s = style(e);
      if (!/(auto|scroll)/.test(s.overflowX)) return false;
      if (e.scrollWidth <= e.clientWidth + 1) return false;
      if (e.closest(sideways)) return false;
      return !hidden(e);
    })
    .map(describe);

  const modals = [
    ...document.querySelectorAll<HTMLElement>('[aria-modal="true"]'),
  ].filter((e) => !hidden(e));
  const scope: Element = modals.at(-1) ?? document.body;
  const graph = ".canvas, .run-graph";
  const controls = [
    ...scope.querySelectorAll<HTMLElement>(
      'button, a[href], input:not([type="hidden"]), select, textarea, summary, [role="button"], [role="tab"], [role="checkbox"], [role="radio"], [role="switch"], [role="combobox"], [role="menuitem"], [role="option"]',
    ),
    // Inert controls wait on purpose (the palette while simulating).
  ].filter((e) => !hidden(e) && !e.closest("[inert]"));

  const offscreen: string[] = [];
  const clipped: string[] = [];
  const covered: string[] = [];
  /** The control's box as far as clipping and scrolling ancestors show it. */
  const visibleBox = (e: HTMLElement) => {
    const box = e.getBoundingClientRect();
    let [left, top, right, bottom] = [box.left, box.top, box.right, box.bottom];
    let fixed = style(e).position === "fixed";
    for (let a = e.parentElement; a && !fixed; a = a.parentElement) {
      const s = style(a);
      const r = a.getBoundingClientRect();
      if (s.overflowX !== "visible") {
        left = Math.max(left, r.left);
        right = Math.min(right, r.right);
      }
      if (s.overflowY !== "visible") {
        top = Math.max(top, r.top);
        bottom = Math.min(bottom, r.bottom);
      }
      if (s.position === "fixed") fixed = true;
    }
    return {
      left: Math.max(left, 0),
      top: Math.max(top, 0),
      right: Math.min(right, width),
      bottom: Math.min(bottom, height),
    };
  };
  const centerShown = (e: HTMLElement) => {
    const box = e.getBoundingClientRect();
    const view = visibleBox(e);
    const x = box.left + box.width / 2;
    const y = box.top + box.height / 2;
    return (
      x >= view.left && x <= view.right && y >= view.top && y <= view.bottom
    );
  };
  for (const control of controls) {
    const inGraph = !!control.closest(graph);
    if (!inGraph && !centerShown(control))
      control.scrollIntoView({
        block: "center",
        inline: "nearest",
        behavior: "instant",
      });
    const box = control.getBoundingClientRect();
    // Partly outside the viewport sideways (fully outside is off-canvas UI).
    // The graph pans like a map, and focus pans its controls into view.
    if (
      !inGraph &&
      ((box.left < -1 && box.right > 0) ||
        (box.right > width + 1 && box.left < width))
    )
      offscreen.push(describe(control));
    // Cut by a container that clips instead of scrolling. A scrolling
    // container in between can reveal the rest, so each axis stops there.
    if (!inGraph) {
      let fixed = style(control).position === "fixed";
      let [checkX, checkY] = [true, true];
      for (
        let a = control.parentElement;
        a && !fixed && (checkX || checkY);
        a = a.parentElement
      ) {
        const s = style(a);
        const r = a.getBoundingClientRect();
        const cutX =
          checkX &&
          /(hidden|clip)/.test(s.overflowX) &&
          ((box.left < r.left - 1 && box.right > r.left) ||
            (box.right > r.right + 1 && box.left < r.right));
        const cutY =
          checkY &&
          /(hidden|clip)/.test(s.overflowY) &&
          ((box.top < r.top - 1 && box.bottom > r.top) ||
            (box.bottom > r.bottom + 1 && box.top < r.bottom));
        if (cutX || cutY) {
          clipped.push(`${describe(control)} cut by ${describe(a)}`);
          break;
        }
        if (/(auto|scroll)/.test(s.overflowX)) checkX = false;
        if (/(auto|scroll)/.test(s.overflowY)) checkY = false;
        if (s.position === "fixed") fixed = true;
      }
    }
    if (!centerShown(control)) continue;
    /** What a pointer at the control's center reaches, if not the control. */
    const blocker = () => {
      const now = control.getBoundingClientRect();
      const x = now.left + now.width / 2;
      const y = now.top + now.height / 2;
      if (x < 0 || y < 0 || x > width || y > height) return null;
      const hit = document.elementFromPoint(x, y);
      if (!hit || hit === control || control.contains(hit)) return null;
      const labels = [...((control as HTMLInputElement).labels ?? [])];
      if (labels.some((l) => l === hit || l.contains(hit))) return null;
      const layer = hit.closest(overlays);
      if (layer && !layer.contains(control)) return null;
      return { hit, at: `@${Math.round(x)},${Math.round(y)}` };
    };
    let found = blocker();
    // Content may scroll under a sticky header or footer: centered in its
    // scroller, the control must be free.
    if (found) {
      control.scrollIntoView({
        block: "center",
        inline: "nearest",
        behavior: "instant",
      });
      found = blocker();
    }
    if (found)
      covered.push(
        `${describe(control)} ${found.at} covered by ${describe(found.hit)}`,
      );
  }
  const truncated = [...document.querySelectorAll<HTMLElement>("body *")]
    .filter((e) => {
      if (e.children.length && !e.innerText) return false;
      const s = style(e);
      return (
        s.textOverflow === "ellipsis" &&
        e.scrollWidth > e.clientWidth + 1 &&
        !hidden(e)
      );
    })
    .map(describe);
  for (const [e, top, left] of offsets) {
    e.scrollTop = top;
    e.scrollLeft = left;
  }
  return {
    scrollWidth,
    width,
    scrollDown,
    below,
    scrollers,
    offscreen,
    clipped,
    covered,
    truncated,
  };
}

/**
 * Remembers how each focusable element looks before it is focused: itself,
 * its parent and grandparent (rings drawn with :focus-within) and its first
 * label (a visually hidden input shows focus on its label).
 */
function rememberFocusStyles() {
  const signature = (e: Element | null | undefined) => {
    if (!e) return "";
    const s = getComputedStyle(e);
    // A ring drawn by ::after (a row-sized hit area) shows focus too.
    const after = getComputedStyle(e, "::after");
    return [
      s.outlineStyle,
      s.outlineWidth,
      s.outlineColor,
      s.boxShadow,
      s.borderTopColor,
      s.borderBottomColor,
      s.backgroundColor,
      s.color,
      s.textDecorationLine,
      after.outlineStyle,
      after.outlineColor,
    ].join("|");
  };
  const parts = (e: Element) => [
    e,
    e.parentElement,
    e.parentElement?.parentElement ?? null,
    (e as HTMLInputElement).labels?.[0] ?? null,
  ];
  const store = new WeakMap<Element, string[]>();
  for (const e of document.querySelectorAll(
    'a[href], button, input, select, textarea, summary, [tabindex]:not([tabindex="-1"])',
  ))
    store.set(e, parts(e).map(signature));
  (window as unknown as { __tourFocus: unknown }).__tourFocus = {
    store,
    signature,
    parts,
  };
}

/**
 * The focused element: whether anything about it (or what shows its focus)
 * looks different, whether that is on screen, and what covers it.
 */
function focusedLooksFocused(overlays: string) {
  const { store, signature, parts } = (
    window as unknown as {
      __tourFocus: {
        store: WeakMap<Element, string[]>;
        signature: (e: Element | null) => string;
        parts: (e: Element) => (Element | null)[];
      };
    }
  ).__tourFocus;
  const active = document.activeElement as HTMLElement | null;
  if (!active || active === document.body) return null;
  const text = (
    active.getAttribute("aria-label") ||
    active.innerText ||
    active.getAttribute("placeholder") ||
    active.getAttribute("type") ||
    ""
  )
    .trim()
    .replace(/\s+/g, " ")
    .slice(0, 40);
  const name = `${active.tagName.toLowerCase()}${typeof active.className === "string" && active.className ? "." + active.className.trim().split(/\s+/)[0] : ""} "${text}"`;
  const s = getComputedStyle(active);
  const box = active.getBoundingClientRect();
  // A visually hidden control (1 px, clipped) must show focus elsewhere.
  const concealed =
    (box.width <= 1 && box.height <= 1) ||
    (s.clip !== "auto" && s.clip !== "") ||
    /inset\(50%/.test(s.clipPath);
  const before = store.get(active);
  const now = parts(active).map(signature);
  const changed = before
    ? before.some((value, i) =>
        concealed && i === 0 ? false : value !== now[i],
      )
    : false;
  const ring =
    !concealed &&
    s.outlineStyle !== "none" &&
    parseFloat(s.outlineWidth) >= 1 &&
    !/rgba\(\d+, \d+, \d+, 0\)|transparent/.test(s.outlineColor);
  // An outline ring reaches 3:1 against what is painted behind it (1.4.11).
  let ringContrast: number | null = null;
  if (ring) {
    type Rgba = [number, number, number, number];
    const parse = (value: string): Rgba | null => {
      const m = value.match(/rgba?\(([^)]+)\)/);
      if (!m) return null;
      const p = m[1]
        .split(/[ ,/]+/)
        .filter(Boolean)
        .map(Number);
      return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
    };
    const over = (top: Rgba, below: Rgba): Rgba => {
      const a = top[3] + below[3] * (1 - top[3]);
      const mix = (i: number) =>
        a ? (top[i] * top[3] + below[i] * below[3] * (1 - top[3])) / a : 0;
      return [mix(0), mix(1), mix(2), a];
    };
    const luminance = (c: Rgba) => {
      const [r, g, b] = c.slice(0, 3).map((v) => {
        const k = v / 255;
        return k <= 0.04045 ? k / 12.92 : ((k + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    };
    // The ring is drawn outside the box, over the parent's painted layers.
    const layers: Rgba[] = [];
    for (let e = active.parentElement; e; e = e.parentElement) {
      const color = parse(getComputedStyle(e).backgroundColor);
      if (color && color[3] > 0) {
        layers.push(color);
        if (color[3] >= 1) break;
      }
    }
    let back: Rgba = [0, 0, 0, 0];
    for (const layer of layers.reverse()) back = over(layer, back);
    const ink = parse(s.outlineColor);
    if (ink && back[3] > 0) {
      const [l1, l2] = [luminance(over(ink, back)), luminance(back)].sort(
        (x, y) => y - x,
      );
      ringContrast = (l1 + 0.05) / (l2 + 0.05);
    }
  }
  const shown =
    concealed && (active as HTMLInputElement).labels?.[0]
      ? (active as HTMLInputElement).labels![0].getBoundingClientRect()
      : box;
  const onScreen =
    shown.bottom > 0 &&
    shown.right > 0 &&
    shown.top < window.innerHeight &&
    shown.left < window.innerWidth;
  // Nothing the page drew may hide the focused control, and focus never goes
  // behind an open overlay (a drawer, a sheet); the two are told apart.
  let obscured = "";
  let behindOverlay = "";
  if (onScreen && !concealed) {
    // A text area shows its caret on the first line: that must be free.
    const probe =
      active.tagName === "TEXTAREA"
        ? Math.min(box.top + 16, box.top + box.height / 2)
        : box.top + box.height / 2;
    const x = Math.min(Math.max(box.left + box.width / 2, 0), innerWidth - 1);
    const y = Math.min(Math.max(probe, 0), innerHeight - 1);
    const hit = document.elementFromPoint(x, y);
    if (hit && hit !== active && !active.contains(hit)) {
      const label = [...((active as HTMLInputElement).labels ?? [])].some((l) =>
        l.contains(hit),
      );
      const layer = hit.closest(overlays);
      const what = `${hit.tagName.toLowerCase()}.${typeof hit.className === "string" ? hit.className.split(" ")[0] : ""} "${(hit as HTMLElement).innerText?.trim().replace(/\s+/g, " ").slice(0, 30) ?? ""}"`;
      if (label || (layer && layer.contains(active))) {
        // Covered by part of its own surface: fine.
      } else if (layer) behindOverlay = what;
      else obscured = what;
    }
  }
  return {
    name,
    visible: changed || ring,
    onScreen,
    obscured,
    behindOverlay,
    ringContrast,
  };
}

/**
 * Text contrast (WCAG 1.4.3) of every visible text run against the colors
 * painted behind it in the DOM: 4.5:1, or 3:1 for large text. Disabled and
 * inert controls are exempt, as WCAG allows; text over an image or a
 * gradient other than the canvas dot grid is skipped.
 */
function contrastProblems(): string[] {
  type Rgba = [number, number, number, number];
  const parse = (value: string): Rgba | null => {
    const m = value.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1]
      .split(/[ ,/]+/)
      .filter(Boolean)
      .map(Number);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
  };
  const over = (top: Rgba, below: Rgba): Rgba => {
    const a = top[3] + below[3] * (1 - top[3]);
    if (!a) return [0, 0, 0, 0];
    const mix = (i: number) =>
      (top[i] * top[3] + below[i] * below[3] * (1 - top[3])) / a;
    return [mix(0), mix(1), mix(2), a];
  };
  const luminance = (c: Rgba) => {
    const [r, g, b] = c.slice(0, 3).map((v) => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const ratio = (a: Rgba, b: Rgba) => {
    const [l1, l2] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (l1 + 0.05) / (l2 + 0.05);
  };
  // The page's own ground: html's background, then body's. Dark pages are
  // never measured against an assumed white.
  const problems: string[] = [];
  const ground = [document.documentElement, document.body]
    .map((e) => parse(getComputedStyle(e).backgroundColor))
    .find((c): c is Rgba => !!c && c[3] >= 1);
  const probe = document.createElement("i");
  probe.style.color = "var(--bg)";
  document.body.append(probe);
  const token = parse(getComputedStyle(probe).color);
  probe.remove();
  if (!ground) problems.push("the page background (html, body) is not opaque");
  else if (!token || ground.some((v, i) => i < 3 && v !== token[i]))
    problems.push(
      `the page background rgb(${ground.slice(0, 3).join(", ")}) is not --bg`,
    );
  /** The color painted behind an element, or null when it's an image. */
  const backdrop = (start: Element): Rgba | null => {
    const layers: Rgba[] = [];
    for (let e: Element | null = start; e; e = e.parentElement) {
      const s = getComputedStyle(e);
      const image = s.backgroundImage;
      const color = parse(s.backgroundColor);
      if (image !== "none" && !/radial-gradient/.test(image)) return null;
      if (color && color[3] > 0) {
        layers.push(color);
        if (color[3] >= 1) break;
      }
    }
    let result: Rgba = ground ?? [0, 0, 0, 1];
    for (const layer of layers.reverse()) result = over(layer, result);
    return result;
  };
  const exempt =
    'button:disabled, input:disabled, select:disabled, textarea:disabled, fieldset:disabled, [aria-disabled="true"], [inert], [aria-hidden="true"], .is-unreached, .sr-only, option';
  const found = new Map<string, string>();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.textContent?.trim();
    const parent = node.parentElement;
    if (!text || !parent || parent.closest(exempt)) continue;
    if (parent.closest("svg, script, style, textarea")) continue;
    const s = getComputedStyle(parent);
    if (s.visibility !== "visible") continue;
    const rects = [...parent.getClientRects()];
    if (!rects.some((r) => r.width > 1 && r.height > 1)) continue;
    const box = parent.getBoundingClientRect();
    if (box.bottom < 0 || box.top > innerHeight) continue;
    let opacity = 1;
    for (let e: Element | null = parent; e; e = e.parentElement)
      opacity *= Number(getComputedStyle(e).opacity);
    if (opacity < 0.1) continue;
    const ink = parse(s.color);
    const back = backdrop(parent);
    if (!ink || !back) continue;
    const shown = over([ink[0], ink[1], ink[2], ink[3] * opacity], back);
    const size = parseFloat(s.fontSize);
    const bold = Number(s.fontWeight) >= 700;
    const large = size >= 24 || (bold && size >= 18.66);
    const value = ratio(shown, back);
    if (value + 0.005 >= (large ? 3 : 4.5)) continue;
    const key = `"${text.slice(0, 32)}" ${value.toFixed(2)}:1 (${s.color} on rgb(${back
      .slice(0, 3)
      .map(Math.round)
      .join(", ")}), ${size}px)`;
    found.set(key, key);
  }
  return [...problems, ...found.values()];
}

class Tour {
  readonly problems: string[] = [];
  /** Findings that are reported but don't fail the tour. */
  readonly notes: string[] = [];
  private readonly report: Record<string, unknown> = {};
  constructor(
    readonly page: Page,
    readonly size: Size,
    readonly scene: string,
  ) {}

  /**
   * Lets fonts, transitions and deferred blocks settle before a capture.
   * Resolves false when Studio's own Manrope is not what renders the text.
   */
  async settle(): Promise<boolean> {
    return this.page.evaluate(async () => {
      await document.fonts.ready;
      const manrope =
        document.fonts.check("14px Manrope") &&
        [...document.fonts].some(
          (face) =>
            face.family.replace(/["']/g, "") === "Manrope" &&
            face.status === "loaded",
        );
      const finite = document
        .getAnimations()
        .filter((a) => a.effect?.getComputedTiming().iterations !== Infinity)
        .map((a) => a.finished.catch(() => undefined));
      await Promise.race([
        Promise.all(finite),
        new Promise((done) => setTimeout(done, 1000)),
      ]);
      await new Promise((done) =>
        requestAnimationFrame(() => requestAnimationFrame(done)),
      );
      // The designer fits a new graph a few frames after it renders: wait
      // until the canvas stops moving.
      const graph = () =>
        [...document.querySelectorAll(".canvas, .run-graph")]
          .map((canvas) => {
            const first = canvas.querySelector("[fNode], .graph-node");
            const box = first?.getBoundingClientRect();
            return `${box?.left}:${box?.top}:${box?.width}`;
          })
          .join("|");
      for (let i = 0, last = graph(); i < 20; i++) {
        await new Promise((done) => setTimeout(done, 120));
        const now = graph();
        if (now === last) break;
        last = now;
      }
      return manrope;
    });
  }

  private async capture(name: string) {
    const file = join(folder, this.size.tag, `${name}.png`);
    mkdirSync(dirname(file), { recursive: true });
    await this.page.screenshot({ path: file, fullPage: true });
    if (copies) {
      const copy = join(copies, this.size.tag, `${name}.png`);
      mkdirSync(dirname(copy), { recursive: true });
      copyFileSync(file, copy);
    }
  }

  /**
   * Captures the state and checks its layout and focus. `end` also captures
   * the main scroller scrolled to its end when the page is taller than the
   * window; `tabs` is how many Tab presses check the focus indicator.
   */
  shot = async (
    name: string,
    options: { end?: string; tabs?: number } = {},
  ) => {
    const page = this.page;
    const where = `${this.size.tag} ${name}`;
    // Park the pointer on the window's edge so no hover state is captured.
    await page.mouse.move(this.size.width - 1, this.size.height - 1);
    if (!(await this.settle()))
      this.problems.push(`${where}: Manrope did not render the text`);
    await this.capture(name);
    if (options.end) {
      // Page through the main scroller so every part of a long state is seen.
      for (let part = 2; part <= 8; part++) {
        const scrolled = await page.evaluate((selector) => {
          const tall = (e: HTMLElement) =>
            e.scrollHeight > e.clientHeight + 4 &&
            /(auto|scroll)/.test(getComputedStyle(e).overflowY) &&
            !e.closest("textarea, .canvas, .run-graph");
          const area = (e: HTMLElement) => e.clientWidth * e.clientHeight;
          // The named scroller, or else the largest one in the open modal or
          // on the page.
          const modal = [
            ...document.querySelectorAll<HTMLElement>('[aria-modal="true"]'),
          ].at(-1);
          const scroller =
            [...document.querySelectorAll<HTMLElement>(selector)].find(tall) ??
            [
              ...(modal ? [modal] : []),
              ...(modal ?? document.body).querySelectorAll<HTMLElement>("*"),
            ]
              .filter(tall)
              .sort((a, b) => area(b) - area(a))[0];
          if (!scroller) return false;
          const before = scroller.scrollTop;
          scroller.scrollTop =
            before + Math.round(scroller.clientHeight * 0.85);
          return scroller.scrollTop > before + 1;
        }, options.end);
        if (!scrolled) break;
        await this.settle();
        await this.capture(`${name}-${part}`);
      }
    }
    const audit = await page.evaluate(auditPage, overlays);
    if (audit.scrollDown > 1)
      this.problems.push(
        `${where}: the app shell scrolls down by ${audit.scrollDown}px (${audit.below.join("; ")})`,
      );
    if (audit.scrollWidth > audit.width + 1)
      this.problems.push(
        `${where}: page scrolls sideways (${audit.scrollWidth} > ${audit.width})`,
      );
    for (const s of audit.scrollers)
      this.problems.push(`${where}: sideways scroller ${s}`);
    for (const s of audit.offscreen)
      this.problems.push(`${where}: partly off-screen ${s}`);
    for (const s of audit.clipped) this.problems.push(`${where}: clipped ${s}`);
    for (const s of audit.covered) this.problems.push(`${where}: ${s}`);
    for (const s of await page.evaluate(contrastProblems))
      this.problems.push(`${where}: low text contrast ${s}`);
    // An icon name without a drawing falls back to "workflows" silently.
    for (const s of await page
      .locator("weave-icon[data-icon-missing]")
      .evaluateAll((icons) => icons.map((i) => i.getAttribute("data-icon"))))
      this.problems.push(`${where}: no icon named "${s}"`);
    this.report[name] = { truncated: audit.truncated };
    await this.focus(where, options.tabs ?? 3);
  };

  /** Each Tab press lands on an on-screen element that looks focused. */
  private async focus(where: string, presses: number) {
    const page = this.page;
    // Focus returns to where the scene left it, so the scene can go on.
    const before = await page.evaluateHandle(() => document.activeElement);
    for (let i = 0; i < presses; i++) {
      await page.evaluate(rememberFocusStyles);
      await page.keyboard.press("Tab");
      // The canvas pans a focused step or "+" into view on the next frame.
      await page.evaluate(
        () =>
          new Promise((done) =>
            requestAnimationFrame(() => requestAnimationFrame(done)),
          ),
      );
      const focused = await page.evaluate(focusedLooksFocused, overlays);
      if (!focused) continue;
      if (!focused.visible)
        this.problems.push(`${where}: no visible focus on ${focused.name}`);
      if (!focused.onScreen)
        this.problems.push(`${where}: focus off-screen on ${focused.name}`);
      if (focused.obscured)
        this.problems.push(
          `${where}: focus on ${focused.name} hidden by ${focused.obscured}`,
        );
      // A panel over the page is a modal sheet: Tab never reaches what it
      // covers.
      if (focused.behindOverlay)
        this.problems.push(
          `${where}: focus on ${focused.name} behind ${focused.behindOverlay}`,
        );
      if (focused.ringContrast !== null && focused.ringContrast < 3)
        this.problems.push(
          `${where}: focus ring on ${focused.name} is ${focused.ringContrast.toFixed(2)}:1, under 3:1`,
        );
    }
    await before.evaluate((element) => {
      if (element instanceof HTMLElement && element !== document.body)
        element.focus({ preventScroll: true });
      else (document.activeElement as HTMLElement | null)?.blur();
    });
    await before.dispose();
  }

  /** Writes the informational report and fails on any layout problem. */
  finish() {
    const file = join(folder, "report", `${this.size.tag}-${this.scene}.json`);
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(
      file,
      JSON.stringify(
        { problems: this.problems, notes: this.notes, screens: this.report },
        null,
        2,
      ),
    );
    expect(this.problems, `layout problems in ${this.scene}`).toEqual([]);
  }
}

// ---------------------------------------------------------------- helpers

/** The fake local host with extra routes registered before Studio opens. */
async function hosted(
  page: Page,
  options: HostOptions,
  extra?: (host: PlatformHost) => Promise<void>,
): Promise<PlatformHost> {
  const host = new PlatformHost(options);
  await page.route(
    (url) => url.pathname.startsWith("/studio/"),
    (route) => host.handle(route),
  );
  await extra?.(host);
  await page.goto("/");
  return host;
}

const wizardHeading = (page: Page) => page.locator("#wizard-heading");

/** Closes an inspector overlay that covers the toolbar on narrow layouts. */
async function closeInspector(page: Page) {
  const close = page.getByRole("button", { name: "Close inspector" });
  if (await close.isVisible()) await close.click();
}

/**
 * Appends a palette step, opening the narrow-layout palette when needed.
 */
async function addStep(page: Page, kind: StepKind) {
  const toggle = page.getByRole("button", { name: "Insert step", exact: true });
  const popup = await toggle.isVisible();
  const item = page
    .locator(".palette")
    .getByRole("button", { name: stepLabels[kind], exact: true });
  if (!(await item.isVisible())) await toggle.click();
  await item.click();
  if (popup) await expect(item).toBeHidden();
}

/** Opens a main view from the navigation. */
async function open(page: Page, name: string) {
  await page.getByRole("button", { name, exact: true }).click();
}

const runs = [
  {
    id: "00000000-0000-4000-8000-000000000104",
    business_key: "expense-2026-104",
    correlation_key: "batch-7",
    artifact_digest: "sha256:0123456789abcdef0123456789abcdef",
    activation: {
      name: "expense-review",
      revision: 1,
      request: { version_id: versionId },
    },
    created_at: "2026-10-01T09:12:00Z",
    state: {
      status: "waiting",
      active: ["review"],
      current_nodes: ["review"],
    },
  },
  {
    id: "00000000-0000-4000-8000-000000000105",
    business_key: "expense-2026-105",
    correlation_key: "batch-7",
    artifact_digest: "sha256:0123456789abcdef0123456789abcdef",
    created_at: "2026-10-01T09:20:00Z",
    state: { status: "succeeded", active: [] },
  },
  {
    id: "00000000-0000-4000-8000-000000000106",
    business_key:
      "expense-2026-106-with-a-much-longer-business-key-for-wrapping",
    correlation_key: "batch-8",
    artifact_digest: "sha256:0123456789abcdef0123456789abcdef",
    created_at: "2026-10-01T10:02:00Z",
    state: { status: "failed", active: [] },
  },
];
const tasks = [
  {
    id: "task-1",
    run_id: runs[0].id,
    node_id: "review",
    revision: 1,
    status: "ready",
    title: "Review expense 104",
    context: { amount: 125, currency: "EUR", submittedBy: "jane@acme.example" },
    form_schema: {
      type: "object",
      required: ["note"],
      properties: {
        note: { type: "string", title: "Review note" },
        approvedAmount: { type: "number", title: "Approved amount" },
      },
    },
    decisions: ["approve", "reject"],
    claimant_id: null,
    due_at: "2026-10-03T17:00:00Z",
  },
  {
    id: "task-2",
    run_id: runs[1].id,
    node_id: "review",
    revision: 1,
    status: "ready",
    title: "Confirm the supplier's bank details before the first payment",
    context: { supplier: "Globex" },
    form_schema: { type: "object" },
    decisions: ["approve", "reject"],
    claimant_id: null,
  },
];
const reviewWorkflow = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "expense-review", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [
      { id: "check", kind: "transform", value: { literal: true } },
      {
        id: "review",
        kind: "humanTask",
        assignment: "reviewers",
        title: { literal: "Review" },
        context: { literal: {} },
        formSchema: { type: "object" },
        decisions: ["approve", "reject"],
      },
    ],
    output: { literal: {} },
  },
};

/** Run list, detail, lifecycle, history and the pinned workflow version. */
async function runRoutes(page: Page) {
  // Published versions: a run's title reads "expense-review 1.0.0".
  await page.route("**/projects/*/workflows?*", (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: versionId,
            kind: "Workflow",
            name: "expense-review",
            version: "1.0.0",
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/environments/*/runs?*", (r) =>
    r.fulfill({ json: { items: runs, next_cursor: null } }),
  );
  for (const run of runs) {
    await page.route(`**/environments/*/runs/${run.id}`, (r) =>
      r.fulfill({ json: run }),
    );
    await page.route(`**/runs/${run.id}/lifecycle`, (r) =>
      r.fulfill({ json: { archived: false, purged: false, revision: 1 } }),
    );
    await page.route(`**/runs/${run.id}/history?*`, (r) =>
      r.fulfill({
        json: {
          events: [
            { sequence: 1, type: "run.started", at: "2026-10-01T09:12:00Z" },
            {
              sequence: 2,
              type: "step.completed",
              node: "check",
              at: "2026-10-01T09:12:01Z",
            },
            {
              sequence: 3,
              type: "human_task.created",
              node: "review",
              at: "2026-10-01T09:12:02Z",
            },
          ],
          next_cursor: null,
        },
      }),
    );
  }
  await page.route(`**/projects/*/workflows/${versionId}/export`, (r) =>
    r.fulfill({ json: { id: versionId, document: reviewWorkflow } }),
  );
}

/** The Studio host's local check answers with these problems. */
async function localProblems(page: Page) {
  await page.route("**/studio/local/validate", (r) =>
    r.fulfill({
      json: {
        validationOk: false,
        errorCount: 2,
        partial: true,
        diagnostics: [
          {
            code: "WV-COMP-UNAVAILABLE_REFERENCE",
            severity: "error",
            stage: "semantic",
            path: "/spec/steps/1/value/ref",
            message:
              "Referenced step does not dominate this expression in its lexical scope.",
          },
          {
            code: "WV-COMP-SCHEMA",
            severity: "error",
            stage: "schema",
            path: "/spec/steps/2/durationSeconds",
            message: "durationSeconds must be greater than or equal to 1.",
          },
          {
            code: "WV-COMP-UNUSED",
            severity: "warning",
            stage: "semantic",
            path: "/spec/steps/0",
            message: "The output of this step is never read.",
          },
        ],
      },
    }),
  );
}

// ---------------------------------------------------- platform for builders

const petstore = `openapi: 3.1.0
info:
  title: Pet Store
  version: 1.0.0
servers:
  - url: https://api.petstore.test
paths:
  /pets/{petId}:
    get:
      operationId: getPet
      summary: Read one pet
      parameters:
        - name: petId
          in: path
          required: true
          schema:
            type: string
            maxLength: 64
      responses:
        "200":
          description: The pet
          content:
            application/json:
              schema:
                type: object
                properties:
                  id:
                    type: string
                    maxLength: 64
                  name:
                    type: string
                    maxLength: 200
  /pets:
    post:
      operationId: createPet
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                name:
                  type: string
                  maxLength: 200
      responses:
        "201":
          description: Created
          content:
            application/json:
              schema:
                type: object
                properties:
                  id:
                    type: string
                    maxLength: 64
`;

/** A platform where weave-http@2.0.0 is installed, published and released. */
async function httpPlatform(
  page: Page,
  options: { capabilities?: string[]; connections?: unknown[] } = {},
) {
  const actions = new Map<string, Record<string, unknown>>();
  const connections: Request[] = [];
  await connected(page, {
    capabilities: options.capabilities ?? [...allCapabilities, "compile"],
    catalog: [],
  });
  await page.route(`${project}/actions?*`, (r) =>
    r.fulfill({
      json: {
        items: [...actions].map(([id, document]) => ({
          id,
          name: (document["metadata"] as Record<string, string>)["name"],
          version: (document["metadata"] as Record<string, string>)["version"],
        })),
        next_cursor: null,
      },
    }),
  );
  await page.route(`${project}/actions/*/export`, (r) => {
    const id = r.request().url().split("/").at(-2)!;
    return r.fulfill({ json: { id, document: actions.get(id) } });
  });
  await page.route(`${project}/actions`, (r) => {
    const body = r.request().postDataJSON() as { source: string };
    const document = parse(body.source) as Record<string, unknown>;
    const id = `h${actions.size + 1}`;
    actions.set(id, document);
    const metadata = document["metadata"] as Record<string, string>;
    return r.fulfill({
      status: 201,
      json: { id, kind: "Action", ...metadata },
    });
  });
  await page.route(`${project}/compiler/compile`, (r) =>
    r.fulfill({
      json: {
        ok: true,
        validationOk: true,
        errorCount: 0,
        partial: false,
        diagnostics: [],
        artifact: {},
      },
    }),
  );
  await page.route(`${project}/capabilities`, (r) =>
    r.fulfill({ json: { connectors: ["weave-http-v2", "weave-postgresql"] } }),
  );
  await page.route(`${project}/connector-descriptors/weave-http-v2`, (r) =>
    r.fulfill({
      json: {
        adapter: "weave-http-v2",
        reference: "weave-http@2.0.0",
        digest,
        manifest: {},
        source: "{}",
        implementation_version: "2.0.0",
        capabilities: [],
        bindings: [],
        actions: [],
        connection: { config_schema: {}, auth_schema: {} },
        published_version_id: connectorVersion,
      },
    }),
  );
  await page.route(`${project}/connectors?*`, (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: connectorVersion,
            kind: "Connector",
            name: "weave-http",
            version: "2.0.0",
            digest: "a".repeat(64),
            definition_digest: digest,
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route(`${environment}/worker-releases?*`, (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: httpRelease,
            image_digest: "sha256:" + "b".repeat(64),
            capabilities: [
              { taskType: "weave-connector-http-read", taskVersion: "2.0.0" },
            ],
            connector_bindings: [
              {
                connector_digest: digest,
                action: "read",
                adapter: "weave-http-v2",
                implementation_version: "2.0.0",
                task_reference: "weave-connector-http-read@2.0.0",
              },
            ],
          },
        ],
        next_cursor: null,
      },
    }),
  );
  const created = () => [
    ...(options.connections ?? []),
    ...connections.map((request) => ({
      ...(request.postDataJSON() as Record<string, unknown>),
      id: revisionId,
      revision: 1,
      connector: "weave-http@2.0.0",
      connector_digest: digest,
      adapter: "weave-http-v2",
    })),
  ];
  await page.route(`${environment}/connections?*`, (r) =>
    r.fulfill({ json: { items: created(), next_cursor: null } }),
  );
  await page.route(`${environment}/connections`, (r) => {
    connections.push(r.request());
    return r.fulfill({ status: 201, json: created().at(-1) });
  });
  await page.route(`${environment}/connections/*/test`, (r) =>
    r.fulfill({ json: { ok: true, code: "ok" } }),
  );
  return { actions, connections };
}

const builder = (page: Page) =>
  page.getByRole("dialog", { name: "New API action" });

// ---------------------------------------------------------- simulation

const simulated = `apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: simulated
  version: 1.0.0
spec:
  inputSchema:
    type: object
    additionalProperties: false
    required: [customerId]
    properties:
      customerId: { type: string, minLength: 1 }
  outputSchema: { type: object }
  steps:
    - id: action-1
      kind: action
      uses: onboarding.check-customer@1.0.0
      with:
        object:
          customerId: { ref: /input/customerId }
    - id: wait-1
      kind: wait
      durationSeconds: 60
    - id: approval
      kind: signal
      name: customer-approved
      timeoutSeconds: 3600
      payloadSchema:
        type: object
        additionalProperties: false
        required: [approved]
        properties:
          approved: { type: boolean }
    - id: review
      kind: humanTask
      assignment: reviewers
      title: { literal: Review the customer }
      context: { literal: {} }
      decisions: [approve, reject]
      formSchema: { type: object, properties: { note: { type: string } } }
  output: { literal: {} }
`;
let compiledArtifact: Record<string, unknown> | null | undefined;
/** The real compiled artifact of `simulated`, once per worker. */
function simulationArtifact() {
  if (compiledArtifact !== undefined) return compiledArtifact;
  compiledArtifact = hasPython
    ? JSON.parse(
        execFileSync(
          python,
          [
            "-c",
            `
import json, pathlib, sys
import yaml
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
action = yaml.safe_load(pathlib.Path("examples/definitions/check-customer.action.yaml").read_text())
spec = action["spec"]
catalog = CatalogSnapshot.from_definitions([load_definition(action)], tasks=[{
    "taskType": "onboarding.check-customer", "taskVersion": "1.0.0",
    "inputSchema": spec["inputSchema"], "outputSchema": spec["outputSchema"],
    "sideEffect": "read_only", "timeoutSeconds": 60}])
result = compile_source(sys.stdin.read(), format="yaml", catalog=catalog)
assert result.ok, [d.code for d in result.diagnostics]
print(result.artifact.to_bytes().decode())
`,
          ],
          {
            cwd: repository,
            env: { ...process.env, PYTHONPATH: resolve(repository, "src") },
            encoding: "utf8",
            input: simulated,
            timeout: 30000,
          },
        ),
      )
    : null;
  return compiledArtifact;
}
const now = "2026-10-02T09:00:00.000Z";
const later = (seconds: number) =>
  new Date(Date.parse(now) + seconds * 1000).toISOString();
const debugSession = (
  revision: number,
  status: string,
  active: string[],
  waits: Record<string, string> = {},
  finished: string[] = [],
) => ({
  id: "44444444-4444-4444-8444-444444444444",
  revision,
  view: {
    status,
    current_nodes: active,
    active_nodes: active,
    // The steps the run finished, so the canvas draws the live thread.
    variables: {
      waits,
      steps: Object.fromEntries(
        finished.map((id) => [id, { output: { eligible: true } }]),
      ),
    },
    diagnostics: [],
    events: [],
    now,
  },
});

// --------------------------------------------------------------- scenes

type Scene = (tour: Tour) => Promise<void>;
const scenes: Record<string, Scene> = {
  async lumi({ page, shot }) {
    await connected(page, {
      capabilities: [...allCapabilities, "lumi.use", "lumi.manage"],
    });
    const schema = JSON.parse(
      execFileSync(
        python,
        [
          "-c",
          "import json; from firefly_weave.contracts.lumi import LumiConfigurationRequest; print(json.dumps(LumiConfigurationRequest.model_json_schema(by_alias=True)))",
        ],
        { cwd: repository, encoding: "utf8" },
      ),
    );
    await page.route("**/studio/contracts/lumi-configuration", (r) =>
      r.fulfill({ json: schema }),
    );
    await page.route("**/lumi/configuration", (r) =>
      r.fulfill({ status: 404, json: { message: "Not configured" } }),
    );
    await page.route("**/lumi/status", (r) =>
      r.fulfill({
        json: { configured: true, provider: "openai", model: "team-assistant" },
      }),
    );
    await page.route("**/lumi/ask", (r) =>
      r.fulfill({
        json: {
          answer:
            "The workflow can wait for a reviewer before continuing. Review the suggested draft and validate it before applying changes.",
          proposals: [
            {
              title: "Review workflow draft",
              kind: "workflow",
              format: "yaml",
              source:
                "apiVersion: weave/v1alpha1\nkind: Workflow\nmetadata: {name: reviewed-draft, version: 1.0.0}\nspec: {steps: []}",
            },
          ],
          followUps: ["Explain the reviewer deadline"],
        },
      }),
    );
    await newWorkflow(page);
    await page
      .getByRole("button", { name: "Ask Weave AI", exact: true })
      .click();
    const panel = page.getByRole("dialog", {
      name: "Ask Weave AI",
      exact: true,
    });
    await panel
      .getByLabel("Message to Weave AI")
      .fill("Help me add a review step.");
    await panel.getByLabel("Include current source", { exact: true }).check();
    await shot("lumi-context", { end: ".modal-panel" });
    await panel
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    await expect(panel).toContainText("The workflow can wait");
    await shot("lumi-conversation", { end: ".modal-panel" });
    await panel
      .getByRole("button", { name: "Review workflow draft", exact: true })
      .click();
    await shot("lumi-review", { end: ".modal-panel" });
    await panel
      .getByRole("button", { name: "Weave AI settings", exact: true })
      .click();
    await expect(panel.getByLabel("Model", { exact: true })).toBeVisible();
    await shot("lumi-settings", { end: ".modal-panel" });
  },
  async pairing({ page, shot }) {
    await page.route("**/studio/session", (r) =>
      r.fulfill({
        json: { paired: false, version: "1", mode: "offline", profile: null },
      }),
    );
    await page.goto("/");
    await expect(page.locator(".pair-card")).toBeVisible();
    await shot("00-pairing");
  },
  async "brand-header"({ page, size, problems, shot }) {
    await hosted(page, {});
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    const where = `${size.tag} brand-header`;
    const home = page.getByRole("link", { name: "Firefly Weave Studio home" });
    const lockup = home.locator("img.brand-lockup");
    const mark = home.locator("img.brand-mark");
    if (size.width > 1280) {
      // The expanded sidebar: the lockup at 192 x 35, inside the sidebar, its
      // woven w's crossing open at this size's pixel density.
      await expect(lockup).toBeVisible();
      await expect(mark).toBeHidden();
      await lockup.evaluate((e: HTMLImageElement) => e.decode());
      const box = (await lockup.boundingBox())!;
      const sidebar = (await page.locator(".sidebar").boundingBox())!;
      if (Math.round(box.width) !== 192 || Math.round(box.height) !== 35)
        problems.push(
          `${where}: the lockup is ${box.width} x ${box.height}, not 192 x 35`,
        );
      if (box.x < sidebar.x || box.x + box.width > sidebar.x + sidebar.width)
        problems.push(`${where}: the sidebar cuts the lockup`);
      const crossing = await crossingOf(lockup);
      const limit = (size.scale ?? 1) > 1 ? 0.05 : 0.4;
      if (
        crossing.missing.length ||
        crossing.measured < 4 ||
        crossing.depth > limit
      )
        problems.push(
          `${where}: the woven w's crossing is not open ${JSON.stringify(crossing)}`,
        );
    } else {
      // The narrow sidebar shows the Firefly mark, whole, inside the window.
      await expect(lockup).toBeHidden();
      await expect(mark).toBeVisible();
      const box = (await mark.boundingBox())!;
      if (box.x < 0 || box.y < 0 || box.x + box.width > size.width)
        problems.push(`${where}: the window cuts the Firefly mark`);
    }
    await shot("00-brand-header", { tabs: 0 });
  },

  async "home-local"({ page, shot }) {
    await hosted(page, {});
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    await expect(page.locator("weave-template-gallery")).toBeVisible();
    await shot("01-home-local", { end: ".home-view" });
    await open(page, "Workflows");
    await page.getByRole("button", { name: "More ways to start" }).click();
    await expect(
      page.getByRole("menu", { name: "More ways to start" }),
    ).toBeVisible();
    await shot("01-workflows-new-menu", { tabs: 0 });
  },

  async "workflows-local"({ page, shot }) {
    await hosted(page, {});
    await newWorkflow(page);
    await addStep(page, "transform");
    await addStep(page, "humanTask");
    await closeInspector(page);
    await open(page, "Workflows");
    await page
      .getByRole("dialog", { name: "Leave untitled-workflow?" })
      .getByRole("button", { name: "Leave", exact: true })
      .click();
    await expect(page.locator(".resource-row")).toHaveCount(1);
    await shot("03-workflows-local");
    await open(page, "Home");
    await expect(
      page.getByRole("button", { name: /^Continue editing/ }),
    ).toBeVisible();
    await shot("03-home-continue", { end: ".home-view" });
  },

  async "home-connected"({ page, shot }) {
    await hosted(
      page,
      {
        platforms: [acmePlatform(), globexPlatform()],
        active: "Acme",
      },
      async () => {
        await runRoutes(page);
        await page.route("**/environments/*/human-tasks?*", (r) => {
          const status = new URL(r.request().url()).searchParams.get("status");
          const items = tasks.filter((t) => !status || t.status === status);
          return r.fulfill({ json: { items, next_cursor: null } });
        });
      },
    );
    await expect(page.locator("weave-home-dashboard")).toBeVisible();
    await expect(page.locator(".needs-you")).toContainText("Review expense");
    await shot("02-home-connected", { end: ".home-view" });
    await page.locator(".platform-indicator").click();
    await expect(page.locator("#platform-menu")).toBeVisible();
    await shot("02-platform-menu", { tabs: 0 });
  },

  async wizard({ page, shot }) {
    const host = await hosted(page, {
      firstRun: true,
      discover: {
        "weave.acme.example": { json: acmeDiscovery },
        "down.example": {
          status: 502,
          json: { status: 502, code: "WV-CONNECT-UNREACHABLE", message: "x" },
        },
      },
    });
    const heading = wizardHeading(page);
    await expect(heading).toHaveText("How do you want to work?");
    await shot("10-wizard-choice");
    await page.getByRole("button", { name: "Connect to a platform" }).click();
    await expect(heading).toHaveText("Connect to a platform");
    await shot("11-wizard-server");
    await page.getByLabel("Server address").fill("down.example");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(page.locator("#wizard-server-problem")).toBeVisible();
    await shot("12-wizard-server-error");
    await page.getByLabel("Server address").fill("weave.acme.example");
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(heading).toHaveText("Review and trust");
    await page.getByText("Advanced", { exact: true }).click();
    await shot("13-wizard-review", { end: ".wizard-body, .wizard-card" });
    await page.getByLabel("I trust this server and identity provider").check();
    await page.getByRole("button", { name: "Continue" }).click();
    await expect(heading).toHaveText("Sign in to Acme Weave");
    await shot("14-wizard-sign-in");
    await page
      .getByRole("button", { name: "Sign in with your browser" })
      .click();
    await expect(
      page.getByRole("textbox", { name: "Sign-in address" }),
    ).toBeVisible({ timeout: 15_000 });
    await shot("15-wizard-sign-in-waiting", { tabs: 0 });
    await page.getByRole("button", { name: "Cancel sign-in" }).click();
    await expect(page.locator("#wizard-sign-in-problem")).toContainText(
      "Sign-in canceled.",
    );
    await shot("16-wizard-sign-in-cancelled");
    await page.getByRole("button", { name: "Back" }).click();
    await page.getByRole("button", { name: "Continue" }).click();
    await page.getByRole("button", { name: "Use a code instead" }).click();
    await expect(
      page.getByRole("status", { name: "Sign-in code" }),
    ).toBeVisible({ timeout: 15_000 });
    await shot("17-wizard-sign-in-code", { tabs: 0 });
    host.finish("authenticated");
    await expect(heading).toHaveText("Choose a workspace", { timeout: 15_000 });
    await page
      .getByRole("radio", { name: "Acme / Billing / Production" })
      .check();
    await shot("18-wizard-workspace");
  },

  async "wizard-no-access"({ page, shot }) {
    await hosted(page, {
      platforms: [acmePlatform({ workspace: null, workspaces: [] })],
      active: "Acme",
    });
    await page.locator(".platform-indicator").click();
    await page.getByRole("button", { name: "Switch workspace" }).click();
    await expect(
      page.getByRole("heading", {
        name: "Your account doesn't have access to a workspace yet",
      }),
    ).toBeVisible();
    await shot("19-wizard-no-access");
  },

  async "wizard-not-linked"({ page, shot }) {
    await hosted(page, {
      platforms: [acmePlatform()],
      active: "Acme",
      test: () => ({
        status: 401,
        json: {
          status: 401,
          code: "WV-AUTH-NOT-LINKED",
          message:
            "You signed in, but this platform does not know your account yet.",
          account: {
            provider_id: "acme",
            issuer: acmeOption.issuer,
            subject: "00u9-new",
            display_name: "new.person@acme.example",
          },
        },
      }),
    });
    const banner = page.locator(".platform-banner");
    await expect(banner).toContainText("doesn't recognize your account yet");
    await shot("20-home-not-linked");
    await banner.getByRole("button", { name: "See details" }).click();
    await expect(
      page.getByRole("heading", {
        name: "You signed in, but this platform doesn't recognize your account yet",
      }),
    ).toBeVisible();
    await shot("21-wizard-not-linked");
  },

  async "settings-local"({ page, shot }) {
    await hosted(page, {});
    await open(page, "Settings");
    await expect(
      page.getByRole("button", { name: "Connect to a platform" }),
    ).toBeVisible();
    await shot("30-settings-local", { end: ".page-content" });
  },

  async "settings-platforms"({ page, shot }) {
    await hosted(page, {
      platforms: [
        acmePlatform(),
        globexPlatform({ signedIn: false }),
        acmePlatform({
          name: "Acme staging with a long saved platform name",
          server: "https://weave-staging.eu-west.internal.acme.example",
          workspace: null,
        }),
      ],
      active: "Acme",
    });
    await open(page, "Settings");
    await expect(
      page.getByRole("heading", { name: "Platforms" }),
    ).toBeVisible();
    await shot("31-settings-platforms", { end: ".page-content" });
  },

  async "settings-people"({ page, shot }) {
    const tenant = acmePlatform().workspace!.tenant_id;
    // An administrator: platform directory and tenant role bindings.
    const administrator = (host: PlatformHost) => ({
      ...host.identity(),
      grants: [
        {
          role: "platform_admin",
          scope: null,
          resources: [],
          capabilities: ["grant.admin"],
        },
        {
          role: "tenant_admin",
          scope: { tenant_id: tenant },
          resources: [],
          capabilities: ["grant.manage", ...allCapabilities],
        },
      ],
    });
    await hosted(
      page,
      {
        platforms: [acmePlatform()],
        active: "Acme",
        test: (host) => ({
          json: {
            session: host.session(),
            identity: administrator(host),
            workspaces: host.current?.workspaces ?? [],
            truncated: false,
            workspace_revoked: false,
          },
        }),
      },
      async (host) => {
        await page.route("**/studio/api/api/v1/identity", (r) =>
          r.fulfill({ json: administrator(host) }),
        );
        await page.route("**/studio/api/api/v1/admin/principals?*", (r) =>
          r.fulfill({
            json: {
              items: [
                {
                  id: "00000000-0000-4000-8000-000000000005",
                  kind: "human",
                  active: true,
                },
                {
                  id: "00000000-0000-4000-8000-000000000006",
                  kind: "application",
                  active: false,
                },
              ],
              next_cursor: null,
            },
          }),
        );
        await page.route("**/members?*", (r) =>
          r.fulfill({
            json: {
              items: [
                {
                  id: "00000000-0000-4000-8000-000000000007",
                  principal_id: "00000000-0000-4000-8000-000000000005",
                  kind: "human",
                  active: true,
                  role: "developer",
                  scope: { tenant_id: tenant },
                  resources: [],
                },
              ],
              next_cursor: null,
            },
          }),
        );
      },
    );
    await open(page, "Settings");
    await page.getByRole("tab", { name: "People and access" }).click();
    const section = page.locator(".settings-card.administration");
    await expect(section).toBeVisible();
    await section.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(section).toContainText("Account 00000000");
    // A state reads as a status pill, not as a monospace tag.
    await expect(
      section.locator(".pill", { hasText: "Deactivated" }),
    ).toBeVisible();
    await shot("32-settings-people", { end: ".page-content" });
    await section
      .getByRole("button", { name: /^Actions for Account/ })
      .first()
      .click();
    await page.getByRole("menuitem", { name: "Assign role" }).click();
    await expect(
      page.getByRole("heading", { name: /^Assign a role to Account/ }),
    ).toBeVisible();
    await shot("33-settings-assign-role", { end: ".page-content" });
  },

  async "designer-lanes"({ page, shot }) {
    await offline(page);
    await page
      .getByLabel("Choose a workflow file")
      .setInputFiles(resolve("tests/fixtures/vendor-payment-approval.yaml"));
    await expect(page.locator(".editor-bar")).toBeVisible();
    const canvas = page.getByRole("button", { name: "Show canvas" });
    if (await canvas.isVisible()) await canvas.click();
    await expect(page.locator('[data-step="record-result"]')).toBeAttached();
    await closeInspector(page);
    await page.getByRole("button", { name: "Fit all", exact: true }).click();
    await expect(
      page.locator('.lane-header[data-owner="pay-and-notify/ledger"]'),
    ).toBeVisible();
    await expect(
      page.locator('.lane-header[data-owner="pay-and-notify/email"]'),
    ).toBeVisible();
    await expect(page.locator('[data-terminal="rejected"]')).toBeVisible();
    await expect(page.locator('.edge[data-from="rejected"]')).toHaveCount(0);
    await shot("38-designer-lanes");
  },

  async "inspector-fields"({ page, shot }) {
    await offline(page);
    await newWorkflow(page);
    const designer = new DesignerPage(page);
    await designer.append("transform");
    await designer.selectStep("transform-1");
    const fields = designer.inspector.locator('[data-field="value"]');
    const name = fields.getByRole("textbox", {
      name: "Field name",
      exact: true,
    });
    await name.fill("customer");
    await name.press("Tab");
    await fields
      .getByLabel("Property value", { exact: true })
      .fill("Northwind");
    await fields
      .getByRole("button", { name: "+ Add field", exact: true })
      .click();
    await expect(name.nth(1)).toBeFocused();
    await shot("39-inspector-fields", { end: ".inspector-body" });
  },

  async "inspector-paths-settings"({ page, shot }) {
    await offline(page);
    await page
      .getByLabel("Choose a workflow file")
      .setInputFiles(resolve("tests/fixtures/vendor-payment-approval.yaml"));
    await expect(page.locator(".editor-bar")).toBeVisible();
    const showCanvas = page.getByRole("button", {
      name: "Show canvas",
      exact: true,
    });
    if (await showCanvas.isVisible()) await showCanvas.click();
    await closeInspector(page);
    await page.getByRole("button", { name: "Fit all", exact: true }).click();
    const designer = new DesignerPage(page);
    await designer.selectStep("route");
    await expect(designer.inspector.locator(".decision-path")).toHaveCount(3);
    await shot("39-path-cards", { end: ".inspector-body" });
    await designer.inspector
      .getByRole("button", { name: "Workflow settings", exact: true })
      .click();
    await expect(
      designer.inspector.getByRole("heading", { name: "Inputs", exact: true }),
    ).toBeVisible();
    const rows = designer.inspector.locator(
      '[data-field="spec/inputSchema"] .sd-summary',
    );
    await expect(rows.first()).toBeVisible();
    await shot("39-workflow-settings", { end: ".inspector-body" });
  },

  async "condition-operators"({ page, shot }) {
    await offline(page);
    await newWorkflow(page);
    const designer = new DesignerPage(page);
    await designer.setSource(`apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: region-rules, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      region: {type: string, title: Region, enum: [Europe, North America, Asia]}
  outputSchema: {type: object}
  steps:
    - id: route
      kind: switch
      cases:
        - when: {op: {name: in, args: [{ref: /input/region}, {literal: [Europe, North America]}]}}
          steps: []
          output: {literal: {}}
      default: {steps: [], output: {literal: {}}}
  output: {literal: {}}
`);
    await designer.selectStep("route");
    const field = designer.inspector.locator('[data-field="cases/0/when"]');
    await field.getByRole("button", { name: "Add item", exact: true }).click();
    await expect(
      field.getByLabel("Condition 1 item 3", { exact: true }),
    ).toBeFocused();
    await selectChoice(
      field.getByLabel("Condition 1 item 3", { exact: true }),
      '"Asia"',
    );
    await shot("39-condition-operators", { end: ".inspector-body" });
  },

  async "designer-canvas"({ page, shot }) {
    await offline(page);
    await newWorkflow(page);
    const designer = new DesignerPage(page);
    await expect(designer.canvas).toBeVisible();
    await shot("40-designer-empty");
    await closeInspector(page);
    await addStep(page, "switch");
    await addStep(page, "parallel");
    await closeInspector(page);
    await designer.fit();
    await expect(
      page.locator('.lane-header[data-owner="decision-1/case 1"]'),
    ).toBeVisible();
    await shot("41-designer-branches");
    const kinds: StepKind[] = [
      "action",
      "transform",
      "wait",
      "signal",
      "humanTask",
      "fail",
    ];
    for (const kind of kinds) await addStep(page, kind);
    await closeInspector(page);
    await designer.fit();
    await shot("42-designer-steps");
    await page.locator('[data-step="decision-1"] .node-body').focus();
    await page.keyboard.press("/");
    await expect(
      page.getByRole("combobox", { name: "Search steps and actions" }),
    ).toBeFocused();
    await shot("43-designer-step-picker", { tabs: 0 });
    await page.keyboard.press("Escape");
    // Default names say what a step does (decision-1, call-action-1).
    for (const [id, kind] of [
      ["decision-1", "switch"],
      ["parallel-1", "parallel"],
      ["call-action-1", "action"],
      ["transform-1", "transform"],
      ["wait-1", "wait"],
      ["signal-1", "signal"],
      ["fail-1", "fail"],
    ]) {
      await designer.selectStep(id);
      await shot(`44-inspector-${kind}`, {
        end: ".inspector-body",
      });
    }
    await designer.selectStep("approval-1");
    const form = designer.inspector.locator('[data-field="formSchema"]');
    await form.getByRole("button", { name: "Add field" }).click();
    await form.getByLabel("Field name").fill("comment");
    await expect(form.locator(".sd-preview")).toContainText("Comment");
    await shot("44-inspector-humanTask", { end: ".inspector-body" });
  },

  async "human-task-files"({ page, shot }) {
    await connected(page, {
      capabilities: [...allCapabilities, "human_task.complete"],
    });
    const file = {
      kind: "weave/file",
      id: "11111111-1111-4111-8111-111111111111",
      filename: "vendor-payment-invoice-october.pdf",
      contentType: "application/pdf",
      sizeBytes: 1250,
      sha256: "0".repeat(64),
    };
    const task = {
      id: "task-1",
      run_id: "run",
      node_id: "review",
      revision: 1,
      status: "claimed",
      claimant_id: "human",
      title: "Review invoice attachments",
      decisions: ["approve", "reject"],
      context: { invoice: file },
      form_schema: {
        type: "object",
        properties: {
          receipt: {
            type: "object",
            properties: {
              kind: { const: "weave/file" },
              id: { type: "string" },
            },
          },
        },
      },
    };
    await page.route("**/human-tasks?*", (route) =>
      route.fulfill({ json: { items: [task], next_cursor: null } }),
    );
    await page.route("**/human-tasks/task-1", (route) =>
      route.fulfill({ json: task }),
    );
    await page.getByRole("button", { name: "My tasks", exact: true }).click();
    await page.locator(".resource-row").click();
    await expect(
      page.getByLabel("Upload Receipt", { exact: true }),
    ).toBeVisible();
    await shot("human-task-files", { end: ".record-detail, .page-content" });
  },

  async "human-task-inspector"({ page, shot }) {
    await offline(page);
    await newWorkflow(page);
    const designer = new DesignerPage(page);
    await designer.append("humanTask");
    await designer.selectStep("approval-1");
    await expect(designer.inspector.locator(".human-section > h3")).toHaveText([
      "Who",
      "What they see",
      "How they answer",
    ]);
    const deadlines = designer.inspector.locator(".human-deadlines");
    await expect(deadlines.locator(":scope > summary")).toHaveText(
      "Deadlines Optional",
    );
    await expect(deadlines).not.toHaveAttribute("open", "");
    await shot("human-task-who", { end: ".inspector-body" });
    const answers = designer.inspector.getByRole("heading", {
      name: "How they answer",
      exact: true,
    });
    await answers.scrollIntoViewIfNeeded();
    await shot("human-task-answers", { end: ".inspector-body" });
  },

  async "designer-schema"({ page, shot }) {
    await offline(page);
    await newWorkflow(page);
    const designer = new DesignerPage(page);
    await closeInspector(page);
    await designer.fit();
    await page
      .getByRole("button", { name: "Start — workflow settings" })
      .click();
    const input = designer.inspector.locator('[data-field="spec/inputSchema"]');
    await input.getByRole("button", { name: "Add field" }).click();
    await input.getByLabel("Field name").fill("customerId");
    await input
      .getByRole("checkbox", { name: /^Required: .customerId.$/ })
      .check();
    await input.getByRole("button", { name: "Add field" }).click();
    await input.getByLabel("Field name").nth(1).fill("amount");
    await expect(input).toContainText("What people starting a run will see");
    await input.scrollIntoViewIfNeeded();
    await shot("45-schema-designer", { end: ".inspector-body" });
  },

  async "designer-diagnostics"({ page, shot }) {
    await offline(page);
    await localProblems(page);
    await newWorkflow(page);
    const designer = new DesignerPage(page);
    await closeInspector(page);
    await addStep(page, "transform");
    await addStep(page, "transform");
    await addStep(page, "wait");
    await closeInspector(page);
    await expect(designer.diagnostics).toContainText("2 errors");
    await designer.fit();
    await shot("46-diagnostics-errors");
    await page.getByRole("tab", { name: "Source", exact: true }).click();
    await expect(
      page.getByRole("textbox", { name: "Workflow source" }),
    ).toBeVisible();
    await shot("47-source-with-errors");
  },

  async "designer-action"({ page, shot }) {
    await connected(page);
    await newWorkflow(page);
    const designer = new DesignerPage(page);
    await designer.setSource(`apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: customer-lookup
  version: 1.0.0
spec:
  inputSchema:
    type: object
    properties:
      customerId: { type: string, title: Customer ID }
  outputSchema:
    type: object
  connections:
    db:
      connector: weave-postgresql@1.0.0
  steps:
    - id: action-1
      kind: action
      uses: your-action@1.0.0
      with:
        literal: {}
  output:
    literal: {}
`);
    await designer.selectStep("action-1");
    await chooseAction(page, "sql.lookup@1.0.0");
    await page.locator(".action-details > summary").click();
    await expect(page.locator(".integration-requirements")).toBeVisible();
    await shot("48-inspector-action-contract", { end: ".inspector-body" });
  },

  async "designer-simulation"({ page, shot }) {
    const artifact = simulationArtifact();
    test.skip(!artifact, "Needs the repository's Python environment.");
    await connected(page, { capabilities: [...allCapabilities, "compile"] });
    const replies = [
      debugSession(2, "waiting", ["approval"], { approval: later(3600) }, [
        "action-1",
        "wait-1",
      ]),
      debugSession(3, "waiting", ["review"], {}, [
        "action-1",
        "wait-1",
        "approval",
      ]),
    ];
    let commands = 0;
    await page.route(`${project}/compiler/compile`, (r) =>
      r.fulfill({
        json: {
          ok: true,
          validationOk: true,
          partial: false,
          errorCount: 0,
          diagnostics: [],
          artifact,
        },
      }),
    );
    await page.route(`${project}/debug/sessions`, (r: Route) =>
      r.fulfill({
        status: 201,
        json: debugSession(1, "waiting", ["wait-1"], { "wait-1": later(60) }, [
          "action-1",
        ]),
      }),
    );
    await page.route(`${project}/debug/sessions/*/commands`, (r: Route) =>
      r.fulfill({ json: replies[commands++] }),
    );
    await newWorkflow(page);
    const designer = new DesignerPage(page);
    await designer.setSource(simulated);
    await closeInspector(page);
    await command(page, "Simulate");
    const setup = page.getByRole("dialog", { name: "Simulate this workflow" });
    await setup.getByLabel(/customer ?id/i).fill("c-104");
    await shot("50-simulation-setup");
    await setup.getByRole("tab", { name: /Action results/ }).click();
    await setup
      .locator('[data-node="action-1"]')
      .getByLabel(/eligible/i)
      .check();
    await shot("50-simulation-setup-results");
    await setup.getByRole("button", { name: "Start simulation" }).click();
    // A region beside the canvas, or a modal sheet where it covers it.
    const simulation = page
      .getByRole("region", { name: "Simulation", exact: true })
      .or(page.getByRole("dialog", { name: "Simulation", exact: true }));
    await expect(simulation).toContainText("Waiting");
    await expect(page.locator('[data-step="wait-1"]')).toHaveClass(
      /\bis-live\b/,
    );
    await shot("51-simulation-wait");
    await simulation
      .getByRole("button", { name: "Finish the wait at wait-1 (1 min)" })
      .click();
    await expect(page.locator('[data-step="approval"]')).toHaveClass(
      /\bis-live\b/,
    );
    await shot("52-simulation-signal");
    await simulation
      .getByRole("button", { name: "Send customer-approved" })
      .click();
    await expect(
      simulation.getByRole("radio", { name: "approve" }),
    ).toBeVisible();
    await shot("53-simulation-human-task");
  },

  async "designer-activation"({ page, shot }) {
    const getPet = {
      apiVersion: "weave/v1alpha1",
      kind: "Action",
      metadata: { name: "get-pet", version: "1.0.0" },
      spec: {
        implementation: {
          kind: "connector",
          uses: "weave-http@2.0.0",
          action: "read",
        },
        connection: { connector: "weave-http@2.0.0" },
        sideEffect: "read_only",
        timeoutSeconds: 30,
        inputSchema: { type: "object" },
        outputSchema: { type: "object" },
      },
    };
    const catalog = [
      { id: "h1", document: getPet },
      { id: "a2", document: workerAction },
    ] as unknown as CatalogEntry[];
    const recorder = await connected(page, { catalog });
    await page.route(`${environment}/connections?*`, (r) =>
      r.fulfill({
        json: {
          items: [
            {
              id: revisionId,
              name: "pets",
              revision: 1,
              connector: "weave-http@2.0.0",
              adapter: "weave-http-v2",
            },
          ],
          next_cursor: null,
        },
      }),
    );
    await page.route(`${project}/connectors?*`, (r) =>
      r.fulfill({
        json: {
          items: [
            {
              id: connectorVersion,
              kind: "Connector",
              name: "weave-http",
              version: "2.0.0",
              digest: "a".repeat(64),
              definition_digest: digest,
            },
          ],
          next_cursor: null,
        },
      }),
    );
    await page.route(`${project}/connector-descriptors/*`, (r) =>
      r.fulfill({ status: 404, json: { code: "WV-NOT-FOUND" } }),
    );
    await page.route(`${environment}/worker-releases?*`, (r) =>
      r.fulfill({
        json: {
          items: [
            {
              id: httpRelease,
              image_digest: "sha256:" + "b".repeat(64),
              capabilities: [
                { taskType: "weave-connector-http-read", taskVersion: "2.0.0" },
              ],
              connector_bindings: [
                {
                  connector_digest: digest,
                  action: "read",
                  adapter: "weave-http-v2",
                  implementation_version: "2.0.0",
                  task_reference: "weave-connector-http-read@2.0.0",
                },
              ],
            },
            {
              id: workerRelease,
              image_digest: "sha256:" + "d".repeat(64),
              capabilities: [{ taskType: "crm-lookup", taskVersion: "1.0.0" }],
            },
          ],
          next_cursor: null,
        },
      }),
    );
    await page.route(`${environment}/human-assignments`, (r) =>
      r.fulfill({
        json: {
          items: [
            {
              binding_id: "3f2a9b1c-0000-4000-8000-000000000003",
              name: "reviewers",
              enabled: true,
              revision: 2,
              principal_ids: ["8c1f1c55-0000-4000-8000-000000000001"],
              group_ids: [],
            },
          ],
        },
      }),
    );
    await page.route(`${project}/workflows`, (r) =>
      r.fulfill({
        status: 201,
        json: {
          id: versionId,
          name: "pets",
          version: "1.0.0",
          digest: "e".repeat(64),
        },
      }),
    );
    await page.route(`${project}/workflows/${versionId}/export`, (r) =>
      r.fulfill({
        json: {
          id: versionId,
          document: {
            spec: { connections: { pets: { connector: "weave-http@2.0.0" } } },
          },
          artifact: {
            executable: {
              connections: {
                pets: { connector: "weave-http@2.0.0", required: true },
              },
              dependencies: [
                {
                  kind: "Action",
                  reference: "get-pet@1.0.0",
                  digest: "2".repeat(64),
                  document: getPet,
                },
                {
                  kind: "Action",
                  reference: "crm.lookup@2.0.0",
                  digest: "3".repeat(64),
                  document: workerAction,
                },
                {
                  kind: "Connector",
                  reference: "weave-http@2.0.0",
                  digest,
                  document: { spec: { adapter: "weave-http-v2" } },
                },
              ],
              graph: {
                nodes: [
                  { kind: "humanTask", id: "review", assignment: "reviewers" },
                ],
              },
            },
          },
        },
      }),
    );
    await newWorkflow(page);
    const designer = new DesignerPage(page);
    await designer.setSource(`apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: pets
  version: 1.0.0
spec:
  inputSchema:
    type: object
  outputSchema:
    type: object
  connections:
    pets:
      connector: weave-http@2.0.0
  steps:
    - id: fetch
      kind: action
      uses: get-pet@1.0.0
      connection: pets
      with:
        literal: {}
    - id: crm
      kind: action
      uses: crm.lookup@2.0.0
      with:
        literal:
          customer: c-1
    - id: review
      kind: humanTask
      assignment: reviewers
      title:
        literal: Review
      context:
        literal: {}
      formSchema:
        type: object
      decisions: [approve, reject]
  output:
    literal: {}
`);
    await designer.selectStep("crm");
    await expect.poll(() => recorder.exports).toContain("a2");
    await closeInspector(page);
    await command(page, "Publish…");
    const publish = page.getByRole("dialog");
    await expect(
      publish.getByRole("button", { name: "Publish version" }),
    ).toBeVisible();
    await shot("54-publish-dialog");
    await publish.getByRole("button", { name: "Publish version" }).click();
    await command(page, "Activate…");
    const activate = page.getByRole("dialog");
    await expect(activate).toContainText(/Activate \S+ \d+\.\d+\.\d+/);
    await expect(activate.getByRole("status")).toHaveCount(0);
    await shot("55-activation-dialog", { end: ".modal-panel" });
  },

  async "start-run"({ page, shot }) {
    await connected(page);
    const activation = {
      id: "act-1",
      name: "onboarding",
      revision: 1,
      request: { version_id: versionId },
    };
    await page.route(`${environment}/activations?*`, (r) =>
      r.fulfill({ json: { items: [activation], next_cursor: null } }),
    );
    await page.route(`${project}/workflows/${versionId}/export`, (r) =>
      r.fulfill({
        json: {
          document: {
            apiVersion: "weave/v1alpha1",
            kind: "Workflow",
            metadata: { name: "onboarding", version: "1.0.0" },
            spec: {
              inputSchema: {
                type: "object",
                required: ["customerId"],
                properties: {
                  customerId: { type: "string", title: "Customer ID" },
                  priority: { type: "integer", title: "Priority" },
                  apiKey: { type: "string", title: "API key", writeOnly: true },
                },
              },
              outputSchema: { type: "object" },
              steps: [],
              output: { literal: {} },
            },
          },
        },
      }),
    );
    await open(page, "Runs");
    await page.getByRole("button", { name: "Start run", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Start a run" });
    await selectChoice(
      dialog.getByLabel("Version to run", { exact: true }),
      "act-1",
    );
    await expect(dialog.getByLabel("Customer ID")).toBeVisible();
    await dialog.getByRole("button", { name: "Start run" }).click();
    await expect(dialog.getByRole("alert")).toHaveText("Fill in Customer ID.");
    await shot("56-start-run-dialog", { end: ".modal-panel" });
  },

  async "api-builder"({ page, shot }) {
    test.skip(!hasPython, "Needs the repository's Python environment.");
    await httpPlatform(page);
    await localAuthoring(page);
    await newWorkflow(page);
    const palette = await openPaletteIntegrations(page);
    await palette.getByRole("button", { name: "New API action" }).click();
    await expect(
      builder(page).getByLabel("Name", { exact: true }),
    ).toBeFocused();
    await expect(builder(page).locator(".readiness-line")).toContainText(
      "Platform ready for API actions",
    );
    await shot("60-api-builder", { end: ".modal-panel" });
    await builder(page).getByLabel("Name", { exact: true }).fill("get-record");
    await builder(page)
      .getByLabel("API address")
      .fill("https://api.records.test");
    await builder(page)
      .getByLabel("Path", { exact: true })
      .fill("/v1/records/{id}");
    await selectChoice(
      builder(page).getByLabel("How the API checks who is calling"),
      "api-key",
    );
    await builder(page)
      .getByLabel("Header that carries the key")
      .fill("X-API-Key");
    await builder(page)
      .getByLabel("Example response")
      .fill('{"id": "r-7", "status": "open"}');
    await openYaml(page);
    const preview = page.getByRole("region", { name: /Action YAML for/ });
    await expect(preview).toContainText("sideEffect: read_only");
    await shot("61-api-builder-describe");
    await preview.scrollIntoViewIfNeeded();
    await shot("62-api-builder-preview");
    await builder(page).getByRole("button", { name: "Publish action" }).click();
    const confirm = page.getByRole("dialog", {
      name: "Publish get-record 1.0.0?",
    });
    await expect(confirm).toBeVisible();
    await shot("63-api-builder-publish-confirm");
    await confirm.getByRole("button", { name: "Publish action" }).click();
    await expect(builder(page)).toContainText("Published get-record@1.0.0.");
    await builder(page)
      .getByRole("button", { name: "Insert into workflow" })
      .scrollIntoViewIfNeeded();
    await shot("64-api-builder-published");
  },

  async "openapi-import"({ page, shot }) {
    test.skip(!hasPython, "Needs the repository's Python environment.");
    await httpPlatform(page);
    await localAuthoring(page);
    await newWorkflow(page);
    const palette = await openPaletteIntegrations(page);
    await palette.getByRole("button", { name: "New API action" }).click();
    await builder(page).getByRole("tab", { name: "Import OpenAPI" }).click();
    const panel = page.getByRole("tabpanel", { name: "Import OpenAPI" });
    await shot("65-openapi-import");
    await panel.getByLabel("Or paste it here").fill(petstore);
    await panel.getByRole("button", { name: "List operations" }).click();
    const operation = panel
      .locator(".hb-op")
      .filter({ hasText: "/pets/{petId}" });
    await expect(operation).toContainText("Ready");
    await operation.scrollIntoViewIfNeeded();
    await shot("66-openapi-listed");
    await operation.getByRole("checkbox").check();
    await panel.getByRole("button", { name: "Create 1 action" }).click();
    await expect(
      panel.getByRole("heading", { name: "1 action created" }),
    ).toBeVisible();
    await shot("67-openapi-created");
  },

  async "connection-form"({ page, shot }) {
    await httpPlatform(page);
    await open(page, "Connections");
    await page.getByRole("button", { name: "New connection" }).click();
    const dialog = page.getByRole("dialog", { name: "New API connection" });
    await expect(
      dialog.getByLabel("Connection name", { exact: true }),
    ).toBeVisible();
    await expect(dialog).toContainText("Looks ready.");
    await shot("70-connection-form", { end: ".modal-panel" });
    await dialog.getByLabel("Connection name", { exact: true }).fill("pets");
    await dialog
      .getByLabel("API address", { exact: true })
      .fill("https://api.pets.example");
    await selectChoice(
      dialog.getByLabel("How the API checks who is calling", { exact: true }),
      "machine-token",
    );
    await dialog.getByLabel("Client ID", { exact: true }).fill("studio-client");
    await dialog
      .getByLabel("Token endpoint", { exact: true })
      .fill("https://login.pets.example/oauth2/token");
    await dialog
      .getByLabel("Scopes (optional)", { exact: true })
      .fill("pets.read");
    const secret = dialog.getByLabel("Client secret handle", { exact: true });
    await secret.fill("ghp_abcdefghijklmnopqrstuvwxyz0123456789");
    await dialog.getByRole("button", { name: "Create connection" }).click();
    await expect(dialog).toContainText("This looks like a secret value.");
    await shot("71-connection-form-error");
    await secret.fill("pets-client-secret");
    await dialog.getByRole("button", { name: "Create connection" }).click();
    await expect(dialog).toContainText("Created pets (revision 1).");
    await dialog
      .getByRole("button", { name: "Check configuration (no request is sent)" })
      .click();
    await expect(dialog).toContainText(
      "The platform accepted this configuration.",
    );
    await shot("72-connection-created", { end: ".modal-panel" });
  },

  async connections({ page, shot }) {
    await httpPlatform(page, {
      connections: [
        {
          id: "33333333-3333-4333-8333-333333333333",
          name: "pets",
          connector: "weave-http@2.0.0",
          connector_digest: "d".repeat(64),
          adapter: "weave-http-v2",
          revision: 2,
          config: {
            baseUrl: "https://api.pets.example",
            auth: { kind: "api-key", header: "X-API-Key" },
          },
          secretRef: { api_key: "pets-api-key" },
          allowed_destinations: ["https://api.pets.example"],
        },
        {
          id: "0f8fad5b-d9cb-469f-a165-70867728950f",
          name: "orders-db",
          revision: 3,
          connector: "weave-postgresql@1.0.0",
          adapter: "postgresql",
          allowed_destinations: ["db.internal:5432"],
        },
        { id: "44444444-4444-4444-8444-444444444444", unavailable: true },
      ],
    });
    await page.route(
      `${environment}/connections/33333333-3333-4333-8333-333333333333`,
      (r) =>
        r.fulfill({
          json: {
            id: "33333333-3333-4333-8333-333333333333",
            name: "pets",
            connector: "weave-http@2.0.0",
            connector_digest: "d".repeat(64),
            adapter: "weave-http-v2",
            revision: 2,
            config: {
              baseUrl: "https://api.pets.example",
              auth: { kind: "api-key", header: "X-API-Key" },
            },
            secretRef: { api_key: "pets-api-key" },
            allowed_destinations: ["https://api.pets.example"],
          },
        }),
    );
    await open(page, "Connections");
    await expect(page.locator(".resource-row")).toHaveCount(3);
    await shot("73-connections-list");
    await page.locator(".resource-row").filter({ hasText: "pets" }).click();
    const detail = page.locator(".record-detail");
    await expect(detail).toContainText("https://api.pets.example");
    await detail
      .getByRole("button", { name: "Check configuration (no request is sent)" })
      .click();
    await expect(detail).toContainText(
      "The platform accepted this configuration.",
    );
    await shot("74-connections-detail", { end: ".record-detail" });
  },

  async runs({ page, shot }) {
    await connected(page, {
      capabilities: [
        ...allCapabilities,
        "run.archive",
        "run.purge",
        "run.pause",
        "run.resume",
      ],
    });
    await runRoutes(page);
    await open(page, "Runs");
    await expect(page.locator(".resource-row")).toHaveCount(3);
    await shot("80-runs-list");
    await page.locator(".resource-row").first().click();
    await expect(page.locator(".record-detail")).toContainText(
      "Workflow expense-review 1.0.0",
    );
    await shot("81-runs-detail", { end: ".record-detail, .page-content" });
  },

  async tasks({ page, shot }) {
    await connected(page, {
      capabilities: [
        ...allCapabilities,
        "human_task.claim",
        "human_task.release",
        "human_task.complete",
      ],
    });
    let task: Record<string, unknown> = { ...tasks[0] };
    await page.route("**/human-tasks?*", (r) =>
      r.fulfill({ json: { items: [task, tasks[1]], next_cursor: null } }),
    );
    await page.route("**/human-tasks/task-1", (r) => r.fulfill({ json: task }));
    await page.route("**/human-tasks/task-1/claim", (r) => {
      task = { ...task, status: "claimed", claimant_id: "human", revision: 2 };
      return r.fulfill({ json: task });
    });
    await open(page, "My tasks");
    await expect(page.locator(".resource-row")).toHaveCount(2);
    await shot("82-tasks-list");
    await page.locator(".resource-row").first().click();
    await page.getByRole("button", { name: "Claim task", exact: true }).click();
    await expect(page.getByLabel("Review note")).toBeVisible();
    await shot("83-tasks-detail", { end: ".record-detail, .page-content" });
  },

  async email({ page, shot }) {
    await connected(page, {
      capabilities: [...allCapabilities, "email.read", "email.send"],
    });
    const conversation = {
      id: "thread",
      connection_revision_id: "connection",
      subject: "Expense receipt for the October offsite in Lisbon",
      created_at: "2026-10-01",
    };
    await page.route("**/email/conversations?*", (r) =>
      r.fulfill({ json: { items: [conversation], next_cursor: null } }),
    );
    await page.route("**/email/conversations/thread", (r) =>
      r.fulfill({
        json: {
          conversation,
          messages: [
            {
              id: "message",
              direction: "inbound",
              state: "received",
              accepted_at: "2026-10-01",
              mail: {
                sender: "person@example.test",
                to: ["review@example.test"],
                subject: "Receipt attached",
                text: "Please review my expense. The receipt is attached.",
              },
            },
            {
              id: "reply",
              direction: "outbound",
              state: "accepted",
              accepted_at: "2026-10-01",
              mail: {
                sender: "review@example.test",
                to: ["person@example.test"],
                subject: "Re: Receipt attached",
                text: "Thanks, reviewing now.",
              },
            },
          ],
        },
      }),
    );
    await open(page, "Email");
    await expect(page.locator(".resource-row")).toHaveCount(1);
    await shot("84-email-list");
    await page.locator(".resource-row").click();
    await expect(
      page.getByRole("textbox", { name: "Email reply" }),
    ).toBeVisible();
    await shot("85-email-detail", { end: ".record-detail, .page-content" });
  },

  async workers({ page, shot }) {
    await connected(page);
    const workers = [
      {
        id: "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        release_id: "11111111-1111-4111-8111-111111111111",
        task_types: ["crm-lookup", "weave-connector-http-read"],
        capacity: 4,
        principal_id: "22222222-2222-4222-8222-222222222222",
        revoked: false,
      },
      {
        id: "7c9e6679-7425-40de-944b-e07fc1f90ae8",
        release_id: "11111111-1111-4111-8111-111111111112",
        task_types: ["email-send"],
        capacity: 1,
        principal_id: "22222222-2222-4222-8222-222222222223",
        revoked: true,
      },
    ];
    await page.route(`${environment}/workers?*`, (r) =>
      r.fulfill({ json: { items: workers, next_cursor: null } }),
    );
    await page.route(`**/workers/${workers[0].id}`, (r) =>
      r.fulfill({ json: workers[0] }),
    );
    await open(page, "Workers");
    await expect(page.locator(".resource-row")).toHaveCount(2);
    await shot("86-workers-list");
    await page.locator(".resource-row").first().click();
    await expect(page.locator(".record-detail")).toContainText("crm-lookup");
    await shot("87-workers-detail");
  },

  // The editor's navigation: nothing chosen (full at 1440 px and wider, the
  // rail below), and collapsed by the person. A step is open on the right.
  async "editor-nav-expanded"({ page, shot }) {
    const canvas = await openWorkflow(page);
    await openStepDetails(page, canvas);
    await shot("88-editor-nav-expanded");
  },

  async "editor-nav-collapsed"({ page, shot }) {
    const canvas = await openWorkflow(page);
    const toggle = page.getByRole("button", {
      name: /^(Collapse|Expand) navigation$/,
    });
    // The person collapses it: from the rail, they expand it first. Below
    // 900 px the editor has no button; the rail is all there is.
    if (await toggle.count()) {
      if ((await toggle.getAttribute("aria-expanded")) === "false")
        await toggle.click();
      await toggle.click();
      await expect(toggle).toHaveAttribute("aria-expanded", "false");
    }
    await openStepDetails(page, canvas);
    await shot("89-editor-nav-collapsed");
  },
};

/** Selects a step on the new canvas and waits for its details on the right. */
async function openStepDetails(page: Page, canvas: CanvasPage) {
  await canvas.tileBody("prepare-request").click();
  const details = new DesignerPage(page).inspector;
  // Phones keep the panel closed until asked for: Enter opens it.
  try {
    await expect(details).toBeVisible({ timeout: 1500 });
  } catch {
    await canvas.tileBody("prepare-request").press("Enter");
    await expect(details).toBeVisible();
  }
}

for (const size of sizes)
  test.describe(size.tag, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      deviceScaleFactor: size.scale ?? 1,
    });
    for (const [name, scene] of Object.entries(scenes))
      test(name, async ({ page }) => {
        test.setTimeout(120_000);
        const tour = new Tour(page, size, name);
        await scene(tour);
        tour.finish();
      });
  });
