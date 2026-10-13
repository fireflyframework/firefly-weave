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
// Tidy layout on the new canvas: Shift+Alt+T or the tools' button moves free
// notes off the steps, keeps anchored notes, fits the view, and is one undo
// step with an Undo toast.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { stringify } from "yaml";
import { openStoredWorkflow } from "./canvas-po";

const source = stringify({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "tidy", version: "1.0.0" },
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
const note = (id: string, text: string, extra: Record<string, unknown>) => ({
  id,
  text,
  color: "yellow",
  x: 0,
  y: 0,
  width: 240,
  height: 120,
  ...extra,
});
const canvas = {
  schemaVersion: 2,
  kind: "weave.studio/canvas",
  notes: [
    note("note-0000000a", "Covers check", { x: 360, y: 120 }),
    note("note-0000000b", "Follows record", {
      anchor: { stepId: "record", dx: 0, dy: 0 },
    }),
    note("note-0000000c", "Clear of the steps", { x: 400, y: 560 }),
  ],
};
const at = async (locator: Locator) =>
  locator.evaluate((element) => ({
    left: (element as HTMLElement).style.left,
    top: (element as HTMLElement).style.top,
  }));
const toast = (page: Page, text: string) =>
  page.locator(".toast").filter({ hasText: text });

test("Shift+Alt+T moves a free note off the steps, keeps the others, fits the view, and Undo puts it back", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const view = await openStoredWorkflow(page, {
    id: "tidy-keys",
    source,
    canvas,
  });
  const group = (name: string) =>
    view.root.getByRole("group", { name: `Note: ${name}` });
  const covers = group("Covers check");
  const before = {
    covers: await at(covers),
    follows: await at(group("Follows record")),
    clear: await at(group("Clear of the steps")),
  };
  await view.root.focus();
  await page.keyboard.press("+");
  await page.keyboard.press("+");
  await view.root.focus();
  await page.keyboard.press("Alt+Shift+T");
  await expect(toast(page, "Tidied the canvas.")).toBeVisible();
  await expect.poll(() => at(covers)).not.toEqual(before.covers);
  expect(await at(group("Follows record"))).toEqual(before.follows);
  expect(await at(group("Clear of the steps"))).toEqual(before.clear);
  const covering = await view.root.evaluate((root) => {
    const tiles = [...root.querySelectorAll(".tile-node")].map((tile) =>
      tile.getBoundingClientRect(),
    );
    return [...root.querySelectorAll<HTMLElement>(".sticky-note")]
      .filter(
        (item) => item.getAttribute("aria-label") !== "Note: Follows record",
      )
      .filter((item) => {
        const a = item.getBoundingClientRect();
        return tiles.some(
          (b) =>
            a.left < b.right &&
            b.left < a.right &&
            a.top < b.bottom &&
            b.top < a.bottom,
        );
      })
      .map((item) => item.getAttribute("aria-label"));
  });
  expect(covering).toEqual([]);
  for (const item of [covers, group("Clear of the steps"), view.tile("record")])
    await expect(item).toBeInViewport();
  await toast(page, "Tidied the canvas.")
    .getByRole("button", { name: "Undo", exact: true })
    .click();
  await expect.poll(() => at(covers)).toEqual(before.covers);
});

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test(`the tools' Tidy layout says when the canvas is already tidy at ${size.width}x${size.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(size);
    const view = await openStoredWorkflow(page, {
      id: `tidy-tools-${size.width}`,
      source,
      canvas: { ...canvas, notes: canvas.notes.slice(1) },
    });
    if (size.width >= 768)
      await view.root
        .getByRole("toolbar", { name: "Canvas view" })
        .getByRole("button", { name: "Tidy layout", exact: true })
        .click();
    else {
      await view.root
        .getByRole("button", { name: "Canvas view", exact: true })
        .click();
      await page
        .getByRole("menuitem", { name: "Tidy layout", exact: true })
        .click();
    }
    await expect(toast(page, "The canvas is already tidy.")).toBeVisible();
    await expect(toast(page, "Tidied the canvas.")).toHaveCount(0);
  });
