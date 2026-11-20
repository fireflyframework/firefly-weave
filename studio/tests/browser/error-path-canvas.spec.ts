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
// A step's error path on the canvas: Success and Error outputs, each with
// its label (Error with its icon too, so color is never alone), the empty
// path's slot that adds a step to it, and tile names that say what a
// step's failure does.
import { expect, test, type Page } from "@playwright/test";
import { parse } from "yaml";
import { openWorkflow, type CanvasPage } from "./canvas-po";
import { yamlFile } from "./step-details-po";
import { sourceText } from "./support";

/** lookup continues on error; submit continues with error output into an empty path; then summarize. */
const GUARDED = yamlFile(
  "request-intake.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: request-intake, version: 1.3.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - id: lookup
      kind: transform
      value: {object: {found: {literal: true}}}
      onError: continue
    - id: submit
      kind: transform
      value: {object: {sent: {literal: true}}}
      onError: errorOutput
      errorPath:
        steps: []
        output: {literal: null}
    - id: summarize
      kind: transform
      value: {object: {done: {literal: true}}}
  output: {literal: null}
`,
);
/** The steps of the error path of submit, read from Source. */
async function errorPathOf(page: Page): Promise<string[]> {
  const source = parse(await sourceText(page)) as {
    spec: { steps: { id: string; errorPath?: { steps: { id: string }[] } }[] };
  };
  const submit = source.spec.steps.find((step) => step.id === "submit");
  return (submit?.errorPath?.steps ?? []).map((step) => step.id);
}

test.use({ viewport: { width: 1440, height: 900 } });

test("a step that continues with error output shows Success and Error outputs, each labeled", async ({
  page,
}) => {
  const canvas = await openWorkflow(page, GUARDED);
  const success = canvas.branchLabel("submit/success");
  const error = canvas.branchLabel("submit/errorPath");
  await expect(success).toHaveText("Success");
  await expect(success).toHaveAttribute("data-tone", "muted");
  await expect(error).toHaveText("Error");
  await expect(error).toHaveAttribute("data-tone", "danger");
  // Color is never alone: the Error label carries the warning icon.
  await expect(error.locator("weave-icon")).toHaveAttribute("name", "warning");
  await expect(canvas.handle("submit:path:errorPath")).toHaveAttribute(
    "data-tone",
    "danger",
  );
  await expect(canvas.handle("submit:path:success")).toBeVisible();
});

test("tiles say what their failure does, in their names and under a step that continues", async ({
  page,
}) => {
  const canvas = await openWorkflow(page, GUARDED);
  await expect(canvas.tileBody("lookup")).toHaveAccessibleName(
    /^lookup, continues on error, /,
  );
  await expect(canvas.tileBody("submit")).toHaveAccessibleName(
    /^submit, continues with error output, /,
  );
  await expect(canvas.tile("lookup")).toContainText("Continues on error");
});

/** The names Tab reaches from submit, selected, until its error path's entry (at most 6). */
async function tabToErrorPath(page: Page, canvas: CanvasPage) {
  await canvas.tileBody("submit").click();
  await canvas.closeInspector();
  await canvas.tileBody("submit").focus();
  const reached: string[] = [];
  for (let i = 0; i < 6; i++) {
    await page.keyboard.press("Tab");
    const name = await page.evaluate(
      () => document.activeElement?.getAttribute("aria-label") ?? "",
    );
    reached.push(name);
    if (name === "Add a step to the error path of submit") break;
  }
  return reached;
}

test("the empty error path's slot adds a step to the path, by keyboard", async ({
  page,
}) => {
  const canvas = await openWorkflow(page, GUARDED);
  const slot = canvas.insertTarget("Add a step to the error path of submit");
  // Tab reaches the slot from its step: the step's commands, the "+" on
  // its Success edge, then the slot below it.
  expect(await tabToErrorPath(page, canvas)).toEqual([
    "Delete submit",
    "More commands for submit",
    "Insert a step between submit and summarize",
    "Add a step to the error path of submit",
  ]);
  await expect(slot).toBeFocused();
  await page.keyboard.press("Enter");
  await canvas.pick("Transform");
  await canvas.closeInspector();
  expect(await errorPathOf(page)).toEqual(["transform-1"]);
  await canvas.ready();
  // The new step sits on the Error row, below submit.
  const submit = await canvas.tileBody("submit").boundingBox();
  const added = await canvas.tileBody("transform-1").boundingBox();
  expect(added!.y).toBeGreaterThan(submit!.y + submit!.height);
});

test("Send email, added from the empty error path's slot, runs on the error path", async ({
  page,
}) => {
  const canvas = await openWorkflow(page, GUARDED);
  await canvas.insertTarget("Add a step to the error path of submit").click();
  await canvas.pick("Send email");
  await canvas.closeInspector();
  expect(await errorPathOf(page)).toEqual(["send-email"]);
  expect(await canvas.rootSteps()).not.toContain("send-email");
});

test("a drag from the Error output adds a step to the error path", async ({
  page,
}) => {
  const canvas = await openWorkflow(page, GUARDED);
  const from = (await canvas.handle("submit:path:errorPath").boundingBox())!;
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  // Let go on empty canvas, below and right of the handle.
  await page.mouse.move(from.x + 160, from.y + 120, { steps: 10 });
  // While it runs, the words beside the pointer say where the step goes,
  // and the empty path's slot is the target.
  await expect(canvas.root.locator(".band-ghost")).toHaveText(
    "Add to the error path",
  );
  await expect(
    canvas.insertTarget("Add a step to the error path of submit"),
  ).toHaveClass(/\bdrop-target\b/);
  await page.mouse.up();
  await expect(canvas.root.locator(".band-ghost")).toHaveCount(0);
  await canvas.pick("Transform");
  await canvas.closeInspector();
  expect(await errorPathOf(page)).toEqual(["transform-1"]);
});

/** submit's error path holds notify-team and flag-request. */
const WITH_PATH = yamlFile(
  "request-intake-path.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: request-intake, version: 1.3.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - id: submit
      kind: transform
      value: {object: {sent: {literal: true}}}
      onError: errorOutput
      errorPath:
        steps:
          - id: notify-team
            kind: transform
            value: {ref: /steps/submit/error/code}
          - id: flag-request
            kind: transform
            value: {object: {flagged: {literal: true}}}
        output: {literal: null}
    - id: summarize
      kind: transform
      value: {object: {done: {literal: true}}}
  output: {literal: null}
`,
);

