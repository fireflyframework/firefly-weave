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
import { test, expect, type Locator, type Page } from "@playwright/test";
import {
  connected,
  defaultCatalog,
  expectHitTarget,
  lookupAction,
  newWorkflow,
} from "./support";
import { DesignerPage } from "./designer-po";

const workflow = JSON.stringify({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "inspector-overlays", version: "1.0.0" },
  spec: {
    inputSchema: {
      type: "object",
      properties: Object.fromEntries(
        Array.from({ length: 18 }, (_, i) => [
          `field${i + 1}`,
          { type: "string", title: `Field ${i + 1}` },
        ]),
      ),
    },
    outputSchema: { type: "object" },
    steps: [
      { id: "transform", kind: "transform", value: { ref: "/input" } },
      {
        id: "decision",
        kind: "switch",
        cases: [
          {
            when: {
              op: {
                name: "eq",
                args: [{ ref: "/input/field1" }, { literal: "yes" }],
              },
            },
            steps: [],
            output: { literal: {} },
          },
        ],
        default: { steps: [], output: { ref: "/input" } },
      },
      {
        id: "action",
        kind: "action",
        uses: "sql.lookup@1.0.0",
        with: { object: { parameters: { ref: "/input" } } },
      },
    ],
    output: { literal: {} },
  },
});

async function setup(page: Page) {
  await connected(page, {
    catalog: [
      ...defaultCatalog,
      ...Array.from({ length: 12 }, (_, i) => ({
        id: `overlay-action-${i}`,
        document: {
          ...lookupAction,
          metadata: {
            name: `example.action-${String(i).padStart(2, "0")}`,
            version: "1.0.0",
          },
        },
      })),
    ],
  });
  await newWorkflow(page);
  const designer = new DesignerPage(page);
  await designer.setSource(workflow);
  return designer;
}

async function openList(field: Locator) {
  await field.click();
  await field.press("Alt+ArrowDown");
  await expect(field).toHaveAttribute("aria-expanded", "true");
  const id = await field.getAttribute("aria-controls");
  return field.page().locator(`[id="${id}"]`);
}

async function expectAnchored(list: Locator, field: Locator) {
  await expect
    .poll(() => list.evaluate((e) => e.matches(":popover-open")))
    .toBe(true);
  await expect
    .poll(async () => {
      const box = (await list.boundingBox())!;
      const viewport = field.page().viewportSize()!;
      return (
        box.x >= 8 &&
        box.y >= 8 &&
        box.x + box.width <= viewport.width - 8 &&
        box.y + box.height <= viewport.height - 8
      );
    })
    .toBe(true);
  const box = await list.boundingBox();
  const fieldBox = await field.locator("..").boundingBox();
  const viewport = field.page().viewportSize()!;
  expect(box).not.toBeNull();
  expect(fieldBox).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(8);
  expect(box!.y).toBeGreaterThanOrEqual(8);
  expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width - 8);
  expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height - 8);
  expect(box!.width).toBeCloseTo(
    Math.min(Math.max(fieldBox!.width, 320), viewport.width - 16),
    0,
  );
  if ((await list.getAttribute("data-side")) === "above")
    expect(box!.y + box!.height).toBeLessThanOrEqual(fieldBox!.y - 4);
  else
    expect(box!.y).toBeGreaterThanOrEqual(fieldBox!.y + fieldBox!.height + 4);
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1280, height: 720 },
  { width: 600, height: 500 },
]) {
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    for (const context of [
      "Transform",
      "Decision rule",
      "grouped action input",
      "last inspector field",
      "action picker",
    ] as const) {
      test(`${context} suggestions escape inspector clipping`, async ({
        page,
      }) => {
        const designer = await setup(page);
        const step =
          context === "Transform"
            ? "transform"
            : context === "Decision rule" || context === "last inspector field"
              ? "decision"
              : "action";
        await designer.selectStep(step);
        if (context === "last inspector field")
          await designer.inspector.locator(".otherwise summary").click();
        const field =
          context === "Transform"
            ? designer.inspector.getByRole("combobox", {
                name: "Value reference",
                exact: true,
              })
            : context === "Decision rule"
              ? designer.inspector.getByRole("combobox", {
                  name: "Condition 1 data",
                  exact: true,
                })
              : context === "last inspector field"
                ? designer.inspector.getByRole("combobox", {
                    name: "Otherwise result reference",
                    exact: true,
                  })
                : context === "grouped action input"
                  ? designer.inspector
                      .locator('[data-path="parameters"]')
                      .getByRole("combobox")
                  : designer.inspector
                      .locator("weave-action-picker")
                      .getByRole("combobox");
        const list = await openList(field);
        await expectAnchored(list, field);
        for (let index = 0; index < 5; index++)
          await expectHitTarget(list.getByRole("option").nth(index));
        await expect(field).toBeFocused();
        await field.press("Escape");
        await expect(list).toBeHidden();
        await expect(designer.inspector).toBeVisible();
      });
    }
    test("step actions stay in the top layer and retain menu keyboard focus", async ({
      page,
    }) => {
      const designer = await setup(page);
      await designer.selectStep("transform");
      const button = designer.inspector.getByRole("button", {
        name: "Step actions",
        exact: true,
      });
      await button.click();
      const menu = designer.inspector.getByRole("menu", {
        name: "Step actions",
        exact: true,
      });
      await expect
        .poll(() => menu.evaluate((e) => e.matches(":popover-open")))
        .toBe(true);
      for (const item of await menu.getByRole("menuitem").all())
        await expectHitTarget(item);
      await page.keyboard.press("Escape");
      await expect(button).toBeFocused();
      await expect(menu).toHaveCount(0);
    });
  });
}

