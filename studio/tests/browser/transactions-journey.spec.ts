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
// A transaction from end to end, as a builder makes one: Add a step ›
// Transaction, three steps inside it from the project's actions, how each is
// undone, Move to end for the one that can't be undone, test output, a
// simulated failure that Execute workflow undoes in order, and an undo that
// keeps failing, which leaves the transaction in doubt. Then a recorded run
// of it on the Runs tab. The platform is mocked; its compiler, the Studio
// host that checks test data and simulates, and the language manifest are the
// repository's own Python.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { parse, stringify } from "yaml";
import { AddStepPanel } from "./add-step-po";
import { CanvasPage } from "./canvas-po";
import { hasPython } from "./local-authoring";
import { applySource } from "./owned-actions";
import { StepDetailsPage } from "./step-details-po";
import { sourceText } from "./support";
import { outputPane } from "./test-data-po";
import {
  chooseRun,
  frameState as recordedFrame,
  IDS,
  INCIDENT,
  openTransactionRuns,
  word as recordedWord,
} from "./transaction-runs-po";
import {
  executeWorkflow,
  executed,
  openInside,
  openMoveToExecute,
  stepInSource,
  undoSelect,
} from "./transaction-po";

test.skip(!hasPython, "Needs the repository's Python environment.");
// It builds, checks and executes on the repository's Python, which a busy
// machine starts slowly.
test.setTimeout(240_000);
const ANSWER = { timeout: 30_000 };

/** move-between-systems before anything is in it. */
const EMPTY = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: move-between-systems, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      moveId: {type: string}
      quantity: {type: integer}
    required: [moveId, quantity]
  outputSchema: {}
  steps: []
  output: {literal: null}
