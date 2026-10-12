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
// Geometry for step details' alignment checks: element boxes, the box of an
// element's own text, and text cut off by its own box. Measured in the page,
// in CSS pixels.
import { expect, type Locator, type Page } from "@playwright/test";

export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
  right: number;
  bottom: number;
  /** Vertical center. */
  middle: number;
}

/** The border box of the first matching element. */
export async function box(locator: Locator): Promise<Box> {
  return locator.first().evaluate((element) => {
    const r = element.getBoundingClientRect();
    return {
      x: r.x,
      y: r.y,
      width: r.width,
      height: r.height,
      right: r.right,
      bottom: r.bottom,
      middle: r.y + r.height / 2,
    };
  });
}

/** The boxes of every visible matching element. */
export async function boxes(locator: Locator): Promise<Box[]> {
  return locator.evaluateAll((elements) =>
    elements
      .filter((element) => element.getClientRects().length > 0)
      .map((element) => {
        const r = element.getBoundingClientRect();
        return {
          x: r.x,
          y: r.y,
          width: r.width,
          height: r.height,
          right: r.right,
          bottom: r.bottom,
          middle: r.y + r.height / 2,
        };
      }),
  );
}

/** The box of the element's first line of text (not of its padding or icons). */
export async function textBox(locator: Locator): Promise<Box> {
  return locator.first().evaluate((element) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.textContent?.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      const r = [...range.getClientRects()].find((rect) => rect.width > 0);
      if (!r) continue;
      return {
        x: r.x,
        y: r.y,
        width: r.width,
        height: r.height,
        right: r.right,
        bottom: r.bottom,
        middle: r.y + r.height / 2,
      };
    }
    throw new Error(`No text in ${element.outerHTML.slice(0, 120)}`);
  });
}

/** The left and right edges of an element's content box. */
export async function contentEdges(
  locator: Locator,
): Promise<{ left: number; right: number }> {
  return locator.first().evaluate((element) => {
    const r = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    const scrollbar =
      (element as HTMLElement).offsetWidth - element.clientWidth;
    return {
      left:
        r.x + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft),
      right:
        r.right -
        parseFloat(style.borderRightWidth) -
        parseFloat(style.paddingRight) -
        scrollbar,
    };
  });
}

/**
 * Visible text inside `scope` that its own box cuts off: leaf text, and the
 * text a person sees in a text box. Text may end in "…" only where a tooltip
 * with the whole text is reachable: an element with a title that takes the
 * pointer (a tooltip on a `pointer-events: none` layer never shows).
 */
export async function clippedText(scope: Locator): Promise<string[]> {
  return scope.evaluate((container) => {
    const painted = (element: Element) => {
      const match = getComputedStyle(element).color.match(/rgba?\(([^)]*)\)/);
      const parts = match ? match[1].split(",").map(Number) : [];
      return parts.length < 4 || parts[3] > 0;
    };
    const reachableTitle = (element: Element) => {
      const titled = element.closest<HTMLElement>("[title]");
      return !!titled && getComputedStyle(titled).pointerEvents !== "none";
    };
    const shown = (element: HTMLElement) =>
      element.getClientRects().length > 0 && !element.closest(".sr-only");
    const text = [
      ...container.querySelectorAll<HTMLElement>(
        ".sd-pane:not([hidden]) :is(p, span, label, button, strong, b, h3, h4, small)",
      ),
    ]
      .filter((element) => {
        if (!shown(element)) return false;
        const style = getComputedStyle(element);
        if (style.textOverflow === "ellipsis" && reachableTitle(element))
          return false;
        return (
          element.children.length === 0 &&
          element.getBoundingClientRect().width > 2 &&
          element.scrollWidth > element.clientWidth + 1 &&
          style.overflowX !== "visible"
        );
      })
      .map((element) => element.innerText.trim().slice(0, 80));
    const boxes = [
      ...container.querySelectorAll<HTMLInputElement>(
        ".sd-pane:not([hidden]) input:not([type=checkbox], [type=radio], [type=hidden])",
      ),
    ]
      .filter(
        (input) =>
          shown(input) &&
          !!input.value &&
          painted(input) &&
          input.scrollWidth > input.clientWidth + 1 &&
          // A box at rest may end in "…" when its tooltip is the whole value.
          !(
            document.activeElement !== input &&
            getComputedStyle(input).textOverflow === "ellipsis" &&
            input.title === input.value &&
            reachableTitle(input)
          ),
      )
      .map((input) => input.value.slice(0, 80));
    return [...text, ...boxes];
  });
}

