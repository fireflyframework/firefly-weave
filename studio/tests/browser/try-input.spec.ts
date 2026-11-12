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
// Trying a table, with no platform at all: Input takes the test values, each
// rule's mark and the result show as you type, then Weave's own check on
// this computer's Studio host.
import { expect, test, type Page } from "@playwright/test";
import { openStepFixture, sizes, yamlFile } from "./step-details-po";
import {
  DecisionGridPage,
  keepTableDraft,
  TablePanes,
} from "./decision-grid-po";
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

async function openTry(page: Page) {
  await keepTableDraft(page);
  const details = await openStepFixture(page, workflow);
  const calls = await localAuthoring(page);
  await details.open("score");
  const grid = new DecisionGridPage(page);
  await grid.open();
  const panes = new TablePanes(page);
  await panes.show("Input");
  await expect(panes.input).toBeVisible();
  return { details, grid, panes, calls };
}
const mark = (grid: DecisionGridPage, row: number) =>
  grid.root
    .locator(".dg-row")
    .nth(row + 1)
    .locator(".dg-mark");

for (const size of sizes)
  test.describe(`Trying a table at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("each rule shows whether it matches as you type, then Weave confirms", async ({
      page,
    }) => {
      const { grid, panes } = await openTry(page);
      await panes.type({ amount: "500", tier: "silver" });
      await panes.show("Parameters");
      await expect(mark(grid, 0)).toHaveAttribute("data-mark", "no-match");
      await expect(mark(grid, 0)).toContainText("Doesn't match");
      await expect(mark(grid, 1)).toHaveAttribute("data-mark", "match");
      await expect(mark(grid, 1)).toContainText("Matches");
      // First match: the rules after the one that won weren't checked.
      await expect(mark(grid, 2)).toHaveCount(0);
      await panes.show("Input");
      await panes.type({ amount: "2000", tier: "gold" });
      await panes.show("Output");
      await expect(panes.count).toHaveText("Rule 1 matched");
      await expect(panes.cards).toHaveCount(1);
      await expect(panes.cards.first()).toContainText("Rule 1 · big-gold");
      await expect(panes.output).toContainText(
        "First match: rules 2 and 3 weren't checked.",
      );
      await expect(panes.result).toContainText("5000");
      await expect(panes.checked).toBeVisible();
      await panes.show("Parameters");
      await expect(mark(grid, 0)).toHaveAttribute("data-mark", "match");
      await expect(mark(grid, 1)).toHaveCount(0);
    });

    test("Exactly one with two matches says which rules, and that a run stops", async ({
      page,
    }) => {
      const { grid, panes } = await openTry(page);
      await panes.show("Parameters");
      await grid.policy("Exactly one");
      await panes.show("Input");
      await panes.type({ amount: "6000", tier: "gold" });
      await panes.show("Output");
      await expect(panes.output.locator(".dg-callout")).toContainText(
        "Two rules matched but the policy allows exactly one (rules 1 and 3). A run with this input stops for review.",
      );
      await expect(panes.cards).toHaveCount(2);
      await expect(panes.output).toContainText(
        "No result for this input. Change one of the two rules so only one matches, or choose First match.",
      );
    });

    test("an input that breaks the input fields is refused by Weave", async ({
      page,
    }) => {
      const { panes } = await openTry(page);
      // payment-policy needs an amount.
      await panes.type({ tier: "gold" });
      await panes.show("Output");
      await expect(panes.output.locator(".dg-callout")).toContainText(
        "The input doesn't fit the table's input fields.",
      );
      await expect(panes.checked).toHaveAttribute("data-tone", "neutral");
      await expect(panes.result).toHaveCount(0);
    });

    test("editing while Weave checks shows only the newest answer", async ({
      page,
    }) => {
      const { panes, calls } = await openTry(page);
      // Weave's first check is held until the newer answer has shown.
      let release = () => {};
      const held = new Promise<void>((done) => (release = done));
      let arrived = () => {};
      const inFlight = new Promise<void>((done) => (arrived = done));
      let first = true;
      await page.route("**/studio/local/decision/evaluate", async (route) => {
        if (first) {
          first = false;
          arrived();
          await held;
        }
        await route.fallback();
      });
      const checks = () =>
        calls.filter((call) => call.path === "/studio/local/decision/evaluate")
          .length;
      await panes.type({ amount: "2000", tier: "gold" });
      await inFlight;
      await panes.type({ amount: "500" });
      await panes.show("Output");
      await expect(panes.count).toHaveText("Rule 2 matched");
      await expect(panes.checked).toBeVisible();
      // The older answer lands now, and changes nothing.
      const stale = page.waitForEvent(
        "requestfinished",
        (request) =>
          request.url().endsWith("/studio/local/decision/evaluate") &&
          (request.postDataJSON() as { input?: { amount?: unknown } })?.input
            ?.amount === 2000,
      );
      release();
      await stale;
      await expect.poll(checks).toBeGreaterThanOrEqual(2);
      await page.evaluate(
        () =>
          new Promise((done) =>
            requestAnimationFrame(() =>
              requestAnimationFrame(() => done(null)),
            ),
          ),
      );
      await expect(panes.count).toHaveText("Rule 2 matched");
      await expect(panes.cards.first()).toContainText("Rule 2 · small");
    });

    test("adding an input column keeps the values being tried", async ({
      page,
    }) => {
      const { grid, panes } = await openTry(page);
      await panes.type({ amount: "500", tier: "silver" });
      await panes.show("Parameters");
      await grid.addColumn("input", "region", "Text");
      await panes.show("Input");
      await expect(panes.field("region")).toBeVisible();
      await expect(panes.field("amount")).toHaveValue("500");
      await expect(panes.field("tier")).toHaveValue("silver");
      await panes.show("Output");
      await expect(panes.count).toHaveText("Rule 2 matched");
    });

    test("trying an input sends nothing to the platform", async ({ page }) => {
      const platform: string[] = [];
      page.on("request", (request) => {
        if (request.url().includes("/studio/api/"))
          platform.push(request.url());
      });
      const { panes, calls } = await openTry(page);
      platform.length = 0;
      await panes.type({ amount: "2000", tier: "gold" });
      await panes.show("Output");
      await expect(panes.checked).toBeVisible();
      expect(platform).toEqual([]);
      expect(calls.map((call) => call.path)).toContain(
        "/studio/local/decision/evaluate",
      );
    });

    test("the input you try isn't kept anywhere", async ({ page }) => {
      const logged: string[] = [];
      page.on("console", (message) => logged.push(message.text()));
      const { grid, panes } = await openTry(page);
      const marker = "zq-marker-7731";
      await panes.type({ amount: "2000", tier: marker });
      await panes.show("Output");
      await expect(panes.checked).toBeVisible();
      await panes.show("Parameters");
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
