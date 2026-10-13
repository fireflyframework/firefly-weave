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
// The Add a step panel: every "+", a dragged edge, Add first step, N and /
// open it; it starts with triggers on an empty workflow, searches by the
// words people use, and is driven entirely by the keyboard.
import { expect, test, type Page } from "@playwright/test";
import { AddStepPanel } from "./add-step-po";
import { CanvasPage, openNewWorkflow, openWorkflow } from "./canvas-po";
import { expectHitTarget, newWorkflow } from "./support";

const triggers = [
  "Manual form",
  "Webhook",
  "Schedule",
  "Kafka message",
  "Email",
  "App event",
  "Called by a workflow",
];
const categories = [
  "AI",
  "Action in an app",
  "Data transformation",
  "Flow",
  "Wait",
  "Human review",
  "Add another trigger",
];
const vendorSteps = ["prepare-request", "approval", "route", "record-result"];

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`Add a step at ${size.width}x${size.height}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize(size);
    });

    test("an empty workflow offers Add first step; N opens What triggers this workflow? with the search focused and the seven triggers first", async ({
      page,
    }) => {
      const canvas = await openNewWorkflow(page);
      await expect(canvas.insertTarget("Add first step")).toBeVisible();
      await canvas.root.focus();
      await page.keyboard.press("n");
      const panel = new AddStepPanel(page);
      await expect(panel.title).toHaveText("What triggers this workflow?");
      await expect(panel.search).toBeFocused();
      expect((await panel.labels()).slice(0, 7)).toEqual(triggers);
      expect(await panel.headings()).toEqual(["Triggers", "Not sure yet?"]);
      await expect(panel.option("Start from a template")).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(panel.root).toHaveCount(0);
      await canvas.insertTarget("Add first step").click();
      await expect(panel.title).toHaveText("What triggers this workflow?");
    });

    test("with a trigger, the panel opens on What happens next? with the seven categories, in order", async ({
      page,
    }) => {
      const canvas = await openWorkflow(page);
      await canvas.insertTarget("Add a step after record-result").click();
      const panel = new AddStepPanel(page);
      await expect(panel.title).toHaveText("What happens next?");
      expect(await panel.labels()).toEqual(categories);
    });

    test("search finds steps by everyday words, groups triggers first on an empty workflow, and offers a custom API call when nothing matches", async ({
      page,
    }) => {
      const empty = await openNewWorkflow(page);
      await empty.root.focus();
      await page.keyboard.press("/");
      const panel = new AddStepPanel(page);
      await expect(panel.search).toBeFocused();
      await page.keyboard.type("mail");
      await expect(panel.search).toHaveValue("mail");
      await expect
        .poll(() => panel.headings())
        .toEqual(["Triggers", "Actions"]);
      const found = await panel.labels();
      expect(found[0]).toBe("Email");
      expect(found.indexOf("Send email")).toBeGreaterThan(0);
      expect(found.indexOf("Reply to an email")).toBeGreaterThan(
        found.indexOf("Send email"),
      );
      await page.keyboard.press("Escape");
      const canvas = await openWorkflow(page);
      await canvas.tileBody("prepare-request").focus();
      await page.keyboard.press("/");
      await page.keyboard.type("http");
      await expect.poll(() => panel.activeLabel()).toBe("Call an API");
      await panel.search.fill("zzzz");
      await expect(panel.list).toContainText("Nothing matches “zzzz”.");
      await expect(panel.option("Make a custom API call")).toBeVisible();
      await page.keyboard.press("Enter");
      await expect(
        page.getByRole("dialog", { name: "Step details: call-api" }),
      ).toBeVisible();
    });

    test("the keyboard drives the panel: ↑ ↓ move, Enter inserts, → opens, ← goes back, Escape closes; the list is a listbox with aria-activedescendant", async ({
      page,
    }) => {
      const canvas = await openWorkflow(page);
      await canvas.tileBody("prepare-request").focus();
      await page.keyboard.press("n");
      const panel = new AddStepPanel(page);
      await expect(panel.title).toHaveText("What happens next?");
      await expect(panel.list).toHaveAttribute("role", "listbox");
      await expect.poll(() => panel.activeLabel()).toBe("AI");
      await page.keyboard.type("approval");
      await page.keyboard.press("Enter");
      await expect(panel.root).toHaveCount(0);
      await expect(
        page.getByRole("dialog", { name: "Step details: approval-1" }),
      ).toBeVisible();
      await canvas.closeInspector();
      expect(await canvas.rootSteps()).toEqual([
        "prepare-request",
        "approval-1",
        ...vendorSteps.slice(1),
      ]);
      await canvas.tileBody("prepare-request").focus();
      await page.keyboard.press("n");
      await page.keyboard.press("ArrowDown");
      await expect.poll(() => panel.activeLabel()).toBe("Action in an app");
      await page.keyboard.press("ArrowRight");
      await expect(panel.title).toHaveText("Action in an app");
      await expect.poll(() => panel.activeLabel()).toBe("Call an API");
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("ArrowRight");
      await expect(panel.title).toHaveText("HTTP APIs");
      await page.keyboard.press("ArrowLeft");
      await expect(panel.title).toHaveText("Action in an app");
      await expect.poll(() => panel.activeLabel()).toBe("HTTP APIs");
      await page.keyboard.press("ArrowLeft");
      await expect(panel.title).toHaveText("What happens next?");
      await expect.poll(() => panel.activeLabel()).toBe("Action in an app");
      await page.keyboard.press("Escape");
      await expect(panel.root).toHaveCount(0);
      await expect(canvas.tileBody("prepare-request")).toBeFocused();
    });

    test("typing in the search never acts on the canvas", async ({ page }) => {
      const canvas = await openWorkflow(page);
      const panel = new AddStepPanel(page);
      // An edit to undo: Ctrl/Cmd+Z in the search must never reach it.
      await canvas.tileBody("record-result").focus();
      await page.keyboard.press("n");
      await panel.choose("Wait for time");
      await canvas.closeInspector();
      const steps = [...vendorSteps, "wait-1"];
      await canvas.tileBody("approval").focus();
      await page.keyboard.press("/");
      await expect(panel.search).toBeFocused();
      await page.keyboard.type("n/");
      await page.keyboard.press("Backspace");
      await page.keyboard.press("Delete");
      await page.keyboard.press("Shift+S");
      await page.keyboard.press("ControlOrMeta+a");
      await expect(panel.search).toHaveValue("nS");
      await page.keyboard.press("ControlOrMeta+c");
      await page.keyboard.press("ControlOrMeta+x");
      await page.keyboard.press("ControlOrMeta+v");
      await page.keyboard.press("ControlOrMeta+z");
      await page.keyboard.press("ControlOrMeta+Shift+z");
      await page.keyboard.press("ControlOrMeta+z");
      await expect(panel.search).toBeFocused();
      await expect(panel.root).toHaveCount(1);
      await expect(canvas.tile("approval")).toHaveCount(1);
      await expect(canvas.tile("wait-1")).toHaveCount(1);
      await page.keyboard.press("Escape");
      await expect(panel.root).toHaveCount(0);
      expect(await canvas.rootSteps()).toEqual(steps);
    });

    test("the panel closes when its place disappears", async ({ page }) => {
      const canvas = await openNewWorkflow(page);
      await canvas.addFirstStep("Decision");
      await canvas.closeInspector();
      await canvas
        .insertTarget("Add a step to path Otherwise of decision-1")
        .click();
      const panel = new AddStepPanel(page);
      await expect(panel.title).toHaveText("What happens next?");
      await page.keyboard.press("Shift+Tab");
      await expect(
        panel.root.getByRole("button", { name: "Close", exact: true }),
      ).toBeFocused();
      await page.keyboard.press("ControlOrMeta+z");
      await expect(panel.root).toHaveCount(0);
      await expect(
        page.locator(".toast").filter({
          hasText: "That place in the workflow changed. Choose + again.",
        }),
      ).toBeVisible();
    });

    test("the toolbar's Add a step opens the panel after the selected step", async ({
      page,
    }) => {
      const canvas = await openWorkflow(page);
      await canvas.tileBody("approval").click();
      await canvas.closeInspector();
      await page
        .getByRole("toolbar", { name: "Workflow commands" })
        .getByRole("button", { name: "Add a step", exact: true })
        .click();
      await canvas.pick("Wait for time");
      await canvas.closeInspector();
      expect(await canvas.rootSteps()).toEqual([
        "prepare-request",
        "approval",
        "wait-1",
        "route",
        "record-result",
      ]);
    });

    test("a dragged edge opens the panel and stays drawn until the panel closes", async ({
      page,
    }) => {
      const canvas = await openNewWorkflow(page);
      await canvas.addFirstStep("Transform");
      await canvas.closeInspector();
      await canvas.dragFromHandle("transform-1>out");
      const panel = new AddStepPanel(page);
      await expect(panel.title).toHaveText("What happens next?");
      await expect(panel.search).toBeFocused();
      await expect(canvas.root.locator(".rubber-band")).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(panel.root).toHaveCount(0);
      await expect(canvas.root.locator(".rubber-band")).toHaveCount(0);
    });

    test("every control has a name and a target of its density, and focus is visible", async ({
      page,
    }) => {
      const canvas = await openWorkflow(page);
      await canvas.tileBody("prepare-request").focus();
      await page.keyboard.press("n");
      const panel = new AddStepPanel(page);
      await page.keyboard.press("ArrowDown");
      await page.keyboard.press("ArrowRight");
      const back = panel.root.getByRole("button", {
        name: "Back to What happens next?",
        exact: true,
      });
      const close = panel.root.getByRole("button", {
        name: "Close",
        exact: true,
      });
      const target = size.width < 768 ? 44 : 32;
      for (const control of [back, close]) {
        await expectHitTarget(control);
        const box = (await control.boundingBox())!;
        expect(box.width).toBeGreaterThanOrEqual(target);
        expect(box.height).toBeGreaterThanOrEqual(target);
      }
      const row = (await panel.option("Call an API").boundingBox())!;
      expect(row.height).toBeGreaterThanOrEqual(44);
      await back.focus();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      await expect(back).toBeFocused();
      expect(
        await back.evaluate(
          (element) => getComputedStyle(element).outlineStyle,
        ),
      ).not.toBe("none");
    });
  });

test("the panel is 384 px wide on the right at 1440x900, and a full sheet below 768 px", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const canvas = await openNewWorkflow(page);
  await canvas.insertTarget("Add first step").click();
  const panel = new AddStepPanel(page);
  await panel.settled();
  const wide = (await panel.root.boundingBox())!;
  expect(wide.width).toBe(384);
  expect(wide.x + wide.width).toBe(1440);
  expect(wide.y + wide.height).toBe(900);
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 600, height: 500 });
  await canvas.ready();
  await canvas.insertTarget("Add first step").click();
  await expect(panel.root).toHaveAttribute("aria-modal", "true");
  await panel.settled();
  expect(await panel.root.boundingBox()).toEqual({
    x: 0,
    y: 0,
    width: 600,
    height: 500,
  });
});

test.describe("Add a step when the workflow changes under it", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
  });
  const changed = "That place in the workflow changed. Choose + again.";

  /** N on a step, then focus on the panel's Close button. */
  async function openAfter(page: Page, canvas: CanvasPage, id: string) {
    await canvas.tileBody(id).focus();
    await page.keyboard.press("n");
    const panel = new AddStepPanel(page);
    await expect(panel.title).toHaveText("What happens next?");
    await expect(
      page.getByRole("dialog", { name: "What happens next?" }),
    ).toHaveAccessibleDescription(`Add a step after ${id}`);
    await page.keyboard.press("Shift+Tab");
    await expect(
      panel.root.getByRole("button", { name: "Close", exact: true }),
    ).toBeFocused();
    return panel;
  }
  test("leaving the designer closes the panel quietly: a new workflow opens without it or its sentence", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await openAfter(page, canvas, "approval");
    // By keyboard: focus moves to Home and Enter opens it.
    const home = page.getByRole("button", { name: "Home", exact: true });
    await home.focus();
    await page.keyboard.press("Enter");
    await expect(page.locator(".editor-bar")).toHaveCount(0);
    await newWorkflow(page);
    await expect(new AddStepPanel(page).root).toHaveCount(0);
    await page.waitForTimeout(300);
    await expect(
      page.locator(".toast").filter({ hasText: changed }),
    ).toHaveCount(0);
  });

  test("Undo from the panel that moves its place closes it and inserts nothing", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("prepare-request").focus();
    await page.keyboard.press("n");
    await new AddStepPanel(page).choose("Wait for time");
    await canvas.closeInspector();
    // After approval, with wait-1 before it: Undo removes wait-1, and the
    // place's index would then point after route.
    const panel = await openAfter(page, canvas, "approval");
    await page.keyboard.press("ControlOrMeta+z");
    await expect(panel.root).toHaveCount(0);
    await expect(
      page.locator(".toast").filter({ hasText: changed }),
    ).toBeVisible();
    expect(await canvas.rootSteps()).toEqual(vendorSteps);
  });

  test("Redo from the panel that moves its place closes it and inserts nothing", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("prepare-request").focus();
    await page.keyboard.press("n");
    await new AddStepPanel(page).choose("Wait for time");
    await canvas.closeInspector();
    await canvas.tileBody("approval").focus();
    await page.keyboard.press("ControlOrMeta+z");
    await expect(canvas.tile("wait-1")).toHaveCount(0);
    const panel = await openAfter(page, canvas, "approval");
    await page.keyboard.press("ControlOrMeta+Shift+z");
    await expect(panel.root).toHaveCount(0);
    await expect(
      page.locator(".toast").filter({ hasText: changed }),
    ).toBeVisible();
    expect(await canvas.rootSteps()).toEqual([
      "prepare-request",
      "wait-1",
      ...vendorSteps.slice(1),
    ]);
  });

  test("a Source edit that moves its place closes it when the canvas comes back", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    const panel = await openAfter(page, canvas, "approval");
    // To Source by keyboard: a press outside would close the panel first.
    const sourceTab = page.getByRole("tab", { name: "Source", exact: true });
    await sourceTab.focus();
    await page.keyboard.press("Enter");
    const source = page.getByRole("textbox", { name: "Workflow source" });
    await source.fill(
      (await source.inputValue()).replace(
        "- id: route\n",
        "- id: choose-path\n",
      ),
    );
    await page
      .getByRole("button", { name: "Apply changes", exact: true })
      .click();
    await page.getByRole("tab", { name: "Designer", exact: true }).click();
    await canvas.ready();
    await expect(panel.root).toHaveCount(0);
    await expect(
      page.locator(".toast").filter({ hasText: changed }),
    ).toBeVisible();
    expect(await canvas.rootSteps()).toEqual([
      "prepare-request",
      "approval",
      "choose-path",
      "record-result",
    ]);
  });
});

test.describe("Add a step lets go of the keys held while it opens", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  test("pressed twice from the toolbar, it closes and holds no key afterwards", async ({
    page,
  }) => {
    const canvas = await openWorkflow(page);
    const button = page
      .getByRole("toolbar", { name: "Workflow commands" })
      .getByRole("button", { name: "Add a step", exact: true });
    await button.click();
    await button.click();
    await expect(page.locator(".add-step-panel")).toHaveCount(0);
    await canvas.tileBody("approval").focus();
    await page.keyboard.press("ArrowRight");
    await expect(canvas.tileBody("route")).toBeFocused();
  });
});

for (const size of [
  { tag: "1440x900", width: 1440, height: 900, touch: false },
  { tag: "1024x768 touch", width: 1024, height: 768, touch: true },
  { tag: "360x740", width: 360, height: 740, touch: false },
  { tag: "360x740 touch", width: 360, height: 740, touch: true },
])
  test.describe(`Add a step's rows at ${size.tag}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: size.touch,
    });

    test("descriptions wrap to two lines at most, never cut, and key hints show only with a keyboard and a fine pointer", async ({
      page,
    }) => {
      const canvas = await openWorkflow(page);
      await canvas.tileBody("approval").focus();
      await page.keyboard.press("n");
      const panel = new AddStepPanel(page);
      await expect(panel.search).toBeFocused();
      await panel.option("Action in an app").click();
      await expect(panel.title).toHaveText("Action in an app");
      const descriptions = await panel.list
        .locator(".entry-description")
        .evaluateAll((elements) =>
          elements.map((element) => {
            const style = getComputedStyle(element);
            return {
              text: element.textContent,
              cut:
                element.scrollWidth > element.clientWidth + 1 ||
                element.scrollHeight > element.clientHeight + 1,
              lines: Math.round(
                element.clientHeight / parseFloat(style.lineHeight),
              ),
            };
          }),
        );
      expect(descriptions.length).toBeGreaterThan(2);
      for (const item of descriptions) {
        expect(item.cut, item.text ?? "").toBe(false);
        expect(item.lines, item.text ?? "").toBeLessThanOrEqual(2);
      }
      const coarse = await page.evaluate(
        () => matchMedia("(pointer: coarse)").matches,
      );
      expect(coarse).toBe(size.touch);
      for (const hint of [".add-step-kbd", ".entry-enter"])
        if (size.touch) await expect(panel.root.locator(hint)).toBeHidden();
        else await expect(panel.root.locator(hint).first()).toBeVisible();
      // The footer's key legend: a keyboard and a fine pointer, from 768 px.
      const legend = panel.root.locator(".add-step-foot");
      if (size.touch || size.width < 768) await expect(legend).toBeHidden();
      else await expect(legend).toBeVisible();
    });
  });
