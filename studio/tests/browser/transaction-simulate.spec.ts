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
// Simulate a failure inside a transaction: on a step, with what it may have
// changed, or on its undo, its release or its confirm; and the test output an
// undo step needs when Execute can't make it up. The platform is mocked; the
// Studio host that checks test data and simulates is the repository's own
// Python.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { hasPython } from "./local-authoring";
import type { StepDetailsPage } from "./step-details-po";
import { outputPane } from "./test-data-po";
import {
  ADD_STEP,
  executed,
  executeWorkflow,
  HOLD_STEP,
  moveWorkflow,
  NOTIFY_STEP,
  openInside,
  openMoveToExecute,
  step,
  SUBMIT_STEP,
} from "./transaction-po";

test.skip(!hasPython, "Needs the repository's Python environment.");
// The host simulates with the repository's Python, which a busy machine starts slowly.
test.setTimeout(90_000);
const ANSWER = { timeout: 30_000 };

const MOVE = moveWorkflow(HOLD_STEP() + ADD_STEP + SUBMIT_STEP + NOTIFY_STEP);
/** hold is confirmed by an action whose result Studio can't make up. */
const CONFIRMED_BY_HOLD = moveWorkflow(
  HOLD_STEP("source-system.hold-quantity@1.0.0") + ADD_STEP + SUBMIT_STEP,
);
const CONFIRM = {
  title: "Set test output to execute",
  text: "hold's Confirm with needs test output: source-system.hold-quantity returns fields Studio can't make up.",
};

const dialogOf = (page: Page, id: string) =>
  page.getByRole("dialog", { name: `Simulate a failure of ${id}` });
/** Opens Simulate a failure… from the step's Output. */
async function simulate(page: Page, details: StepDetailsPage, id: string) {
  await openInside(details, id);
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "More output actions" }).click();
  await page.getByRole("menuitem", { name: "Simulate a failure…" }).click();
  const dialog = dialogOf(page, id);
  await expect(dialog).toBeVisible();
  return { pane, dialog, code: dialog.getByLabel("Code") };
}
const radio = (dialog: Locator, name: string | RegExp) =>
  dialog.getByRole("radio", { name });
const submit = (dialog: Locator) =>
  dialog.getByRole("button", { name: "Simulate failure" }).click();

