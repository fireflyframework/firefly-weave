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
// Formula previews in step details: "= value" under a Mapped field at rest,
// computed from the trigger's test input.
import { expect, test } from "@playwright/test";
import {
  fieldLine,
  formulasFixture,
  replaceFormula,
  setTestInput,
} from "./formula-po";
import { box, intersects } from "./step-geometry";
import {
  openStepFixture,
  sizes,
  withLanguageFeatures,
} from "./step-details-po";

for (const size of sizes)
  test.describe(`formula previews at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a Mapped field shows its value under it once test input exists", async ({
      page,
    }) => {
      await withLanguageFeatures(page);
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("review");
      await expect(fieldLine(details, "title")).not.toContainText("=");
      await details.close();
      await setTestInput(page, details, {
        "Customer ID": "c-1042",
        Amount: "1500",
      });
      await details.openWithKeyboard("review");
      await expect(fieldLine(details, "title")).toHaveText(
        "= Review order of 1500",
      );
    });
    test("focusing a Mapped field opens its Result panel above the next field, never over it", async ({
      page,
    }) => {
      await withLanguageFeatures(page);
      const details = await openStepFixture(page, formulasFixture);
      await setTestInput(page, details, {
        "Customer ID": "c-1042",
        Amount: "1500",
      });
      await details.openWithKeyboard("review");
      const title = details.field("title");
      await title.locator("weave-formula-editor .cm-content").click();
      const result = title.getByRole("region", { name: "Result of Title" });
      await expect(result).toBeVisible();
      await expect(result).toContainText("Result");
      await expect(result).toContainText("Text");
      await expect(result).toContainText("From test input");
      await expect(result).toContainText("Review order of 1500");
      await expect(fieldLine(details, "title")).not.toContainText("=");
      const next = details.field("answers");
      const [panel, below] = [await box(result), await box(next)];
      expect(intersects(panel, below)).toBe(false);
      expect(panel.bottom).toBeLessThanOrEqual(below.y + 0.5);
      await page.keyboard.press("Tab");
      await expect(result).toHaveCount(0);
      await expect(fieldLine(details, "title")).toHaveText(
        "= Review order of 1500",
      );
    });

    test("the Result panel offers a tip while the formula reads no data", async ({
      page,
    }) => {
      await withLanguageFeatures(page);
      const details = await openStepFixture(page, formulasFixture);
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await replaceFormula(page, field, "1 < 2");
      const result = field.getByRole("region", { name: "Result of Fields" });
      await expect(result).toContainText("Yes/No");
      await expect(result).toContainText("true");
      await expect(result).toContainText(
        "Tip: drag an input field from the left to use it here.",
      );
    });

    test("typing and previewing a formula sends no test input anywhere and never shows a secret", async ({
      page,
    }) => {
      await withLanguageFeatures(page);
      const sent: string[] = [];
      page.on("request", (request) =>
        sent.push(`${request.url()} ${request.postData() ?? ""}`),
      );
      const details = await openStepFixture(page, formulasFixture);
      await setTestInput(page, details, {
        "Customer ID": "c-7Q2-marker",
        Amount: "1500",
      });
      await details.openWithKeyboard("flag");
      const field = details.field("value");
      await replaceFormula(page, field, 'coalesce(input.customerId, "none")');
      const result = field.getByRole("region", { name: "Result of Fields" });
      await expect(result).toContainText("c-7Q2-marker");
      // A formula whose value is text would turn the step's Fields into its
      // Text; coalesce keeps the field while it reads the secret.
      await replaceFormula(
        page,
        field,
        "coalesce(input.password, input.customerId)",
      );
      await expect(result).toContainText(
        "This formula reads a secret value, so Studio doesn't preview it.",
      );
      await expect(result).not.toContainText("c-7Q2-marker");
      await page.keyboard.press("Tab");
      await details.close();
      expect(sent.filter((line) => line.includes("c-7Q2-marker"))).toEqual([]);
    });
  });
