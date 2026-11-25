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
// How it's undone in a step's Settings, inside a transaction: nothing chosen
// until the builder chooses, each way's own fields under what it does, the
// toast when a chosen operation goes, a step that only reads, and a step that
// keeps fields outside a transaction. The platform is mocked; the language
// manifest is the repository's own.
import { expect, test } from "@playwright/test";
import {
  ADD,
  manifest,
  moveWorkflow,
  openInside,
  openMove,
  stepInSource,
  undoSelect,
} from "./transaction-po";
import { drawn, sourceText } from "./support";

const UNDECLARED = "This step changes data. Choose how it's undone.";
const HOLD = `- id: hold
  kind: transform
  value: {literal: {}}
`;

test.describe("how a step is undone at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a step that changes data has no choice until one is made, and opening it writes nothing", async ({
    page,
  }) => {
    const details = await openMove(page, moveWorkflow(HOLD + ADD));
    await openInside(details, "add");
    await details.tab("Settings").click();
    await expect
      .poll(() =>
        details
          .topFields()
          .evaluateAll((items) =>
            items.map((item) => item.getAttribute("data-param")),
          ),
      )
      .toEqual(["retry", "timeout", "undo", "onError", "notes", "about"]);
    const select = undoSelect(details);
    await expect(select).toHaveValue("");
    await expect(select).toHaveAttribute(
      "placeholder",
      "Choose how it's undone",
    );
    await expect(select).toHaveAttribute("aria-invalid", "true");
    await expect(select).toHaveAccessibleDescription(UNDECLARED);
    await expect(details.field("undo").getByRole("alert")).toHaveText(
      UNDECLARED,
    );
    // The issues list names the field once, and goes to it.
    await details.dialog.getByRole("button", { name: /issues?$/ }).click();
    const listed = details.dialog
      .getByRole("menuitem")
      .filter({ hasText: UNDECLARED });
    await expect(listed).toHaveCount(1);
    await expect(listed).toContainText("How it's undone");
    await listed.click();
    await expect(select).toBeFocused();
    // The three ways, each with what it does; none is chosen.
    await select.click();
    const options = page.getByRole("listbox", {
      name: "How it's undone options",
    });
    await expect(options.getByRole("option")).toHaveText([
      /Reserve, then confirm.*This step holds something; Weave confirms it when the transaction succeeds, or releases it if anything fails\./s,
      /An action that reverses it.*If anything later fails, Weave runs this to reverse the step\./s,
      /Can't be undone.*Once this step runs, the transaction can't go back\. It must be the last step that changes data\./s,
    ]);
    await expect(options.getByRole("option", { selected: true })).toHaveCount(
      0,
    );
    await page.keyboard.press("Escape");
    await expect(select).toHaveValue("");
    expect(await stepInSource(page, details, "add")).toEqual({
      id: "add",
      kind: "action",
      uses: "move-between-systems.add@1.0.0",
      connection: "target",
      with: { object: { body: { object: { quantity: { literal: 1 } } } } },
    });
    await expect(page.getByRole("button", { name: /^Undo/ })).toBeDisabled();
  });

  test("An action that reverses it asks for Reverse with; Can't be undone takes it away, and Undo brings it back", async ({
    page,
  }) => {
    const details = await openMove(page, moveWorkflow(HOLD + ADD));
    await openInside(details, "add");
    await details.tab("Settings").click();
    const select = undoSelect(details);
    await select.focus();
    await page.keyboard.press("ArrowDown");
    const options = page.getByRole("listbox", {
      name: "How it's undone options",
    });
    await expect(options).toBeVisible();
    await page.keyboard.press("ArrowDown");
    await expect(select).toHaveAttribute(
      "aria-activedescendant",
      (await options
        .getByRole("option", { name: /^An action that reverses it/ })
        .getAttribute("id"))!,
    );
    await page.keyboard.press("Enter");
    await expect(select).toHaveValue("An action that reverses it");
    await expect(details.field("undo")).toContainText(
      "If anything later fails, Weave runs this to reverse the step.",
    );
    // Its first field is next, and asks for an operation.
    const field = details.field("compensate");
    const name = field.getByRole("textbox", { name: "Reverse with" });
    await expect(name).toBeFocused();
    await expect(field.getByRole("alert")).toHaveText(
      "Choose what reverses this step.",
    );
    await name.fill("target-system.remove-quantity@1.0.0");
    await name.press("Enter");
    await expect(field.getByRole("alert")).toHaveCount(0);

    await select.click();
    await options.getByRole("option", { name: /^Can't be undone/ }).click();
    await expect(field).toHaveCount(0);
    const toast = page.locator(".toast-region");
    await expect(toast).toContainText("Removed Reverse with.");
    await toast.getByRole("button", { name: "Undo" }).click();
    await expect(select).toHaveValue("An action that reverses it");
    await expect(name).toHaveValue("target-system.remove-quantity@1.0.0");
    expect((await stepInSource(page, details, "add"))["compensate"]).toEqual({
      kind: "action",
      uses: "target-system.remove-quantity@1.0.0",
      with: { literal: {} },
    });
  });

  test("Reserve, then confirm shows both operations and how long it holds", async ({
    page,
  }) => {
    const details = await openMove(
      page,
      moveWorkflow(ADD, { extra: "policy: {holdSeconds: 600}\n" }),
    );
    await openInside(details, "add");
    await details.tab("Settings").click();
    await undoSelect(details).click();
    await page.getByRole("option", { name: /^Reserve, then confirm/ }).click();
    for (const id of ["confirm", "cancel"])
      await expect(details.field(id).getByRole("alert")).toHaveText(
        "A step that reserves needs both Confirm with and Release with.",
      );
    const hold = details.field("undoHold");
    await expect(hold).toContainText("Hold for");
    await expect(hold).toContainText(
      "Same as the transaction's Hold reservations for.",
    );
    const amount = hold.getByRole("textbox");
    await expect(amount).toHaveAttribute("placeholder", "10");
    // Key by key: 10 minutes on the way is the transaction's own hold.
    await amount.click();
    for (const typed of ["1", "10", "105"]) {
      await page.keyboard.press(typed.slice(-1));
      await drawn(page);
      expect(await amount.inputValue()).toBe(typed);
      await expect(amount).toBeFocused();
    }
    await amount.press("Tab");
    await expect(hold).not.toContainText("Same as the transaction's");
    expect((await stepInSource(page, details, "add"))["holdSeconds"]).toBe(
      6300,
    );
  });

  test("a step that only reads has nothing to undo", async ({ page }) => {
    const details = await openMove(page, moveWorkflow(ADD), { method: "GET" });
    await openInside(details, "add");
    await details.tab("Settings").click();
    await expect(details.field("undo")).toContainText(
      "Doesn't change anything, nothing to undo",
    );
    await expect(undoSelect(details)).toHaveCount(0);
    await expect(details.dialog).not.toContainText(UNDECLARED);
  });

  test("outside a transaction, what the step keeps is cleared in one undo step", async ({
    page,
  }) => {
    const outside = moveWorkflow(HOLD, {
      after: `${ADD}  compensate: {kind: action, uses: target-system.remove-quantity@1.0.0, with: {literal: {}}}\n`,
    });
    const details = await openMove(page, outside);
    const before = await sourceText(page);
    await page
      .getByRole("tab", { name: "Designer" })
      .or(page.getByRole("button", { name: "Designer" }))
      .first()
      .click();
    await details.open("add");
    await details.tab("Settings").click();
    const field = details.field("undo");
    await expect(field).toContainText(
      "How a step is undone applies only to steps inside a transaction. Move the step into a transaction, or clear How it's undone.",
    );
    await expect(field).toContainText("An action that reverses it");
    await field.getByRole("button", { name: "Clear How it's undone" }).click();
    await expect(field).toHaveCount(0);
    expect(await stepInSource(page, details, "add")).not.toHaveProperty(
      "compensate",
    );
    await page.keyboard.press("ControlOrMeta+z");
    expect(await sourceText(page)).toBe(before);
  });

  test("a platform without transactions offers nothing", async ({ page }) => {
    const details = await openMove(page, moveWorkflow(ADD), {
      features: manifest.features.filter((name) => name !== "flow.transaction"),
    });
    await openInside(details, "add");
    await details.tab("Settings").click();
    await expect(details.field("onError")).toBeVisible();
    await expect(details.field("undo")).toHaveCount(0);
  });
});

