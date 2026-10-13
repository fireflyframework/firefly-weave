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
import { expect, test } from "@playwright/test";
import { openNewWorkflow, openWorkflow } from "./canvas-po";
import { sourceText, tokenColor } from "./support";

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
});

for (const gesture of [
  "Space",
  "Space+Escape",
  "ControlOrMeta",
  "middle",
] as const) {
  test(`${gesture} drag on a step pans without moving or selecting it`, async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    const source = await sourceText(page);
    await canvas.ready();
    await canvas.closeInspector();
    const body = canvas.tileBody("prepare-request");
    await body.focus();
    const before = (await body.boundingBox())!;
    const selected = await body.getAttribute("aria-pressed");
    const held = gesture === "Space+Escape" ? "Space" : gesture;
    if (held !== "middle") await page.keyboard.down(held);
    await page.mouse.move(
      before.x + before.width / 2,
      before.y + before.height / 2,
    );
    await page.mouse.down({ button: gesture === "middle" ? "middle" : "left" });
    await page.mouse.move(
      before.x + before.width / 2 + 80,
      before.y + before.height / 2 + 40,
      { steps: 8 },
    );
    await expect(canvas.root.locator("f-minimap")).toHaveCSS("opacity", "1");
    await expect(canvas.root.locator("[data-toolbar-for]")).toHaveCount(0);
    if (gesture === "Space+Escape") await page.keyboard.press("Escape");
    await page.mouse.up({ button: gesture === "middle" ? "middle" : "left" });
    if (held !== "middle") await page.keyboard.up(held);
    await expect(
      page.getByRole("dialog", { name: /^Step details: / }),
    ).toHaveCount(0);
    await expect
      .poll(async () => (await body.boundingBox())!.x - before.x)
      .toBeCloseTo(80, 0);
    await expect(body).toHaveAttribute("aria-pressed", selected!);
    await expect(
      page.getByRole("complementary", { name: "Inspector" }),
    ).toBeHidden();
    await expect(
      page.getByRole("combobox", { name: "Search steps and actions" }),
    ).toHaveCount(0);
    expect(await sourceText(page)).toBe(source);
    expect(await canvas.rootSteps()).toEqual([
      "prepare-request",
      "approval",
      "route",
      "record-result",
    ]);
  });
}

test("a slipped click inside a + opens its picker without a refusal", async ({
  page,
}) => {
  const canvas = await openNewWorkflow(page);
  await canvas.addFirstStep("Transform");
  await canvas.closeInspector();
  await canvas.root.getByRole("button", { name: /Reset zoom to 100%/ }).click();
  const plus = (await canvas
    .insertTarget("Add a step after transform-1")
    .boundingBox())!;
  await page.mouse.move(plus.x + plus.width / 2, plus.y + plus.height / 2);
  await page.mouse.down();
  await page.mouse.move(plus.x + plus.width / 2 + 6, plus.y + plus.height / 2, {
    steps: 3,
  });
  await page.mouse.up();
  await expect(
    page.getByRole("combobox", { name: "Search steps and actions" }),
  ).toBeFocused();
  await expect(
    page.locator(".toast").filter({ hasText: "Steps run in order" }),
  ).toHaveCount(0);
});

test("Move to keeps + presses for placing the step", async ({ page }) => {
  const canvas = await openWorkflow(page);
  await canvas.tileBody("approval").click({ button: "right" });
  await page.getByRole("menuitem", { name: "Move to…" }).click();
  const plus = canvas.insertTarget("Move approval after record-result");
  await plus.focus();
  const box = (await plus.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 4, box.y + box.height / 2, {
    steps: 2,
  });
  await expect(canvas.root.locator(".rubber-band")).toHaveCount(0);
  await page.mouse.up();
});

test("selection boxes hide hover toolbars while being drawn", async ({
  page,
}) => {
  const canvas = await openWorkflow(page);
  await canvas.tileBody("prepare-request").focus();
  const spot = await canvas.emptySpot();
  await page.mouse.move(spot.x, spot.y);
  await page.mouse.down();
  await page.mouse.move(spot.x + 30, spot.y + 30, { steps: 4 });
  await expect(canvas.root.locator(".f-selection-area")).toBeVisible();
  await expect(canvas.root.locator("[data-toolbar-for]")).toHaveCount(0);
  await page.mouse.up();
});

test("blank space beside a step lets the pointer draw a selection box", async ({
  page,
}) => {
  const canvas = await openWorkflow(page);
  const card = (await canvas.tileBody("approval").boundingBox())!;
  const spot = { x: card.x + card.width / 2, y: card.y - 20 };
  expect(
    await page.evaluate(({ x, y }) => {
      const target = document.elementFromPoint(x, y);
      return (
        !!target?.closest(".graph-flow") &&
        !target.closest(".tile-node, .tile-paint, .tile-content")
      );
    }, spot),
  ).toBe(true);
  await page.mouse.move(spot.x, spot.y);
  await page.mouse.down();
  await page.mouse.move(spot.x + 20, spot.y - 15, { steps: 4 });
  await expect(canvas.root.locator(".f-selection-area")).toBeVisible();
  await page.mouse.up();
});

