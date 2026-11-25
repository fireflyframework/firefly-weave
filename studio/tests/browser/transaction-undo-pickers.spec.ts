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
// Choosing what undoes a step: the project's published actions and workflows
// with the fact each row ends with, why some can't be chosen, the input rows
// of the one chosen, and what a pair still needs. The platform is mocked; its
// compiler and the Studio host's check are the repository's own Python.
import { expect, test, type Page } from "@playwright/test";
import { hasPython } from "./local-authoring";
import {
  moveWorkflow,
  openConnectedMove,
  openInside,
  openMove,
  step,
  stepInSource,
  undoSelect,
  ADD,
} from "./transaction-po";
import type { StepDetailsPage } from "./step-details-po";

test.skip(!hasPython, "Needs the repository's Python environment.");

const RETRY =
  "Weave retries undo steps until they succeed, so this action must be safe to retry.";
const LIMIT = "Called workflows inside a transaction need a time limit.";
const PAIR = "A step that reserves needs both Confirm with and Release with.";
const ADD_STEP = step("add", "target-system.add-quantity@1.0.0");
const HOLD_STEP = step("hold", "source-system.hold-quantity@1.0.0");

async function openSettings(page: Page, source: string, id: string) {
  const details = await openConnectedMove(page, source);
  await openInside(details, id);
  await details.tab("Settings").click();
  return details;
}
const choose = async (
  page: Page,
  details: StepDetailsPage,
  way: string | RegExp,
) => {
  await undoSelect(details).click();
  await page.getByRole("option", { name: way }).click();
};
const picker = (details: StepDetailsPage, label: string) =>
  details.dialog.getByRole("combobox", { name: label, exact: true });
const list = (page: Page, label: string) =>
  page.getByRole("listbox", { name: `${label} options` });

