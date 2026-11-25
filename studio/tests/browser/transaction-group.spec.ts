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
// Group in a transaction: from the selection's toolbar and a selected step's
// menu, the steps become one transaction in one undo step; later reads of
// their data are listed first, with Cancel ahead; where it can't run, the
// command stays in place and says why. The platform is mocked.
import { expect, test, type Page } from "@playwright/test";
import { parse } from "yaml";
import { languageFeatures } from "./add-step-po";
import { CanvasPage } from "./canvas-po";
import { openStepFixture, yamlFile } from "./step-details-po";
import { offline, sourceText } from "./support";

const steps = (text: string) =>
  yamlFile(
    "move-between-systems.yaml",
    `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: move-between-systems, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
${text}  output: {literal: null}
`,
  );
const transform = (id: string, value = "{literal: {}}") =>
  `    - {id: ${id}, kind: transform, value: ${value}}\n`;
/** hold, add and submit; notify reads hold's result, record reads add's. */
const READ = steps(
  transform("hold") +
    transform("add") +
    transform("submit") +
    transform(
      "notify",
      "{object: {holdId: {ref: /steps/hold/output/holdId}}}",
    ) +
    transform("record", "{object: {id: {ref: /steps/add/output/id}}}"),
);
/** hold and add, which no later step reads; move holds a transaction. */
const PLAIN = steps(
  transform("hold") +
    transform("add") +
    `    - id: move
      kind: transaction
      steps:
        - {id: submit, kind: transform, value: {literal: {}}}
      output: {literal: null}
`,
);

async function open(
  page: Page,
  file = READ,
  features = ["text.concat", "flow.transaction"],
) {
  await openStepFixture(page, file, async (p) => {
    await offline(p);
    await languageFeatures(p, features);
  });
  const canvas = new CanvasPage(page);
  await canvas.ready();
  return canvas;
}
/** Selects steps as a person does: a click, then Shift+click. */
async function select(canvas: CanvasPage, ...ids: string[]) {
  for (const [index, id] of ids.entries())
    await canvas
      .tileBody(id)
      .click(index ? { modifiers: ["Shift"] } : undefined);
}
const toolbar = (page: Page) =>
  page.getByRole("toolbar", { name: "Selected steps" });
const groupButton = (page: Page) =>
  toolbar(page).getByRole("button", { name: "Group in a transaction" });
const dialog = (page: Page) =>
  page.getByRole("dialog", { name: /^Group \d+ steps in a transaction\?$/ });
interface Read {
  id: string;
  kind: string;
  steps?: Read[];
  output?: unknown;
}
const stepsOf = async (page: Page) =>
  (parse(await sourceText(page)) as { spec: { steps: Read[] } }).spec.steps;

