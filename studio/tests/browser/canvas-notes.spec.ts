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
// Sticky notes on the new canvas: Shift+S, Markdown, six colors, resizing,
// moving by drag, keys or Move…, anchored notes that follow their step,
// and limits that refuse an edit with a reason instead of losing text.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { AddStepPanel } from "./add-step-po";
import { openStoredWorkflow, openWorkflow } from "./canvas-po";
import { expectHitTarget } from "./support";

test.use({ permissions: ["clipboard-read", "clipboard-write"] });

const source = stringify({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "noted", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [
      { id: "check", kind: "transform", value: { literal: {} } },
      { id: "record", kind: "transform", value: { literal: {} } },
    ],
    output: { literal: {} },
  },
});
const note = (
  id: string,
  text: string,
  extra: Record<string, unknown> = {},
) => ({
  id,
  text,
  color: "green",
  x: 400,
  y: 360,
  width: 240,
  height: 120,
  ...extra,
});
const canvasWith = (notes: unknown[]) => ({
  schemaVersion: 2,
  kind: "weave.studio/canvas",
  notes,
});
const px = async (locator: Locator, property: "left" | "top" | "width") =>
  Number(
    (
      await locator.evaluate(
        (element, name) => (element as HTMLElement).style[name],
        property,
      )
    ).replace("px", ""),
  );
const toast = (page: Page, text: string) =>
  page.locator(".toast").filter({ hasText: text });

