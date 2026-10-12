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
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { openStepFixture, sizes, yamlFile } from "./step-details-po";
import {
  box,
  contentEdges,
  expectAligned,
  intersects,
  showParameters,
  textBox,
  type Box,
} from "./step-geometry";

const longId = "check-the-customer-record-against-the-ledger-before-routing-it";
const wide = (cases: number) => `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: many-paths, version: 1.0.0}
spec:
  inputSchema: {type: object, properties: {amount: {type: number}}}
  outputSchema: {type: object}
  steps:
    - {id: start-here, kind: wait, durationSeconds: 60}
    - id: ${longId}
      kind: switch
      cases:
${Array.from(
  { length: cases },
  (
    _,
    i,
  ) => `        - when: {op: {name: gt, args: [{ref: /input/amount}, {literal: ${i}}]}}
          steps: [{id: path-${i + 1}, kind: wait, durationSeconds: 60}]
          output: {literal: {}}`,
).join("\n")}
      default: {steps: [], output: {literal: {}}}
  output: {literal: {}}
`;

for (const size of sizes)
  test.describe(`step details layout at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("the header holds the kind tile, name, Execute step, More and the tabs", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("lookup");
      const dialog = details.dialog;
      if (size.width < 768)
        await dialog
          .getByRole("tab", { name: "Parameters", exact: true })
          .first()
          .click();
      await expect(dialog.locator(".sd-kind")).toBeVisible();
      await expect(
        dialog.getByRole("button", { name: "Rename lookup" }),
      ).toBeVisible();
      await expect(
        dialog.getByRole("button", { name: "Execute step", exact: true }),
      ).toHaveAttribute("aria-disabled", "true");
      const execute = dialog.getByRole("button", {
        name: "Execute step",
        exact: true,
      });
      // Unavailable, it reads as disabled (the disabled tokens), never as the accent action.
      const tokens = await execute.evaluate((el) => {
        const sample = document.createElement("span");
        el.append(sample);
        const read = (background: string, color: string) => {
          sample.style.backgroundColor = background;
          sample.style.color = color;
          const style = getComputedStyle(sample);
          return { background: style.backgroundColor, color: style.color };
        };
        const result = {
          disabled: read("var(--disabled-bg)", "var(--disabled-ink)"),
          enabled: read("var(--accent)", "var(--on-accent)"),
        };
        sample.remove();
        return result;
      });
      await expect(execute).toHaveCSS(
        "background-color",
        tokens.disabled.background,
      );
      await expect(execute).toHaveCSS("color", tokens.disabled.color);
      expect(tokens.disabled.background).not.toBe(tokens.enabled.background);
      const toggle = dialog.getByRole("button", {
        name: "More ways to execute",
      });
      await expect(toggle).toHaveCSS(
        "background-color",
        tokens.disabled.background,
      );
      await expect(toggle).toHaveCSS("color", tokens.disabled.color);
      const box = (await execute.boundingBox())!;
      const center = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
      expect(
        await execute.evaluate(
          (el, point) =>
            el.contains(document.elementFromPoint(point.x, point.y)),
          center,
        ),
      ).toBe(true);
      // It is focusable and explains its limitation; the native button is enabled.
      await page.mouse.click(center.x, center.y);
      await expect(page.locator(".toast-region")).toContainText(
        "Executing a single step isn't available yet.",
      );
      await page.getByRole("button", { name: "Dismiss notification" }).click();
      await expect(page.locator(".toast-region .toast")).toHaveCount(0);
      await execute.focus();
      await page.keyboard.press("Enter");
      await expect(page.locator(".toast-region")).toContainText(
        "Executing a single step isn't available yet.",
      );
      await expect(
        dialog.getByRole("button", { name: "More ways to execute" }),
      ).toBeVisible();
      await expect(
        dialog.getByRole("button", { name: "More actions for lookup" }),
      ).toBeVisible();
      await expect(details.tab("Parameters")).toHaveAttribute(
        "aria-selected",
        "true",
      );
      await expect(dialog.getByRole("link", { name: "Docs" })).toHaveAttribute(
        "href",
        /studio-step-reference\/#configure-call-an-action$/,
      );
    });

    test("Ctrl/Cmd+Enter explains that single-step execution isn't available", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("lookup");
      await page.keyboard.press("ControlOrMeta+Enter");
      await expect(page.locator(".toast-region")).toContainText(
        "Executing a single step isn't available yet.",
      );
      await details.dialog
        .getByRole("button", { name: "More ways to execute" })
        .click();
      await expect(
        page.getByRole("menuitem", { name: /Simulate the workflow/ }),
      ).toHaveAttribute("aria-disabled", "true");
      await page.keyboard.press("Escape");
      await expect(details.dialog).toBeVisible();
    });

    test("a confirmation from step details returns focus to it", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("route-by-value");
      const more = details.dialog.getByRole("button", {
        name: "More actions for route-by-value",
      });
      await more.click();
      await page.getByRole("menuitem", { name: "Delete", exact: true }).click();
      const confirm = page.getByRole("dialog", {
        name: /Delete route-by-value/,
      });
      await expect(confirm).toBeVisible();
      await confirm.getByRole("button", { name: "Cancel" }).click();
      await expect(more).toBeFocused();
      await expect(details.dialogFor("route-by-value")).toBeVisible();
    });

    test("Edit as YAML applies the step's YAML and refuses another ID", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("wait-for-payment");
      await details.dialog
        .getByRole("button", { name: "More actions for wait-for-payment" })
        .click();
      await page.getByRole("menuitem", { name: "Edit as YAML" }).click();
      const editor = page.getByRole("dialog", {
        name: "Edit wait-for-payment as YAML",
      });
      const text = editor.getByRole("textbox", { name: "Step YAML" });
      await text.fill(
        (await text.inputValue()).replace(
          "payment-received",
          "payment-settled",
        ),
      );
      await editor.getByRole("button", { name: "Apply" }).click();
      await expect(editor).toHaveCount(0);
      await details.dialog
        .getByRole("button", { name: "More actions for wait-for-payment" })
        .click();
      await page.getByRole("menuitem", { name: "Edit as YAML" }).click();
      await expect(text).toHaveValue(/payment-settled/);
      await text.fill(
        (await text.inputValue()).replace("id: wait-for-payment", "id: other"),
      );
      await editor.getByRole("button", { name: "Apply" }).click();
      await expect(editor).toContainText(
        "Keep the step's ID and kind. Rename the step with Rename.",
      );
    });

    test("long names and seven neighbors fit at narrow sizes", async ({
      page,
    }) => {
      const details = await openStepFixture(
        page,
        yamlFile("many-paths.yaml", wide(7)),
      );
      const node = details.node(longId);
      await node.scrollIntoViewIfNeeded();
      await node.dblclick();
      await expect(details.dialogFor(longId)).toBeVisible();
      await expect(details.dialog.locator(".sd-name-text")).toHaveText(longId);
      await expect(
        details.dialog.getByRole("button", { name: `Rename ${longId}` }),
      ).toHaveAttribute("title", longId);
      if (size.width >= 768) {
        await expect(
          details.dialog.locator(".sd-after .sd-neighbor"),
        ).toHaveCount(6);
        await expect(
          details.dialog.getByRole("button", {
            name: "More steps that run right after",
          }),
        ).toContainText("+1");
      }
      const sideways = await page.evaluate(
        () => document.scrollingElement!.scrollWidth - window.innerWidth,
      );
      expect(sideways).toBeLessThanOrEqual(1);
    });

    if (size.width >= 1100)
      test("separators resize by pointer and keyboard, reset on double-click and persist", async ({
        page,
      }) => {
        const details = await openStepFixture(page);
        await details.open("lookup");
        const input = details.dialog.locator(".sd-input");
        const separator = details.dialog.getByRole("separator", {
          name: "Resize Input",
        });
        const before = (await input.boundingBox())!.width;
        const handle = (await separator.boundingBox())!;
        await page.mouse.move(handle.x + handle.width / 2, handle.y + 200);
        await page.mouse.down();
        await page.mouse.move(
          handle.x + handle.width / 2 + 80,
          handle.y + 200,
          { steps: 4 },
        );
        await page.mouse.up();
        await expect
          .poll(async () =>
            Math.round((await input.boundingBox())!.width - before),
          )
          .toBe(80);
        await separator.focus();
        await page.keyboard.press("ArrowRight");
        await page.keyboard.press("ArrowRight");
        await expect
          .poll(async () =>
            Math.round((await input.boundingBox())!.width - before),
          )
          .toBe(112);
        await page.keyboard.press("Home");
        await expect
          .poll(async () => Math.round((await input.boundingBox())!.width))
          .toBe(240);
        await details.close();
        await details.open("lookup");
        await expect
          .poll(async () =>
            Math.round(
              (await details.dialog.locator(".sd-input").boundingBox())!.width,
            ),
          )
          .toBe(240);
        await details.dialog
          .getByRole("separator", { name: "Resize Input" })
          .dblclick();
        await expect
          .poll(async () =>
            Math.round(
              (await details.dialog.locator(".sd-input").boundingBox())!.width,
            ),
          )
          .toBe(Math.round(before));
      });

    test("F6 and Shift+F6 move between regions", async ({ page }) => {
      const details = await openStepFixture(page);
      await details.open("lookup");
      await details.dialog
        .getByRole("button", { name: "Rename lookup" })
        .focus();
      await page.keyboard.press("F6");
      await expect
        .poll(() =>
          page.evaluate(() =>
            document.activeElement
              ?.closest("[data-region]")
              ?.getAttribute("data-region"),
          ),
        )
        .toBe("input");
      await page.keyboard.press("F6");
      await expect
        .poll(() =>
          page.evaluate(() =>
            document.activeElement
              ?.closest("[data-region]")
              ?.getAttribute("data-region"),
          ),
        )
        .toBe("parameters");
      await page.keyboard.press("F6");
      await expect
        .poll(() =>
          page.evaluate(() =>
            document.activeElement
              ?.closest("[data-region]")
              ?.getAttribute("data-region"),
          ),
        )
        .toBe("output");
      await page.keyboard.press("Shift+F6");
      await expect
        .poll(() =>
          page.evaluate(() =>
            document.activeElement
              ?.closest("[data-region]")
              ?.getAttribute("data-region"),
          ),
        )
        .toBe("parameters");
    });
  });

test.describe("step details below 768 px", () => {
  test.use({ viewport: { width: 600, height: 500 } });
  test("fill the screen with Input | Parameters | Output and keep the header in place", async ({
    page,
  }) => {
    const details = await openStepFixture(page);
    await details.open("lookup");
    const box = (await details.dialog.boundingBox())!;
    expect([box.x, box.y, box.width, box.height]).toEqual([0, 0, 600, 500]);
    const panes = details.dialog.getByRole("tablist", {
      name: "Step details panes",
    });
    await expect(
      panes.getByRole("tab", { name: "Parameters" }),
    ).toHaveAttribute("aria-selected", "true");
    await panes.getByRole("tab", { name: "Input" }).click();
    await expect(details.dialog.locator(".sd-input")).toBeVisible();
    await expect(details.dialog.locator(".sd-main")).toBeHidden();
    await panes.getByRole("tab", { name: "Parameters" }).click();
    await expect(details.dialog.locator(".sd-main")).toBeVisible();
    await details.dialog.locator("#sd-panel-parameters").evaluate((el) => {
      // The form body is supplied separately; exercise a body taller than this viewport.
      const content = document.createElement("div");
      content.style.height = "1000px";
      content.textContent = "Step parameters";
      el.append(content);
    });
    await details.dialog
      .locator(".sd-panes")
      .evaluate((el) => (el.scrollTop = el.scrollHeight));
    expect(
      await details.dialog.locator(".sd-panes").evaluate((el) => el.scrollTop),
    ).toBeGreaterThan(0);
    await expect(panes).toBeInViewport();
    await expect(details.dialog.locator(".sd-head")).toBeInViewport();
    // The sticky header meets the sticky pane tabs: no scrolled text between them.
    const tabs = (await panes.boundingBox())!;
    const head = (await details.dialog.locator(".sd-head").boundingBox())!;
    expectAligned(
      [head.y, tabs.y + tabs.height],
      "sticky header under the pane tabs",
    );
  });
});

test.describe("step details between 768 and 1099 px", () => {
  test.use({ viewport: { width: 900, height: 700 } });
  test("show Parameters beside one data pane with Input and Output tabs", async ({
    page,
  }) => {
    const details = await openStepFixture(page);
    await details.open("lookup");
    const data = details.dialog.getByRole("tablist", { name: "Data" });
    await expect(data.getByRole("tab", { name: "Input" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(details.dialog.locator(".sd-output")).toBeHidden();
    await data.getByRole("tab", { name: "Output" }).click();
    await expect(details.dialog.locator(".sd-output")).toBeVisible();
    await expect(details.dialog.locator(".sd-input")).toBeHidden();
  });
});

test.describe("pane keyboard routes", () => {
  test.use({ viewport: { width: 600, height: 500 } });
  test("sheet tabs use arrows and Home/End without exposing hidden controls", async ({
    page,
  }) => {
    const details = await openStepFixture(page);
    await details.open("lookup");
    const tabs = details.dialog.getByRole("tablist", {
      name: "Step details panes",
    });
    await tabs.getByRole("tab", { name: "Parameters" }).focus();
    await page.keyboard.press("ArrowRight");
    await expect(tabs.getByRole("tab", { name: "Output" })).toBeFocused();
    await expect(details.dialog.locator(".sd-output")).toBeVisible();
    await page.keyboard.press("Home");
    await expect(tabs.getByRole("tab", { name: "Input" })).toBeFocused();
    await page.keyboard.press("End");
    await expect(tabs.getByRole("tab", { name: "Output" })).toBeFocused();
    await page.keyboard.press("F6");
    await expect(
      details.dialog.getByRole("button", { name: "Rename lookup" }),
    ).toBeFocused();
  });
  test("YAML owns shortcuts, focuses its text and restores its opener", async ({
    page,
  }) => {
    const details = await openStepFixture(page);
    await details.open("wait-for-payment");
    const more = details.dialog.getByRole("button", {
      name: "More actions for wait-for-payment",
    });
    await more.click();
    await page.getByRole("menuitem", { name: "Edit as YAML" }).click();
    const editor = page.getByRole("dialog", {
      name: "Edit wait-for-payment as YAML",
    });
    const text = editor.getByRole("textbox", { name: "Step YAML" });
    await expect(text).toBeFocused();
    await page.keyboard.press("F6");
    await expect(text).toBeFocused();
    await page.keyboard.press("ControlOrMeta+Enter");
    await expect(page.locator(".toast-region")).not.toContainText(
      "Executing a single step",
    );
    await text.fill("id: [");
    await editor.getByRole("button", { name: "Apply", exact: true }).click();
    await expect(editor).toContainText("This isn't valid YAML:");
    await text.fill("id: wait-for-payment\nkind: wait\ndurationSeconds: 10");
    await editor.getByRole("button", { name: "Apply", exact: true }).click();
    await expect(editor).toContainText("Keep the step's ID and kind.");
    await text.focus();
    await page.keyboard.press("Escape");
    await expect(editor).toHaveCount(0);
    await expect(more).toBeFocused();
    await expect(details.dialog).toBeVisible();
  });
});

test.describe("divider ownership", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  test("Output arrows and limits keep Input fixed", async ({ page }) => {
    const details = await openStepFixture(page);
    await details.open("lookup");
    const input = details.dialog.locator(".sd-input");
    const output = details.dialog.locator(".sd-output");
    const separator = details.dialog.getByRole("separator", {
      name: "Resize Output",
    });
    const beforeInput = (await input.boundingBox())!.width;
    const before = (await output.boundingBox())!.width;
    await separator.focus();
    await page.keyboard.press("ArrowLeft");
    await expect
      .poll(async () => (await output.boundingBox())!.width)
      .toBe(before + 16);
    await page.keyboard.press("End");
    await expect
      .poll(
        async () =>
          (await details.dialog.locator(".sd-main").boundingBox())!.width,
      )
      .toBe(368);
    expect((await input.boundingBox())!.width).toBe(beforeInput);
    expect(Number(await separator.getAttribute("aria-valuenow"))).toBe(
      Number(await separator.getAttribute("aria-valuemax")),
    );
    await page.keyboard.press("Home");
    await expect
      .poll(async () => (await output.boundingBox())!.width)
      .toBe(240);
    await page.keyboard.press("ArrowRight");
    expect((await output.boundingBox())!.width).toBe(240);
    await page.keyboard.press("ControlOrMeta+Alt+Shift+ArrowRight");
    await expect(details.dialogFor("route-by-value")).toBeVisible();
  });
  test("only the captured pointer can move or end a divider drag", async ({
    page,
  }) => {
    const details = await openStepFixture(page);
    await details.open("lookup");
    const separator = details.dialog.getByRole("separator", {
      name: "Resize Input",
    });
    const input = details.dialog.locator(".sd-input");
    const before = (await input.boundingBox())!.width;
    const handle = (await separator.boundingBox())!;
    await page.mouse.move(handle.x + handle.width / 2, handle.y + 200);
    await page.mouse.down();
    await separator.dispatchEvent("pointermove", {
      pointerId: 77,
      clientX: handle.x + 600,
    });
    await separator.dispatchEvent("pointerup", { pointerId: 77 });
    expect((await input.boundingBox())!.width).toBe(before);
    await page.mouse.move(handle.x + handle.width / 2 + 32, handle.y + 200);
    await expect
      .poll(async () => (await input.boundingBox())!.width)
      .toBe(before + 32);
    await separator.dispatchEvent("pointercancel", { pointerId: 1 });
    await page.mouse.move(handle.x + handle.width / 2 + 80, handle.y + 200);
    await page.mouse.up();
    await expect
      .poll(async () => (await input.boundingBox())!.width)
      .toBe(before + 32);
    expect(await separator.evaluate((el) => el.hasPointerCapture(1))).toBe(
      false,
    );
  });
  test("saved malformed widths cannot collapse panes, and a viewport change preserves proportions", async ({
    page,
  }) => {
    await page.addInitScript(() =>
      localStorage.setItem(
        "ui:weave.ndv.layout.v1",
        '{"input":1e400,"output":0.2}',
      ),
    );
    const details = await openStepFixture(page);
    await details.open("lookup");
    const input = details.dialog.locator(".sd-input");
    expect((await input.boundingBox())!.width).toBe(390);
    const separator = details.dialog.getByRole("separator", {
      name: "Resize Input",
    });
    await separator.focus();
    await page.keyboard.press("Home");
    await page.setViewportSize({ width: 1920, height: 1080 });
    await expect
      .poll(async () => (await input.boundingBox())!.width)
      .toBe(Math.round(1872 * 0.172));
    await page.setViewportSize({ width: 600, height: 500 });
    await expect(details.dialog).toHaveAttribute("data-layout", "sheet");
    await page.setViewportSize({ width: 1440, height: 900 });
    await expect(details.dialog).toHaveAttribute("data-layout", "three");
    await expect(input).toBeVisible();
    await expect.poll(async () => (await input.boundingBox())!.width).toBe(240);
  });
});

test.describe("focus across pane layouts", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  const activeRegion = async (page: import("@playwright/test").Page) =>
    page.evaluate(() => {
      const active = document.activeElement as HTMLElement;
      return {
        inside: !!active.closest(".step-details"),
        visible: active.getClientRects().length > 0,
        region:
          active.closest("[data-region]")?.getAttribute("data-region") ?? null,
      };
    });
  test("data region focus survives narrower layouts and immediately owns F6", async ({
    page,
  }) => {
    const details = await openStepFixture(page);
    await details.open("lookup");
    const name = details.dialog.getByRole("button", { name: "Rename lookup" });
    await name.focus();
    await page.keyboard.press("Shift+F6");
    await expect(
      details.dialog
        .locator(".sd-output")
        .getByRole("radio", { name: "Schema", exact: true }),
    ).toBeFocused();
    await page.setViewportSize({ width: 900, height: 700 });
    await expect(details.dialog).toHaveAttribute("data-layout", "two");
    await expect
      .poll(() => activeRegion(page))
      .toEqual({ inside: true, visible: true, region: "output" });
    await expect(
      details.dialog
        .getByRole("tablist", { name: "Data" })
        .getByRole("tab", { name: "Output" }),
    ).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("F6");
    await expect(name).toBeFocused();
    await page.keyboard.press("F6");
    await expect(
      details.dialog
        .locator(".sd-input")
        .getByRole("radio", { name: "Schema", exact: true }),
    ).toBeFocused();
    await page.setViewportSize({ width: 600, height: 500 });
    await expect(details.dialog).toHaveAttribute("data-layout", "sheet");
    await expect
      .poll(() => activeRegion(page))
      .toEqual({ inside: true, visible: true, region: "input" });
    await expect(
      details.dialog
        .getByRole("tablist", { name: "Step details panes" })
        .getByRole("tab", { name: "Input", exact: true }),
    ).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("F6");
    const resourceMode = details
      .field("action")
      .getByRole("radio", { name: "By name", exact: true });
    await expect(resourceMode).toBeChecked();
    await expect(resourceMode).toBeFocused();
  });
  test("removed dividers and a previously hidden header recover visible focus", async ({
    page,
  }) => {
    const details = await openStepFixture(page);
    await details.open("lookup");
    await details.dialog
      .getByRole("separator", { name: "Resize Output" })
      .focus();
    await page.setViewportSize({ width: 600, height: 500 });
    await expect(details.dialog).toHaveAttribute("data-layout", "sheet");
    await expect
      .poll(() => activeRegion(page))
      .toEqual({ inside: true, visible: true, region: "parameters" });
    await page.keyboard.press("F6");
    await expect(
      details.dialog
        .locator(".sd-output")
        .getByRole("radio", { name: "Schema", exact: true }),
    ).toBeFocused();
    await details.dialog
      .getByRole("tablist", { name: "Step details panes" })
      .getByRole("tab", { name: "Input", exact: true })
      .click();
    await page.setViewportSize({ width: 1440, height: 900 });
    await expect(details.dialog).toHaveAttribute("data-layout", "three");
    const name = details.dialog.getByRole("button", { name: "Rename lookup" });
    await name.focus();
    await page.setViewportSize({ width: 600, height: 500 });
    await expect(details.dialog).toHaveAttribute("data-layout", "sheet");
    await expect
      .poll(() => activeRegion(page))
      .toEqual({ inside: true, visible: true, region: "header" });
    await page.getByRole("button", { name: "Validate", exact: true }).focus();
    await expect(name).toBeFocused();
    await page.keyboard.press("F6");
    await expect(
      details.dialog
        .locator(".sd-input")
        .getByRole("radio", { name: "Schema", exact: true }),
    ).toBeFocused();
  });
  test("a removed Data tab preserves its logical Output region", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 900, height: 700 });
    const details = await openStepFixture(page);
    await details.open("lookup");
    await details.dialog
      .getByRole("tablist", { name: "Data" })
      .getByRole("tab", { name: "Output" })
      .click();
    await page.setViewportSize({ width: 1440, height: 900 });
    await expect(details.dialog).toHaveAttribute("data-layout", "three");
    await expect
      .poll(() => activeRegion(page))
      .toEqual({ inside: true, visible: true, region: "output" });
    await page.keyboard.press("F6");
    await expect(
      details.dialog.getByRole("button", { name: "Rename lookup" }),
    ).toBeFocused();
  });
  for (const nested of ["YAML", "confirmation"])
    test(`${nested} retains focus while layouts change`, async ({ page }) => {
      const details = await openStepFixture(page);
      const id = nested === "YAML" ? "wait-for-payment" : "route-by-value";
      await details.open(id);
      const more = details.dialog.getByRole("button", {
        name: `More actions for ${id}`,
      });
      await more.click();
      await page
        .getByRole("menuitem", {
          name: nested === "YAML" ? "Edit as YAML" : "Delete",
          exact: true,
        })
        .click();
      const modal = page.getByRole("dialog", {
        name:
          nested === "YAML" ? `Edit ${id} as YAML` : /^Delete route-by-value/,
      });
      const focus =
        nested === "YAML"
          ? modal.getByRole("textbox", { name: "Step YAML" })
          : modal.getByRole("button", { name: "Cancel", exact: true });
      await expect(focus).toBeFocused();
      for (const size of [
        { width: 900, height: 700 },
        { width: 600, height: 500 },
      ]) {
        await page.setViewportSize(size);
        await expect(details.dialog).toHaveAttribute(
          "data-layout",
          size.width === 900 ? "two" : "sheet",
        );
        await expect(focus).toBeFocused();
        await page.keyboard.press("F6");
        await expect(focus).toBeFocused();
      }
      await modal.getByRole("button", { name: "Cancel", exact: true }).click();
      await expect(modal).toHaveCount(0);
      await expect(more).toBeFocused();
      await page.keyboard.press("F6");
      await expect(
        details.dialog
          .locator(".sd-input")
          .getByRole("radio", { name: "Schema", exact: true }),
      ).toBeFocused();
    });
});

const screenSizes = [
  { tag: "360x740", width: 360, height: 740 },
  { tag: "600x500", width: 600, height: 500 },
  { tag: "768x1024", width: 768, height: 1024 },
  { tag: "1280x720", width: 1280, height: 720 },
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "1920x1080", width: 1920, height: 1080 },
  { tag: "1280x720-zoom200", width: 640, height: 360, scale: 2 },
];
for (const size of screenSizes) {
  test.describe(`pane layout controls at ${size.tag}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      deviceScaleFactor: size.scale ?? 1,
      hasTouch: size.width <= 768,
    });
    test("controls fit, stay reachable and meet target sizes", async ({
      page,
    }) => {
      const details = await openStepFixture(
        page,
        yamlFile("many-paths.yaml", wide(7)),
      );
      await details.open(longId);
      const audit = async () => {
        const failures = [];
        for (const control of await details.dialog
          .locator("button:not([disabled]), a[href], input")
          .all()) {
          if (!(await control.isVisible())) continue;
          await control.evaluate((element) => {
            for (
              let parent = element.parentElement;
              parent;
              parent = parent.parentElement
            ) {
              if (
                parent.scrollHeight > parent.clientHeight &&
                /^(auto|scroll)$/.test(getComputedStyle(parent).overflowY)
              ) {
                const bounds = parent.getBoundingClientRect();
                const target = element.getBoundingClientRect();
                parent.scrollTop +=
                  target.y -
                  bounds.y -
                  (parent.clientHeight - target.height) / 2;
                break;
              }
            }
          });
          const failure = await control.evaluate((e, touch) => {
            const r = e.getBoundingClientRect();
            const hit = document.elementFromPoint(
              r.x + r.width / 2,
              r.y + r.height / 2,
            );
            const minimum = touch ? 44 : 24;
            // A tool hidden at rest takes no tap on a touch screen until
            // focus in its field shows it (step-mode-touch checks that).
            let painted = 1;
            for (let node: Element | null = e; node; node = node.parentElement)
              painted *= parseFloat(getComputedStyle(node).opacity);
            const reachable = painted === 0 || (!!hit && e.contains(hit));
            return r.width < minimum ||
              r.height < minimum ||
              r.x < 0 ||
              r.right > window.innerWidth ||
              r.y < 0 ||
              r.bottom > window.innerHeight ||
              !reachable
              ? {
                  name: e.getAttribute("aria-label") || e.textContent?.trim(),
                  width: r.width,
                  height: r.height,
                  x: r.x,
                  y: r.y,
                  reached: !!hit && e.contains(hit),
                }
              : null;
          }, size.width <= 768);
          if (failure) failures.push(failure);
        }
        return {
          width: await page.evaluate(
            () => document.documentElement.scrollWidth,
          ),
          viewport: size.width,
          failures,
        };
      };
      const first = await audit();
      expect(first.width).toBeLessThanOrEqual(first.viewport);
      expect(first.failures).toEqual([]);
      await page.screenshot({
        path: resolve(`../build/editor-m3/task-11/layout-${size.tag}.png`),
      });
      if (size.width < 768) {
        const tabs = details.dialog.getByRole("tablist", {
          name: "Step details panes",
        });
        for (const choice of ["Input", "Output", "Parameters"]) {
          const tab = tabs.getByRole("tab", { name: choice, exact: true });
          await tab.tap();
          await expect(tab).toHaveAttribute("aria-selected", "true");
          expect((await audit()).failures).toEqual([]);
        }
      }
      await details.close();
      await expect(details.node(longId)).toBeFocused();
    });
  });
}