test("hovering another step adds no toolbar Tab stops", async ({ page }) => {
  const canvas = await openWorkflow(page);
  await canvas.closeInspector();
  await canvas.tileBody("prepare-request").focus();
  await canvas.tileBody("approval").hover();
  const bar = canvas.root.locator('[data-toolbar-for="approval"]');
  await expect(bar).toBeVisible();
  for (const button of await bar.getByRole("button").all())
    await expect(button).toHaveAttribute("tabindex", "-1");
  for (let i = 0; i < 8; i++) {
    await page.keyboard.press("Tab");
    await expect(bar.locator(":focus")).toHaveCount(0);
  }
});

test("double-click opens step details with the same focus as Enter", async ({
  page,
}) => {
  const canvas = await openWorkflow(page);
  await canvas.tileBody("approval").dblclick();
  for (const [id, title] of [
    ["approval", "approval"],
    ["$trigger:manual", "Manual form trigger"],
    ["$end", "End"],
  ]) {
    if (id !== "approval") await canvas.tileBody(id).dblclick();
    const details = page.getByRole("dialog", {
      name: `Step details: ${title}`,
      exact: true,
    });
    await expect(details).toBeVisible();
    await expect
      .poll(() =>
        details.evaluate((dialog) => dialog.contains(document.activeElement)),
      )
      .toBe(true);
    await canvas.closeInspector();
  }
});

test("End has a readable label below its circle at 50 percent", async ({
  page,
}) => {
  const canvas = await openWorkflow(page);
  expect(await canvas.zoomPercent()).toBe(50);
  const label = canvas.tile("$end").locator(".tile-label strong");
  await expect(label).toHaveText("End");
  const pixels = await label.evaluate((element) => {
    const style = getComputedStyle(element);
    const scale = element.getBoundingClientRect().height / element.clientHeight;
    return parseFloat(style.fontSize) * scale;
  });
  expect(Math.round(pixels * 100) / 100).toBeGreaterThanOrEqual(12);
  expect((await label.boundingBox())!.y).toBeGreaterThan(
    (await canvas.tileBody("$end").boundingBox())!.y + 20,
  );
});

test("canvas controls stay at the bottom left with the minimap above", async ({
  page,
}) => {
  const canvas = await openWorkflow(page);
  const area = (await canvas.root.boundingBox())!;
  const tools = (await canvas.root
    .getByRole("toolbar", { name: "Canvas view" })
    .boundingBox())!;
  const minimap = (await canvas.root.locator("f-minimap").boundingBox())!;
  expect(tools.x - area.x).toBeCloseTo(12, 0);
  expect(minimap.y + minimap.height).toBeLessThanOrEqual(tools.y);
});

test("empty workflow hint fits one line and its open slot is selected", async ({
  page,
}) => {
  await page.setViewportSize({ width: 360, height: 740 });
  const canvas = await openNewWorkflow(page);
  const hint = canvas.root.locator(".empty-hint");
  expect(
    await hint.evaluate(
      (element) =>
        element.clientHeight / parseFloat(getComputedStyle(element).lineHeight),
    ),
  ).toBeLessThan(1.1);
  const area = (await canvas.root.boundingBox())!;
  const hintBox = (await hint.boundingBox())!;
  expect(hintBox.x).toBeGreaterThanOrEqual(area.x);
  expect(hintBox.x + hintBox.width).toBeLessThanOrEqual(area.x + area.width);
  await canvas.insertTarget("Add first step").click();
  await expect(canvas.root.locator(".empty-slot")).toHaveCSS(
    "border-top-color",
    await tokenColor(page, "--accent"),
  );
});

test("selection Duplicate uses neutral toolbar text", async ({ page }) => {
  const canvas = await openWorkflow(page);
  await canvas.tileBody("prepare-request").click();
  await canvas.tileBody("approval").click({ modifiers: ["Shift"] });
  await expect(
    canvas.root
      .getByRole("toolbar", { name: "Selected steps" })
      .getByRole("button", { name: "Duplicate", exact: true }),
  ).toHaveCSS("color", await tokenColor(page, "--text"));
});

test("selecting steps keeps the exported viewport and workflow source unchanged", async ({
  page,
}) => {
  const canvas = await openWorkflow(page);
  await canvas.closeInspector();
  const source = await sourceText(page);
  await canvas.ready();
  const viewport = async () => {
    const download = page.waitForEvent("download", (file) =>
      file.suggestedFilename().endsWith(".layout.json"),
    );
    await canvas.root.focus();
    await page.keyboard.press("ControlOrMeta+s");
    const stream = await (await download).createReadStream();
    const parts: Buffer[] = [];
    for await (const part of stream!) parts.push(part);
    return JSON.parse(Buffer.concat(parts).toString()).viewport;
  };
  const before = await viewport();
  for (const id of ["record-result", "approval", "prepare-request"]) {
    await canvas.tileBody(id).focus();
    await page.keyboard.press("Enter");
    await expect(
      page.getByRole("dialog", { name: `Step details: ${id}`, exact: true }),
    ).toBeVisible();
    await canvas.closeInspector();
  }
  expect(await viewport()).toEqual(before);
  expect(await sourceText(page)).toBe(source);
  await expect(
    page
      .locator(".editor-identity .status-chip")
      .filter({ hasText: "Unsaved" }),
  ).toHaveCount(0);
});
