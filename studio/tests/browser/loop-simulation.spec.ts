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
// Execute workflow with a loop, simulated by the repository's own Python
// through the local authoring routes: the frame's header follows the items
// as the playback runs ("2 of 3"), then says how the loop ended ("3 of 3",
// "failed at item 3"); under the frame, how many items it had.
import { expect, test, type Page } from "@playwright/test";
import { openExecuteWorkflow } from "./execute-po";
import { hasPython } from "./local-authoring";
import { StepDetailsPage, yamlFile } from "./step-details-po";

test.skip(!hasPython, "Create the Python development environment first.");

const workflow = (name: string, rows: unknown[], loop: string) =>
  yamlFile(
    `${name}.yaml`,
    `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: ${name}, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {}
  steps:
    - id: fetch
      kind: transform
      value: {literal: {rows: ${JSON.stringify(rows)}}}
    - id: orders
      kind: forEach
      items: {ref: /steps/fetch/output/rows}
${loop}    - id: summarize
      kind: transform
      value: {literal: done}
  output: {literal: null}
`,
  );
/** Three orders, two at a time: check, then send. */
const SENT = workflow(
  "order-notify",
  [{ name: "a" }, { name: "b" }, { name: "c" }],
  `      concurrency: 2
      maxItems: 1000
      collect: all
      body:
        steps:
          - {id: check, kind: transform, value: {ref: /item}}
          - {id: send, kind: transform, value: {ref: /item/name}}
        output: {ref: /item}
`,
);
/** Four orders; the third isn't ok, so its path ends in a failure. */
const REJECTED = workflow(
  "order-reject",
  [
    { name: "a", ok: true },
    { name: "b", ok: true },
    { name: "c", ok: false },
    { name: "d", ok: true },
  ],
  `      concurrency: 1
      maxItems: 1000
      collect: all
      body:
        steps:
          - {id: check, kind: transform, value: {ref: /item}}
          - id: route
            kind: switch
            cases:
              - when: {ref: /item/ok}
                steps: [{id: send, kind: transform, value: {ref: /item/name}}]
                output: {literal: null}
            default:
              steps: [{id: reject, kind: fail, code: SMTP_REJECTED, message: rejected}]
              output: {literal: null}
        output: {ref: /item}
`,
);
/** Five orders; Skip empty results leaves out the two without a note. */
const SKIPPED = workflow(
  "order-notes",
  [{ note: "x" }, { note: null }, { note: "y" }, { note: null }, { note: "z" }],
  `      concurrency: 1
      maxItems: 1000
      collect: nonNull
      body:
        steps:
          - {id: check, kind: transform, value: {ref: /item}}
        output: {ref: /item/note}
`,
);

/** Three orders, but Up to 2: the loop fails before its first item, as a real run does. */
const OVER = workflow(
  "order-limit",
  [{ name: "a" }, { name: "b" }, { name: "c" }],
  `      concurrency: 1
      maxItems: 2
      collect: all
      body:
        steps:
          - {id: check, kind: transform, value: {ref: /item}}
        output: {ref: /item}
`,
);

const header = (page: Page) =>
  page.locator('.canvas-v2 .tile-node[data-tile="orders"] .frame-header');
const runText = (page: Page) => header(page).locator(".frame-run");
const execute = (page: Page) =>
  page
    .locator(".editor-bar")
    .getByRole("button", { name: "Execute workflow" })
    .click();