for (const size of [
  { width: 1440, height: 900, inset: 24 },
  { width: 1100, height: 800, inset: 24 },
  { width: 900, height: 700, inset: 16 },
])
  test.describe(`step details below the shell header at ${size.width}x${size.height}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test("the top strip sits on a clear band below the shell header", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("lookup");
      const header = await box(page.locator("header.topbar"));
      const dialog = await box(details.dialog);
      // The dialog's opaque layer covers everything under the shell's top bar;
      // the insets are padding inside it.
      expectAligned([dialog.y, header.bottom], "dialog top");
      expectAligned([dialog.x, 0], "dialog left");
      expectAligned([dialog.right, size.width], "dialog right");
      expectAligned([dialog.bottom, size.height], "dialog bottom");
      const panes = await box(details.dialog.locator(".sd-panes"));
      expectAligned([panes.x, size.inset], "panes left");
      expectAligned([panes.right, size.width - size.inset], "panes right");
      expectAligned([panes.bottom, size.height - size.inset], "panes bottom");
      // Nothing of the shell paints in the inset strips around the panes.
      const strips = await page.evaluate(
        ({ top, inset, width, height }) => {
          const points: [number, number][] = [];
          for (let y = top + 2; y < height; y += 6)
            for (const x of [
              2,
              inset / 2,
              inset - 2,
              width - inset + 2,
              width - inset / 2,
              width - 2,
            ])
              points.push([x, y]);
          for (let x = 2; x < width; x += 8)
            for (const y of [
              top + 2,
              top + inset / 2,
              height - inset + 2,
              height - inset / 2,
              height - 2,
            ])
              points.push([x, y]);
          const dialog = document.querySelector(".step-details")!;
          const panes = dialog
            .querySelector(".sd-panes")!
            .getBoundingClientRect();
          return points
            .filter(
              ([x, y]) =>
                !(
                  x >= panes.left &&
                  x <= panes.right &&
                  y >= panes.top &&
                  y <= panes.bottom
                ),
            )
            .map(([x, y]) => {
              const hit = document.elementFromPoint(x, y);
              return hit && dialog.contains(hit)
                ? null
                : `${x},${y}: ${hit?.tagName.toLowerCase()}.${hit?.className}`;
            })
            .filter(Boolean)
            .slice(0, 10);
        },
        {
          top: header.bottom,
          inset: size.inset,
          width: size.width,
          height: size.height,
        },
      );
      expect(strips).toEqual([]);
      // The scrim still covers the whole window.
      expect(await box(page.locator(".sd-scrim"))).toMatchObject({
        x: 0,
        y: 0,
        width: size.width,
        height: size.height,
      });
      const strip = await box(details.dialog.locator(".sd-top"));
      const shellText = await page.evaluate(() => {
        const found: Box[] = [];
        const add = (r: DOMRect) =>
          r.width > 0 &&
          r.height > 0 &&
          found.push({
            x: r.x,
            y: r.y,
            width: r.width,
            height: r.height,
            right: r.right,
            bottom: r.bottom,
            middle: r.y + r.height / 2,
          });
        for (const area of document.querySelectorAll(
          "header.topbar, .sidebar .brand",
        )) {
          const walker = document.createTreeWalker(area, NodeFilter.SHOW_TEXT);
          for (let node = walker.nextNode(); node; node = walker.nextNode()) {
            if (!node.textContent?.trim()) continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            for (const r of range.getClientRects()) add(r);
          }
          for (const element of area.querySelectorAll("img, svg"))
            if (element.getClientRects().length)
              add(element.getBoundingClientRect());
        }
        return found;
      });
      expect(shellText.length).toBeGreaterThan(0);
      expectAligned([strip.y, header.bottom + size.inset], "strip top");
      // The dialog is opaque: nothing of the editor shows above, under or beside the strip.
      const opaque = await details.dialog.evaluate(
        (element, points) => {
          const probe = document.createElement("span");
          probe.style.backgroundColor = "var(--bg)";
          element.append(probe);
          const bg = getComputedStyle(probe).backgroundColor;
          probe.remove();
          return {
            background: getComputedStyle(element).backgroundColor === bg,
            hits: points.map((point) =>
              element.contains(document.elementFromPoint(point.x, point.y)),
            ),
          };
        },
        [
          { x: strip.x + strip.width / 2, y: strip.y - 4 },
          { x: strip.x + strip.width / 2, y: strip.bottom + 4 },
          { x: strip.x + 4, y: strip.bottom + 4 },
        ],
      );
      expect(opaque).toEqual({ background: true, hits: [true, true, true] });
      for (const text of shellText)
        expect(
          intersects(strip, text),
          `strip ${JSON.stringify(strip)} over ${JSON.stringify(text)}`,
        ).toBe(false);
      // The strip's controls still work, and Escape still closes: a text box
      // that has focus gives it up on the first Escape, the next one closes.
      await details.dialog
        .getByRole("button", { name: "Next step", exact: true })
        .click();
      await expect(details.dialogFor("route-by-value")).toBeVisible();
      if (
        await page.evaluate(
          () => !!document.activeElement?.matches("input, textarea"),
        )
      ) {
        await page.keyboard.press("Escape");
        await expect(details.dialog).toBeVisible();
        expect(
          await page.evaluate(
            () => !document.activeElement?.matches("input, textarea"),
          ),
        ).toBe(true);
      }
      await page.keyboard.press("Escape");
      await expect(details.dialog).toHaveCount(0);
    });
  });

for (const size of [
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "600x500", width: 600, height: 500 },
  { tag: "360x640", width: 360, height: 640 },
  { tag: "640x360", width: 640, height: 360 },
])
  test.describe(`step details header alignment at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test("the header, the tabs and the form share one left edge and one center line", async ({
      page,
    }) => {
      const details = await openStepFixture(page);
      await details.open("lookup");
      const dialog = details.dialog;
      await showParameters(page, dialog);
      const tile = await box(dialog.locator(".sd-kind"));
      const name = await textBox(dialog.locator(".sd-name-text"));
      const nameButton = await box(dialog.locator("button.sd-name"));
      const subtitle = await textBox(dialog.locator(".sd-subtitle"));
      const block = await box(dialog.locator(".sd-name-block"));
      const execute = await box(
        dialog.getByRole("button", { name: "Execute step", exact: true }),
      );
      const toggle = await box(
        dialog.getByRole("button", { name: "More ways to execute" }),
      );
      const more = await box(
        dialog.getByRole("button", { name: "More actions for lookup" }),
      );
      // The name starts beside the kind tile, its subtitle under it.
      expectAligned([nameButton.x, tile.right + 12], "name beside the tile", 2);
      expectAligned([name.x, subtitle.x], "name and subtitle start");
      expect(subtitle.y).toBeGreaterThanOrEqual(name.bottom - 1);
      // Tile and name block share a center line; so do Execute step, its menu and More.
      expectAligned([tile.middle, block.middle], "tile and name centers");
      expectAligned(
        [execute.middle, toggle.middle, more.middle],
        "action centers",
      );
      expectAligned([execute.height, toggle.height], "split button heights");
      if (execute.y < tile.bottom)
        expectAligned([tile.middle, execute.middle], "header centers");
      // Tabs and Docs sit on one baseline.
      const tabs = details.dialog.getByRole("tablist", {
        name: "Step details sections",
      });
      const parameters = await textBox(
        tabs.getByRole("tab", { name: "Parameters" }),
      );
      const settings = await textBox(
        tabs.getByRole("tab", { name: "Settings" }),
      );
      const docs = await textBox(tabs.getByRole("link", { name: "Docs" }));
      expectAligned(
        [parameters.bottom, settings.bottom, docs.bottom],
        "tab and Docs baselines",
      );
      // One left edge: tile, first tab, field labels and controls.
      const edge = await contentEdges(dialog.locator("#sd-panel-parameters"));
      const label = await textBox(
        dialog.locator(
          "#sd-panel-parameters .param-form > weave-param-field > .param > .param-label-row .param-label",
        ),
      );
      expectAligned(
        [edge.left, tile.x, parameters.x, label.x],
        "Parameters pane left edge",
      );
      // One right edge: More, Docs and the field menus.
      const docsBox = await box(tabs.getByRole("link", { name: "Docs" }));
      expectAligned(
        [edge.right, more.right, docsBox.right],
        "Parameters pane right edge",
      );
    });
  });