test.describe("Simulate a failure inside a transaction at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("a step says what it may have changed, by the platform's rule until the builder picks", async ({
    page,
  }) => {
    const { details, calls } = await openMoveToExecute(page, MOVE);
    const { pane, dialog, code } = await simulate(page, details, "add");
    await expect(dialog).toContainText(
      "Execute workflow fails add with this code, and transaction move undoes what's done. Real runs ignore it.",
    );
    // What fails comes first, and takes focus.
    const what = dialog.getByRole("group", { name: "What fails" });
    await expect(what.getByRole("radio")).toHaveCount(2);
    await expect(radio(what, "The step")).toBeChecked();
    await expect(radio(what, "The step")).toBeFocused();
    const changed = dialog.getByRole("group", {
      name: "Did it change anything?",
    });
    const may = radio(changed, /^It may have/);
    const nothing = radio(changed, /^It changed nothing/);
    // The action's own code says it changed nothing.
    await expect(code).toHaveValue("VALIDATION");
    await expect(nothing).toBeChecked();
    await expect(dialog.locator(".failure-reason")).toHaveText(
      "Chosen from the code: target-system.add-quantity says VALIDATION changes nothing.",
    );
    await expect(radio(dialog, "Didn't happen The step failed.")).toBeChecked();
    await code.fill("REQUEST_REFUSED");
    await expect(may).toBeChecked();
    await expect(dialog.locator(".failure-reason")).toHaveText(
      "Chosen from the code: target-system.add-quantity doesn't say REQUEST_REFUSED changes nothing.",
    );
    // Nobody knows what an unknown outcome changed.
    await radio(dialog, /^Unknown/).check();
    await expect(may).toBeChecked();
    await expect(nothing).toBeDisabled();
    await expect(changed).toContainText(
      "Not with an unknown outcome: nobody knows what changed.",
    );
    await expect(dialog.locator(".failure-reason")).toHaveCount(0);
    await expect(dialog.locator(".failure-warning")).toHaveCount(0);
    await radio(dialog, /^Didn't happen/).check();
    // A pick stays, whatever the code.
    await nothing.check();
    await expect(dialog.locator(".failure-reason")).toHaveCount(0);
    await code.fill("UNAVAILABLE");
    await expect(nothing).toBeChecked();
    await submit(dialog);
    await expect(dialog).toHaveCount(0);
    await expect(
      pane.locator("weave-failure-banner").getByRole("status"),
    ).toHaveText(
      "This step fails when you execute it in Studio. Real runs ignore it.",
    );
    // Edit opens on the pick, which the code alone wouldn't choose.
    await pane.getByRole("button", { name: "Edit", exact: true }).click();
    await expect(nothing).toBeChecked();
    await expect(dialog.locator(".failure-reason")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await details.close();
    await executeWorkflow(page);
    expect((await executed(calls)).errors).toEqual({
      "node:add": { code: "UNAVAILABLE", effect: "none" },
    });
  });

  test("its undo fails every one of the transaction's tries, beside the step's own output", async ({
    page,
  }) => {
    const { details, calls } = await openMoveToExecute(
      page,
      moveWorkflow(HOLD_STEP() + ADD_STEP + SUBMIT_STEP, {
        extra: "policy: {undoRetry: {maxAttempts: 3}}\n",
      }),
    );
    const { pane, dialog, code } = await simulate(page, details, "add");
    await radio(dialog, "Its undo").check();
    await expect(dialog).toContainText(
      "When transaction move is undone, Execute workflow fails add's undo every time Weave tries it. Real runs ignore it.",
    );
    await expect(dialog.locator(".failure-note")).toHaveText([
      "Its undo fails all 3 tries, the transaction's Max tries, so the transaction ends in doubt.",
      "Something must fail after add for the transaction to be undone: simulate a failure of a later step too.",
    ]);
    // An undo either works or it doesn't: nothing else to say about it.
    await expect(dialog.getByRole("group", { name: "Outcome" })).toHaveCount(0);
    await expect(
      dialog.getByRole("group", { name: "Did it change anything?" }),
    ).toHaveCount(0);
    await expect(dialog).toContainText("Any code the action could return.");
    await code.fill("UNAVAILABLE");
    await submit(dialog);
    const banner = pane.locator("weave-failure-banner");
    await expect(banner.getByRole("status")).toHaveText(
      "This step's undo fails when you execute it in Studio. Real runs ignore it.",
    );
    await expect(banner.locator(".failure-code")).toHaveText("UNAVAILABLE");
    await expect(banner.locator(".failure-undo")).toHaveText(
      "· Undo of add · fails all 3 tries",
    );
    await expect(
      pane.locator(".pill", { hasText: "Undo fails when executed" }),
    ).toBeVisible();
    // Edit opens on the undo; the step's own failure is another one.
    await banner.getByRole("button", { name: "Edit" }).click();
    await expect(radio(dialog, "Its undo")).toBeChecked();
    await expect(code).toHaveValue("UNAVAILABLE");
    await radio(dialog, "The step").check();
    await expect(code).toHaveValue("VALIDATION");
    await submit(dialog);
    await expect(banner).toHaveCount(2);
    await details.close();
    await executeWorkflow(page);
    expect((await executed(calls)).errors).toEqual({
      "node:add": { code: "VALIDATION", effect: "none" },
      "node:add#undo": { code: "UNAVAILABLE", attempts: 3 },
    });
    // Removed on its own, with Undo.
    await openInside(details, "add");
    await (await outputPane(details))
      .locator("weave-failure-banner", { hasText: "Undo of add" })
      .getByRole("button", { name: "Remove" })
      .click();
    await expect(
      page.locator(".toast", {
        hasText: "Removed the simulated failure of add's undo.",
      }),
    ).toBeVisible();
    await expect(
      details.dialog.locator("weave-failure-banner .failure-code"),
    ).toHaveText("VALIDATION · Didn't happen");
  });

  test("a reserved step offers its release and its confirm, and one that can't be undone neither", async ({
    page,
  }) => {
    const { details } = await openMoveToExecute(page, MOVE, {
      errors: { add: { code: "VALIDATION", effect: "none", source: "manual" } },
    });
    const hold = await simulate(page, details, "hold");
    await expect(
      hold.dialog.getByRole("group", { name: "What fails" }).getByRole("radio"),
    ).toHaveCount(3);
    await expect(hold.dialog).toContainText(
      "Weave releases hold with the rest.",
    );
    await radio(hold.dialog, "Its release").check();
    // A later step fails already, so nothing more is asked for.
    await expect(hold.dialog.locator(".failure-note")).toHaveText([
      "Its release fails all 10 tries, the transaction's Max tries, so the transaction ends in doubt.",
    ]);
    await radio(hold.dialog, "Its confirm").check();
    await expect(hold.dialog).toContainText(
      "When transaction move succeeds, Execute workflow fails hold's confirm every time Weave tries it.",
    );
    await page.keyboard.press("Escape");
    const last = await simulate(page, details, "submit");
    await expect(
      last.dialog.getByRole("group", { name: "What fails" }),
    ).toHaveCount(0);
    await expect(last.code).toBeFocused();
    await expect(last.dialog).toContainText(
      "Execute workflow fails submit with this code. Real runs ignore it.",
    );
    await expect(last.dialog).toContainText(
      "submit can't be undone, so the transaction is in doubt.",
    );
    await page.keyboard.press("Escape");
    // Past it there is nothing to undo, so nothing to say it changed.
    const after = await simulate(page, details, "notify");
    await expect(
      after.dialog.getByRole("group", { name: "Did it change anything?" }),
    ).toHaveCount(0);
  });

  test("a failure of an undo the step no longer has is left out, and says so", async ({
    page,
  }) => {
    const { details, calls } = await openMoveToExecute(
      page,
      moveWorkflow(
        HOLD_STEP() +
          step("add", "target-system.add-quantity@1.0.0", {
            irreversible: "true",
          }),
      ),
      {
        errors: {
          "add#undo": { code: "UNAVAILABLE", attempts: 10, source: "manual" },
        },
      },
    );
    await openInside(details, "add");
    const pane = await outputPane(details);
    await expect(
      pane.locator("weave-failure-banner").getByRole("status"),
    ).toHaveText("Execute leaves this failure out. add has no undo anymore.");
    await expect(
      pane.locator(".pill", { hasText: "Failure left out" }),
    ).toBeVisible();
    await details.close();
    // The host would refuse the whole request for it.
    await executeWorkflow(page);
    expect(await executed(calls)).not.toHaveProperty("errors");
  });

  test("the dialog fits the window, and What fails is a row of equal parts", async ({
    page,
  }) => {
    const { details } = await openMoveToExecute(page, MOVE);
    const { dialog } = await simulate(page, details, "hold");
    const box = (await dialog.boundingBox())!;
    expect(box.width).toBe(560);
    expect(box.y).toBeGreaterThanOrEqual(0);
    expect(box.y + box.height).toBeLessThanOrEqual(900);
    const parts = await dialog
      .getByRole("group", { name: "What fails" })
      .locator("label")
      .evaluateAll((labels) =>
        labels.map((label) => label.getBoundingClientRect().toJSON()),
      );
    expect(parts).toHaveLength(3);
    for (const part of parts) {
      expect(Math.abs(part.width - parts[0].width)).toBeLessThan(1);
      expect(part.top).toBe(parts[0].top);
    }
    // The whole control, its border included.
    const row = (await dialog.locator(".failure-what").boundingBox())!;
    expect(row.height).toBe(36);
    // Arrow keys move it, as any group of choices.
    await page.keyboard.press("ArrowRight");
    await expect(radio(dialog, "Its release")).toBeChecked();
    await expect(radio(dialog, "Its release")).toBeFocused();
  });
});