test.describe("A loop in Execute workflow at 1440x900", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the frame follows the items as the run plays, then says how many finished", async ({
    page,
  }) => {
    await openExecuteWorkflow(page, SENT);
    await execute(page);
    // The summary under the frame waits for the playback's end: not while
    // summarize, after the loop, still plays.
    const midway = page.waitForFunction(
      () => {
        const done =
          document
            .querySelector('.tile-node[data-tile="orders"] .frame-run')
            ?.getAttribute("data-tone") === "done";
        const later = document.querySelector(
          '.tile-node[data-tile="summarize"] .live',
        );
        return done && later
          ? {
              summary: !!document.querySelector(
                '.tile-node[data-tile="orders"] .tile-output',
              ),
            }
          : null;
      },
      undefined,
      { polling: "raf", timeout: 10_000 },
    );
    // While the items run, the frame is live and counts them.
    await page.waitForFunction(
      () =>
        document.querySelector(
          '.tile-node[data-tile="orders"] .frame-run[data-tone="live"]',
        ) !== null,
      undefined,
      { polling: "raf", timeout: 10_000 },
    );
    expect(await (await midway).jsonValue()).toEqual({ summary: false });
    await expect(runText(page)).toHaveText("· 3 of 3");
    await expect(runText(page)).toHaveAttribute("data-tone", "done");
    // The at-a-time suffix steps aside while a run status shows.
    await expect(header(page).locator(".frame-suffix")).toHaveCount(0);
    await expect(header(page)).toHaveAccessibleName(
      /For each item in steps\.fetch\.output\.rows · 3 of 3/,
    );
    await expect(
      page.locator('.canvas-v2 .tile-node[data-tile="orders"] .tile-output'),
    ).toHaveText("3 items");
  });

  test("a loop that fails names the item on its frame and when the run ends", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openExecuteWorkflow(page, REJECTED);
    await execute(page);
    await expect(runText(page)).toHaveText("· failed at item 3");
    await expect(runText(page)).toHaveAttribute("data-tone", "failed");
    await expect(
      page.locator('.canvas-v2 .loop-frame[data-frame="orders"]'),
    ).toHaveClass(/\berror\b/);
    await expect(
      page.locator('.canvas-v2 .tile-node[data-tile="reject"] .tile-badge'),
    ).toHaveAttribute("data-badge", "failed");
    // The frame says it failed in its header, not again with a badge.
    await expect(
      page.locator('.canvas-v2 .tile-node[data-tile="orders"] .tile-badge'),
    ).toHaveCount(0);
    await expect(page.locator(".canvas-run-alert")).toHaveText(
      "orders failed at item 3.",
    );
    // The run log says where it failed, item by item.
    const log = page.getByRole("region", { name: "Run log" });
    await expect(log.locator(".run-log-status")).toHaveText(
      "Failed at reject, item 3",
    );
    await expect(log.locator('[data-entry="orders"]')).toContainText(
      "2 of 4 items finished",
    );
    const items = log.getByRole("list", { name: "Items of orders" });
    await expect(items.locator(":scope > li > .run-log-item")).toHaveText([
      /Item 1\s*check, route, send/,
      /Item 2\s*check, route, send/,
      /Item 3\s*Failed at reject/,
    ]);
    // The failed item shows open: the steps it went through, then the error.
    const steps = items.getByRole("list", { name: "Steps of Item 3" });
    await expect(steps.getByRole("button")).toHaveText([
      /check\s*2 fields/,
      /^\s*route\s*$/,
      /reject\s*SMTP_REJECTED/,
    ]);
    await expect(steps.getByRole("button").nth(2)).toHaveAttribute(
      "data-status",
      "failed",
    );
    await expect(items.locator(".run-log-item-message")).toHaveText("rejected");
    await expect(items.locator(".run-log-not-run")).toHaveText(
      "Item 4 didn't run.",
    );
    // A step row opens that step on the item it ran in.
    await steps.getByRole("button", { name: /^check/ }).click();
    const details = new StepDetailsPage(page);
    await expect(details.dialog).toBeVisible();
    await expect(
      details.dialog
        .locator("weave-input-pane")
        .getByRole("group", { name: "Items of orders" })
        .getByRole("button", { name: /Choose an item$/ }),
    ).toHaveAccessibleName("Item 3 of 4. Choose an item");
    // The steps inside the loop show only under its items.
    await expect(log.locator('[data-entry="send"]')).toHaveCount(0);
  });

  test("a loop over its Up to fails before its first item and says why", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openExecuteWorkflow(page, OVER);
    await execute(page);
    await expect(runText(page)).toHaveText("· failed: over Up to");
    await expect(runText(page)).toHaveAttribute("data-tone", "failed");
    await expect(
      page.locator('.canvas-v2 .loop-frame[data-frame="orders"]'),
    ).toHaveClass(/\berror\b/);
    await expect(page.locator(".canvas-run-alert")).toHaveText(
      "orders failed: 3 items, more than Up to allows (2).",
    );
    // The run log says it in the same words, and what to do.
    const log = page.getByRole("region", { name: "Run log" });
    await expect(log.locator(".run-log-status")).toHaveText(
      "Failed at orders: more items than Up to",
    );
    await expect(log.locator('[data-entry="orders"]')).toContainText(
      "3 items, Up to 2",
    );
    await expect(log.locator(".run-log-loop-message")).toHaveText(
      "This list has 3 items, more than Up to allows (2). Raise Up to or shorten the list.",
    );
  });

  test("a loop that skips empty results says how many it left out", async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openExecuteWorkflow(page, SKIPPED);
    await execute(page);
    await expect(runText(page)).toHaveText("· 5 of 5");
    const summary = page.locator(
      '.canvas-v2 .tile-node[data-tile="orders"] .tile-output',
    );
    await expect(summary).toHaveText("5 items, 2 skipped");
    // Zoomed out to 40%, it stays whole at screen size, past the frame if need be.
    const tools = page.locator(".canvas-v2");
    for (let i = 0; i < 12; i++) {
      const level = await tools.locator(".zoom-level").textContent();
      if (parseInt(level ?? "100", 10) <= 40) break;
      await tools.getByRole("button", { name: "Zoom out" }).click();
    }
    await expect(tools.locator(".zoom-level")).toHaveText(
      /^\s*(40|3\d|2\d)%\s*$/,
    );
    expect(
      await summary.evaluate(
        (element) => element.scrollWidth <= element.clientWidth + 1,
      ),
    ).toBe(true);
    // Its full text is in the loop's name.
    await expect(header(page)).toHaveAccessibleName(
      /· 5 of 5, 5 items, 2 skipped/,
    );
  });
});