test("deleting a step takes its error path, says which steps went, and Undo brings them back", async ({
  page,
}) => {
  const canvas = await openWorkflow(page, WITH_PATH);
  await canvas.tileBody("submit").focus();
  await page.keyboard.press("Delete");
  // No question first: the toast names what went and offers Undo.
  await expect(page.getByRole("alertdialog")).toHaveCount(0);
  const toast = page.locator(".toast");
  await expect(toast.locator("strong")).toHaveText(
    "Deleted submit and its error path (2 steps)",
  );
  await expect(toast.locator(".toast-detail")).toHaveText(
    "notify-team and flag-request were in it.",
  );
  expect(await canvas.rootSteps()).toEqual(["summarize"]);
  await toast.getByRole("button", { name: "Undo" }).click();
  expect(await canvas.rootSteps()).toEqual(["submit", "summarize"]);
  expect(await errorPathOf(page)).toEqual(["notify-team", "flag-request"]);
});

test("the Error output is a keyboard stop that adds a step at the start of the error path", async ({
  page,
}) => {
  const canvas = await openWorkflow(page, WITH_PATH);
  const output = canvas.root.getByRole("button", {
    name: "Add a step to the error path of submit",
    exact: true,
  });
  await expect(output).toHaveAttribute("data-handle", "submit:path:errorPath");
  // The pointer's tooltip names it; keyboard focus adds what Enter does.
  await expect(output).toHaveAttribute(
    "title",
    "Add a step to the error path of submit",
  );
  const tip = output.locator(".handle-tip");
  await expect(tip).toBeHidden();
  // Tab reaches it from its step, after the step's own commands.
  expect(await tabToErrorPath(page, canvas)).toEqual([
    "Delete submit",
    "More commands for submit",
    "Add a step to the error path of submit",
  ]);
  await expect(output).toBeFocused();
  await expect(tip).toBeVisible();
  await expect(tip).toHaveText(
    "Add a step to the error path of submit · Enter",
  );
  await page.keyboard.press("Enter");
  await canvas.pick("Transform");
  await canvas.closeInspector();
  expect(await errorPathOf(page)).toEqual([
    "transform-1",
    "notify-team",
    "flag-request",
  ]);
});