test.describe("sticky notes at 1440x900", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  test("Shift+S adds a note under the selected step; Markdown shows when editing ends; color, size and the anchor follow; a reload keeps it", async ({
    page,
  }) => {
    const canvas = await openStoredWorkflow(page, {
      id: "notes-journey",
      source,
    });
    await canvas.tileBody("check").click();
    await canvas.tileBody("check").focus();
    await page.keyboard.press("Shift+S");
    const text = canvas.root.getByRole("textbox", { name: "Note text" });
    await expect(text).toBeFocused();
    await text.fill("## Reviewed\nwith **ops**");
    await page.keyboard.press("Escape");
    const reviewed = canvas.root.getByRole("group", { name: "Note: Reviewed" });
    await expect(reviewed).toBeFocused();
    await expect(reviewed.locator("h4")).toHaveText("Reviewed");
    await expect(reviewed.locator("strong")).toHaveText("ops");

    await reviewed.hover();
    await reviewed
      .getByRole("button", { name: "Color of note Reviewed", exact: true })
      .click();
    await page.getByRole("menuitem", { name: "Blue", exact: true }).click();
    await expect(reviewed).toHaveAttribute("data-color", "blue");

    const before = (await reviewed.boundingBox())!;
    const grip = (await reviewed.locator(".note-grip-se").boundingBox())!;
    await page.mouse.move(grip.x + grip.width / 2, grip.y + grip.height / 2);
    await page.mouse.down();
    await page.mouse.move(grip.x + 60, grip.y + 40, { steps: 6 });
    await page.mouse.up();
    const after = (await reviewed.boundingBox())!;
    expect(after.width).toBeGreaterThan(before.width);
    expect(after.height).toBeGreaterThan(before.height);

    const left = await px(reviewed, "left");
    await canvas.hoverEdge("$trigger:manual>check");
    await canvas.insertTarget("Insert a step before check").click();
    await canvas.pick("Wait for time");
    await canvas.closeInspector();
    await expect.poll(() => px(reviewed, "left")).toBe(left + 248);

    await expect
      .poll(() =>
        page.evaluate(
          () => localStorage.getItem("weave.localDraft.notes-journey") ?? "",
        ),
      )
      .toContain("Reviewed");
    await page.reload();
    await canvas.ready();
    await expect(
      canvas.root.getByRole("group", { name: "Note: Reviewed" }),
    ).toHaveAttribute("data-color", "blue");
  });

  test("Shift+S puts each step's note in a free spot: never over a step, a + or another note", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    expect(await canvas.zoomPercent()).toBe(50);
    const text = canvas.root.getByRole("textbox", { name: "Note text" });
    for (const id of ["pay-and-notify", "post-ledger-entry", "rejected"]) {
      await canvas.tileBody(id).click();
      await canvas.tileBody(id).focus();
      await page.keyboard.press("Shift+S");
      await expect(text).toBeFocused();
      await text.fill(`under ${id}`);
      await page.keyboard.press("Escape");
      await expect(
        canvas.root.getByRole("group", { name: `Note: under ${id}` }),
      ).toBeVisible();
    }
    // A second Shift+S on the same step finds yet another spot.
    await canvas.tileBody("rejected").click();
    await canvas.tileBody("rejected").focus();
    await page.keyboard.press("Shift+S");
    await text.fill("again under rejected");
    await page.keyboard.press("Escape");
    const covered = await canvas.root.evaluate((root) => {
      const rect = (element: Element) => element.getBoundingClientRect();
      const meets = (a: DOMRect, b: DOMRect) =>
        a.left < b.right &&
        b.left < a.right &&
        a.top < b.bottom &&
        b.top < a.bottom;
      const notes = [...root.querySelectorAll(".sticky-note")];
      const blockers = [
        ...root.querySelectorAll(
          ".tile-body, .tile-label, .insert-plus, .lane-slot, .lane-slot-label, .join-bar",
        ),
      ].filter((element) => rect(element).width && rect(element).height);
      const name = (element: Element) =>
        element.getAttribute("aria-label") ??
        element.getAttribute("data-tile") ??
        element.closest("[data-tile]")?.getAttribute("data-tile") ??
        element.className;
      const problems: string[] = [];
      notes.forEach((note, i) => {
        for (const other of [...blockers, ...notes.slice(i + 1)])
          if (meets(rect(note), rect(other)))
            problems.push(`${name(note)} covers ${name(other)}`);
      });
      return { count: notes.length, problems };
    });
    expect(covered).toEqual({ count: 4, problems: [] });
  });

  test("a second Shift+S with nothing selected never stacks on the first note", async ({
    page,
  }) => {
    const canvas = await openStoredWorkflow(page, {
      id: "notes-twice",
      source,
    });
    const text = canvas.root.getByRole("textbox", { name: "Note text" });
    for (const words of ["First thought", "Second thought"]) {
      await canvas.root.focus();
      await page.keyboard.press("Shift+S");
      await expect(text).toBeFocused();
      await text.fill(words);
      await page.keyboard.press("Escape");
    }
    const first = (await canvas.root
      .getByRole("group", { name: "Note: First thought" })
      .boundingBox())!;
    const second = (await canvas.root
      .getByRole("group", { name: "Note: Second thought" })
      .boundingBox())!;
    const overlap =
      Math.max(
        0,
        Math.min(first.x + first.width, second.x + second.width) -
          Math.max(first.x, second.x),
      ) *
      Math.max(
        0,
        Math.min(first.y + first.height, second.y + second.height) -
          Math.max(first.y, second.y),
      );
    expect(overlap).toBe(0);
  });

  test("Move… puts a note where you click, and the size menu resizes it", async ({
    page,
  }) => {
    const canvas = await openStoredWorkflow(page, {
      id: "notes-move",
      source,
      canvas: canvasWith([note("note-0000000a", "Check the totals")]),
    });
    const totals = canvas.root.getByRole("group", {
      name: "Note: Check the totals",
    });
    await totals.hover();
    await totals
      .getByRole("button", {
        name: "More commands for note Check the totals",
        exact: true,
      })
      .click();
    await page
      .getByRole("menuitem", { name: "Size: large", exact: true })
      .click();
    await expect.poll(() => px(totals, "width")).toBe(480);
    await totals.hover();
    await totals
      .getByRole("button", {
        name: "More commands for note Check the totals",
        exact: true,
      })
      .click();
    await page.getByRole("menuitem", { name: "Move…", exact: true }).click();
    await expect(
      canvas.root.getByText("Click where the note should go, or press Escape."),
    ).toBeVisible();
    const spot = await canvas.emptySpot();
    await page.mouse.click(spot.x, spot.y);
    await expect
      .poll(async () => {
        const box = (await totals.boundingBox())!;
        return Math.abs(box.x - spot.x) <= 2 && Math.abs(box.y - spot.y) <= 2;
      })
      .toBe(true);
  });

  test("long words wrap inside the note, and the canvas's 256 KB cap refuses an edit with its reason", async ({
    page,
  }) => {
    const filler = Array.from({ length: 64 }, (_, i) =>
      note(`note-${i.toString(16).padStart(8, "0")}`, "x".repeat(4000), {
        x: 3000 + i * 10,
        y: 2000,
      }),
    );
    const canvas = await openStoredWorkflow(page, {
      id: "notes-cap",
      source,
      canvas: canvasWith([...filler, note("note-0000ffff", "Short")]),
    });
    const short = canvas.root.getByRole("group", { name: "Note: Short" });
    await short.dblclick();
    await canvas.root
      .getByRole("textbox", { name: "Note text" })
      .fill("y".repeat(4000));
    await page.keyboard.press("Escape");
    await expect(
      toast(
        page,
        "The canvas can hold 256 KB of notes and settings. Shorten or delete a note first.",
      ),
    ).toBeVisible();
    await expect(short.locator(".sticky-note-body")).toHaveText("Short");

    const wide = await openStoredWorkflow(page, {
      id: "notes-wrap",
      source,
      canvas: canvasWith([
        note("note-0000000b", `https://weave.example/${"a".repeat(300)}`),
      ]),
    });
    const body = wide.root.locator(".sticky-note-body").first();
    expect(
      await body.evaluate(
        (element) => element.scrollWidth <= element.clientWidth + 1,
      ),
    ).toBe(true);
  });

  test("a note shows Markdown as plain text and safe links, never markup", async ({
    page,
  }) => {
    const canvas = await openStoredWorkflow(page, {
      id: "notes-xss",
      source,
      canvas: canvasWith([
        note(
          "note-0000000c",
          "<script>alert(1)</script> <img src=x onerror=alert(1)>\n[bad](javascript:alert(1)) [docs](https://weave.example)",
        ),
      ]),
    });
    const body = canvas.root.locator(".sticky-note-body").first();
    await expect(body.locator("script, img, iframe, style")).toHaveCount(0);
    await expect(body.locator('a[href^="javascript"]')).toHaveCount(0);
    await expect(body.locator("a")).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
    await expect(body).toContainText("<script>alert(1)</script>");
  });

  test("Fit view shows a note far from the steps", async ({ page }) => {
    const canvas = await openStoredWorkflow(page, {
      id: "notes-fit",
      source,
      canvas: canvasWith([
        note("note-0000000d", "Far away", { x: 1400, y: 640 }),
      ]),
    });
    const far = canvas.root.getByRole("group", { name: "Note: Far away" });
    await expect(far).toBeInViewport();
    await canvas.root.focus();
    await page.keyboard.press("0");
    await page.keyboard.press("1");
    await expect(far).toBeInViewport();
    await expect(canvas.tile("check")).toBeInViewport();
  });

  test("the shortcuts sheet lists the note keys", async ({ page }) => {
    const canvas = await openStoredWorkflow(page, {
      id: "notes-sheet",
      source,
    });
    await canvas.root.focus();
    await page.keyboard.press("?");
    const sheet = page.getByRole("dialog", { name: "Keyboard shortcuts" });
    await expect(sheet).toContainText("Sticky notes");
    await expect(sheet).toContainText("Add a sticky note");
    await expect(sheet).toContainText(
      "Move the note left; with Shift, farther",
    );
  });
});

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`sticky notes by keyboard at ${size.width}x${size.height}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize(size);
    });

    test("the keyboard edits, moves and deletes a note, with Undo", async ({
      page,
    }) => {
      const canvas = await openStoredWorkflow(page, {
        id: `notes-keys-${size.width}`,
        source,
        canvas: canvasWith([note("note-0000000a", "Check the totals")]),
      });
      const totals = canvas.root.getByRole("group", {
        name: "Note: Check the totals",
      });
      await totals.focus();
      const left = await px(totals, "left");
      const top = await px(totals, "top");
      await page.keyboard.press("ArrowRight");
      await page.keyboard.press("Shift+ArrowDown");
      await expect.poll(() => px(totals, "left")).toBe(left + 8);
      await expect.poll(() => px(totals, "top")).toBe(top + 32);
      await page.keyboard.press("Enter");
      await expect(
        canvas.root.getByRole("textbox", { name: "Note text" }),
      ).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(totals).toBeFocused();
      for (const button of [
        totals.getByRole("button", {
          name: "Color of note Check the totals",
          exact: true,
        }),
        totals.getByRole("button", {
          name: "More commands for note Check the totals",
          exact: true,
        }),
      ])
        await expectHitTarget(button);
      await page.keyboard.press("Delete");
      await expect(totals).toHaveCount(0);
      await toast(page, "Deleted the note.")
        .getByRole("button", { name: "Undo", exact: true })
        .click();
      await expect(
        canvas.root.getByRole("group", { name: "Note: Check the totals" }),
      ).toHaveCount(1);
    });

    test("typing in a note never acts on the canvas", async ({ page }) => {
      const canvas = await openStoredWorkflow(page, {
        id: `notes-typing-${size.width}`,
        source,
        canvas: canvasWith([note("note-0000000a", "Check the totals")]),
      });
      await canvas.tileBody("check").click();
      await canvas.root
        .getByRole("group", { name: "Note: Check the totals" })
        .dblclick();
      const text = canvas.root.getByRole("textbox", { name: "Note text" });
      await expect(text).toBeFocused();
      await page.keyboard.type("n/");
      await page.keyboard.press("Backspace");
      await page.keyboard.press("Delete");
      await page.keyboard.press("Shift+S");
      await expect(text).toHaveValue("Check the totalsnS");
      await page.evaluate(
        (value) => navigator.clipboard.writeText(value),
        "- {id: pasted, kind: wait, durationSeconds: 60}",
      );
      await page.keyboard.press("ControlOrMeta+v");
      await expect(text).toHaveValue(/id: pasted/);
      await page.keyboard.press("ControlOrMeta+z");
      await expect(text).not.toHaveValue(/id: pasted/);
      await expect(canvas.tile("check")).toHaveCount(1);
      await expect(canvas.tile("pasted")).toHaveCount(0);
      await expect(new AddStepPanel(page).root).toHaveCount(0);
      await expect(canvas.root.locator(".sticky-note")).toHaveCount(1);
    });

    test("while a note's text is edited, its commands never cover the text", async ({
      page,
    }) => {
      const canvas = await openStoredWorkflow(page, {
        id: `notes-editing-${size.width}`,
        source,
        canvas: canvasWith([note("note-0000000a", "Check the totals")]),
      });
      const totals = canvas.root.getByRole("group", {
        name: "Note: Check the totals",
      });
      const commands = totals.getByRole("toolbar", {
        name: "Commands for note Check the totals",
      });
      await totals.hover();
      await expect(commands).toBeVisible();
      await totals.dblclick();
      const text = canvas.root.getByRole("textbox", { name: "Note text" });
      await expect(text).toBeFocused();
      await expect(commands).toBeHidden();
      // Every corner of the text box is the text box itself, under the pointer too.
      const covered = await text.evaluate((element) => {
        const r = element.getBoundingClientRect();
        return [
          [r.left + 2, r.top + 2],
          [r.right - 2, r.top + 2],
          [r.left + 2, r.bottom - 2],
          [r.right - 2, r.bottom - 2],
          [r.left + r.width / 2, r.top + r.height / 2],
        ].filter(([x, y]) => document.elementFromPoint(x, y) !== element);
      });
      expect(covered).toEqual([]);
      await page.keyboard.press("Escape");
      await expect(totals).toBeFocused();
      await expect(commands).toBeVisible();
    });
  });

for (const size of [
  { width: 1440, height: 900, touch: false },
  { width: 600, height: 500, touch: false },
  { width: 360, height: 640, touch: false },
  { width: 360, height: 640, touch: true },
])
  test.describe(`a note's commands at ${size.width}x${size.height}${size.touch ? " on touch" : ""}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
    });

    test("its buttons keep their target size at any zoom and sit in the note's top right corner", async ({
      page,
    }) => {
      const canvas = await openStoredWorkflow(page, {
        id: `notes-targets-${size.width}-${size.touch}`,
        source,
        canvas: canvasWith([
          note("note-0000000a", "Check the totals", { x: 96, y: 360 }),
        ]),
      });
      const totals = canvas.root.getByRole("group", {
        name: "Note: Check the totals",
      });
      await totals.focus();
      const bar = totals.getByRole("toolbar", {
        name: "Commands for note Check the totals",
      });
      await expect(bar).toBeVisible();
      const minimum = size.touch ? 44 : 24;
      const measure = () =>
        totals.evaluate((element) => {
          const own = element.getBoundingClientRect();
          const bar = element
            .querySelector(".sticky-note-toolbar")!
            .getBoundingClientRect();
          const buttons = [
            ...element.querySelectorAll(".sticky-note-toolbar button"),
          ].map((button) => {
            const box = button.getBoundingClientRect();
            return {
              width: box.width,
              height: box.height,
              middle: box.top + box.height / 2,
            };
          });
          return {
            buttons,
            right: own.right - bar.right,
            top: bar.top - own.top,
            inside:
              bar.left >= own.left - 0.5 && bar.bottom <= own.bottom + 0.5,
          };
        });
      const zoomOut = canvas.root.getByRole("button", {
        name: "Zoom out",
        exact: true,
      });
      for (let step = 0; step < 3; step++) {
        const found = await measure();
        expect(found.buttons).toHaveLength(2);
        for (const button of found.buttons) {
          expect(button.width).toBeGreaterThanOrEqual(minimum - 0.5);
          expect(button.height).toBeGreaterThanOrEqual(minimum - 0.5);
          expect(
            Math.abs(button.middle - found.buttons[0].middle),
          ).toBeLessThanOrEqual(0.5);
        }
        expect(Math.abs(found.right - found.top)).toBeLessThanOrEqual(1);
        expect(found.inside).toBe(true);
        for (const button of await bar.getByRole("button").all())
          await expectHitTarget(button);
        if (size.width < 768) break;
        await zoomOut.click();
        await totals.focus();
      }
    });
  });
