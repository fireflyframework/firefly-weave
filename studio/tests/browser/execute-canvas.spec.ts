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
  EXECUTE_ASKING,
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

/** Each tile as drawn (body, label, summary line): wholly in the canvas's free area, wholly out, or cut. */
async function tilePlaces(page: Page) {
  return page.evaluate(() => {
    const canvas = document.querySelector(".canvas-panel .canvas")!;
    const frame = canvas.getBoundingClientRect();
    let bottom = frame.bottom;
    let right = frame.right;
    const log = document.querySelector("weave-run-log");
    if (log) {
      const box = log.getBoundingClientRect();
      if (box.top <= frame.top + 1) right = Math.min(right, box.left);
      else bottom = Math.min(bottom, box.top);
    }
    for (const tools of canvas.querySelectorAll(
      ".canvas-tools, .canvas-v2-tools, .canvas-v2-tools-menu",
    )) {
      const box = tools.getBoundingClientRect();
      if (box.width && box.height) bottom = Math.min(bottom, box.top);
    }
    const places: Record<string, "in" | "out" | "cut"> = {};
    for (const node of canvas.querySelectorAll<HTMLElement>(".tile-node")) {
      const boxes = [
        ...node.querySelectorAll(".tile-body, .tile-label, .tile-output"),
      ]
        .map((part) => part.getBoundingClientRect())
        .filter((box) => box.width > 0 && box.height > 0);
      if (!boxes.length) continue;
      const left = Math.min(...boxes.map((b) => b.left));
      const top = Math.min(...boxes.map((b) => b.top));
      const end = Math.max(...boxes.map((b) => b.right));
      const lower = Math.max(...boxes.map((b) => b.bottom));
      const inside =
        left >= frame.left - 0.5 &&
        top >= frame.top - 0.5 &&
        end <= right + 0.5 &&
        lower <= bottom + 0.5;
      const outside =
        end <= frame.left + 0.5 ||
        left >= right - 0.5 ||
        lower <= frame.top + 0.5 ||
        top >= bottom - 0.5;
      places[node.dataset["tile"] ?? ""] = inside
        ? "in"
        : outside
          ? "out"
          : "cut";
    }
    return places;
  });
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 360, height: 740 },
])
  test.describe(`the run in view at ${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    const wide = viewport.width >= 768;
    test("after Execute workflow no tile is cut, and the steps that ran are in sight", async ({
      page,
    }) => {
      await openExecuteWorkflow(page, EXECUTE_CHAIN);
      await executeWorkflowIn(page);
      for (const id of ["first", "second", "third"])
        await expect(badge(page, id)).toHaveAttribute("data-badge", "done");
      await expect
        .poll(async () => {
          const places = await tilePlaces(page);
          const cut = Object.keys(places).filter((id) => places[id] === "cut");
          const ran = (wide ? ["first", "second", "third"] : ["third"]).filter(
            (id) => places[id] !== "in",
          );
          const trigger = Object.keys(places).filter(
            (id) => id.startsWith("$trigger:") && wide && places[id] !== "in",
          );
          return { cut, ran, trigger };
        })
        .toEqual({ cut: [], ran: [], trigger: [] });
    });
    test("a run waiting for a signal keeps the trigger and the waiting step whole", async ({
      page,
    }) => {
      await openExecuteWorkflow(page, EXECUTE_ASKING);
      await executeWorkflowIn(page);
      await expect(page.locator(".run-log-status")).toContainText("Waiting");
      await expect
        .poll(async () => {
          const places = await tilePlaces(page);
          const cut = Object.keys(places).filter((id) => places[id] === "cut");
          return { cut, paid: places["paid"] };
        })
        .toEqual({ cut: [], paid: "in" });
      // A signal still waiting hasn't received anything.
      await expect(tile(page, "paid").locator(".tile-output")).toHaveCount(0);
    });
  });
