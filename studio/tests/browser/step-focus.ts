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
// A keyboard walk through a dialog: Tab from the current focus until a
// control comes round again, checking that focus stays in the dialog and
// that every stop has a name, a visible focus ring and a 24 px target.
import { expect, type Locator, type Page } from "@playwright/test";

export interface FocusStop {
  key: string;
  ring: boolean;
  size: number;
  link: boolean;
}

/** What the focused control shows: a ring in the focus color on itself, its face or its field box. */
export async function focusStop(page: Page): Promise<FocusStop | null> {
  return page.evaluate(() => {
    const element = document.activeElement as HTMLElement | null;
    const dialog = element?.closest<HTMLElement>('[role="dialog"]');
    if (!element || !dialog) return null;
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    // A ring in the focus color may also be drawn around a smaller face
    // inside the target (the Fixed | Mapped radios, on ::before) or by the
    // field box around a picker's input (:focus-within).
    const probe = document.createElement("span");
    probe.style.color = "var(--focus)";
    dialog.append(probe);
    const focus = getComputedStyle(probe).color;
    probe.remove();
    const rings = (look: CSSStyleDeclaration) =>
      (look.outlineStyle !== "none" &&
        parseFloat(look.outlineWidth) > 0 &&
        look.outlineColor === focus) ||
      look.boxShadow.includes(focus);
    const face = ["::before", "::after"].some((part) =>
      rings(getComputedStyle(element, part)),
    );
    let around = false;
    for (
      let parent = element.parentElement, depth = 0;
      parent && depth < 3 && parent !== dialog;
      parent = parent.parentElement, depth++
    )
      if (parent.matches(":focus-within") && rings(getComputedStyle(parent)))
        around = true;
    // Only a ring in the focus color counts, on the control itself too: a
    // shadow it wears at rest is no focus ring.
    return {
      key: element.outerHTML.slice(0, 160),
      ring: rings(style) || face || around,
      size: Math.min(box.width, box.height),
      link: element.tagName === "A",
    };
  });
}

/** Tabs through `dialog` until a control comes round again; returns how many stops it saw. */
export async function walkFocus(page: Page, dialog: Locator): Promise<number> {
  await expect(dialog).toBeVisible();
  const seen = new Set<string>();
  for (let i = 0; i < 120; i++) {
    await page.keyboard.press("Tab");
    const stop = await focusStop(page);
    expect(stop, "focus stays in the dialog").not.toBeNull();
    if (seen.has(stop!.key)) break;
    seen.add(stop!.key);
    expect(stop!.ring, stop!.key).toBe(true);
    if (!stop!.link) expect(stop!.size, stop!.key).toBeGreaterThanOrEqual(24);
    await expect(page.locator(":focus")).toHaveAccessibleName(/\S/);
  }
  return seen.size;
}
