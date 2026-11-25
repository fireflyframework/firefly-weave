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
import { addStepSearch } from "./add-step-po";
import { openNewWorkflow, openWorkflow, type CanvasPage } from "./canvas-po";
import { yamlFile } from "./step-details-po";
import { sourceText, tokenColor } from "./support";

test.beforeEach(async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
});

/** Zooms with Ctrl and the wheel over the canvas's center, one event per delta. */
async function wheelZoom(canvas: CanvasPage, deltas: number[]) {
  const { page, root } = canvas;
  const area = (await root.boundingBox())!;
  await page.mouse.move(area.x + area.width / 2, area.y + area.height / 2);
  for (const delta of deltas) {
    const before = await canvas.zoomPercent();
    await page.keyboard.down("Control");
    await page.mouse.wheel(0, delta);
    await page.keyboard.up("Control");
    await expect.poll(() => canvas.zoomPercent()).not.toBe(before);
  }
}

/**
 * The elements whose inline style changes while `act` runs: how many, and
 * which of them are outputs, by key. Each one's style is computed again.
 */
async function restyled(canvas: CanvasPage, act: () => Promise<void>) {
  await canvas.root.evaluate((root) => {
    const changed = new Set<Element>();
    const keep = (records: MutationRecord[]) => {
      for (const record of records) changed.add(record.target as Element);
    };
    const observer = new MutationObserver(keep);
    observer.observe(root, {
      subtree: true,
      attributes: true,
      attributeFilter: ["style"],
    });
    (window as unknown as { restyled: () => object }).restyled = () => {
      keep(observer.takeRecords());
      observer.disconnect();
      return {
        elements: changed.size,
        outputs: [...changed]
          .filter((element) => element.matches(".handle"))
          .map((element) => element.getAttribute("data-handle"))
          .sort(),
      };
    };
  });
  await act();
  return canvas.page.evaluate(() =>
    (
      window as unknown as {
        restyled: () => { elements: number; outputs: string[] };
      }
    ).restyled(),
  );
}

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
    await expect(addStepSearch(page)).toHaveCount(0);
    expect(await sourceText(page)).toBe(source);
    expect(await canvas.rootSteps()).toEqual([
      "prepare-request",
      "approval",
      "route",
      "record-result",
    ]);
  });
}

test("a slipped click inside a + opens Add a step without a refusal", async ({
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
  await expect(addStepSearch(page)).toBeFocused();
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

test("zooming restyles no output of steps without an error path", async ({
  page,
}) => {
  const canvas = await openWorkflow(page);
  const opened = await canvas.zoomPercent();
  // A trigger, plain steps, a decision's paths and a parallel step's branches.
  await expect(canvas.root.locator(".handle[data-handle]")).toHaveCount(14);
  const { elements, outputs } = await restyled(canvas, () =>
    wheelZoom(canvas, [-120, -120, 120, 120, 120]),
  );
  expect(await canvas.zoomPercent()).toBeLessThan(opened);
  expect(outputs).toEqual([]);
  // The canvas itself did change: its scale and each step's label.
  expect(elements).toBeGreaterThan(5);
});

test("zooming keeps the canvas's zoom on the Success and Error outputs only", async ({
  page,
}) => {
  // submit's Error output is a drawing beside its empty path's slot;
  // review's is a button.
  const canvas = await openWorkflow(
    page,
    yamlFile(
      "request-intake.yaml",
      `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: request-intake, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - id: submit
      kind: transform
      value: {literal: sent}
      onError: errorOutput
      errorPath: {steps: [], output: {literal: null}}
    - id: review
      kind: transform
      value: {literal: checked}
      onError: errorOutput
      errorPath:
        steps: [{id: notify-team, kind: transform, value: {literal: told}}]
        output: {literal: null}
    - {id: summarize, kind: transform, value: {literal: done}}
  output: {literal: null}
`,
    ),
  );
  await wheelZoom(canvas, [-120, 120, 120]);
  // An output is drawn 12 units across, so its width on screen tells the zoom.
  const outputs = await canvas.root
    .locator(".handle[data-handle]")
    .evaluateAll((elements) =>
      elements.map((element) => ({
        key: element.getAttribute("data-handle"),
        zoom: (element as HTMLElement).style.getPropertyValue("--zoom"),
        drawn: element.getBoundingClientRect().width / 12,
      })),
    );
  expect(
    outputs
      .filter((output) => output.zoom)
      .map((output) => output.key)
      .sort(),
  ).toEqual([
    "review:path:errorPath",
    "review:path:success",
    "submit:path:errorPath",
    "submit:path:success",
  ]);
  for (const output of outputs.filter((item) => item.zoom))
    expect(Number(output.zoom), output.key!).toBeCloseTo(output.drawn, 3);
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

test("selecting steps that are in view, and opening and closing their details, leaves the canvas's pan and zoom alone", async ({
  page,
}) => {
  const canvas = await openWorkflow(page);
  await canvas.closeInspector();
  const view = async () => ({
    zoom: await canvas.zoomPercent(),
    tile: await canvas.tileBody("prepare-request").boundingBox(),
  });
  const before = await view();
  await canvas.tileBody("prepare-request").click();
  await canvas.tileBody("approval").click({ modifiers: ["Shift"] });
  await canvas.tileBody("approval").click();
  expect(await view()).toEqual(before);
  for (const id of ["approval", "prepare-request"]) {
    await canvas.tileBody(id).focus();
    await page.keyboard.press("Enter");
    await expect(
      page.getByRole("dialog", { name: `Step details: ${id}`, exact: true }),
    ).toBeVisible();
    await canvas.closeInspector();
  }
  expect(await view()).toEqual(before);
});

test("selecting steps keeps the exported canvas file and workflow source unchanged", async ({
  page,
}) => {
  const canvas = await openWorkflow(page);
  await canvas.closeInspector();
  const source = await sourceText(page);
  await canvas.ready();
  // The canvas file Save to file writes, whole.
  const exported = async () => {
    const download = page.waitForEvent("download", (file) =>
      file.suggestedFilename().endsWith(".canvas.json"),
    );
    await canvas.root.focus();
    await page.keyboard.press("ControlOrMeta+s");
    const stream = await (await download).createReadStream();
    const parts: Buffer[] = [];
    for await (const part of stream!) parts.push(part);
    return JSON.parse(Buffer.concat(parts).toString());
  };
  const before = await exported();
  for (const id of ["record-result", "approval", "prepare-request"]) {
    await canvas.tileBody(id).focus();
    await page.keyboard.press("Enter");
    await expect(
      page.getByRole("dialog", { name: `Step details: ${id}`, exact: true }),
    ).toBeVisible();
    await canvas.closeInspector();
  }
  expect(await exported()).toEqual(before);
  expect(await sourceText(page)).toBe(source);
  await expect(
    page
      .locator(".editor-identity .status-chip")
      .filter({ hasText: "Unsaved" }),
  ).toHaveCount(0);
});
