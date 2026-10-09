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
      const tokens = await execute.evaluate((el) => {
        const sample = document.createElement("span");
        sample.style.backgroundColor = "var(--accent)";
        sample.style.color = "var(--on-accent)";
        el.append(sample);
        const style = getComputedStyle(sample);
        const result = {
          background: style.backgroundColor,
          color: style.color,
        };
        sample.remove();
        return result;
      });
      await expect(execute).toHaveCSS("background-color", tokens.background);
      await expect(execute).toHaveCSS("color", tokens.color);
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
      const audit = async () =>
        details.dialog.evaluate((dialog, touch) => {
          const controls = [
            ...dialog.querySelectorAll<HTMLElement>(
              "button:not([disabled]), a[href], input",
            ),
          ].filter(
            (e) =>
              e.getClientRects().length > 0 &&
              getComputedStyle(e).visibility !== "hidden",
          );
          return {
            width: document.documentElement.scrollWidth,
            viewport: window.innerWidth,
            failures: controls.flatMap((e) => {
              const r = e.getBoundingClientRect();
              const hit = document.elementFromPoint(
                r.x + r.width / 2,
                r.y + r.height / 2,
              );
              const minimum = touch ? 44 : 24;
              return r.width < minimum ||
                r.height < minimum ||
                r.x < 0 ||
                r.right > window.innerWidth ||
                r.y < 0 ||
                r.bottom > window.innerHeight ||
                !hit ||
                !e.contains(hit)
                ? [
                    {
                      name:
                        e.getAttribute("aria-label") || e.textContent?.trim(),
                      width: r.width,
                      height: r.height,
                      x: r.x,
                      y: r.y,
                      reached: !!hit && e.contains(hit),
                    },
                  ]
                : [];
            }),
          };
        }, size.width <= 768);
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