test("an open action picker follows scrolling and resizing and closes when its field leaves view", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const designer = await setup(page);
  await designer.selectStep("action");
  const field = designer.inspector
    .locator("weave-action-picker")
    .getByRole("combobox");
  const list = await openList(field);
  const before = (await field.boundingBox())!.y;
  const body = designer.inspector.locator(".inspector-body");
  const bodyBox = (await body.boundingBox())!;
  await page.mouse.move(bodyBox.x + bodyBox.width - 2, bodyBox.y + 16);
  await page.mouse.wheel(0, 32);
  await expect
    .poll(async () => (await field.boundingBox())!.y)
    .toBeLessThan(before);
  await expectAnchored(list, field);
  await page.setViewportSize({ width: 1280, height: 720 });
  await expectAnchored(list, field);
  const resized = (await body.boundingBox())!;
  await page.mouse.move(resized.x + resized.width - 2, resized.y + 16);
  await page.mouse.wheel(0, 2000);
  await expect(field).toHaveAttribute("aria-expanded", "false");
  await expect(list).toBeHidden();
});

for (const kind of ["data", "action"] as const) {
  test(`${kind} picker PageDown and PageUp move one visible page`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    const designer = await setup(page);
    await designer.selectStep(kind === "data" ? "transform" : "action");
    const field =
      kind === "data"
        ? designer.inspector.getByRole("combobox", {
            name: "Value reference",
            exact: true,
          })
        : designer.inspector
            .locator("weave-action-picker")
            .getByRole("combobox");
    const list = await openList(field);
    if (kind === "action") await field.fill("example");
    await field.press(kind === "data" ? "ArrowDown" : "Home");
    const activeIndex = async () => {
      const active = await field.getAttribute("aria-activedescendant");
      return list
        .getByRole("option")
        .evaluateAll(
          (options, id) => options.findIndex((option) => option.id === id),
          active,
        );
    };
    await expect.poll(activeIndex).toBe(0);
    await field.press("PageDown");
    await expect.poll(activeIndex).toBeGreaterThan(0);
    expect(await activeIndex()).toBeLessThan(
      (await list.getByRole("option").count()) - 1,
    );
    await field.press("PageUp");
    await expect.poll(activeIndex).toBe(0);
    await expect(field).toBeFocused();
    await field.press("Escape");
    await expect(list).toBeHidden();
  });
}
