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
// Execute workflow on the canvas: the editor bar's button, the trigger's own
// button and Ctrl/Cmd+Enter; the run played back on the tiles, 120 ms per
// step; badges and summary lines; one announcement when it ends.
import { expect, test, type Page } from "@playwright/test";
import {
  EXECUTE_CHAIN,
  executeWorkflowIn,
  executions,
  openExecuteWorkflow,
} from "./execute-po";
import { hasPython } from "./local-authoring";
import { sizes, yamlFile } from "./step-details-po";
import { outputPane } from "./test-data-po";

test.skip(!hasPython, "Create the Python development environment first.");

const CHAIN = yamlFile(
  "chain.yaml",
  `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: chain, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      amount: {type: number, title: Amount}
  outputSchema: {}
  steps:
    - id: first
      kind: transform
      value: {object: {amount: {ref: /input/amount}}}
    - id: second
      kind: transform
      value: {object: {copied: {ref: /steps/first/output/amount}}}
  output: {literal: null}
`,
);
const tile = (page: Page, id: string) =>
  page.locator(`.tile-node[data-tile="${id}"], [data-step="${id}"]`).first();
const badge = (page: Page, id: string) => tile(page, id).locator(".tile-badge");
const executeInBar = (page: Page) =>
  page.locator(".editor-bar").getByRole("button", { name: "Execute workflow" });

for (const size of sizes)
  test.describe(`Execute workflow at ${size.tag}`, () => {
    test.use({ viewport: { width: size.width, height: size.height } });

    test("the editor bar executes the workflow and every step shows its output", async ({
      page,
    }) => {
      const { details, calls } = await openExecuteWorkflow(page, CHAIN);
      await executeInBar(page).click();
      await expect.poll(() => executions(calls).length).toBe(1);
      expect(executions(calls)[0].body["target"]).toEqual({ mode: "workflow" });
      for (const id of ["first", "second"]) {
        await expect(badge(page, id)).toHaveAttribute("data-badge", "done");
        await expect(tile(page, id).locator(".tile-output")).toHaveText(
          "1 field",
        );
        await expect(tile(page, id).locator(".tile-body")).toHaveAttribute(
          "title",
          /Finished in the last test run/,
        );
      }
      await details.open("second");
      const pane = await outputPane(details);
      await expect(pane.locator(".pane-source-tag")).toHaveText("Simulated");
    });
  });

/** Records every change of the tiles' run state ("first:live", "second:done") with its time. */
async function recordTiles(page: Page) {
  await page.evaluate(() => {
    const seen: { at: number; state: string }[] = [];
    (window as unknown as { tileLog: typeof seen }).tileLog = seen;
    const read = () =>
      [...document.querySelectorAll("[data-step]")]
        .map((tile) => {
          const shape = tile.querySelector(".tile-shape");
          const run = shape?.classList.contains("live")
            ? "live"
            : (tile.querySelector(".tile-badge")?.getAttribute("data-badge") ??
              "");
          const pulse = shape?.classList.contains("pulse") ? "+pulse" : "";
          return `${tile.getAttribute("data-step")}:${run}${pulse}`;
        })
        .join(" ");
    new MutationObserver(() => {
      const state = read();
      if (seen.at(-1)?.state !== state)
        seen.push({ at: performance.now(), state });
    }).observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["class", "data-badge"],
    });
  });
  return () =>
    page.evaluate(
      () =>
        (window as unknown as { tileLog: { at: number; state: string }[] })
          .tileLog,
    );
}

test("tiles light up one after another, then show how the run ended", async ({
  page,
}) => {
  const { calls } = await openExecuteWorkflow(page, CHAIN);
  const log = await recordTiles(page);
  await executeInBar(page).click();
  await expect.poll(() => executions(calls).length).toBe(1);
  await expect(badge(page, "second")).toHaveAttribute("data-badge", "done");
  const states = await log();
  const first = states.findIndex((s) => s.state === "first:live second:");
  const second = states.findIndex((s) => s.state === "first:done second:live");
  const end = states.findIndex((s) => s.state === "first:done second:done");
  expect(first).toBeGreaterThanOrEqual(0);
  expect(second).toBeGreaterThan(first);
  expect(end).toBeGreaterThan(second);
  // 120 ms per step (timers may run late, never early).
  expect(states[second].at - states[first].at).toBeGreaterThanOrEqual(110);
  expect(states.some((s) => s.state.includes("+pulse"))).toBe(false);
});

test("with reduced motion the end shows at once", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const { calls } = await openExecuteWorkflow(page, CHAIN);
  const log = await recordTiles(page);
  await executeInBar(page).click();
  await expect.poll(() => executions(calls).length).toBe(1);
  await expect(badge(page, "second")).toHaveAttribute("data-badge", "done");
  const states = await log();
  expect(states.some((s) => s.state.includes("live"))).toBe(false);
});