test.describe("grouping steps at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the button groups the selected steps in one undo step and selects the transaction", async ({
    page,
  }) => {
    const canvas = await open(page, PLAIN);
    const before = await sourceText(page);
    await canvas.ready();
    await select(canvas, "hold", "add");
    await groupButton(page).click();
    await expect(dialog(page)).toHaveCount(0);
    const toast = page.locator(".toast-region");
    await expect(toast).toContainText("Grouped 2 steps in transaction-1.");
    await expect(canvas.tileBody("transaction-1")).toBeFocused();
    await expect(canvas.tile("transaction-1")).toContainText("2 steps");
    const [block, move] = await stepsOf(page);
    expect(block).toEqual({
      id: "transaction-1",
      kind: "transaction",
      steps: [
        { id: "hold", kind: "transform", value: { literal: {} } },
        { id: "add", kind: "transform", value: { literal: {} } },
      ],
      output: { ref: "/steps/add/output" },
    });
    expect(move.id).toBe("move");
    await canvas.ready();
    await toast.getByRole("button", { name: "Undo" }).click();
    expect(await sourceText(page)).toBe(before);
  });

  test("later reads are listed first, starting on Cancel; Group anyway keeps them", async ({
    page,
  }) => {
    const canvas = await open(page);
    await select(canvas, "hold", "add", "submit");
    await groupButton(page).click();
    const question = dialog(page);
    await expect(question).toHaveAccessibleName(
      "Group 3 steps in a transaction?",
    );
    await expect(question).toHaveAccessibleDescription(
      "2 later steps read data from these steps. After grouping they can read only the transaction's result.",
    );
    await expect(
      question.getByRole("list", { name: "Later reads" }).getByRole("listitem"),
    ).toHaveText([
      /^notify\s*reads\s*hold › holdId$/,
      /^record\s*reads\s*add › id$/,
    ]);
    await expect(question).toContainText(
      "Group anyway keeps these reads; Diagnostics then lists each one so you can point it at the transaction's result.",
    );
    await expect(
      question.getByRole("button", { name: "Cancel" }),
    ).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(question).toHaveCount(0);
    await expect(groupButton(page)).toBeFocused();
    expect((await stepsOf(page)).map((step) => step.id)).toEqual([
      "hold",
      "add",
      "submit",
      "notify",
      "record",
    ]);
    await canvas.ready();
    await select(canvas, "hold", "add", "submit");
    await groupButton(page).click();
    await expect(
      question.getByRole("button", { name: "Cancel" }),
    ).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(
      question.getByRole("button", { name: "Group anyway" }),
    ).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.locator(".toast-region")).toContainText(
      "Grouped 3 steps in transaction-1.",
    );
    const [block, notify] = await stepsOf(page);
    expect(block.steps!.map((step) => step.id)).toEqual([
      "hold",
      "add",
      "submit",
    ]);
    expect(notify).toMatchObject({
      value: { object: { holdId: { ref: "/steps/hold/output/holdId" } } },
    });
  });

  test("a selected step's menu groups the selection too", async ({ page }) => {
    const canvas = await open(page, PLAIN);
    await select(canvas, "hold", "add");
    await canvas.tileBody("add").click({ button: "right" });
    await page
      .getByRole("menuitem", { name: "Group in a transaction" })
      .click();
    await expect(canvas.tile("transaction-1")).toBeVisible();
    // A step outside the selection, or alone, has no such command.
    await canvas.tileBody("move").click({ button: "right" });
    await expect(page.getByRole("menuitem", { name: "Delete" })).toBeVisible();
    await expect(
      page.getByRole("menuitem", { name: "Group in a transaction" }),
    ).toHaveCount(0);
  });

  test("where it can't run it stays in place, says why, and changes nothing", async ({
    page,
  }) => {
    const canvas = await open(page, PLAIN);
    const blocked = async (reason: string) => {
      const button = groupButton(page);
      await expect(button).toHaveAttribute("aria-disabled", "true");
      await expect(button).toHaveAccessibleDescription(reason);
      await expect(button).toHaveAttribute("title", reason);
      await button.focus();
      await page.keyboard.press("Enter");
      await expect(page.locator(".toast-region")).toContainText(reason);
    };
    await select(canvas, "add", "move");
    await blocked("A transaction can't contain another transaction.");
    await canvas.tileBody("hold").click();
    await canvas.tileBody("move").click({ modifiers: ["Shift"] });
    await blocked(
      "Group in a transaction works on steps next to each other in one path. Select a single run of steps.",
    );
    expect((await stepsOf(page)).map((step) => step.id)).toEqual([
      "hold",
      "add",
      "move",
    ]);
  });

  test("without transactions on the platform it says what it needs", async ({
    page,
  }) => {
    const canvas = await open(page, PLAIN, ["text.concat"]);
    await select(canvas, "hold", "add");
    await expect(groupButton(page)).toHaveAccessibleDescription(
      "Needs a platform with transactions (flow.transaction)",
    );
    await groupButton(page).focus();
    await page.keyboard.press("Enter");
    await expect(page.locator(".toast-region")).toContainText(
      "Needs a platform with transactions (flow.transaction)",
    );
    await expect(canvas.tile("transaction-1")).toHaveCount(0);
  });
});

const LANGUAGE = "**/studio/contracts/language";

test("a platform that can't be read gives no false reason, and each press asks again", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  let answers = false;
  let asked = 0;
  await page.route(LANGUAGE, (route) => {
    asked++;
    return answers
      ? route.fulfill({
          json: { features: ["text.concat", "flow.transaction"] },
        })
      : route.fulfill({ status: 503, json: {} });
  });
  await openStepFixture(page, PLAIN);
  const canvas = new CanvasPage(page);
  await canvas.ready();
  const before = asked;
  await select(canvas, "hold", "add");
  await expect.poll(() => asked).toBeGreaterThan(before);
  await expect(groupButton(page)).not.toHaveAttribute("aria-disabled", "true");
  await expect(groupButton(page)).not.toHaveAccessibleDescription(/Needs/);
  const toast = page.locator(".toast-region");
  const pressed = asked;
  await groupButton(page).click();
  await expect(toast).toContainText(
    "Studio couldn't read what this platform runs. Press Group in a transaction again.",
  );
  expect(asked).toBeGreaterThan(pressed);
  await expect(canvas.tile("transaction-1")).toHaveCount(0);
  answers = true;
  await groupButton(page).click();
  await expect(toast).toContainText("Grouped 2 steps in transaction-1.");
});

