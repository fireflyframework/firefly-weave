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
// What to do about a transaction's problems: each finding in the Diagnostics
// panel and in step details with its fix, the summary before publishing, and
// the data a step's undo reads. The platform is mocked; its compiler and the
// Studio host's check are the repository's own Python.
import {
  expect,
  test,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test";
import { hasPython, routeValidate } from "./local-authoring";
import { applySource } from "./owned-actions";
import type { StepDetailsPage } from "./step-details-po";
import {
  moveWorkflow,
  openConnectedMove,
  openInside,
  openMove,
  step,
  stepInSource,
  undoSelect,
} from "./transaction-po";
import { offline, showView, sourceText } from "./support";

test.skip(!hasPython, "Needs the repository's Python environment.");

const ORDER =
  "This step can be undone, but it runs after a step that can't. Steps that can't be undone must come last in a transaction.";
const UNDECLARED = "This step changes data. Choose how it's undone.";
const REVERSE =
  "{kind: action, uses: target-system.remove-quantity@1.0.0, with: {object: {reference: {ref: /input/moveId}, addOperation: {ref: /participant/operationKey}}}}";
const HOLD = step("hold", "source-system.hold-quantity@1.0.0", {
  confirm:
    "{kind: action, uses: source-system.confirm-hold@1.0.0, with: {object: {holdId: {ref: /steps/hold/output/holdId}}}}",
  cancel:
    "{kind: action, uses: source-system.release-hold@1.0.0, with: {object: {reference: {ref: /input/moveId}, holdOperation: {ref: /participant/operationKey}}}}",
});
const ADD = step("add", "target-system.add-quantity@1.0.0", {
  compensate: REVERSE,
});
const PLAIN_ADD = step("adjust", "target-system.add-quantity@1.0.0");
const SUBMIT = `- id: submit
  kind: action
  uses: orders.submit-order@1.0.0
  with: {object: {reference: {ref: /input/moveId}}}
  irreversible: true
`;

async function diagnostics(page: Page): Promise<Locator> {
  const toggle = page.getByRole("button", { name: "Diagnostics" });
  await expect(toggle).toBeVisible();
  if ((await toggle.getAttribute("aria-expanded")) === "false")
    await toggle.click();
  return page.getByRole("list", { name: "Problems found" });
}
const rowOf = (list: Locator, text: string) =>
  list.getByRole("listitem").filter({ hasText: text });
const orderIn = async (page: Page, details: StepDetailsPage) =>
  (
    (await stepInSource(page, details, "move"))["steps"] as { id: string }[]
  ).map(({ id }) => id);

test.describe("a transaction's problems at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a step that can be undone after one that can't: Move to end fixes it and the finding clears", async ({
    page,
  }) => {
    const details = await openMove(page, moveWorkflow(HOLD + SUBMIT + ADD), {
      start: async (p) => {
        await offline(p);
        await routeValidate(p);
      },
    });
    const list = await diagnostics(page);
    const row = rowOf(list, ORDER);
    await expect(row).toBeVisible();
    await expect(row).toContainText("Step add · How it's undone");
    const before = await sourceText(page);
    await showView(page, "Designer");
    // One button a row, at its end; the compiler's own edit isn't offered apart.
    const fix = row.getByRole("button", {
      name: "Move submit to the end of transaction move",
    });
    await expect(fix).toHaveText("Move to end");
    await expect(
      row.getByRole("button", { name: "Apply suggested edit" }),
    ).toHaveCount(0);
    const box = (await fix.boundingBox())!;
    const whole = (await row.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(32);
    expect(Math.round(box.x + box.width)).toBe(
      Math.round(whole.x + whole.width),
    );
    await fix.click();
    await expect(list.getByText(ORDER)).toHaveCount(0);
    expect(await orderIn(page, details)).toEqual(["hold", "add", "submit"]);
    await page.keyboard.press("ControlOrMeta+z");
    expect(await sourceText(page)).toBe(before);
  });

  test("the same fix is under How it's undone in step details", async ({
    page,
  }) => {
    const details = await openConnectedMove(
      page,
      moveWorkflow(HOLD + SUBMIT + ADD),
    );
    await openInside(details, "add");
    await details.tab("Settings").click();
    const field = details.field("undo");
    await expect(field.getByRole("alert")).toContainText(ORDER);
    await field.getByRole("button", { name: "Move to end" }).click();
    await expect(field.getByRole("alert")).toHaveCount(0);
    expect(await orderIn(page, details)).toEqual(["hold", "add", "submit"]);
  });

  test("publishing an unsafe transaction lists its problems with their fixes, and Publish stays off", async ({
    page,
  }) => {
    const details = await openConnectedMove(
      page,
      moveWorkflow(HOLD + PLAIN_ADD + SUBMIT + ADD),
    );
    const publish = page.getByRole("button", { name: "Publish…" });
    if (!(await publish.first().isVisible()))
      await page.getByRole("button", { name: "More" }).first().click();
    await page
      .getByRole("button", { name: "Publish…" })
      .or(page.getByRole("menuitem", { name: "Publish…" }))
      .first()
      .click();
    const dialog = page.getByRole("dialog", {
      name: "Publish move-between-systems 1.0.0",
    });
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText(
      "This transaction isn't safe to publish: 3 problems",
    );
    await expect(dialog).toContainText(
      "Fix them, then publish. Each fix is one undo step.",
    );
    await expect(dialog.getByRole("button", { name: "Close" })).toBeFocused();
    await expect(
      dialog.getByRole("button", { name: "Publish", exact: true }),
    ).toBeDisabled();
    await expect(dialog.getByRole("listitem")).toHaveText([
      new RegExp(`adjust.*${UNDECLARED}.*Choose how it's undone`, "s"),
      /submit.*Nothing can be undone after the step that can't be undone.*Move after transaction/s,
      /add.*must come last in a transaction\..*Move to end/s,
    ]);
    // Escape closes it; nothing is published.
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    // Diagnostics has the same fixes: choosing one never chooses for the builder.
    const before = await sourceText(page);
    await showView(page, "Designer");
    const list = await diagnostics(page);
    const row = rowOf(list, UNDECLARED);
    await expect(
      row.getByRole("button", { name: "Apply suggested edit" }),
    ).toHaveCount(0);
    await row.getByRole("button", { name: /^Choose how it's undone/ }).click();
    await expect(details.dialogFor("adjust")).toBeVisible();
    await expect(undoSelect(details)).toBeFocused();
    await expect(undoSelect(details)).toHaveValue("");
    await details.close();
    expect(await sourceText(page)).toBe(before);
  });

  test("a fix chosen while publishing closes the question and is made", async ({
    page,
  }) => {
    const details = await openConnectedMove(
      page,
      moveWorkflow(HOLD + SUBMIT + ADD),
    );
    const publish = page
      .getByRole("button", { name: "Publish…" })
      .or(page.getByRole("menuitem", { name: "Publish…" }));
    if (!(await publish.first().isVisible()))
      await page.getByRole("button", { name: "More" }).first().click();
    await publish.first().click();
    const dialog = page.getByRole("dialog", {
      name: "Publish move-between-systems 1.0.0",
    });
    await expect(dialog).toContainText("1 problem");
    await dialog.getByRole("button", { name: /^Move to end/ }).click();
    await expect(dialog).toHaveCount(0);
    expect(await orderIn(page, details)).toEqual(["hold", "add", "submit"]);
  });

  test("a check that can't run opens no question, and a fix for an older workflow changes nothing", async ({
    page,
  }) => {
    const details = await openConnectedMove(
      page,
      moveWorkflow(HOLD + SUBMIT, { after: ADD }),
    );
    const list = await diagnostics(page);
    const clear = list.getByRole("button", { name: /^Clear How it's undone/ });
    await expect(clear).toBeVisible();
    // The connection goes: no later check answers.
    const away = (route: Route) =>
      route.fulfill({
        status: 503,
        json: { error: { code: "WV-STUDIO-UNAVAILABLE", message: "Away." } },
      });
    await page.route("**/compiler/compile", away);
    await page.route("**/studio/local/validate", away);
    await applySource(page, moveWorkflow(HOLD + ADD + SUBMIT));
    const changed = await sourceText(page);
    await showView(page, "Designer");
    const publish = page
      .getByRole("button", { name: "Publish…" })
      .or(page.getByRole("menuitem", { name: "Publish…" }));
    if (!(await publish.first().isVisible()))
      await page.getByRole("button", { name: "More" }).first().click();
    await publish.first().click();
    await expect(
      page.getByText("Studio couldn't check this workflow. Try again."),
    ).toBeVisible();
    await expect(
      page.getByRole("dialog", { name: "Publish move-between-systems 1.0.0" }),
    ).toHaveCount(0);
    // add is inside now, and its choice is right: the old fix leaves it.
    await (await diagnostics(page))
      .getByRole("button", { name: /^Clear How it's undone/ })
      .click();
    await expect(page.locator(".toast-region")).toContainText(
      "The workflow changed after this suggestion was made.",
    );
    expect(await sourceText(page)).toBe(changed);
    expect(await stepInSource(page, details, "add")).toHaveProperty(
      "compensate",
    );
  });

  test("a hold too short for a wait opens what sets it", async ({ page }) => {
    const details = await openConnectedMove(
      page,
      moveWorkflow(
        `${HOLD}- {id: pause, kind: wait, durationSeconds: 7200}\n${SUBMIT}`,
      ),
    );
    const list = await diagnostics(page);
    const row = rowOf(
      list,
      "This step can wait longer than reservations are held.",
    );
    await expect(row).toContainText("Step pause · Duration");
    await row.getByRole("button", { name: /^Set Hold for/ }).click();
    await expect(details.dialogFor("move")).toBeVisible();
    await expect(
      details.field("holdSeconds").getByRole("textbox"),
    ).toBeFocused();
  });

  test("an action that isn't safe to retry opens from its finding", async ({
    page,
  }) => {
    const details = await openConnectedMove(
      page,
      moveWorkflow(
        HOLD +
          step("add", "target-system.add-quantity@1.0.0", {
            compensate:
              "{kind: action, uses: orders.submit-order@1.0.0, with: {object: {reference: {ref: /input/moveId}}}}",
          }),
      ),
    );
    const validate = page.getByRole("button", { name: "Validate" }).first();
    await validate.click();
    const list = await diagnostics(page);
    const row = rowOf(list, "Weave retries undo steps until they succeed");
    await row.getByRole("button", { name: /^Open action/ }).click();
    await expect(details.dialogFor("add")).toBeVisible();
    const viewer = page.getByRole("dialog", {
      name: "View orders.submit-order@1.0.0",
    });
    await expect(viewer).toContainText("sideEffect");
    await viewer.getByRole("button", { name: "Close" }).first().click();
    await expect(viewer).toHaveCount(0);
    await expect(details.dialogFor("add")).toBeVisible();
  });
});

test.describe("the data a step's undo reads", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  const open = async (
    page: Page,
    details: StepDetailsPage,
    field: string,
  ): Promise<Locator> => {
    const row = details.field(field);
    await row.getByRole("button", { name: /^Field menu for/ }).click();
    await page.getByRole("menuitem", { name: /Use data…/ }).click();
    await expect(row.locator("weave-reference-combobox input")).toBeFocused();
    await page.keyboard.press("ArrowDown");
    const list = page.getByRole("listbox", { name: /suggestions$/ });
    await expect(list).toBeVisible();
    return list;
  };

  test("a release starts with This step's undo and never its step's output", async ({
    page,
  }) => {
    const details = await openConnectedMove(
      page,
      moveWorkflow(
        step("hold", "source-system.hold-quantity@1.0.0", {
          confirm:
            "{kind: action, uses: source-system.confirm-hold@1.0.0, with: {literal: {}}}",
          cancel:
            "{kind: action, uses: source-system.release-hold@1.0.0, with: {literal: {}}}",
        }),
      ),
    );
    await openInside(details, "hold");
    await details.tab("Settings").click();
    const list = await open(page, details, "cancel.input.holdOperation");
    const groups = list.getByRole("group");
    await expect(groups.first()).toHaveAccessibleName(/This step's undo/);
    await expect(groups.first().getByRole("option")).toHaveText([
      /Operation key/,
      /Outcome of hold/,
      /Transaction ID/,
      /Business key/,
    ]);
    await expect(groups.first()).toContainText(
      "hold's own output isn't offered here: Release must work even if the hold didn't go through.",
    );
    await expect(list.locator('[data-ref^="/steps/hold"]')).toHaveCount(0);
    await list.getByRole("option", { name: /Operation key/ }).click();
    const row = details.field("cancel.input.holdOperation");
    await expect(row).toContainText("This step's undo");
    await expect(row).toContainText("Operation key");
    expect(
      (
        (await stepInSource(page, details, "hold"))["cancel"] as {
          with: unknown;
        }
      ).with,
    ).toEqual({
      object: { holdOperation: { ref: "/participant/operationKey" } },
    });
  });

  test("a reverse offers the step's own output, which may be empty", async ({
    page,
  }) => {
    const details = await openConnectedMove(
      page,
      moveWorkflow(
        HOLD +
          step("add", "target-system.add-quantity@1.0.0", {
            compensate:
              "{kind: action, uses: target-system.remove-quantity@1.0.0, with: {literal: {}}}",
          }),
      ),
    );
    await openInside(details, "add");
    await details.tab("Settings").click();
    const list = await open(page, details, "compensate.input.reference");
    const groups = list.getByRole("group");
    await expect(groups.nth(1)).toHaveAccessibleName(
      /add · this step's output/,
    );
    await expect(groups.nth(1)).toContainText("may be absent");
    await expect(groups.nth(1)).toContainText(
      "Empty when add failed with an unknown outcome.",
    );
    await expect(list.locator('[data-ref="/steps/hold/output"]')).toHaveCount(
      1,
    );
  });

  test("a step of a transaction reads the steps before it, and its result reads them all", async ({
    page,
  }) => {
    const details = await openConnectedMove(
      page,
      moveWorkflow(HOLD + ADD).replace(
        "output: {literal: null}\n  output",
        "output: {ref: /steps/add/output}\n  output",
      ),
    );
    await details.open("move");
    await expect(details.field("output")).not.toContainText(
      "isn't available here",
    );
    await openInside(details, "hold");
    await details.tab("Settings").click();
    await expect(details.field("confirm.input")).not.toContainText(
      "isn't available here",
    );
  });
});

test.describe("a transaction's problems at 360x640", () => {
  test.use({ viewport: { width: 360, height: 640 }, hasTouch: true });

  test("each field of the data picker keeps its name beside what is said of it", async ({
    page,
  }) => {
    const details = await openConnectedMove(page, moveWorkflow(HOLD + ADD));
    await openInside(details, "add");
    await details.tab("Settings").click();
    const row = details.field("compensate.input.addOperation");
    await row.getByRole("button", { name: /Operation key/ }).press("Enter");
    await expect(row.locator("weave-reference-combobox input")).toBeFocused();
    await page.keyboard.press("ArrowDown");
    const own = page.locator('[role="option"][data-ref="/steps/add/output"]');
    await own.scrollIntoViewIfNeeded();
    await expect(own).toContainText("may not match this field");
    const list = (await page
      .getByRole("listbox", { name: /suggestions$/ })
      .boundingBox())!;
    for (const part of [".ref-combo-name", ".ref-combo-warn"]) {
      const box = (await own.locator(part).boundingBox())!;
      expect(box.width, part).toBeGreaterThan(16);
      expect(box.x + box.width, part).toBeLessThanOrEqual(list.x + list.width);
    }
    expect(
      await own
        .locator(".ref-combo-name")
        .evaluate((node) => node.scrollWidth - node.clientWidth),
    ).toBe(0);
  });

  test("fixes are at least 44 px, in the panel and in the publishing sheet", async ({
    page,
  }) => {
    await openConnectedMove(page, moveWorkflow(HOLD + SUBMIT + ADD));
    const list = await diagnostics(page);
    const fix = rowOf(list, ORDER).getByRole("button", {
      name: /^Move to end/,
    });
    await fix.scrollIntoViewIfNeeded();
    expect((await fix.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    await page.getByRole("button", { name: "More" }).first().click();
    await page
      .getByRole("button", { name: "Publish…" })
      .or(page.getByRole("menuitem", { name: "Publish…" }))
      .first()
      .click();
    const dialog = page.getByRole("dialog", {
      name: "Publish move-between-systems 1.0.0",
    });
    await expect(dialog).toBeVisible();
    const box = (await dialog.boundingBox())!;
    expect([box.x, box.width]).toEqual([0, 360]);
    expect(Math.round(box.y + box.height)).toBe(640);
    for (const name of [/^Move to end/, "Close"])
      expect(
        (await dialog.getByRole("button", { name }).boundingBox())!.height,
      ).toBeGreaterThanOrEqual(44);
    expect(
      await dialog.evaluate((node) => node.scrollWidth - node.clientWidth),
    ).toBe(0);
  });
});