test("the trigger's button and Ctrl/Cmd+Enter execute the workflow", async ({
  page,
}) => {
  const { calls } = await openExecuteWorkflow(page, CHAIN);
  const trigger = page.locator(".tile-node[data-trigger]").first();
  await trigger.hover();
  await page
    .locator(".tile-toolbar")
    .getByRole("button", { name: "Execute workflow" })
    .click();
  await expect.poll(() => executions(calls).length).toBe(1);
  await expect(badge(page, "second")).toHaveAttribute("data-badge", "done");
  await tile(page, "first").locator(".tile-body").click();
  await page.keyboard.press("ControlOrMeta+Enter");
  await expect.poll(() => executions(calls).length).toBe(2);
});

test("a decision's tile says which path it took", async ({ page }) => {
  const { calls } = await openExecuteWorkflow(
    page,
    yamlFile(
      "routed.yaml",
      `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: routed, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      amount: {type: number, title: Amount}
  outputSchema: {}
  steps:
    - id: route
      kind: switch
      cases:
        - when: {op: {name: gte, args: [{ref: /input/amount}, {literal: 1}]}}
          steps: []
          output: {literal: {tier: big}}
      default: {steps: [], output: {literal: null}}
  output: {literal: null}
`,
    ),
  );
  await executeInBar(page).click();
  await expect.poll(() => executions(calls).length).toBe(1);
  await expect(tile(page, "route").locator(".tile-output")).toHaveText(
    /^Path: .*1/,
  );
});

test("a blocked run marks the step that needs data", async ({ page }) => {
  const { calls } = await openExecuteWorkflow(
    page,
    yamlFile(
      "blocked.yaml",
      `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: blocked, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      customerId: {type: string, title: Customer ID}
  outputSchema: {}
  steps:
    - id: lookup
      kind: action
      uses: crm.lookup@1.0.0
      with: {object: {customerId: {ref: /input/customerId}}}
  output: {literal: null}
`,
    ),
  );
  await executeInBar(page).click();
  await expect.poll(() => executions(calls).length).toBe(1);
  await expect(tile(page, "lookup").locator(".tile-body")).toHaveAttribute(
    "title",
    /Step lookup needs test data\./,
  );
  await expect(tile(page, "lookup").locator(".tile-shape.pulse")).toHaveCount(
    0,
  );
});

test("the live region speaks once", async ({ page }) => {
  const { calls } = await openExecuteWorkflow(page, CHAIN);
  const status = page.locator(".canvas-run-status");
  const said: string[] = [];
  await page.exposeFunction("heard", (text: string) => said.push(text));
  await status.evaluate((element) => {
    new MutationObserver(() => {
      const text = element.textContent?.trim() ?? "";
      if (text) (window as unknown as { heard(t: string): void }).heard(text);
    }).observe(element, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  });
  await executeInBar(page).click();
  await expect.poll(() => executions(calls).length).toBe(1);
  await expect(status).toHaveText("Executed 2 steps.");
  await page.waitForTimeout(500);
  expect(said).toEqual(["Executed 2 steps."]);
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 360, height: 740 },
])
  test.describe(`the run in view at ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    // Every step that ran on a wide window; on a phone, where they don't all
    // fit at a readable zoom, the step the run ended at.
    const shown =
      viewport.width >= 768 ? ["first", "second", "third"] : ["third"];
    test("after Execute workflow the steps that ran are in sight beside the run log", async ({
      page,
    }) => {
      await openExecuteWorkflow(page, EXECUTE_CHAIN);
      await executeWorkflowIn(page);
      await expect(page.locator("weave-run-log")).toBeVisible();
      for (const id of ["first", "second", "third"])
        await expect(badge(page, id)).toHaveAttribute("data-badge", "done");
      const canvas = await page.locator(".canvas-panel .canvas").boundingBox();
      const log = await page.locator("weave-run-log").boundingBox();
      await expect
        .poll(async () => {
          const missing: string[] = [];
          for (const id of shown) {
            const box = await tile(page, id)
              .locator(".tile-body")
              .boundingBox();
            if (
              !box ||
              !canvas ||
              !log ||
              box.x < canvas.x - 0.5 ||
              box.y < canvas.y - 0.5 ||
              box.x + box.width > canvas.x + canvas.width + 0.5 ||
              box.y + box.height > canvas.y + canvas.height + 0.5 ||
              (box.x < log.x + log.width &&
                box.x + box.width > log.x &&
                box.y < log.y + log.height &&
                box.y + box.height > log.y)
            )
              missing.push(id);
          }
          return missing;
        })
        .toEqual([]);
    });
  });
