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
// Pin output in the Output pane: the toggle in its header, the banner that
// says what a pin does, Unpin with Undo, P outside fields, kinds that compute
// their output, and a pin that survives a reload, follows a rename and goes
// with its step.
import { expect, test, type Page } from "@playwright/test";
import {
  fitCanvas,
  openStepFixture,
  sizes,
  withLanguageFeatures,
  withNewEditor,
} from "./step-details-po";
import { connected, defaultCatalog } from "./support";
import {
  banner,
  lookupContract,
  openTestDataFixture,
  PROJECT,
  TENANT,
  testDataFixture,
  outputPane,
  pinButton,
  routeDrafts,
} from "./test-data-po";

const toast = (page: Page, text: string) =>
  page.locator(".toast").filter({ hasText: text });

for (const size of sizes)
  test.describe(`pins at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("Pin output sits in the Output pane header and the banner says what a pin does", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      await details.open("lookup");
      const pane = await outputPane(details);
      await expect(pinButton(pane)).toHaveAttribute("aria-pressed", "false");
      await expect(pinButton(pane)).toHaveAttribute(
        "title",
        "Set test output or generate a sample to pin it.",
      );
      await pane.getByRole("button", { name: "Generate sample" }).click();
      await expect(
        pane.getByRole("button", { name: "Unpin output" }),
      ).toHaveAttribute("aria-pressed", "true");
      await expect(pane.locator(".pane-sub .pill")).toHaveText("Pinned");
      await expect(banner(pane)).toHaveText(
        "This data is pinned for test runs. Production runs ignore it. Unpin",
      );
      // Only the sentence is announced, never the buttons beside it.
      await expect(banner(pane).getByRole("status")).toHaveText(
        "This data is pinned for test runs. Production runs ignore it.",
      );
      await expect(
        banner(pane).getByRole("status").getByRole("button"),
      ).toHaveCount(0);
      // The sample never holds the secret field.
      await expect(
        pane.locator(".data-key", { hasText: /^tier$/ }),
      ).toBeVisible();
    });

    test("Unpin removes the pin, and Undo brings it back", async ({ page }) => {
      const details = await openTestDataFixture(page);
      await details.open("lookup");
      const pane = await outputPane(details);
      await pane.getByRole("button", { name: "Generate sample" }).click();
      await banner(pane).getByRole("button", { name: "Unpin" }).click();
      await expect(banner(pane)).toHaveCount(0);
      await expect(pane.getByText("No output yet")).toBeVisible();
      const undone = toast(page, "Unpinned lookup.");
      await undone.getByRole("button", { name: "Undo" }).click();
      await expect(banner(pane)).toBeVisible();
    });

    test("P pins only outside fields", async ({ page }) => {
      const details = await openTestDataFixture(page);
      await details.open("lookup");
      const pane = await outputPane(details);
      await pane.getByRole("button", { name: "Generate sample" }).click();
      await expect(banner(pane)).toBeVisible();
      // In a field, P types.
      await pane.getByRole("button", { name: "Search output" }).click();
      const field = pane.getByRole("textbox", { name: "Search output" });
      await expect(field).toBeFocused();
      await page.keyboard.press("p");
      await expect(field).toHaveValue("p");
      await expect(banner(pane)).toBeVisible();
      // Escape closes the search and leaves focus on its button: P pins or unpins.
      await page.keyboard.press("Escape");
      await page.keyboard.press("p");
      const shown = await outputPane(details);
      await expect(banner(shown)).toHaveCount(0);
      await expect(toast(page, "Unpinned lookup.")).toBeVisible();
      await page.keyboard.press("p");
      await expect(
        toast(page, "Set test output or generate a sample to pin it."),
      ).toBeVisible();
    });

    test("a transform offers to execute it, never to pin it", async ({
      page,
    }) => {
      const details = await openTestDataFixture(page);
      await details.open("score");
      const pane = await outputPane(details);
      await expect(pane).toContainText("Execute this step to see its output.");
      await expect(
        pane.getByRole("button", { name: "Set test output" }),
      ).toHaveCount(0);
      await expect(pinButton(pane)).toHaveCount(0);
    });

    test("a pin survives reload and follows a rename", async ({ page }) => {
      const details = await openTestDataFixture(page);
      // After the connection's own routes, so these answer first.
      const drafts = await routeDrafts(page);
      await details.open("lookup");
      const pane = await outputPane(details);
      await pane.getByRole("button", { name: "Generate sample" }).click();
      await expect(banner(pane)).toBeVisible();
      await details.close();
      // A change to the workflow: the draft saves itself on the platform
      // (at most every 10 s, and the pin itself just saved).
      await fitCanvas(page);
      await page.locator('[data-step="notify"]').first().click();
      await page.keyboard.press("ControlOrMeta+d");
      await expect.poll(() => drafts.size, { timeout: 15000 }).toBe(1);
      await page.reload();
      await details.open("lookup");
      await expect(banner(await outputPane(details))).toBeVisible();
      // Below 768 px the step's name heads the Parameters pane.
      if (size.width < 768)
        await details.dialog
          .getByRole("tablist", { name: "Step details panes" })
          .getByRole("tab", { name: "Parameters" })
          .click();
      await details.dialog
        .getByRole("button", { name: "Rename lookup" })
        .click();
      await details.dialog
        .getByRole("textbox", { name: "Step name" })
        .fill("find");
      await page.keyboard.press("Enter");
      await expect(details.dialogFor("find")).toBeVisible();
      await expect(banner(await outputPane(details))).toBeVisible();
    });

    test("Generate sample on output fields Studio can't sample says so", async ({
      page,
    }) => {
      const composed = structuredClone(lookupContract) as {
        spec: { outputSchema: unknown };
      };
      composed.spec.outputSchema = {
        anyOf: [
          { type: "object", properties: { a: { type: "string" } } },
          { type: "string" },
        ],
      };
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      await withNewEditor(page);
      const details = await openStepFixture(
        page,
        testDataFixture,
        async (p) => {
          const started = await connected(p, {
            catalog: [...defaultCatalog, { id: "a3", document: composed }],
            tenantId: TENANT,
            projectId: PROJECT,
          });
          await withLanguageFeatures(p);
          return started;
        },
      );
      await details.open("lookup");
      const pane = await outputPane(details);
      await pane.getByRole("button", { name: "Generate sample" }).click();
      await expect(pane.locator(".pane-problem")).toHaveText(
        "Can't make a sample for this output; set it by hand.",
      );
      await expect(banner(pane)).toHaveCount(0);
      expect(errors).toEqual([]);
    });
  });
