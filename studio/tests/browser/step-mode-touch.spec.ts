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
// On a touch screen nothing hovers: a Fixed | Mapped toggle hidden at rest
// takes no tap, so a tap meant for a Fixed value (or beside it) never
// switches it to Mapped. Focus in the field shows the toggle, and from then
// on it takes taps; the keyboard reaches it as before.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import {
  openStepFixture,
  stepFixture,
  yamlFile,
  type StepDetailsPage,
} from "./step-details-po";
import { box, showParameters } from "./step-geometry";

/** The step fixture with check-customer's first row holding a Fixed value. */
const fixture = () =>
  yamlFile(
    "fixed-values.yaml",
    readFileSync(stepFixture, "utf8").replace(
      "value: {object: {customer: {ref: /input/customerId}, amount: {ref: /input/amount}}}",
      "value: {object: {customer: {literal: c-1}, amount: {ref: /input/amount}}}",
    ),
  );

/** The toggle's state and what a person sees of it. */
async function toggleState(toggle: Locator) {
  return toggle.evaluate((group) => {
    const style = getComputedStyle(group);
    return {
      checked: [...group.querySelectorAll("[role=radio]")]
        .filter((radio) => radio.getAttribute("aria-checked") === "true")
        .map((radio) => radio.textContent?.trim()),
      opacity: style.opacity,
      pointer: style.pointerEvents,
    };
  });
}

/** Where a tap at the right end of the value lands: on the hidden Mapped option. */
async function rightEnd(toggle: Locator, value: Locator) {
  const mapped = toggle.getByRole("radio", { name: "Mapped" });
  await mapped.evaluate((element) =>
    element.scrollIntoView({ block: "center" }),
  );
  const hidden = (await toggleState(toggle)).opacity === "0";
  // A toggle shown at rest (a narrow row puts it under the value) isn't
  // hidden: the tap goes to the right end of the value itself.
  const at = await box(hidden ? mapped : value);
  return {
    x: hidden ? at.x + at.width / 2 : at.right - 12,
    y: at.middle,
  };
}

const cases: {
  name: string;
  step: string;
  /** The key that moves from the value to its toggle. */
  toToggle: "Tab" | "Shift+Tab";
  toggle: (details: StepDetailsPage) => Locator;
  value: (details: StepDetailsPage) => Locator;
}[] = [
  {
    name: "a Fixed value",
    step: "notify-sales",
    toToggle: "Shift+Tab",
    toggle: (details) =>
      details.field("title").getByRole("radiogroup", {
        name: "Value mode for Title",
      }),
    value: (details) =>
      details.field("title").locator(".param-control .param-input").first(),
  },
  {
    name: "a condition value",
    step: "route-by-value",
    toToggle: "Tab",
    toggle: (details) =>
      details.dialog
        .getByRole("radiogroup", { name: "Value mode for Condition 1 value" })
        .first(),
    value: (details) =>
      details.dialog
        .getByRole("spinbutton", { name: "Condition 1 value" })
        .first(),
  },
  {
    name: "a key-value row",
    step: "check-customer",
    toToggle: "Tab",
    toggle: (details) =>
      details.field("value").locator(".param-row-value .param-mode").first(),
    value: (details) =>
      details.field("value").locator(".param-row-value .param-input").first(),
  },
];

