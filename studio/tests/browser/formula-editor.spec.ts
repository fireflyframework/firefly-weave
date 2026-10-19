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
// The formula editor in step details: typing with suggestions, previews,
// errors that don't apply, the canonical form, pills and keys.
import { expect, test } from "@playwright/test";
import { parse } from "yaml";
import {
  expectActiveRowOnTop,
  fieldLine,
  formulaBox,
  formulaText,
  formulasFixture,
  replaceFormula,
  setTestInput,
  suggestions,
} from "./formula-po";
import { box, showParameters } from "./step-geometry";
import {
  openStepFixture,
  sizes,
  withLanguageFeatures,
} from "./step-details-po";
import { sourceText } from "./support";

const stepValue = async (page: import("@playwright/test").Page, id: string) =>
  parse(await sourceText(page)).spec.steps.find(
    (step: { id: string }) => step.id === id,
  ).value;

for (const size of sizes)
  test.describe(`the formula editor at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test.beforeEach(async ({ page }) => {
      await withLanguageFeatures(page);
    });

    test("types a formula with suggestions, previews true and false, and shows the canonical form on blur", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await setTestInput(page, details, {
        "Customer ID": "c-1042",
        "Email (optional)": "ada@example.com",
        Amount: "1500",
      });
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.amount > 1000",
      );
      await expect(fieldLine(details, "value")).toHaveText("= true");
      await formulaText(field).click();
      await page.keyboard.press("ControlOrMeta+a");
      await page.keyboard.type("input.am", { delay: 20 });
      const list = suggestions(page);
      await expect(list.getByRole("option", { name: /amount/ })).toBeVisible();
      await expect(list.getByRole("option", { name: /amount/ })).toContainText(
        "Number",
      );
      await page.keyboard.press("Enter");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.amount",
      );
      await page.keyboard.type(" > 1000 && exists(input.email", { delay: 10 });
      await page.keyboard.press("Escape");
      await page.keyboard.type(")");
      // While the field has focus its Result shows the value; at rest, the line.
      const result = field.locator(".formula-result-value");
      await expect(result).toHaveText("true");
      await page.keyboard.press("Tab");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.amount > 1000 and exists(input.email)",
      );
      await expect(fieldLine(details, "value")).toHaveText("= true");
      await replaceFormula(page, field, "input.amount > 2000");
      await expect(result).toHaveText("false");
      await page.keyboard.press("Tab");
      await expect(fieldLine(details, "value")).toHaveText("= false");
    });

    test("a formula that doesn't parse shows Not applied, keeps the workflow, and Escape restores it", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      // The Source tab is read before step details open over it.
      const before = await stepValue(page, "flag");
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await replaceFormula(page, field, "input.amount >");
      await expect(field.locator(".param-line")).toContainText("Not applied");
      await expect(field.locator(".param-line")).toContainText(
        "The formula ends too early. Add a value after `>`.",
      );
      await expect(formulaText(field)).toHaveAttribute("aria-invalid", "true");
      // The error shows once, on the line; the Result says only that there
      // is none yet, and offers no tip: the text already reads data.
      await expect(
        field.getByText("The formula ends too early. Add a value after `>`."),
      ).toHaveCount(1);
      const result = field.getByRole("region", { name: "Result of Fields" });
      await expect(result).toContainText(
        "No result until the formula applies.",
      );
      await expect(result).not.toContainText("Tip:");
      await details.close();
      expect(await stepValue(page, "flag")).toEqual(before);
      await details.openWithKeyboard("flag");
      await expect(formulaBox(details.field("value"))).toHaveAttribute(
        "data-text",
        "input.amount >",
      );
      await formulaText(details.field("value")).click();
      await page.keyboard.press("Escape");
      await expect(formulaBox(details.field("value"))).toHaveAttribute(
        "data-text",
        "input.amount > 1000",
      );
      await expect(
        details.field("value").locator(".param-line"),
      ).not.toContainText("Not applied");
    });

    test("a text formula typed in a transform's Fields keeps the field and its focus until it is left", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await replaceFormula(page, field, 'concat("Order ", input.customerId');
      await page.keyboard.type(")");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        'concat("Order ", input.customerId)',
      );
      await expect(formulaText(field)).toBeFocused();
      await expect(details.field("value-text")).toHaveCount(0);
      await page.keyboard.type(" ");
      await page.keyboard.press("Backspace");
      await expect(formulaText(field)).toBeFocused();
      await page.keyboard.press("Tab");
      // Left: the value is text now, so the transform shows it as Text.
      const text = details.field("value-text");
      await expect(formulaBox(text)).toHaveAttribute(
        "data-text",
        "Order {{ input.customerId }}",
      );
      await details.close();
      expect(await stepValue(page, "flag")).toEqual({
        op: {
          name: "concat",
          args: [{ literal: "Order " }, { ref: "/input/customerId" }],
        },
      });
    });

    test("the step's subtitle waits until the formula is left", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      const subtitle = details.dialog.locator(".sd-subtitle");
      await expect(subtitle).toHaveText("Transform · Computes a value");
      const field = details.field("value");
      await replaceFormula(page, field, "input.customerId");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.customerId",
      );
      await expect(subtitle).toHaveText("Transform · Computes a value");
      // Escape closes the suggestions; Tab leaves the field.
      await page.keyboard.press("Escape");
      await expect(suggestions(page)).toHaveCount(0);
      await page.keyboard.press("Tab");
      await expect(subtitle).toHaveText("Transform · Copies /input/customerId");
    });

    test("pasting {{ input.amount }} stores a reference", async ({ page }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      await replaceFormula(page, details.field("value"), "{{ input.amount }}");
      await page.keyboard.press("Tab");
      await details.close();
      expect(await stepValue(page, "flag")).toEqual({ ref: "/input/amount" });
    });

    test("focusing and leaving a formula never rewrites the workflow", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      const before = await sourceText(page);
      await details.openWithKeyboard("flag");
      await formulaText(details.field("value")).click();
      await page.keyboard.press("Tab");
      await details.close();
      expect(await sourceText(page)).toBe(before);
    });

    test("an extended formula stays as it is", async ({ page }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("opaque");
      // The transform has one field; which one depends on how the value reads.
      const field = details.topFields().first();
      await expect(field.locator(".param-formula")).toContainText(
        '"literal":"hidden"',
      );
      await expect(field.locator("weave-formula-editor")).toHaveCount(0);
    });

    test("Escape closes the list, then reverts, then leaves the field, then closes step details", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await replaceFormula(page, field, "input.");
      await expect(suggestions(page)).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(suggestions(page)).toHaveCount(0);
      await expect(details.dialog).toBeVisible();
      await expect(field.locator(".param-line")).toContainText("Not applied");
      await page.keyboard.press("Escape");
      await expect(formulaBox(field)).toHaveAttribute(
        "data-text",
        "input.amount > 1000",
      );
      await expect(formulaText(field)).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(formulaText(field)).not.toBeFocused();
      await expect(details.dialog).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(details.dialog).toHaveCount(0);
    });

    test("the completion list is a named listbox that stays on screen", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await replaceFormula(page, field, "input.");
      const list = suggestions(page);
      await expect(list).toBeVisible();
      const listBox = await box(list);
      expect(listBox.x).toBeGreaterThanOrEqual(0);
      expect(listBox.right).toBeLessThanOrEqual(size.width);
      expect(listBox.bottom).toBeLessThanOrEqual(size.height);
      await expectActiveRowOnTop(page);
      const active = await formulaText(field).getAttribute(
        "aria-activedescendant",
      );
      await expect(list.locator(`[id="${active}"]`)).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await page.keyboard.press("ArrowDown");
      await expect(list.getByRole("option").nth(1)).toHaveAttribute(
        "aria-selected",
        "true",
      );
    });

    test("typing on a reference pill opens the editor with its path", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("review");
      const context = details.field("context");
      await context.getByRole("button", { name: /^reference check/ }).focus();
      await page.keyboard.press(".");
      await expect(formulaBox(context)).toHaveAttribute(
        "data-text",
        "steps.check.output.",
      );
      await expect(formulaText(context)).toBeFocused();
      await expect(suggestions(page)).toBeVisible();
    });

    test("= in an empty Fixed field puts the caret in an empty formula", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("note");
      const field = details.field("value-text");
      const text = field.getByRole("textbox", { name: "Text" });
      await text.fill("");
      await text.press("=");
      await expect(formulaText(field)).toBeFocused();
      await expect(formulaBox(field)).toHaveAttribute("data-text", "");
      await expect(
        field.getByRole("radio", { name: "Mapped", exact: true }),
      ).toBeChecked();
    });
  });

// Keys in the editor that step details also listen to: Undo, Enter, and the
// suggestions on a phone-sized window.
test.describe("the formula editor's keys", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test.beforeEach(async ({ page }) => {
    await withLanguageFeatures(page);
  });

  test("Ctrl or Cmd+Z in a formula runs exactly one workflow Undo", async ({
    page,
  }) => {
    const details = await openStepFixture(page, formulasFixture);
    await details.openWithKeyboard("flag");
    const field = details.field("value");
    await replaceFormula(page, field, "input.amount > 2000");
    await page.keyboard.press("Tab");
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "input.amount > 2000",
    );
    // Past step details' burst, so the next edit is its own Undo step.
    await page.waitForTimeout(800);
    await replaceFormula(page, field, "input.amount > 3000");
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "input.amount > 3000",
    );
    await page.keyboard.press("ControlOrMeta+z");
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "input.amount > 2000",
    );
    await expect(formulaText(field)).toBeFocused();
    await page.keyboard.press("Tab");
    await details.close();
    expect(await stepValue(page, "flag")).toEqual({
      op: { name: "gt", args: [{ ref: "/input/amount" }, { literal: 2000 }] },
    });
  });

  test("Enter accepts a suggestion and never breaks the line of a one-line formula", async ({
    page,
  }) => {
    const details = await openStepFixture(page, formulasFixture);
    await details.openWithKeyboard("flag");
    const field = details.field("value");
    await replaceFormula(page, field, "");
    await expect(formulaText(field)).toHaveAttribute("aria-multiline", "false");
    await page.keyboard.type("input.am", { delay: 20 });
    await expect(suggestions(page)).toBeVisible();
    await page.keyboard.press("Enter");
    await expect(suggestions(page)).toHaveCount(0);
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "input.amount",
    );
    await page.keyboard.press("Enter");
    await page.keyboard.press("Shift+Enter");
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "input.amount",
    );
    await expect(field.locator("weave-formula-editor .cm-line")).toHaveCount(1);
    await expect(details.dialog).toBeVisible();
  });
});

test.describe("the formula editor on a phone", () => {
  test.use({ viewport: { width: 360, height: 640 } });

  test("the completion list keeps the window's gutter when it opens far right", async ({
    page,
  }) => {
    await withLanguageFeatures(page);
    const details = await openStepFixture(page, formulasFixture);
    await details.openWithKeyboard("flag");
    const field = details.field("value");
    await replaceFormula(page, field, "input.amount > 1000 and input.");
    const list = suggestions(page);
    await expect(list).toBeVisible();
    // CodeMirror places the list after it measures: poll until it settles.
    await expect
      .poll(async () => {
        const frame = await box(list);
        return frame.x >= 16 && frame.right <= 360 - 16;
      })
      .toBe(true);
  });

  test("the completion list stays on screen", async ({ page }) => {
    await withLanguageFeatures(page);
    const details = await openStepFixture(page, formulasFixture);
    await details.openWithKeyboard("flag");
    const field = details.field("value");
    await replaceFormula(page, field, "input.");
    const list = suggestions(page);
    await expect(list).toBeVisible();
    // The list keeps the window's 16 px gutter on both sides.
    const listBox = await box(list);
    expect(listBox.x).toBeGreaterThanOrEqual(16);
    expect(listBox.y).toBeGreaterThanOrEqual(0);
    expect(listBox.right).toBeLessThanOrEqual(360 - 16);
    expect(listBox.bottom).toBeLessThanOrEqual(640);
    await expectActiveRowOnTop(page);
    await page.keyboard.press("Enter");
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      /^input\.[A-Za-z]+$/,
    );
  });
});

/** WCAG contrast of two `rgb(…)` colors. */
function contrast(a: string, b: string): number {
  const luminance = (color: string) => {
    const [r, g, b] = color
      .match(/[\d.]+/g)!
      .slice(0, 3)
      .map((part) => {
        const c = Number(part) / 255;
        return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
      });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}

const frameStyle = (element: Element) => {
  const style = getComputedStyle(element);
  return {
    background: style.backgroundColor,
    border: `${style.borderTopWidth} ${style.borderTopStyle} ${style.borderTopColor}`,
    radius: style.borderTopLeftRadius,
    shadow: style.boxShadow,
  };
};

/**
 * What a menu row and a suggestion row must share, read from each row
 * element: its box, spacing, highlight and the label's and type's fonts,
 * with the colors as painted (their alpha and every ancestor's opacity
 * blended into the frame behind them).
 */
function rowLooks(rows: Element[]) {
  const parse = (text: string) => {
    const parts = text.match(/[\d.]+/g)!.map(Number);
    return { rgb: parts.slice(0, 3), alpha: parts[3] ?? 1 };
  };
  const paint = (element: Element, color: string, behind: string) => {
    let opacity = 1;
    for (let node: Element | null = element; node; node = node.parentElement)
      opacity *= Number(getComputedStyle(node).opacity);
    const front = parse(color);
    const back = parse(behind);
    const alpha = front.alpha * opacity;
    const mixed = front.rgb.map(
      (part, i) => part * alpha + back.rgb[i] * (1 - alpha),
    );
    return `rgb(${mixed.map((part) => Math.round(part)).join(", ")})`;
  };
  const font = (element: Element) => {
    const look = getComputedStyle(element);
    let opacity = 1;
    for (let node: Element | null = element; node; node = node.parentElement)
      opacity *= Number(getComputedStyle(node).opacity);
    return [
      `opacity ${opacity}`,
      look.fontFamily,
      look.fontSize,
      look.fontWeight,
      look.lineHeight,
      look.letterSpacing,
      look.color,
    ].join(" | ");
  };
  return rows.map((row) => {
    const style = getComputedStyle(row);
    const label = row.querySelector("b, .cm-formula-option-label")!;
    const detail = row.querySelector(
      "b + span:not(:empty), .cm-formula-option-detail",
    );
    const frame = getComputedStyle(row.parentElement!).backgroundColor;
    const painted = paint(row, style.backgroundColor, frame);
    const rect = row.getBoundingClientRect();
    return {
      selected:
        row.getAttribute("aria-selected") === "true" ||
        row === document.activeElement,
      height: rect.height,
      padding: [
        style.paddingTop,
        style.paddingRight,
        style.paddingBottom,
        style.paddingLeft,
      ].join(" "),
      radius: style.borderTopLeftRadius,
      gap: `${style.columnGap} ${style.rowGap}`,
      direction: style.flexDirection,
      background: style.backgroundColor,
      outline: [
        style.outlineStyle,
        style.outlineWidth,
        style.outlineColor,
        style.outlineOffset,
      ].join(" "),
      shadow: style.boxShadow,
      label: font(label),
      detail: detail ? font(detail) : null,
      labelTop: label.getBoundingClientRect().top - rect.top,
      painted,
      labelInk: paint(label, getComputedStyle(label).color, painted),
      detailInk: detail
        ? paint(detail, getComputedStyle(detail).color, painted)
        : null,
      detailRight:
        !detail ||
        style.flexDirection === "column" ||
        Math.abs(
          detail.getBoundingClientRect().right -
            (rect.right - parseFloat(style.paddingRight)),
        ) < 1,
    };
  });
}

// The suggestion list is one of Studio's menus: Add option's frame, rows,
// keyboard highlight and type, every computed property equal, and readable
// in every row as painted.
for (const size of [
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "600x500", width: 600, height: 500 },
  { tag: "360x740", width: 360, height: 740 },
])
  test.describe(`the suggestion list's look at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("the suggestion list looks like Studio's menus and every row reads", async ({
      page,
    }) => {
      await withLanguageFeatures(page);
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("review");
      await showParameters(page, details.dialog);
      const add = details.dialog.getByRole("button", { name: "Add option" });
      await add.scrollIntoViewIfNeeded();
      await add.focus();
      await page.keyboard.press("Enter");
      const menu = details.dialog.getByRole("menu", { name: "Add option" });
      // The keyboard's item, with a type: the menu's focus ring and highlight.
      const item = menu
        .locator('[role="menuitem"]:has(span:not(:empty))')
        .first();
      await item.focus();
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("ArrowUp");
      await expect(item).toBeFocused();
      // Buttons ease into their highlight: read it once it has settled.
      let previous = "";
      await expect
        .poll(async () => {
          const now = JSON.stringify(await item.evaluateAll(rowLooks));
          const settled = now === previous;
          previous = now;
          return settled;
        })
        .toBe(true);
      const [menuItem] = await item.evaluateAll(rowLooks);
      const menuLook = await menu.evaluate(frameStyle);
      await page.keyboard.press("Escape");
      await details.close();

      await details.openWithKeyboard("flag");
      await showParameters(page, details.dialog);
      const field = details.field("value");
      await replaceFormula(page, field, "input.");
      const list = suggestions(page);
      await expect(list).toBeVisible();
      await page.keyboard.press("ArrowDown");
      const active = list.locator('[aria-selected="true"]');
      await expect(active).toHaveCount(1);
      expect(await list.evaluate(frameStyle)).toEqual(menuLook);
      const rows = await list.getByRole("option").evaluateAll(rowLooks);
      expect(rows.length).toBeGreaterThan(2);
      const shared = (row: typeof menuItem) => ({
        height: row.height,
        padding: row.padding,
        radius: row.radius,
        gap: row.gap,
        direction: row.direction,
        label: row.label,
        detail: row.detail,
        labelTop: row.labelTop,
      });
      for (const row of rows) {
        expect(shared(row)).toEqual(shared(menuItem));
        if (row.selected) {
          expect(row.background).toBe(menuItem.background);
          expect(row.outline).toBe(menuItem.outline);
          expect(row.shadow).toBe(menuItem.shadow);
        }
        expect(contrast(row.labelInk, row.painted)).toBeGreaterThanOrEqual(4.5);
        if (row.detailInk)
          expect(contrast(row.detailInk, row.painted)).toBeGreaterThanOrEqual(
            4.5,
          );
        expect(row.detailRight).toBe(true);
      }
      // The active row is on top where it is drawn: nothing covers it.
      const center = await box(active);
      expect(
        await page.evaluate(
          ([x, y]) =>
            !!document
              .elementFromPoint(x, y)
              ?.closest('[role="option"][aria-selected="true"]'),
          [center.x + center.width / 2, center.middle] as const,
        ),
      ).toBe(true);
    });
  });