test.describe("the shell while step details is open", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  test("is inert for pointer, keyboard and assistive technology, and comes back on close", async ({
    page,
  }) => {
    const details = await openStepFixture(page);
    const exposed = async () => {
      const cdp = await page.context().newCDPSession(page);
      await cdp.send("Accessibility.enable");
      const { nodes } = (await cdp.send("Accessibility.getFullAXTree")) as {
        nodes: { ignored: boolean; name?: { value: string } }[];
      };
      await cdp.detach();
      return ["Ask Weave AI", "Home", "Workflows"].filter((name) =>
        nodes.some((node) => !node.ignored && node.name?.value === name),
      );
    };
    expect(await exposed()).toEqual(["Ask Weave AI", "Home", "Workflows"]);
    await details.open("lookup");
    await expect(page.locator("header.topbar")).toHaveJSProperty("inert", true);
    expect(await exposed()).toEqual([]);
    // Toasts stay usable: an explanation still reaches the person.
    await page.keyboard.press("ControlOrMeta+Enter");
    await expect(page.locator(".toast-region")).toContainText(
      "Executing a single step isn't available yet.",
    );
    // And it paints above step details, where the person is looking.
    const toast = page.locator(".toast-region .toast").last();
    await expect(toast).toBeVisible();
    const toastBox = await box(toast);
    expect(intersects(toastBox, await box(details.dialog))).toBe(true);
    expect(
      await toast.evaluate(
        (element, point) =>
          element.contains(document.elementFromPoint(point.x, point.y)),
        { x: toastBox.x + toastBox.width / 2, y: toastBox.middle },
      ),
    ).toBe(true);
    await details.close();
    await expect(details.node("lookup")).toBeFocused();
    expect(await exposed()).toEqual(["Ask Weave AI", "Home", "Workflows"]);
    expect(
      await page.evaluate(() => document.querySelectorAll("[inert]").length),
    ).toBe(0);
  });
});