test.describe("how a step is undone at 360x640", () => {
  test.use({ viewport: { width: 360, height: 640 }, hasTouch: true });

  test("one column, its controls wide enough to press, nothing wider than the phone", async ({
    page,
  }) => {
    const details = await openMove(
      page,
      moveWorkflow(
        `${ADD}  confirm: {kind: action, uses: source-system.confirm-hold@1.0.0, with: {literal: {}}}
  cancel: {kind: action, uses: source-system.release-hold@1.0.0, with: {literal: {}}}
`,
      ),
    );
    await openInside(details, "add");
    await details.tab("Settings").click();
    const select = undoSelect(details);
    await expect(select).toHaveValue("Reserve, then confirm");
    const form = details.dialog.locator(
      ".sd-tabpanel:not([hidden]) .settings-form",
    );
    expect(
      await form.evaluate(
        (element) => element.scrollWidth - element.clientWidth,
      ),
    ).toBe(0);
    for (const control of [
      select,
      details.field("confirm").getByRole("textbox"),
      details.field("cancel").getByRole("textbox"),
      details.field("undoHold").getByRole("textbox"),
    ])
      expect(
        (await control
          .locator("xpath=ancestor-or-self::*[self::div or self::input][1]")
          .boundingBox())!.height,
      ).toBeGreaterThanOrEqual(44);
  });
});
