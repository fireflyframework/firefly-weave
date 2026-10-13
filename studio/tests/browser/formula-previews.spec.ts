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
import { fieldLine, formulasFixture, setTestInput } from "./formula-po";
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
  });