for (const size of [
  { tag: "1440x900", width: 1440, height: 900 },
  { tag: "1100x800", width: 1100, height: 800 },
  { tag: "360x640", width: 360, height: 640 },
])
  test.describe(`path card headers at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });
    test("the condition chip outlasts the next step's name", async ({
      page,
    }) => {
      const details = await openStepFixture(
        page,
        yamlFile(
          "long-next.yaml",
          wide(2).replace(
            /id: path-(\d)/g,
            "id: path-$1-and-a-long-next-step-name",
          ),
        ),
      );
      await details.open(longId);
      const cards = details.dialog.locator(".param-row.is-card");
      await expect(cards).toHaveCount(2);
      for (const card of await cards.all()) {
        const cut = (selector: string) =>
          card
            .locator(selector)
            .evaluate(
              (element) => element.scrollWidth > element.clientWidth + 1,
            );
        if (await cut(".param-chip-label"))
          expect(await cut(".param-next")).toBe(true);
        expect(await cut(".param-chip-label")).toBe(false);
        // A narrow card puts the chip and the next step under the title; a
        // next step that doesn't fit beside the chip wraps under the chip.
        const title = await box(card.locator(".param-card-head strong"));
        const chip = await box(card.locator(".param-chip-out"));
        const next = await box(card.locator(".param-next"));
        if (chip.y >= title.bottom - 1) {
          expectAligned([chip.x, title.x], "chip under the title");
          if (next.y >= chip.bottom - 1)
            expectAligned([next.x, chip.x], "next step under the chip");
          else expect(next.x).toBeGreaterThan(chip.right);
        }
      }
    });
  });