test.describe("test output of an undo step at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("Execute workflow asks for it, keeps it at its own key, and the next one sends it", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    const { details, calls } = await openMoveToExecute(page, CONFIRMED_BY_HOLD);
    await executeWorkflow(page);
    const question = page.getByRole("dialog", { name: CONFIRM.title });
    await expect(question).toContainText(CONFIRM.text, ANSWER);
    await expect(question).toContainText(
      "The test output is saved with this workflow's test data, for Execute only. Real runs ignore it.",
    );
    await expect(question.getByRole("listitem")).toHaveText([
      "hold",
      "Confirm with · source-system.hold-quantity 1.0.0",
    ]);
    const set = question.getByRole("button", { name: "Set test output" });
    await expect(set).toBeFocused();
    // Cancel leaves it, and goes back to the button that executed.
    await question.getByRole("button", { name: "Cancel" }).click();
    await expect(question).toHaveCount(0);
    await expect(
      page.locator(".editor-bar").getByRole("button", {
        name: "Execute workflow",
      }),
    ).toBeFocused();
    await executeWorkflow(page);
    await expect(question).toBeVisible(ANSWER);
    await set.click();
    await expect(question).toHaveCount(0);
    const pane = await outputPane(details);
    await expect(details.dialogFor("hold")).toBeVisible();
    const editor = pane.getByRole("textbox", {
      name: "Output of hold's confirm",
    });
    await expect(editor).toBeFocused();
    // It starts from the fields the action returns.
    await expect(editor).toContainText("holdId");
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.insertText('{"holdId": 1}');
    await pane.getByRole("button", { name: "Save" }).click();
    // The host checks it against what confirms, not against hold.
    await expect(pane.getByRole("alert")).toHaveText(
      "This value doesn't fit the step's output fields.",
    );
    await editor.focus();
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.insertText('{"holdId": "confirmed-1"}');
    await pane.getByRole("button", { name: "Save" }).click();
    await expect(editor).toHaveCount(0);
    await details.close();
    await executeWorkflow(page);
    const request = await executed(calls, 3);
    expect(request.mocks).toMatchObject({
      "node:hold": { holdId: "h-1" },
      "node:hold#confirm": { holdId: "confirmed-1" },
    });
    await expect(page.locator(".run-log-status")).toHaveText(
      /^Completed/,
      ANSWER,
    );
    await expect(question).toHaveCount(0);
    // It is the confirm's alone: shown and removed from More output actions.
    await openInside(details, "hold");
    const shown = await outputPane(details);
    const more = shown.getByRole("button", { name: "More output actions" });
    await more.click();
    await page
      .getByRole("menuitem", {
        name: "Test output of its confirm",
        exact: true,
      })
      .click();
    await expect(
      shown.getByRole("textbox", { name: "Output of hold's confirm" }),
    ).toContainText("confirmed-1");
    await shown.getByRole("button", { name: "Cancel" }).click();
    await more.click();
    await page
      .getByRole("menuitem", { name: "Remove test output of its confirm" })
      .click();
    await expect(
      page.locator(".toast", { hasText: "Unpinned hold's confirm." }),
    ).toBeVisible();
    await more.click();
    await expect(
      page.getByRole("menuitem", {
        name: "Test output of its confirm",
        exact: true,
      }),
    ).toHaveCount(0);
  });

  test("Execute step asks for it in step details, on the step it is about", async ({
    page,
  }) => {
    const { details } = await openMoveToExecute(
      page,
      moveWorkflow(
        HOLD_STEP("source-system.hold-quantity@1.0.0") + ADD_STEP + SUBMIT_STEP,
        {
          after:
            "- id: report\n  kind: transform\n  value: {object: {done: {literal: true}}}\n",
        },
      ),
    );
    await details.open("report");
    await details.dialog
      .getByRole("button", { name: "Execute step", exact: true })
      .first()
      .click();
    const question = page.getByRole("dialog", { name: CONFIRM.title });
    await expect(question).toContainText(CONFIRM.text, ANSWER);
    await question.getByRole("button", { name: "Set test output" }).click();
    await expect(details.dialogFor("hold")).toBeVisible();
    await expect(
      (await outputPane(details)).getByRole("textbox", {
        name: "Output of hold's confirm",
      }),
    ).toBeFocused();
  });
});

test.describe("Simulate a failure inside a transaction on a phone", () => {
  test.use({ viewport: { width: 360, height: 640 }, hasTouch: true });

  test("What fails and every choice are 44 px, in a sheet that scrolls to its end", async ({
    page,
  }) => {
    const { details } = await openMoveToExecute(page, MOVE);
    const { dialog } = await simulate(page, details, "hold");
    const heights = await dialog
      .locator("label:has(input[type=radio]), .dialog-actions button")
      .evaluateAll((parts) =>
        parts.map((part) => part.getBoundingClientRect().height),
      );
    expect(heights.length).toBeGreaterThan(6);
    for (const height of heights) expect(height).toBeGreaterThanOrEqual(44);
    expect(
      await dialog.evaluate((element) => element.scrollWidth),
    ).toBeLessThanOrEqual(360);
    const last = dialog.getByRole("button", { name: "Simulate failure" });
    await last.scrollIntoViewIfNeeded();
    const box = (await last.boundingBox())!;
    expect(box.y + box.height).toBeLessThanOrEqual(640);
  });
});