/** Whether two boxes overlap by more than a hairline. */
export function intersects(a: Box, b: Box): boolean {
  return (
    Math.min(a.right, b.right) - Math.max(a.x, b.x) > 0.5 &&
    Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y) > 0.5
  );
}

/** Expects every number to be within `tolerance` of the first. */
export function expectAligned(
  values: number[],
  what: string,
  tolerance = 1,
): void {
  const [first] = values;
  for (const value of values)
    expect(
      Math.abs(value - first),
      `${what}: ${values.map((v) => Math.round(v * 10) / 10).join(", ")}`,
    ).toBeLessThanOrEqual(tolerance);
}

/** Shows the Parameters pane when the panes are tabs (below 768 px). */
export async function showParameters(page: Page, dialog: Locator) {
  const panes = dialog.getByRole("tablist", { name: "Step details panes" });
  if ((page.viewportSize()?.width ?? 1440) < 768)
    await panes.getByRole("tab", { name: "Parameters", exact: true }).click();
}

/** The opacity an element is painted with: its own times its ancestors'. */
export async function paintedOpacity(locator: Locator): Promise<number> {
  return locator.first().evaluate((element) => {
    let opacity = 1;
    for (
      let node: Element | null = element;
      node && node !== document.documentElement;
      node = node.parentElement
    )
      opacity *= parseFloat(getComputedStyle(node).opacity);
    return opacity;
  });
}

/**
 * The part of a scroller the person can see: its box, below any sticky bars
 * that cover its top (the pane tabs and header in the narrow layout).
 */
export async function visibleArea(scroller: Locator): Promise<Box> {
  return scroller.first().evaluate((element) => {
    const r = element.getBoundingClientRect();
    let top = r.top;
    for (const child of element.querySelectorAll<HTMLElement>(
      ":scope > *, :scope > * > *",
    ))
      if (getComputedStyle(child).position === "sticky")
        top = Math.max(top, child.getBoundingClientRect().bottom);
    const bottom = r.top + element.clientHeight;
    const right = r.left + element.clientWidth;
    return {
      x: r.left,
      y: top,
      width: right - r.left,
      height: bottom - top,
      right,
      bottom,
      middle: (top + bottom) / 2,
    };
  });
}

/** The element that scrolls the Parameters form: its tab panel, or the whole sheet. */
export function formScroller(page: Page, dialog: Locator): Locator {
  return (page.viewportSize()?.width ?? 1440) < 768
    ? dialog.locator(".sd-panes")
    : dialog.locator(".sd-tabpanel:not([hidden])");
}

/**
 * Step details has two densities: the desktop one for a fine pointer (fields
 * 36 px, rows 32 px, targets at least 24 px), and the touch one below 768 px
 * or for a coarse pointer (44 px everywhere).
 */
export async function density(page: Page) {
  const touch = await page.evaluate(
    () => matchMedia("(max-width: 767px), (pointer: coarse)").matches,
  );
  return touch
    ? {
        touch,
        control: 44,
        row: 44,
        target: 44,
        labelRow: 44,
        fieldGap: 16,
        fieldInner: 8,
      }
    : {
        touch,
        control: 36,
        row: 32,
        target: 24,
        labelRow: 24,
        fieldGap: 12,
        fieldInner: 4,
      };
}