for (const size of [
  { tag: "360x640", width: 360, height: 640 },
  { tag: "768x1024", width: 768, height: 1024 },
])
  test.describe(`taps on a touch screen at ${size.tag}`, () => {
    test.use({
      viewport: { width: size.width, height: size.height },
      hasTouch: true,
    });

    for (const item of cases)
      test(`a tap at the right end of ${item.name} keeps it Fixed`, async ({
        page,
      }) => {
        const details = await openStepFixture(page, fixture());
        await details.openWithKeyboard(item.step);
        await showParameters(page, details.dialog);
        const toggle = item.toggle(details);
        const value = item.value(details);
        await expect(toggle).toBeAttached();
        expect((await toggleState(toggle)).checked).toEqual(["Fixed"]);
        const { x, y } = await rightEnd(toggle, value);
        await page.touchscreen.tap(x, y);
        // A switch to Mapped would land a moment after the tap: let it.
        await page.waitForTimeout(500);
        // Nothing switched: the value is still Fixed, and nothing asks for data.
        expect((await toggleState(toggle)).checked).toEqual(["Fixed"]);
        await expect(page.getByRole("listbox")).toHaveCount(0);
      });

    for (const item of cases)
      test(`focus in ${item.name} shows its toggle, the keyboard reaches it, and then it takes a tap`, async ({
        page,
      }) => {
        const details = await openStepFixture(page, fixture());
        await details.openWithKeyboard(item.step);
        await showParameters(page, details.dialog);
        const toggle = item.toggle(details);
        await item.value(details).focus();
        await expect
          .poll(async () => {
            const { opacity, pointer } = await toggleState(toggle);
            return [opacity, pointer];
          })
          .toEqual(["1", "auto"]);
        // Past the field's menu, when it has one.
        const fixed = toggle.getByRole("radio", { name: "Fixed" });
        const focused = () =>
          fixed.evaluate((element) => element === document.activeElement);
        for (let i = 0; i < 3 && !(await focused()); i++)
          await page.keyboard.press(item.toToggle);
        await expect(fixed).toBeFocused();
        expect(await toggleState(toggle)).toMatchObject({
          opacity: "1",
          pointer: "auto",
        });
        const mapped = await box(toggle.getByRole("radio", { name: "Mapped" }));
        await page.touchscreen.tap(mapped.x + mapped.width / 2, mapped.middle);
        await expect
          .poll(async () => (await toggleState(toggle)).checked)
          .toEqual(["Mapped"]);
      });

    test("tools shown at rest take a tap: a narrow row's toggle and a path's menu", async ({
      page,
    }) => {
      const details = await openStepFixture(page, fixture());
      await details.openWithKeyboard("route-by-value");
      await showParameters(page, details.dialog);
      // A path card's field menu ends its header at rest.
      const menu = details.dialog
        .locator(".param-row.is-card .param-label-row .param-menu")
        .first()
        .getByRole("button");
      await menu.evaluate((element) =>
        element.scrollIntoView({ block: "center" }),
      );
      const at = await box(menu);
      await page.touchscreen.tap(at.x + at.width / 2, at.middle);
      await expect(page.getByRole("menu")).toBeVisible();
      await page.keyboard.press("Escape");
      await details.close();
      // At 360 px a key-value row is too narrow for its tools beside the
      // value: they show under it, at rest.
      if (size.width > 400) return;
      await details.openWithKeyboard("check-customer");
      await showParameters(page, details.dialog);
      const toggle = details
        .field("value")
        .locator(".param-row-value .param-mode")
        .first();
      expect((await toggleState(toggle)).opacity).toBe("1");
      const mapped = toggle.getByRole("radio", { name: "Mapped" });
      await mapped.evaluate((element) =>
        element.scrollIntoView({ block: "center" }),
      );
      const where = await box(mapped);
      await page.touchscreen.tap(where.x + where.width / 2, where.middle);
      await expect
        .poll(async () => (await toggleState(toggle)).checked)
        .toEqual(["Mapped"]);
    });
  });

/** Opens the fixture at a size, for the desktop check below. */
async function openAt(page: Page) {
  const details = await openStepFixture(page, fixture());
  await details.openWithKeyboard("notify-sales");
  return details;
}

// With a mouse, hover shows a hidden toggle before it can be clicked: it
// keeps taking clicks there.
test.describe("with a mouse at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a hidden toggle shows on hover and takes the click", async ({
    page,
  }) => {
    const details = await openAt(page);
    const toggle = details.field("title").getByRole("radiogroup", {
      name: "Value mode for Title",
    });
    expect(await toggleState(toggle)).toMatchObject({
      opacity: "0",
      pointer: "auto",
    });
    await toggle.getByRole("radio", { name: "Mapped" }).click();
    await expect
      .poll(async () => (await toggleState(toggle)).checked)
      .toEqual(["Mapped"]);
  });
});
