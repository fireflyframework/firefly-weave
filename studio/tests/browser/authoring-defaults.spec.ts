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
import { test, expect } from "@playwright/test";
import { parse } from "yaml";
import { DesignerPage } from "./designer-po";
import {
  expectHitTarget,
  insertStep,
  newWorkflow,
  offline,
  selectChoice,
  sourceText,
} from "./support";

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe(`Required hints and consequences at ${viewport.width}`, () => {
    test.use({ viewport });
    for (const example of [
      {
        palette: "Wait for signal",
        field: "Signal name",
        hint: "message-received",
        error: "Enter a signal name.",
        value: "payment-arrived",
        key: "name",
      },
      {
        palette: "Fail",
        field: "Message",
        hint: "Provide a failure reason",
        error: "Enter a failure reason.",
        value: "Payment was rejected",
        key: "message",
      },
      {
        palette: "Decision table",
        field: "Table version",
        hint: "payment-policy@1.0.0",
        error: "Enter a table name and version, such as payment-policy@1.0.0.",
        value: "payment-policy@2.0.0",
        key: "uses",
      },
    ]) {
      test(`${example.palette} keeps its example as a hint`, async ({
        page,
      }) => {
        await offline(page);
        await newWorkflow(page);
        await insertStep(page, example.palette);
        const designer = new DesignerPage(page);
        if (!(await designer.inspector.isVisible()))
          await designer.selectStep((await designer.stepIds())[0]);
        const inspector = designer.inspector;
        const input = inspector.getByRole("textbox", {
          name: example.field,
          exact: true,
        });
        await expect(input).toHaveValue("");
        await expect(input).toHaveAttribute("placeholder", example.hint);
        await expect(
          inspector.getByText(example.error, { exact: true }),
        ).toBeHidden();
        await input.focus();
        await input.press("Tab");
        await expect(
          inspector.getByText(example.error, { exact: true }),
        ).toBeVisible();
        await expect(input).toHaveAttribute("aria-invalid", "true");
        await input.fill(example.value);
        await input.press("Tab");
        await expect(
          inspector.getByText(example.error, { exact: true }),
        ).toBeHidden();
        expect(parse(await sourceText(page)).spec.steps[0][example.key]).toBe(
          example.value,
        );
      });
    }
    for (const duration of [
      {
        palette: "Wait for time",
        label: "Duration",
        unit: "h",
        help: "The run pauses for 2 hours, then continues to the next step.",
        key: "durationSeconds",
        seconds: 7200,
      },
      {
        palette: "Wait for signal",
        label: "Timeout",
        unit: "min",
        help: "If nothing arrives within 2 minutes, the run ends as timed out.",
        key: "timeoutSeconds",
        seconds: 120,
      },
    ]) {
      test(`${duration.palette} explains its selected duration`, async ({
        page,
      }) => {
        await offline(page);
        await newWorkflow(page);
        await insertStep(page, duration.palette);
        const designer = new DesignerPage(page);
        if (!(await designer.inspector.isVisible()))
          await designer.selectStep((await designer.stepIds())[0]);
        const inspector = designer.inspector;
        if (duration.palette === "Wait for signal")
          await inspector
            .getByRole("textbox", { name: "Signal name", exact: true })
            .fill("payment-arrived");
        const amount = inspector.getByRole("spinbutton", {
          name: duration.label,
          exact: true,
        });
        await amount.fill("2");
        await selectChoice(
          inspector.getByRole("combobox", {
            name: `${duration.label} unit`,
            exact: true,
          }),
          duration.unit,
        );
        await expect(
          inspector.getByText(duration.help, { exact: true }),
        ).toBeVisible();
        await expect(amount).toHaveAccessibleDescription(duration.help);
        await amount.press("Tab");
        expect(parse(await sourceText(page)).spec.steps[0][duration.key]).toBe(
          duration.seconds,
        );
      });
    }
    test("parallel limit reads as a sentence and saves the chosen limit", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      await insertStep(page, "Parallel");
      const designer = new DesignerPage(page);
      if (!(await designer.inspector.isVisible()))
        await designer.selectStep((await designer.stepIds())[0]);
      const inspector = designer.inspector;
      const limit = inspector.getByRole("spinbutton", {
        name: "Run at most",
        exact: true,
      });
      await expect(limit).toHaveValue("2");
      await expect(
        inspector.getByText("branches at once", { exact: true }),
      ).toBeVisible();
      await expectHitTarget(limit);
      await limit.fill("3");
      await limit.press("Tab");
      expect(parse(await sourceText(page)).spec.steps[0].concurrency).toBe(3);
    });
  });
}
