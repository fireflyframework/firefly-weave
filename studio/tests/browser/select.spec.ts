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
import { offline, newWorkflow, sourceText, expectHitTarget } from "./support";
import { DesignerPage } from "./designer-po";
import { expectInViewport } from "./ux-assertions";

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test.describe(`Themed choices at ${viewport.width}`, () => {
    test.use({ viewport });
    test("connection choice searches, pages, stays visible and writes the slot name", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      const connections = Object.fromEntries(
        Array.from({ length: 16 }, (_, i) => [
          `connection-${String(i + 1).padStart(2, "0")}`,
          { connector: "example@1.0.0", required: false },
        ]),
      );
      const workflow = {
        apiVersion: "weave/v1alpha1",
        kind: "Workflow",
        metadata: { name: "choices", version: "1.0.0" },
        spec: {
          inputSchema: { type: "object" },
          outputSchema: {},
          connections,
          steps: [
            {
              id: "call",
              kind: "action",
              uses: "example.run@1.0.0",
              connection: "connection-01",
              with: { literal: {} },
            },
          ],
          output: { literal: {} },
        },
      };
      const designer = new DesignerPage(page);
      await designer.setSource(JSON.stringify(workflow));
      await designer.selectStep("call");
      const field = page.getByRole("combobox", {
        name: "Connection slot",
        exact: true,
      });
      await field.scrollIntoViewIfNeeded();
      await field.click();
      const list = page.getByRole("listbox", {
        name: "Connection slot options",
        exact: true,
      });
      await expectInViewport(list, 8);
      await field.press("Home");
      const first = await field.getAttribute("aria-activedescendant");
      await field.press("PageDown");
      await expect(field).not.toHaveAttribute("aria-activedescendant", first!);
      const middle = await field.getAttribute("aria-activedescendant");
      expect(middle).not.toMatch(/option-16$/);
      await field.fill("connection-12");
      await expect(list.getByRole("option")).toHaveCount(1);
      await expectHitTarget(list.getByRole("option"));
      await field.press("Enter");
      await expect(list).toBeHidden();
      await expect(field).toHaveValue("connection-12");
      expect(parse(await sourceText(page)).spec.steps[0].connection).toBe(
        "connection-12",
      );
    });
  });
}