for (const size of [
  { width: 1440, height: 900 },
  { width: 768, height: 1024 },
]) {
  test.describe(`Execute workflow's framing at ${size.width}x${size.height}`, () => {
    test.use({ viewport: size });

    test("the loop that failed, else its failed step, is in view beside the run log", async ({
      page,
    }) => {
      await page.emulateMedia({ reducedMotion: "reduce" });
      await openExecuteWorkflow(page, REJECTED);
      const panel = page.locator("section.inspector");
      const wide = size.width >= 1280;
      if (wide) await expect(panel).toBeVisible();
      await execute(page);
      await expect(runText(page)).toHaveText("· failed at item 3");
      const log = page.getByRole("region", { name: "Run log" });
      await expect(log).toBeVisible();
      // Below 1600 px, the run log takes the Workflow panel's room.
      if (wide) await expect(panel).toBeHidden();
      const canvas = page.locator(".canvas-v2");
      await expect
        .poll(async () => {
          const area = (await canvas.boundingBox())!;
          const logBox = (await log.boundingBox())!;
          // The loop where it fits at 55%; else the step that failed.
          const shown = (await page
            .locator(
              wide
                ? '.canvas-v2 .loop-frame[data-frame="orders"]'
                : '.canvas-v2 .tile-node[data-tile="reject"] .tile-body',
            )
            .boundingBox())!;
          const right = logBox.y <= area.y + 1 ? logBox.x : area.x + area.width;
          const bottom =
            logBox.y <= area.y + 1 ? area.y + area.height : logBox.y;
          return (
            shown.x >= area.x &&
            shown.x + shown.width <= right &&
            shown.y >= area.y &&
            shown.y + shown.height <= bottom
          );
        })
        .toBe(true);
      const level = await canvas.locator(".zoom-level").textContent();
      expect(parseInt(level ?? "0", 10)).toBeGreaterThanOrEqual(55);
      // The panel comes back when the run log closes.
      if (wide) {
        await log.getByRole("button", { name: /^Close/ }).click();
        await expect(panel).toBeVisible();
      }
    });
  });
}
