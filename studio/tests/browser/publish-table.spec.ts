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
// Publish table from the grid, and the workflow that waits for it: a
// workflow that uses a table draft can't be published until the table is.
import { expect, test, type Page } from "@playwright/test";
import { parse } from "yaml";
import { command, sourceText } from "./support";
import { openStepFixture, sizes } from "./step-details-po";
import {
  DecisionGridPage,
  ordersWorkflow,
  paymentPolicy,
  tablePlatform,
  type TablePlatformOptions as Options,
} from "./decision-grid-po";
import { hasPython } from "./local-authoring";

test.skip(!hasPython, "The platform's check runs the repository's compiler.");

/** Opens orders' score step on the grid and starts payment-policy 1.2.1 with one change. */
async function startDraft(page: Page, options: Options = {}) {
  const platform = await tablePlatform(page, options);
  const details = await openStepFixture(page, ordersWorkflow, async () => {
    // Already connected.
  });
  await details.open("score");
  const grid = new DecisionGridPage(page);
  await grid.open();
  await grid.edit(1, 1, "<= 900");
  await expect(grid.root).toContainText(
    "payment-policy 1.2.1 · on this computer",
  );
  return { ...platform, details, grid };
}

for (const size of sizes)
  test.describe(`Publish table at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("a workflow that uses a table draft can't be published until the table is", async ({
      page,
    }) => {
      const { details, grid, posts } = await startDraft(page);
      await grid.back();
      await details.close();
      await command(page, "Publish…");
      await expect(
        page
          .getByText(
            "Publish payment-policy@1.2.1 before publishing this workflow.",
          )
          .first(),
        // The platform's check runs the repository's compiler.
      ).toBeVisible({ timeout: 15_000 });
      expect(posts.filter((post) => post.path === "workflows")).toEqual([]);
      await details.open("score");
      await grid.open();
      await grid.root.getByRole("button", { name: "Publish table" }).click();
      await expect(grid.root).toContainText("payment-policy 1.2.1 · published");
      await expect(page.locator(".toast-region")).toContainText(
        "Published payment-policy 1.2.1.",
      );
      await grid.back();
      await details.close();
      await command(page, "Publish…");
      await page
        .getByRole("dialog")
        .getByRole("button", { name: "Publish version" })
        .click();
      await expect
        .poll(() => posts.filter((post) => post.path === "workflows").length)
        .toBe(1);
      expect(parse(await sourceText(page)).spec.steps[0].uses).toBe(
        "payment-policy@1.2.1",
      );
    });

    test("publishing a table with problems asks to fix them first", async ({
      page,
    }) => {
      const { grid, posts } = await startDraft(page);
      await grid.edit(1, 0, "big-gold");
      await grid.root.getByRole("button", { name: "Publish table" }).click();
      await expect(grid.root).toContainText("Fix 1 problem before publishing.");
      expect(posts).toEqual([]);
    });

    test("a version published meanwhile moves the draft to the next one", async ({
      page,
    }) => {
      const { details, grid } = await startDraft(page, {
        takenLater: ["1.2.1"],
      });
      await grid.root.getByRole("button", { name: "Publish table" }).click();
      await expect(grid.root).toContainText(
        "payment-policy 1.2.1 was published by someone else. Your changes start 1.2.2.",
      );
      await expect(grid.root).toContainText(
        "payment-policy 1.2.2 · on this computer",
      );
      await grid.back();
      await details.close();
      expect(parse(await sourceText(page)).spec.steps[0].uses).toBe(
        "payment-policy@1.2.2",
      );
    });

    test("a role that can't publish is told so by the platform", async ({
      page,
    }) => {
      const { grid, posts } = await startDraft(page, { publishStatus: 403 });
      const button = grid.root.getByRole("button", { name: "Publish table" });
      await button.click();
      await expect(grid.root).toContainText(
        "Your role can't publish tables in this project.",
      );
      await expect(button).toBeVisible();
      expect(
        posts.filter((post) => post.path === "decision-tables"),
      ).toHaveLength(1);
    });
  });

test("offline, Publish table says to connect", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const entry = {
    kind: "draft",
    name: "payment-policy",
    version: "1.2.1",
    base: "1.2.0",
    document: paymentPolicy("1.2.1"),
    savedAt: "2026-10-27T08:00:00.000Z",
  };
  await page.addInitScript(
    (value) =>
      localStorage.setItem("local:weave.tableDraft.payment-policy", value),
    JSON.stringify(entry),
  );
  const details = await openStepFixture(
    page,
    ordersWorkflow.buffer
      ? {
          ...ordersWorkflow,
          buffer: Buffer.from(
            ordersWorkflow.buffer.toString().replace("@1.2.0", "@1.2.1"),
          ),
        }
      : ordersWorkflow,
  );
  await details.open("score");
  const grid = new DecisionGridPage(page);
  await grid.open();
  await grid.root.getByRole("button", { name: "Publish table" }).click();
  await expect(grid.root).toContainText(
    "Connect to a platform to publish this table.",
  );
});