test("a platform whose transactions Studio can't add yet says so, as Add a step does", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.route(LANGUAGE, (route) =>
    route.fulfill({
      json: {
        features: ["text.concat", "flow.transaction"],
        step_kinds: [{ kind: "transaction", studio: "pending" }],
      },
    }),
  );
  await openStepFixture(page, PLAIN);
  const canvas = new CanvasPage(page);
  await canvas.ready();
  await select(canvas, "hold", "add");
  await expect(groupButton(page)).toHaveAccessibleDescription(
    "Studio can't add transactions yet.",
  );
});

test("steps that changed while the platform was asked are not grouped", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  let answer = () => {};
  const asked = new Promise<void>((done) => (answer = done));
  await page.route(LANGUAGE, async (route) => {
    await asked;
    await route.fulfill({
      json: { features: ["text.concat", "flow.transaction"] },
    });
  });
  await openStepFixture(page, PLAIN);
  const canvas = new CanvasPage(page);
  await canvas.ready();
  await canvas.tileBody("hold").click();
  await page.keyboard.press("ControlOrMeta+D");
  await expect(canvas.tile("hold-1")).toBeVisible();
  await select(canvas, "hold", "hold-1");
  await groupButton(page).click();
  // Undo takes the copy back before the platform answers.
  await page
    .getByRole("toolbar", { name: "Workflow commands" })
    .getByRole("button", { name: "Undo" })
    .click();
  await expect(canvas.tile("hold-1")).toHaveCount(0);
  answer();
  await expect(page.locator(".toast-region")).toContainText(
    "The workflow changed since. Select the steps again.",
  );
  expect((await stepsOf(page)).map((step) => step.id)).toEqual([
    "hold",
    "add",
    "move",
  ]);
});

test("the toolbar keeps one row where the canvas is narrow, its commands in More", async ({
  page,
}) => {
  await page.setViewportSize({ width: 600, height: 500 });
  const canvas = await open(page);
  await select(canvas, "hold", "add");
  const bar = (await toolbar(page).boundingBox())!;
  expect(bar.height).toBeLessThanOrEqual(56);
  await expect(groupButton(page)).toBeHidden();
  for (const name of ["Copy", "Duplicate", "Delete"])
    await expect(
      toolbar(page).getByRole("button", { name, exact: true }),
    ).toBeVisible();
  await toolbar(page)
    .getByRole("button", { name: "More commands for the selected steps" })
    .click();
  await expect(page.getByRole("menuitem")).toHaveText([
    "Group in a transaction",
  ]);
  await page.keyboard.press("Escape");
  // Wide, the buttons are back in the row.
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(groupButton(page)).toBeVisible();
  expect((await toolbar(page).boundingBox())!.height).toBeLessThanOrEqual(56);
});

test.describe("grouping steps on a phone", () => {
  test.use({ viewport: { width: 360, height: 640 }, hasTouch: true });

  test("the toolbar's menu offers it, and the question is a sheet with 44 px answers", async ({
    page,
  }) => {
    const canvas = await open(page);
    await canvas.root.focus();
    await select(canvas, "hold", "add");
    await toolbar(page)
      .getByRole("button", { name: "More commands for the selected steps" })
      .click();
    await page
      .getByRole("menuitem", { name: "Group in a transaction" })
      .click();
    const question = dialog(page);
    await expect(question).toBeVisible();
    const box = (await question.boundingBox())!;
    expect(box.x).toBe(0);
    expect(box.width).toBe(360);
    expect(Math.round(box.y + box.height)).toBe(640);
    for (const name of ["Group anyway", "Cancel"]) {
      const button = (await question
        .getByRole("button", { name })
        .boundingBox())!;
      expect(button.height).toBeGreaterThanOrEqual(44);
    }
    expect(
      await question.evaluate((node) => node.scrollWidth - node.clientWidth),
    ).toBe(0);
  });
});
