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
// Dragging Input fields into formulas and templates: the path goes where it
// is dropped; a template gets {{ path }}; a lone reference is replaced.
import { expect, test, type Locator, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { parse, stringify } from "yaml";
import {
  formulaBox,
  formulaText,
  formulasFixture,
  replaceFormula,
} from "./formula-po";
import {
  openConnectedFixture,
  openStepFixture,
  stepFixture,
  withLanguageFeatures,
  yamlFile,
  type StepDetailsPage,
} from "./step-details-po";
import { sourceText } from "./support";

/** Drags an Input field to the right end of a target, inside its box. */
async function dropAtEnd(
  page: Page,
  details: StepDetailsPage,
  row: string,
  target: Locator,
) {
  const input = details.dialog.locator(".sd-input");
  await input
    .getByRole("combobox", { name: "Input source" })
    .selectOption("input");
  const area = (await target.boundingBox())!;
  await input
    .getByRole("listitem", { name: new RegExp(`^${row}, `) })
    .first()
    .dragTo(target, {
      targetPosition: { x: Math.max(4, area.width - 48), y: area.height / 2 },
    });
}

test.describe("drops into formulas at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  test.beforeEach(async ({ page }) => {
    await withLanguageFeatures(page);
  });

  test("a field dropped on a formula goes in at the drop point", async ({
    page,
  }) => {
    const details = await openStepFixture(page, formulasFixture);
    await details.openWithKeyboard("flag");
    const field = details.field("value");
    await replaceFormula(page, field, "input.amount > 1000 and ");
    await expect(field.locator(".param-line")).toContainText("Not applied");
    await dropAtEnd(page, details, "email", formulaText(field));
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "input.amount > 1000 and input.email",
    );
    await expect(field.locator(".param-line")).not.toContainText("Not applied");
  });

  test("a field dropped on a template goes in as a placeholder", async ({
    page,
  }) => {
    const details = await openStepFixture(page, formulasFixture);
    await details.openWithKeyboard("review");
    const field = details.field("title");
    await dropAtEnd(page, details, "customerId", formulaText(field));
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "Review order of {{ input.amount }}{{ input.customerId }}",
    );
  });

  test("a field dropped on a lone reference replaces it", async ({ page }) => {
    const details = await openStepFixture(page, formulasFixture);
    await details.openWithKeyboard("review");
    const context = details.field("context");
    await dropAtEnd(
      page,
      details,
      "customerId",
      context.getByRole("button", { name: /^reference check/ }),
    );
    await expect(
      context.getByRole("button", { name: "reference Input Customer ID" }),
    ).toBeVisible();
  });

  test("a field dropped after a backslash keeps the backslash as text", async ({
    page,
  }) => {
    const workflow = parse(readFileSync(stepFixture, "utf8"));
    workflow.spec.steps.find(
      (step: { id: string }) => step.id === "summarize",
    ).prompt = {
      op: {
        name: "concat",
        args: [{ ref: "/input/email" }, { literal: "C:\\" }],
      },
    };
    const details = await openConnectedFixture(
      page,
      yamlFile("backslash.yaml", stringify(workflow)),
    );
    await details.openWithKeyboard("summarize");
    const field = details.field("prompt");
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "{{ input.email }}C:\\",
    );
    await dropAtEnd(page, details, "customerId", formulaText(field));
    await expect(formulaBox(field)).toHaveAttribute(
      "data-text",
      "{{ input.email }}C:\\\\{{ input.customerId }}",
    );
    await page.keyboard.press("Tab");
    await details.close();
    expect(
      parse(await sourceText(page)).spec.steps.find(
        (step: { id: string }) => step.id === "summarize",
      ).prompt,
    ).toEqual({
      op: {
        name: "concat",
        args: [
          { ref: "/input/email" },
          { literal: "C:\\" },
          { ref: "/input/customerId" },
        ],
      },
    });
  });
});