test.describe("what undoes a step at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Reverse with lists actions then workflows with their facts, and why some can't be chosen", async ({
    page,
  }) => {
    const details = await openSettings(page, moveWorkflow(ADD_STEP), "add");
    await choose(page, details, /^An action that reverses it/);
    const reverse = picker(details, "Reverse with");
    await expect(reverse).toBeFocused();
    await reverse.click();
    const options = list(page, "Reverse with");
    await expect(reverse).toHaveAttribute(
      "placeholder",
      "Search actions and workflows",
    );
    await expect(options.locator(".choice-group")).toHaveText([
      "Actions",
      "Workflows",
    ]);
    const row = (name: string) =>
      options.getByRole("option", { name: new RegExp(`^${name} 1\\.0\\.0`) });
    const submit = row("orders.submit-order");
    await expect(submit).toHaveAttribute("aria-disabled", "true");
    await expect(submit).toContainText("Changes data");
    await expect(submit).toContainText(RETRY);
    await expect(row("target-system.remove-quantity")).toContainText(
      "Safe to retry",
    );
    await expect(row("reverse-target-addition")).toContainText(
      "Up to 5 minutes",
    );
    const unlimited = row("reverse-without-limit");
    await expect(unlimited).toHaveAttribute("aria-disabled", "true");
    await expect(unlimited).toContainText("No time limit");
    await expect(unlimited).toContainText(LIMIT);
    // A row that can't be chosen does nothing.
    await reverse.fill("submit-order");
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await expect(options).toBeVisible();
    // Typing narrows the list; Enter chooses.
    await reverse.fill("remove");
    await expect(options.getByRole("option")).toHaveCount(1);
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await expect(reverse).toHaveValue("target-system.remove-quantity 1.0.0");
    // Its input shows in rows, each Fixed or Mapped.
    const reference = details.field("compensate.input.reference");
    await expect(reference).toContainText("Reference");
    await reference.getByRole("radio", { name: "Mapped" }).click();
    expect(
      (await stepInSource(page, details, "add"))["compensate"],
    ).toMatchObject({
      kind: "action",
      uses: "target-system.remove-quantity@1.0.0",
    });
  });

  test("a workflow is called, and another choice starts its input over", async ({
    page,
  }) => {
    const details = await openSettings(
      page,
      moveWorkflow(
        step("add", "target-system.add-quantity@1.0.0", {
          compensate:
            "{kind: action, uses: target-system.remove-quantity@1.0.0, with: {object: {reference: {ref: /input/moveId}}}}",
        }),
      ),
      "add",
    );
    await expect(details.field("compensate.input.reference")).toContainText(
      "moveId",
    );
    const reverse = picker(details, "Reverse with");
    await reverse.click();
    await list(page, "Reverse with")
      .getByRole("option", { name: /^reverse-target-addition/ })
      .click();
    await expect(details.field("compensate.input.addOperation")).toBeVisible();
    await expect(details.field("compensate.input.reference")).not.toContainText(
      "moveId",
    );
    expect((await stepInSource(page, details, "add"))["compensate"]).toEqual({
      kind: "callWorkflow",
      uses: "reverse-target-addition@1.0.0",
      with: { literal: {} },
    });
  });

  test("a pair asks for both, in step details and in Diagnostics, until both are chosen", async ({
    page,
  }) => {
    const details = await openSettings(page, moveWorkflow(HOLD_STEP), "hold");
    await choose(page, details, /^Reserve, then confirm/);
    for (const id of ["confirm", "cancel"])
      await expect(details.field(id).getByRole("alert")).toHaveText(PAIR);
    const problems = page.locator(".diagnostics-headline");
    await details.close();
    const toggle = page.getByRole("button", { name: "Diagnostics" });
    await expect(problems).toContainText("1 error");
    if ((await toggle.getAttribute("aria-expanded")) === "false")
      await toggle.click();
    const found = page.getByRole("list", { name: "Problems found" });
    await expect(found).toContainText(PAIR);
    await expect(found).toContainText("Step hold");
    await toggle.click();

    await openInside(details, "hold");
    await details.tab("Settings").click();
    const pick = async (label: string, name: string) => {
      await picker(details, label).click();
      await list(page, label)
        .getByRole("option", { name: new RegExp(`^${name}`) })
        .click();
    };
    await pick("Confirm with", "source-system.confirm-hold");
    await expect(details.field("confirm").getByRole("alert")).toHaveCount(0);
    await expect(details.field("cancel").getByRole("alert")).toHaveText(PAIR);
    await pick("Release with", "source-system.release-hold");
    await expect(details.field("cancel").getByRole("alert")).toHaveCount(0);
    // Release can't count on what the hold returned.
    await expect(details.field("cancel.input")).toContainText(
      "Release must work even if the hold didn't go through. Use the step's operation key or your own reference.",
    );
    await expect(details.field("cancel.input.holdOperation")).toBeVisible();
    await details.close();
    await toggle.click();
    await expect(problems).toContainText("Checked locally");
    await expect(page.getByText(PAIR)).toHaveCount(0);
  });

  test("without a platform the list is empty and says why; a name still works", async ({
    page,
  }) => {
    const details = await openMove(page, moveWorkflow(ADD));
    await openInside(details, "add");
    await details.tab("Settings").click();
    await choose(page, details, /^An action that reverses it/);
    const field = details.field("compensate");
    await picker(details, "Reverse with").click();
    await expect(list(page, "Reverse with")).toContainText(
      "Connect to a platform to choose published actions and workflows.",
    );
    await page.keyboard.press("Escape");
    await field.getByRole("radio", { name: "By name" }).click();
    const name = field.getByRole("textbox", { name: "Reverse with" });
    await name.fill("target-system.remove-quantity@1.0.0");
    await name.press("Enter");
    expect((await stepInSource(page, details, "add"))["compensate"]).toEqual({
      kind: "action",
      uses: "target-system.remove-quantity@1.0.0",
      with: { literal: {} },
    });
  });
});

test.describe("what undoes a step at 360x640", () => {
  test.use({ viewport: { width: 360, height: 640 }, hasTouch: true });

  test("the pickers and their rows stay in one column, each row at least 44 px", async ({
    page,
  }) => {
    const details = await openSettings(
      page,
      moveWorkflow(
        step("hold", "source-system.hold-quantity@1.0.0", {
          confirm:
            "{kind: action, uses: source-system.confirm-hold@1.0.0, with: {object: {holdId: {ref: /steps/hold/output/holdId}}}}",
          cancel:
            "{kind: action, uses: source-system.release-hold@1.0.0, with: {literal: {}}}",
        }),
      ),
      "hold",
    );
    const form = details.dialog.locator(
      ".sd-tabpanel:not([hidden]) .settings-form",
    );
    await expect(details.field("cancel.input.reference")).toBeVisible();
    expect(
      await form.evaluate(
        (element) => element.scrollWidth - element.clientWidth,
      ),
    ).toBe(0);
    const confirm = picker(details, "Confirm with");
    await confirm.scrollIntoViewIfNeeded();
    await confirm.click();
    const rows = list(page, "Confirm with").getByRole("option");
    await expect(rows.first()).toBeVisible();
    for (const height of await rows.evaluateAll((items) =>
      items.map((item) => item.getBoundingClientRect().height),
    ))
      expect(height).toBeGreaterThanOrEqual(44);
  });
});