`;
const ORDER =
  "This step can be undone, but it runs after a step that can't. Steps that can't be undone must come last in a transaction.";
/** What each step and undo step reads, as a builder maps it in Source. */
const INPUTS: Record<string, object> = {
  hold: {
    object: {
      quantity: { ref: "/input/quantity" },
      reference: { ref: "/input/moveId" },
    },
  },
  add: {
    object: {
      quantity: { ref: "/input/quantity" },
      reference: { ref: "/input/moveId" },
    },
  },
  submit: { object: { reference: { ref: "/input/moveId" } } },
};
const UNDO_INPUTS: Record<string, object> = {
  confirm: { object: { holdId: { ref: "/steps/hold/output/holdId" } } },
  cancel: {
    object: {
      reference: { ref: "/input/moveId" },
      holdOperation: { ref: "/participant/operationKey" },
    },
  },
  compensate: {
    object: {
      reference: { ref: "/input/moveId" },
      addOperation: { ref: "/participant/operationKey" },
    },
  },
};

const frameState = (canvas: CanvasPage) =>
  canvas.tileBody("move").locator(".frame-state");
const word = (canvas: CanvasPage, id: string) =>
  canvas.tile(id).locator(".tile-output");
const runLog = (page: Page) => page.getByRole("region", { name: "Run log" });
/** The run log's rows as they read, in order. */
const logged = async (page: Page) =>
  (await runLog(page).locator(".run-log-entry").allInnerTexts()).map((text) =>
    text.replace(/\s+/g, " ").trim(),
  );
const divider = (canvas: CanvasPage) =>
  canvas.root.getByRole("img", {
    name: "Point of no return, after submit",
    exact: true,
  });

/** Names the step whose details are open. */
async function name(page: Page, to: string) {
  const dialog = page.getByRole("dialog", { name: /^Step details: / });
  await dialog.getByRole("button", { name: /^Rename / }).click();
  const field = dialog.getByRole("textbox", { name: "Step name" });
  await field.fill(to);
  await field.press("Enter");
  await expect(
    page.getByRole("dialog", { name: `Step details: ${to}`, exact: true }),
  ).toBeVisible();
}
/** Adds a project action at a "+" of the canvas; its details open. */
async function addAction(canvas: CanvasPage, at: string, action: string) {
  await canvas.insertTarget(at).click();
  await new AddStepPanel(canvas.page).choose(action);
  await expect(
    canvas.page.getByRole("dialog", { name: /^Step details: / }),
  ).toBeVisible();
}
/** Chooses how the open step is undone, in its Settings. */
async function undoneBy(page: Page, details: StepDetailsPage, way: RegExp) {
  await details.tab("Settings").click();
  await undoSelect(details).click();
  await page.getByRole("option", { name: way }).click();
}
/** Picks what undoes the open step, from the project's actions. */
async function pick(details: StepDetailsPage, label: string, search: string) {
  const picker = details.dialog.getByRole("combobox", {
    name: label,
    exact: true,
  });
  await picker.click();
  await picker.fill(search);
  const options = details.page
    .getByRole("listbox", { name: `${label} options` })
    .getByRole("option");
  await expect(options).toHaveCount(1);
  await options.click();
  await expect(picker).toHaveValue(new RegExp(`${search}.* 1\\.0\\.0$`));
}
/** Simulate a failure… of a step, from its Output. */
async function simulate(page: Page, details: StepDetailsPage, id: string) {
  await openInside(details, id);
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "More output actions" }).click();
  await page.getByRole("menuitem", { name: "Simulate a failure…" }).click();
  return page.getByRole("dialog", { name: `Simulate a failure of ${id}` });
}
/** Sets a step's test output from its Output. */
async function testOutput(
  details: StepDetailsPage,
  id: string,
  output: object,
) {
  await openInside(details, id);
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "Set test output" }).click();
  await pane
    .locator(".output-editor")
    .getByRole("textbox")
    .first()
    .fill(JSON.stringify(output));
  await pane.getByRole("button", { name: "Save", exact: true }).click();
  // Kept once the host's check of it answers.
  await expect(pane.locator(".pinned-banner")).toBeVisible(ANSWER);
}
async function diagnostics(page: Page): Promise<Locator> {
  const toggle = page.getByRole("button", { name: "Diagnostics" });
  await expect(toggle).toBeVisible();
  if ((await toggle.getAttribute("aria-expanded")) === "false")
    await toggle.click();
  return page.getByRole("list", { name: "Problems found" });
}

test.describe("a transaction from end to end at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("build it, fix its order, simulate a failure and an undo that fails, and execute", async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("console", (message) => {
      if (
        message.type() === "error" &&
        !message.text().startsWith("Failed to load")
      )
        errors.push(message.text());
    });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const { details, calls } = await openMoveToExecute(page, EMPTY, {
      pins: {},
    });
    const canvas = new CanvasPage(page);

    // A new workflow starts with its trigger: a form that asks for the move.
    await canvas.insertTarget("Add first step").click();
    const panel = new AddStepPanel(page);
    await panel.option("Manual form").click();
    await details.close();
    // Add a step › Flow › Transaction: a frame with its header and a slot.
    await canvas.insertTarget("Add a step at the start").click();
    await panel.option("Flow").click();
    await panel.option("Transaction").click();
    await name(page, "move");
    await details.close();
    await expect(canvas.tileBody("move")).toHaveAccessibleName(
      /^move, Transaction, All of these succeed, or what's done is undone, Setup needed: Add a step to the transaction\./,
    );

    // Three of the project's actions inside it, each with how it's undone.
    await addAction(
      canvas,
      "Add a step to transaction move",
      "source-system.hold-quantity",
    );
    await name(page, "hold");
    await undoneBy(page, details, /^Reserve, then confirm/);
    await pick(details, "Confirm with", "confirm-hold");
    await pick(details, "Release with", "release-hold");
    await details.close();
    await addAction(
      canvas,
      "Add a step at the end of transaction move",
      "orders.submit-order",
    );
    await name(page, "submit");
    await undoneBy(page, details, /^Can't be undone/);
    await details.close();
    // The line after the step that can't be undone.
    await expect(divider(canvas)).toBeVisible();
    await addAction(
      canvas,
      "Add a step at the end of transaction move",
      "target-system.add-quantity",
    );
    await name(page, "add");
    await undoneBy(page, details, /^An action that reverses it/);
    await pick(details, "Reverse with", "remove-quantity");
    await details.close();
    for (const [id, label] of [
      ["hold", "Reserved"],
      ["submit", "Can't be undone"],
      ["add", "Reversible"],
    ])
      await expect(canvas.tile(id).locator(".tile-undo")).toHaveText(label);

    // What each reads, mapped in Source; the choices made above stay.
    const document = parse(await sourceText(page)) as {
      spec: { steps: { steps: Record<string, unknown>[] }[] };
    };
    for (const step of document.spec.steps[0].steps) {
      step["with"] = INPUTS[step["id"] as string];
      for (const phase of ["confirm", "cancel", "compensate"])
        if (step[phase])
          (step[phase] as Record<string, unknown>)["with"] = UNDO_INPUTS[phase];
    }
    await applySource(page, stringify(document));

    // add can be undone but runs after submit: Move to end puts submit last.
    const list = await diagnostics(page);
    const finding = list.getByRole("listitem").filter({ hasText: ORDER });
    await expect(finding).toBeVisible(ANSWER);
    await finding
      .getByRole("button", {
        name: "Move submit to the end of transaction move",
      })
      .click();
    await expect(list.getByText(ORDER)).toHaveCount(0, ANSWER);
    expect(
      (
        (await stepInSource(page, details, "move"))["steps"] as { id: string }[]
      ).map(({ id }) => id),
    ).toEqual(["hold", "add", "submit"]);
    await expect(divider(canvas)).toBeVisible();

    // What hold and add return, for Execute.
    await testOutput(details, "hold", { holdId: "h-1" });
    await testOutput(details, "add", { id: "a-1" });

    // submit fails, and changed nothing: what's done before it is undone.
    let dialog = await simulate(page, details, "submit");
    await dialog.getByLabel("Code").fill("REQUEST_REFUSED");
    await dialog.getByRole("radio", { name: /^It changed nothing/ }).check();
    await dialog.getByRole("button", { name: "Simulate failure" }).click();
    await expect(dialog).toHaveCount(0);
    await details.close();
    await executeWorkflow(page);
    expect((await executed(calls)).errors).toEqual({
      "node:submit": { code: "REQUEST_REFUSED", effect: "none" },
    });
    await expect(frameState(canvas)).toHaveText("Undone", ANSWER);
    await expect(word(canvas, "add")).toHaveText("Undone");
    await expect(word(canvas, "hold")).toHaveText("Released");
    await expect(word(canvas, "submit")).toHaveText("Failed");
    await expect(divider(canvas)).toBeVisible();
    expect(await logged(page)).toEqual([
      "hold 1 field",
      "add 1 field",
      "submit REQUEST_REFUSED",
      "Transaction move Undone",
      "Undo of add Undone",
      "Release of hold Released",
    ]);

    // add's undo keeps failing: the transaction is left in doubt.
    dialog = await simulate(page, details, "add");
    await dialog.getByRole("radio", { name: "Its undo" }).check();
    await dialog.getByLabel("Code").fill("UNAVAILABLE");
    await dialog.getByRole("button", { name: "Simulate failure" }).click();
    await expect(dialog).toHaveCount(0);
    await details.close();
    await executeWorkflow(page);
    await expect(frameState(canvas)).toHaveText(
      "In doubt · action needed",
      ANSWER,
    );
    await expect(word(canvas, "add")).toHaveText("Undo failed");
    await expect(word(canvas, "hold")).toHaveText("Still reserved");
    await expect(runLog(page).locator(".run-log-outcome p").first()).toHaveText(
      "The transaction move is in doubt, so the simulated run stopped.",
    );
    expect(errors).toEqual([]);
  });
});

test.describe("the transaction's recorded runs at 1440x900", () => {
  test.use({
    viewport: { width: 1440, height: 900 },
    contextOptions: { reducedMotion: "reduce" },
  });

  test("a run that was undone, then one in doubt that opens its incident in Operate", async ({
    page,
  }) => {
    const { runs, errors } = await openTransactionRuns(page);
    // The platform undid it: each step says what happened to it.
    let canvas = await chooseRun(page, runs, IDS.undone);
    await expect(recordedFrame(canvas)).toHaveText("Undone");
    await expect(recordedWord(canvas, "hold")).toHaveText("Released");
    await expect(recordedWord(canvas, "add")).toHaveText("Undone");
    await expect(recordedWord(canvas, "submit")).toHaveText("Failed");
    await expect(divider(canvas)).toBeVisible();
    // An undo kept failing: what failed, and what is still applied.
    canvas = await chooseRun(page, runs, IDS.doubt);
    await expect(runs.canvas.locator(".run-in-doubt")).toHaveText(
      "Failed at submit: REQUEST_REFUSED · transaction move is in doubt · WV-TXN-UNDO-STUCK",
    );
    await expect(recordedFrame(canvas)).toHaveText("In doubt · action needed");
    await expect(recordedWord(canvas, "hold")).toHaveText("Still reserved");
    await expect(recordedWord(canvas, "add")).toHaveText("Undo failed");
    const details = new StepDetailsPage(page);
    await details.open("move");
    await (await outputPane(details))
      .getByRole("button", { name: "Open in Operate" })
      .click();
    await expect(page).toHaveURL(
      new RegExp(`/operate/incidents\\?incident=${INCIDENT}$`),
    );
    expect(errors).toEqual([]);
  });
});
