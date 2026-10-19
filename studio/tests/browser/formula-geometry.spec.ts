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
// The formula editor measured: one height and one pair of edges with the
// other fields, ⤢ centered inside the box, pills inside the line, list rows
// at the pane's density, the Result panel on the field's edges and on screen,
// and Edit formula's columns side by side without overlap or sideways scroll.
// Desktop with a fine pointer: fields 36 px, rows 32 px; touch and below
// 768 px: 44 px.
import { expect, test } from "@playwright/test";
import {
  formulaBox,
  formulaText,
  formulasFixture,
  replaceFormula,
  setTestInput,
  suggestions,
} from "./formula-po";
import {
  box,
  boxes,
  clippedText,
  density,
  expectAligned,
  intersects,
  showParameters,
} from "./step-geometry";
import { openStepFixture, withLanguageFeatures } from "./step-details-po";

const sizes = [
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "1280x720", width: 1280, height: 720 },
  { tag: "768x1024", width: 768, height: 1024 },
  { tag: "600x500", width: 600, height: 500 },
  { tag: "360x640", width: 360, height: 640 },
];

for (const size of sizes)
  test.describe(`formula geometry at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test.beforeEach(async ({ page }) => {
      await withLanguageFeatures(page);
    });

    test("the formula box has a field's height and edges, with ⤢ centered inside it", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("review");
      await showParameters(page, details.dialog);
      const want = await density(page);
      const title = details.field("title");
      const editor = await box(formulaBox(title));
      const expand = await box(
        title.getByRole("button", { name: "Edit formula" }),
      );
      const assign = await box(
        details.field("assignment").locator(".param-input").first(),
      );
      expect(
        Math.abs(editor.height - want.control),
        `box height ${editor.height}`,
      ).toBeLessThanOrEqual(1);
      expectAligned([editor.x, assign.x], "left edges");
      expectAligned([editor.right, assign.right], "right edges");
      expect(expand.width).toBeGreaterThanOrEqual(want.target);
      expect(expand.height).toBeGreaterThanOrEqual(want.target);
      expect(expand.right).toBeLessThanOrEqual(editor.right);
      expectAligned([expand.middle, editor.middle], "⤢ centered");
      const pills = await boxes(title.locator(".cm-formula-pill"));
      expect(pills.length).toBeGreaterThan(0);
      for (const pill of pills) {
        expect(pill.y, "pill top").toBeGreaterThanOrEqual(editor.y);
        expect(pill.bottom, "pill bottom").toBeLessThanOrEqual(editor.bottom);
        expect(pill.right, "pill clear of ⤢").toBeLessThanOrEqual(expand.x);
        expectAligned([pill.middle, editor.middle], "pill centered");
      }
      expect(await clippedText(details.dialog)).toEqual([]);
    });

    test("suggestion rows follow the pane's density and stay on screen", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      await showParameters(page, details.dialog);
      const want = await density(page);
      await replaceFormula(page, details.field("value"), "input.");
      const list = suggestions(page);
      await expect(list).toBeVisible();
      await expect
        .poll(async () => (await boxes(list.getByRole("option"))).length)
        .toBeGreaterThan(2);
      const rows = await boxes(list.getByRole("option"));
      // At 600 px and below a row shows the name over its type, as Add
      // option's items do: 4 + 16 + 8 + 16 + 4.
      const height = size.width <= 600 ? 48 : want.row;
      for (const row of rows) expect(row.height, "row height").toBe(height);
      expectAligned(
        rows.map((row) => row.x),
        "row left edges",
      );
      expectAligned(
        rows.map((row) => row.right),
        "row right edges",
      );
      const outer = await box(list);
      expect(outer.x).toBeGreaterThanOrEqual(0);
      expect(outer.right).toBeLessThanOrEqual(size.width);
      expect(outer.y).toBeGreaterThanOrEqual(0);
      expect(outer.bottom).toBeLessThanOrEqual(size.height);
      expect(await clippedText(list, "")).toEqual([]);
    });

    test("the Result panel sits on the field's edges, 8 px under it, on screen and above the next field", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await setTestInput(page, details, {
        "Customer ID": "c-1042",
        Amount: "1500",
      });
      await details.openWithKeyboard("review");
      await showParameters(page, details.dialog);
      const title = details.field("title");
      await formulaText(title).click();
      const region = title.getByRole("region", { name: "Result of Title" });
      await expect(region).toContainText("1500");
      // Opening it scrolls the form so all of it shows.
      await expect
        .poll(async () => (await box(region)).bottom)
        .toBeLessThanOrEqual(size.height);
      const panel = await box(region);
      const editor = await box(formulaBox(title));
      const next = await box(details.field("answers"));
      expectAligned([panel.x, editor.x], "left edges");
      expectAligned([panel.right, editor.right], "right edges");
      expect(
        Math.abs(panel.y - editor.bottom - 8),
        "gap under the box",
      ).toBeLessThanOrEqual(1);
      expect(editor.y, "the field stays on screen").toBeGreaterThanOrEqual(0);
      expect(intersects(panel, next)).toBe(false);
      expect(await clippedText(region, "")).toEqual([]);
    });

    test("Edit formula lays out its parts without overlap or sideways scroll", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      await showParameters(page, details.dialog);
      await details
        .field("value")
        .getByRole("button", { name: "Edit formula" })
        .click();
      const dialog = page.getByRole("dialog", { name: "Edit formula: Fields" });
      await expect(dialog).toBeVisible();
      const column = dialog.locator("weave-input-pane");
      const input = await box(column);
      const editor = await box(formulaBox(dialog));
      const result = await box(
        dialog.getByRole("region", { name: "Result of Fields" }),
      );
      expect(intersects(input, editor), "Input and formula").toBe(false);
      expect(intersects(editor, result), "formula and Result").toBe(false);
      expect(intersects(input, result), "Input and Result").toBe(false);
      const panel = await box(dialog);
      for (const part of [input, editor, result]) {
        expect(part.x).toBeGreaterThanOrEqual(panel.x);
        expect(part.right).toBeLessThanOrEqual(panel.right + 0.5);
      }
      if (size.width >= 768) {
        // Side by side: the fields on the left, one top edge.
        expect(input.right).toBeLessThanOrEqual(editor.x);
        expectAligned([input.y, editor.y], "column tops");
        const search = await box(
          column.getByRole("textbox", {
            name: "Search fields of earlier steps",
          }),
        );
        expectAligned([search.y, editor.y], "search level with the formula");
      } else {
        // Stacked: the formula and its Result first, then the fields.
        expect(result.y).toBeGreaterThanOrEqual(editor.bottom);
        expect(input.y).toBeGreaterThanOrEqual(result.bottom);
        expect(input.height, "the fields keep room").toBeGreaterThan(40);
      }
      // Nothing in the Input column scrolls sideways or is cut.
      expect(
        await column.evaluate((element) =>
          [element, ...element.querySelectorAll<HTMLElement>("*")]
            .filter(
              (child) =>
                child.scrollWidth > child.clientWidth + 1 &&
                /(auto|scroll)/.test(getComputedStyle(child).overflowX),
            )
            .map((child) => child.className),
        ),
      ).toEqual([]);
      expect(await clippedText(dialog, "")).toEqual([]);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= window.innerWidth,
        ),
      ).toBe(true);
    });

    test("Edit formula's fields show at once, under a search and source that nothing covers", async ({
      page,
    }) => {
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      await showParameters(page, details.dialog);
      await details
        .field("value")
        .getByRole("button", { name: "Edit formula" })
        .click();
      const dialog = page.getByRole("dialog", { name: "Edit formula: Fields" });
      const column = dialog.locator("weave-input-pane");
      const search = await box(
        column.getByRole("textbox", { name: "Search fields of earlier steps" }),
      );
      const source = await box(
        column.getByRole("combobox", { name: "Input source" }),
      );
      expect(intersects(search, source), "search and source").toBe(false);
      // On the column's edges, which are the formula's when stacked.
      const edges = await box(column);
      const editor = await box(formulaBox(dialog));
      expectAligned([search.x, edges.x], "search on the column's left edge");
      const count = await box(column.locator(".pane-count"));
      expectAligned([count.right, edges.right], "count on the right edge");
      if (size.width < 768) {
        expectAligned([edges.x, editor.x], "column and formula left edges");
        expectAligned(
          [edges.right, editor.right],
          "column and formula right edges",
        );
      }
      // One row only where the column has the dialog's width (the
      // formula-first layout of a narrow window); otherwise stacked.
      if (size.width >= 600 && size.width < 768)
        expectAligned(
          [search.middle, source.middle],
          "search and source on one row",
        );
      else
        expect(source.y, "source under the search").toBeGreaterThanOrEqual(
          search.bottom,
        );
      // Every control of the column, and the field rows, without scrolling:
      // on screen inside the column and hit where they are drawn.
      const report = await column.evaluate((element) => {
        const frame = element.getBoundingClientRect();
        const scroller = element.querySelector(".sd-pane-body")!;
        const parts = [
          ...element.querySelectorAll<HTMLElement>(
            "input, select, button, .data-tree > li",
          ),
        ].map((part) => {
          const r = part.getBoundingClientRect();
          const inside =
            r.top >= frame.top - 0.5 &&
            r.bottom <= Math.min(frame.bottom, innerHeight) + 0.5;
          const hit = document.elementFromPoint(
            r.x + r.width / 2,
            r.y + r.height / 2,
          );
          return {
            name:
              part.getAttribute("aria-label") ??
              part.textContent?.trim().slice(0, 24) ??
              part.tagName,
            row: part.matches(".data-tree > li"),
            head: !part.closest(".sd-pane-body"),
            inside,
            hit: !!hit && (hit === part || part.contains(hit)),
          };
        });
        return { scrolled: scroller.scrollTop, parts };
      });
      expect(report.scrolled).toBe(0);
      const rows = report.parts.filter((part) => part.row && part.inside);
      expect(rows.length, "field rows shown at once").toBeGreaterThan(0);
      for (const part of report.parts.filter((part) => part.inside))
        expect(part.hit, `${part.name} is drawn on top`).toBe(true);
      for (const part of report.parts.filter((part) => part.head))
        expect(part.inside, `${part.name} inside the column`).toBe(true);
    });
  });

// Edit formula's layout by the room it has: three columns (fields, formula,
// Result) when the dialog is wide, the Result under the formula beside the
// fields in a tablet's window, the formula first on a narrow window, and
// side by side again on a short one (a phone on its side, 200 % zoom), where
// stacking would push the fields out of sight.
const layouts = [
  { tag: "1920x1080", width: 1920, height: 1080, layout: "three" },
  { tag: "1440x900", width: 1440, height: 900, layout: "three" },
  { tag: "1280x720", width: 1280, height: 720, layout: "three" },
  { tag: "768x1024", width: 768, height: 1024, layout: "two" },
  { tag: "600x500", width: 600, height: 500, layout: "stacked" },
  { tag: "360x640", width: 360, height: 640, layout: "stacked" },
  { tag: "640x360", width: 640, height: 360, layout: "two" },
] as const;
for (const size of layouts)
  test.describe(`Edit formula's layout at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test(`Edit formula lays out ${size.layout === "three" ? "three columns" : size.layout === "two" ? "the fields beside the formula" : "the formula over the fields"}, with a field row and the search in view`, async ({
      page,
    }) => {
      await withLanguageFeatures(page);
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      await showParameters(page, details.dialog);
      await details
        .field("value")
        .getByRole("button", { name: "Edit formula" })
        .click();
      const dialog = page.getByRole("dialog", { name: "Edit formula: Fields" });
      await expect(dialog).toBeVisible();
      const column = dialog.locator("weave-input-pane");
      const input = await box(column);
      const editor = await box(formulaBox(dialog));
      const result = await box(
        dialog.getByRole("region", { name: "Result of Fields" }),
      );
      const search = await box(
        column.getByRole("textbox", { name: "Search fields of earlier steps" }),
      );
      const source = await box(
        column.getByRole("combobox", { name: "Input source" }),
      );
      const footer = await box(dialog.locator(".dialog-actions"));
      if (size.layout === "three") {
        expect(input.right, "fields left of the formula").toBeLessThanOrEqual(
          editor.x,
        );
        expect(editor.right, "Result right of the formula").toBeLessThanOrEqual(
          result.x,
        );
        expectAligned([input.y, editor.y, result.y], "column tops");
      } else if (size.layout === "two") {
        expect(input.right, "fields left of the formula").toBeLessThanOrEqual(
          editor.x,
        );
        expectAligned([input.y, editor.y], "column tops");
        expectAligned([result.x, editor.x], "Result under the formula");
        expect(result.y).toBeGreaterThanOrEqual(editor.bottom);
      } else {
        expect(result.y).toBeGreaterThanOrEqual(editor.bottom);
        expect(input.y).toBeGreaterThanOrEqual(result.bottom);
      }
      // The source's face is as tall as the search box.
      const face = await column
        .locator(".pane-source-field")
        .evaluate((element) => {
          const style = getComputedStyle(element, "::before");
          const box = element.getBoundingClientRect();
          return box.height - parseFloat(style.top) - parseFloat(style.bottom);
        });
      expect(face, "source face height").toBe(search.height);
      // The search keeps a usable width, and the source never squeezes it.
      expect(search.width, "search width").toBeGreaterThanOrEqual(160);
      expect(intersects(search, source)).toBe(false);
      // Without scrolling: the search above the footer, and a field row in
      // the column, above the footer too.
      expect(search.bottom, "search above the footer").toBeLessThanOrEqual(
        footer.y,
      );
      const rows = await boxes(column.locator(".data-tree > li"));
      const shown = rows.filter(
        (row) =>
          row.y >= input.y - 0.5 &&
          row.bottom <= Math.min(input.bottom, footer.y) + 0.5,
      );
      expect(shown.length, "field rows in view").toBeGreaterThan(0);
    });
  });
