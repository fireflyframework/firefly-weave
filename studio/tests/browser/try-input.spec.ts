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
// Try an input, with no platform at all: each rule's mark and the result as
// you type, then Weave's own check on this computer's Studio host.
import { expect, test, type Page } from "@playwright/test";
import { openStepFixture, sizes, yamlFile } from "./step-details-po";
import { DecisionGridPage, paymentPolicy } from "./decision-grid-po";
import { hasPython, localAuthoring } from "./local-authoring";

test.skip(!hasPython, "Weave's check runs the repository's Python.");

const workflow = yamlFile(
  "orders.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: { name: orders, version: 1.0.0 }
spec:
  inputSchema:
    type: object
    properties:
      amount: { type: number }
      tier: { type: string }
  outputSchema: { type: object }
  steps:
    - id: score
      kind: decisionTable
      uses: payment-policy@1.2.1
      with: { literal: {} }
  output: { literal: {} }
`,
);

/** payment-policy 1.2.1, a draft kept on this computer before Studio opens. */
async function keepDraft(page: Page) {
  const document = paymentPolicy("1.2.1");
  const entry = {
    kind: "draft",
    name: "payment-policy",
    version: "1.2.1",
    base: "1.2.0",
    document,
    savedAt: "2026-10-27T08:00:00.000Z",
  };
  await page.addInitScript(
    (value) =>
      localStorage.setItem("local:weave.tableDraft.payment-policy", value),
    JSON.stringify(entry),
  );
}

async function openTry(page: Page) {
  await keepDraft(page);
  const details = await openStepFixture(page, workflow);
  const calls = await localAuthoring(page);
  await details.open("score");
  const grid = new DecisionGridPage(page);
  await grid.open();
  await grid.root.getByRole("button", { name: "Try an input" }).click();
  const panel = grid.root.getByRole("region", { name: "Try an input" });
  await expect(panel).toBeVisible();
  return { details, grid, panel, calls };
}
const mark = (grid: DecisionGridPage, row: number) =>
  grid.root
    .locator(".dg-row")
    .nth(row + 1)
    .locator(".dg-mark");

for (const size of sizes)
  test.describe(`Try an input at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("each rule shows whether it matches as you type, then Weave confirms", async ({
      page,
    }) => {
      const { grid, panel } = await openTry(page);
      await panel.getByLabel("amount").fill("2000");
      await panel.getByLabel("tier").fill("gold");
      await expect(mark(grid, 0)).toHaveAttribute("data-mark", "match");
      await expect(mark(grid, 0)).toContainText("Matches");
      await expect(mark(grid, 1)).toHaveAttribute("data-mark", "no-match");
      await expect(mark(grid, 1)).toContainText("Doesn't match");
      await expect(mark(grid, 2)).toHaveAttribute("data-mark", "no-match");
      await expect(panel.locator(".dg-try-output")).toContainText(
        '"limit": 5000',
      );
      await expect(panel).toContainText("Matched: big-gold");
      await expect(
        panel.locator(".pill", { hasText: "Checked by Weave" }),
      ).toBeVisible();
    });

    test("Exactly one with two matches says which rules", async ({ page }) => {
      const { grid, panel } = await openTry(page);
      await grid.policy("Exactly one");
      await panel.getByLabel("amount").fill("6000");
      await panel.getByLabel("tier").fill("gold");
      await expect(panel).toContainText(
        "Two rules matched but the policy allows exactly one (rules 1 and 3).",
      );
    });

    test("an input that breaks the input fields is refused by Weave", async ({
      page,
    }) => {
      const { panel } = await openTry(page);
      // payment-policy needs an amount.
      await panel.getByLabel("tier").fill("gold");
      await expect(panel).toContainText(
        "The input doesn't fit the table's input fields.",
      );
      await expect(
        panel.locator(".pill", { hasText: "Checked by Weave" }),
      ).toHaveCount(0);
    });

    test("editing while Weave checks shows only the newest answer", async ({
      page,
    }) => {
      const { panel, calls } = await openTry(page);
      let first = true;
      await page.route("**/studio/local/decision/evaluate", async (route) => {
        if (first) {
          first = false;
          await new Promise((resolve) => setTimeout(resolve, 1000));
        }
        await route.fallback();
      });
      await panel.getByLabel("amount").fill("2000");
      await panel.getByLabel("tier").fill("gold");
      await page.waitForTimeout(500);
      await panel.getByLabel("amount").fill("500");
      await expect(panel).toContainText("Matched: small");
      await expect(
        panel.locator(".pill", { hasText: "Checked by Weave" }),
      ).toBeVisible();
      await page.waitForTimeout(1200);
      await expect(panel).toContainText("Matched: small");
      expect(
        calls.filter((call) => call.path === "/studio/local/decision/evaluate")
          .length,
      ).toBeGreaterThanOrEqual(2);
    });

    test("trying an input sends nothing to the platform", async ({ page }) => {
      const platform: string[] = [];
      page.on("request", (request) => {
        if (request.url().includes("/studio/api/"))
          platform.push(request.url());
      });
      const { panel, calls } = await openTry(page);
      platform.length = 0;
      await panel.getByLabel("amount").fill("2000");
      await panel.getByLabel("tier").fill("gold");
      await expect(
        panel.locator(".pill", { hasText: "Checked by Weave" }),
      ).toBeVisible();
      expect(platform).toEqual([]);
      expect(calls.map((call) => call.path)).toContain(
        "/studio/local/decision/evaluate",
      );
    });

    test("the input you try isn't kept anywhere", async ({ page }) => {
      const logged: string[] = [];
      page.on("console", (message) => logged.push(message.text()));
      const { grid, panel } = await openTry(page);
      const marker = "zq-marker-7731";
      await panel.getByLabel("amount").fill("2000");
      await panel.getByLabel("tier").fill(marker);
      await expect(
        panel.locator(".pill", { hasText: "Checked by Weave" }),
      ).toBeVisible();
      await grid.back();
      const kept = await page.evaluate(() => ({
        local: Object.entries(localStorage).join("\n"),
        session: Object.entries(sessionStorage).join("\n"),
        url: location.href,
      }));
      expect(kept.local).not.toContain(marker);
      expect(kept.session).not.toContain(marker);
      expect(kept.url).not.toContain(marker);
      expect(logged.join("\n")).not.toContain(marker);
    });
  });
