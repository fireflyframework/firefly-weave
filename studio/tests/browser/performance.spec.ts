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
import { test, expect, type Page } from "@playwright/test";
import { cpus, platform, arch } from "node:os";
import { useNewEditor } from "./canvas-po";
import { openStepFixture, withLanguageFeatures } from "./step-details-po";
import {
  formulaBox,
  formulaText,
  formulasFixture,
  setTestInput,
} from "./formula-po";
import { calibrateFormulaWork } from "./formula-calibration";
import { timeFingerprintWork } from "./fingerprint-calibration";
import {
  EXECUTE_BUDGET_MS,
  EXECUTE_RUNS,
  longWorkflow,
  scaledMs,
} from "./execute-calibration";
import { executions, openExecuteWorkflow, pinSample } from "./execute-po";
import { withClassicDesigner } from "./support";

/** One frame at 60 Hz, in milliseconds. */
const FRAME_BUDGET_MS = 16.7;
/**
 * Animation-frame intervals are quantized to the display's refresh, so a
 * steady 60 Hz reads between 16.6 and 16.8 ms. A frame that misses a refresh
 * reads 33 ms or more, which this allowance still fails once such frames are
 * more than 5 % of the frames that handled a wheel event (the p95).
 */
const VSYNC_ALLOWANCE_MS = 1;
const OPEN_BUDGET_MS = 1000;

/** Answers the session check as an offline Studio that is already paired. */
async function mockSession(page: Page) {
  await page.route("**/studio/session", (r) =>
    r.fulfill({
      json: {
        paired: true,
        csrfToken: "test",
        mode: "offline",
        profile: null,
        version: "1",
      },
    }),
  );
}

/** Opens Studio at 1600x1000 on a new, empty workflow. */
async function openNewWorkflow(page: Page) {
  await page.setViewportSize({ width: 1600, height: 1000 });
  await mockSession(page);
  await page.goto("/");
  await page.getByRole("button", { name: "New workflow", exact: true }).click();
}

/** A workflow of `count` transform steps in one sequence, as source text. */
function transformSteps(count: number) {
  return JSON.stringify({
    apiVersion: "weave/v1alpha1",
    kind: "Workflow",
    metadata: { name: "performance-measurement", version: "1.0.0" },
    spec: {
      inputSchema: { type: "object" },
      outputSchema: { type: "object" },
      steps: Array.from({ length: count }, (_, i) => ({
        id: "step-" + i,
        kind: "transform",
        value: { literal: {} },
      })),
      output: { literal: {} },
    },
  });
}

/** Types a workflow of `count` steps into Source. */
async function enterSteps(page: Page, count: number) {
  await page.getByRole("tab", { name: "Source", exact: true }).click();
  await page
    .getByRole("textbox", { name: "Workflow source" })
    .fill(transformSteps(count));
}

async function applyChanges(page: Page) {
  await page
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
}

/** Intervals between animation frames, smallest first, in milliseconds. */
interface FrameIntervals {
  /** Every frame while the gesture ran, idle ones too. */
  all: number[];
  /** Only the frames that follow a wheel event (see `sampleFrames`). */
  afterWheel: number[];
}

/**
 * Runs `gesture` while recording the time between animation frames. Between
 * two wheel events the page waits for Playwright, and those idle frames are
 * always fast, so the statistic that gates a pan or zoom is `afterWheel`: a
 * wheel event is handled just before a frame's callbacks, and the work it
 * caused (layout, paint) delays the next frame, so the interval kept is the
 * one that starts at the first frame after the event. The first interval runs
 * from the start of sampling to the next frame, so it is not a frame and is
 * left out.
 */
async function sampleFrames(
  page: Page,
  gesture: () => Promise<void>,
): Promise<FrameIntervals> {
  await page.evaluate(() => {
    const w = window as unknown as { stopSampling: () => FrameIntervals };
    const all: number[] = [];
    const afterWheel: number[] = [];
    let wheeled = false;
    let armed = false;
    let last: number | undefined;
    const onWheel = () => (wheeled = true);
    const frame = (time: number) => {
      if (last !== undefined) {
        all.push(time - last);
        if (armed) afterWheel.push(time - last);
      }
      armed = wheeled;
      wheeled = false;
      last = time;
      handle = requestAnimationFrame(frame);
    };
    window.addEventListener("wheel", onWheel, { capture: true, passive: true });
    let handle = requestAnimationFrame(frame);
    w.stopSampling = () => {
      cancelAnimationFrame(handle);
      window.removeEventListener("wheel", onWheel, { capture: true });
      return { all, afterWheel };
    };
  });
  await gesture();
  await page.waitForTimeout(100);
  const frames = await page.evaluate(() =>
    (
      window as unknown as { stopSampling: () => FrameIntervals }
    ).stopSampling(),
  );
  frames.all.sort((a, b) => a - b);
  frames.afterWheel.sort((a, b) => a - b);
  return frames;
}

/** The 95th percentile of values sorted smallest first. */
function p95(sorted: number[]) {
  return sorted[Math.floor(sorted.length * 0.95)];
}

test(
  "records pointer frame latency for 50/250/1000 step workflows",
  { tag: "@performance" },
  async ({ page }, info) => {
    test.setTimeout(60000);
    // These numbers measure the classic designer's canvas.
    await withClassicDesigner(page);
    await openNewWorkflow(page);
    const result: any = {
      platform: platform(),
      arch: arch(),
      cpu: cpus()[0]?.model,
      logicalCpus: cpus().length,
      viewport: "1600x1000",
      measurements: [],
    };
    for (const count of [50, 250, 1000]) {
      await enterSteps(page, count);
      const start = Date.now();
      await applyChanges(page);
      await page.getByRole("tab", { name: "Designer", exact: true }).click();
      await expect(page.locator("[data-step]")).toHaveCount(count);
      const sourceToGraphMs = Date.now() - start;
      const node = page.locator('[data-step="step-0"] .node-body');
      const bounds = await node.boundingBox();
      expect(bounds).not.toBeNull();
      let interaction = 0;
      const { all: frames } = await sampleFrames(page, async () => {
        await page.mouse.move(bounds!.x + 40, bounds!.y + 20);
        await page.mouse.down();
        interaction = Date.now();
        await page.mouse.move(bounds!.x + 150, bounds!.y + 40, { steps: 30 });
        await page.mouse.up();
      });
      result.measurements.push({
        count,
        sourceToGraphMs,
        pointerGestureMs: Date.now() - interaction,
        p95FrameMs: p95(frames),
        samples: frames.length,
      });
    }
    await info.attach("interaction-measurements", {
      body: JSON.stringify(result, null, 2),
      contentType: "application/json",
    });
    console.log(JSON.stringify(result));
  },
);

test(
  "the new canvas opens 200 steps within a second and pans and zooms within a frame",
  { tag: "@performance" },
  async ({ page }, info) => {
    test.setTimeout(180_000);
    await useNewEditor(page);
    await openNewWorkflow(page);
    // The canvas's code loads once, with the first workflow; the gate measures
    // opening a workflow, not loading the editor.
    await expect(page.locator(".canvas-v2[data-ready]")).toBeVisible();
    const canvas = page.locator(".canvas-v2");
    const zoomLevel = canvas.locator(".canvas-v2-tools .zoom-level");
    const zoomPercent = async () =>
      Number((await zoomLevel.innerText()).replace("%", ""));
    const result: {
      platform: string;
      arch: string;
      cpu: string | undefined;
      logicalCpus: number;
      viewport: string;
      measurements: Record<string, number>[];
    } = {
      platform: platform(),
      arch: arch(),
      cpu: cpus()[0]?.model,
      logicalCpus: cpus().length,
      viewport: "1600x1000",
      measurements: [],
    };
    for (const count of [200, 1000]) {
      await enterSteps(page, count);
      await applyChanges(page);
      const started = await page.evaluate(() => performance.now());
      await page.getByRole("tab", { name: "Designer", exact: true }).click();
      await page
        .locator(".canvas-v2[data-ready] [data-step]")
        .nth(count - 1)
        .waitFor({ state: "attached", timeout: 60_000 });
      // Interactive means on screen: one painted frame after the last tile.
      await page.evaluate(
        () =>
          new Promise<void>((done) =>
            requestAnimationFrame(() => requestAnimationFrame(() => done())),
          ),
      );
      const openMs = (await page.evaluate(() => performance.now())) - started;
      // The workflow is one row of steps, so the gestures stay on that row:
      // panning along it and zooming around a step, with steps always in view.
      const step3 = (await canvas
        .locator('[data-step="step-3"] .tile-body')
        .boundingBox())!;
      await page.mouse.move(
        step3.x + step3.width / 2,
        step3.y + step3.height / 2,
      );
      const pan = await sampleFrames(page, async () => {
        for (let i = 0; i < 45; i++) await page.mouse.wheel(40, 0);
      });
      const movedTo = (await canvas
        .locator('[data-step="step-3"] .tile-body')
        .boundingBox())!;
      expect(step3.x - movedTo.x, "the pan moved the steps").toBeGreaterThan(
        1000,
      );
      // Out to the smallest zoom, in past the opening zoom, and back out to it,
      // so both thresholds of level of detail are crossed in each direction.
      const zoom = await sampleFrames(page, async () => {
        await page.keyboard.down("Control");
        for (let i = 0; i < 14; i++) await page.mouse.wheel(0, 30);
        expect(await zoomPercent(), "zoomed out").toBeLessThanOrEqual(30);
        for (let i = 0; i < 26; i++) await page.mouse.wheel(0, -30);
        expect(await zoomPercent(), "zoomed in").toBeGreaterThanOrEqual(80);
        for (let i = 0; i < 12; i++) await page.mouse.wheel(0, 30);
        await page.keyboard.up("Control");
      });
      // The gate is the p95 of the frames that handled a wheel event; the
      // numbers over every frame are recorded next to it.
      const handled = [...pan.afterWheel, ...zoom.afterWheel].sort(
        (a, b) => a - b,
      );
      const all = [...pan.all, ...zoom.all].sort((a, b) => a - b);
      const p95FrameMs = p95(handled);
      result.measurements.push({
        count,
        openMs,
        p95FrameMs,
        panP95FrameMs: p95(pan.afterWheel),
        zoomP95FrameMs: p95(zoom.afterWheel),
        maxFrameMs: handled[handled.length - 1],
        samples: handled.length,
        allFramesP95Ms: p95(all),
        allFramesMaxMs: all[all.length - 1],
        allFrames: all.length,
      });
      // Only 200 steps are asserted; 1,000 steps are recorded for the PR.
      if (count === 200) {
        expect
          .soft(openMs, "open 200 steps to interactive")
          .toBeLessThanOrEqual(OPEN_BUDGET_MS);
        expect
          .soft(p95FrameMs, "pan and zoom p95 at 200 steps")
          .toBeLessThanOrEqual(FRAME_BUDGET_MS + VSYNC_ALLOWANCE_MS);
      }
    }
    await info.attach("canvas-measurements", {
      body: JSON.stringify(result, null, 2),
      contentType: "application/json",
    });
    console.log(JSON.stringify(result));
  },
);

test(
  "pastes 50 steps on the new canvas in at most 300 ms",
  { tag: "@performance" },
  async ({ page, context }, info) => {
    test.setTimeout(120_000);
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await useNewEditor(page);
    await openNewWorkflow(page);
    await expect(page.locator(".canvas-v2[data-ready]")).toBeVisible();
    const steps = Array.from({ length: 50 }, (_, i) => ({
      id: `pasted-${i}`,
      kind: "transform",
      value: { literal: {} },
    }));
    const clipboard = `# Firefly Weave steps. Paste into Studio.\n${JSON.stringify({ weaveClipboard: 1, steps, connections: {}, llmProfiles: {}, pins: {} })}`;
    await page.evaluate(
      (text) => navigator.clipboard.writeText(text),
      clipboard,
    );
    await page.evaluate(() => {
      const w = window as unknown as { pasteStart?: number; pasteEnd?: number };
      document.addEventListener(
        "paste",
        () => (w.pasteStart = performance.now()),
        {
          capture: true,
          once: true,
        },
      );
      const observer = new MutationObserver(() => {
        if (
          w.pasteStart !== undefined &&
          document.querySelectorAll(".canvas-v2 [data-step]").length >= 50
        ) {
          w.pasteEnd = performance.now();
          observer.disconnect();
        }
      });
      observer.observe(document.body, { childList: true, subtree: true });
    });
    await page.locator(".canvas-v2").focus();
    await page.keyboard.press("ControlOrMeta+v");
    await page
      .locator(".canvas-v2 [data-step]")
      .nth(49)
      .waitFor({ state: "attached" });
    const pasteMs = await page.evaluate(() => {
      const w = window as unknown as { pasteStart: number; pasteEnd: number };
      return Math.round(w.pasteEnd - w.pasteStart);
    });
    await info.attach("paste-measurement", {
      body: JSON.stringify({ steps: 50, pasteMs }),
      contentType: "application/json",
    });
    console.log(JSON.stringify({ steps: 50, pasteMs }));
    expect(pasteMs, "paste 50 steps").toBeLessThanOrEqual(300);
  },
);

/** Keystroke to the painted preview, on a runner as fast as the reference. */
const FORMULA_PREVIEW_BUDGET_MS = 50;
/** The first keystrokes run cold code (JIT, first renders): not measured. */
const FORMULA_WARMUP_KEYSTROKES = 3;
/** Measured keystrokes: with 60, the p95 is the 57th, not the 2nd slowest. */
const FORMULA_MEASURED_KEYSTROKES = 60;
/**
 * The calibration: Studio's formula parser and evaluator on the formula under
 * test, timed in the same page before the keystrokes, with no DOM.
 */
const FORMULA_CALIBRATION = { rounds: 4000, runs: 9 };
/**
 * The calibration's median on a GitHub-hosted runner with an AMD EPYC 9V45,
 * the fastest CPU those runners report: 19.9 ms in job 118428684852, where
 * an EPYC 7763 read 31.2 to 33.4 ms and a Xeon 8573C 24.2 ms.
 */
const FORMULA_CALIBRATION_REFERENCE_MS = 20;
/**
 * Shared runners differ about 1.6x in this calibration (AMD EPYC 7763
 * against 9V45). A runner slower than the reference gets its own slowdown as
 * headroom, up to 2x; a faster one gets none.
 */
const FORMULA_MAX_SPEED_ALLOWANCE = 2;

test(
  "previews a formula within 50 ms of a keystroke",
  { tag: "@performance" },
  async ({ page }, info) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    const features = ["text.concat", "text.join"];
    await withLanguageFeatures(page, features);
    const details = await openStepFixture(page, formulasFixture);
    await setTestInput(page, details, {
      "Customer ID": "c-1042",
      Amount: "1500",
    });
    await details.openWithKeyboard("review");
    const title = details.field("title");
    await formulaText(title).click();
    await page.keyboard.press("End");
    const result = title.getByRole("region", { name: "Result of Title" });
    await expect(result).toContainText("Review order of 1500");
    const text = await formulaBox(title).getAttribute("data-text");
    expect(text, "the Title formula's text").toBeTruthy();
    const calibration = await calibrateFormulaWork(page, {
      text: text!,
      context: { input: { customerId: "c-1042", amount: 1500 }, steps: {} },
      features,
      ...FORMULA_CALIBRATION,
    });
    // Every round must parse and evaluate, or the workload measures an error.
    expect(calibration.evaluated).toBe(FORMULA_CALIBRATION.rounds);
    // From the keydown to the frame that shows the new value: the timer
    // stops once the Result reads the expected text, after the next
    // animation frame and the task that follows it (when the frame paints).
    await page.evaluate(() => {
      const w = window as unknown as {
        previewMs: number[];
        pressedAt: number;
        typed: string;
      };
      w.previewMs = [];
      w.pressedAt = 0;
      w.typed = "";
      document.addEventListener(
        "keydown",
        (event) => {
          w.typed += event.key;
          w.pressedAt = performance.now();
        },
        { capture: true },
      );
      const region = document.querySelector('[aria-label="Result of Title"]')!;
      const painted = new MessageChannel();
      let started = 0;
      painted.port1.onmessage = () => {
        w.previewMs.push(performance.now() - started);
        w.pressedAt = 0;
      };
      new MutationObserver(() => {
        if (w.pressedAt <= 0) return;
        const value = region.querySelector(".formula-result-value");
        if (value?.textContent !== `Review order of 1500${w.typed}`) return;
        started = w.pressedAt;
        w.pressedAt = -1;
        requestAnimationFrame(() => painted.port2.postMessage(null));
      }).observe(region, {
        subtree: true,
        childList: true,
        characterData: true,
      });
    });
    const keystrokes = FORMULA_WARMUP_KEYSTROKES + FORMULA_MEASURED_KEYSTROKES;
    for (let index = 0; index < keystrokes; index++) {
      await page.keyboard.press(String.fromCharCode(97 + (index % 26)));
      await page.waitForFunction(
        () => (window as unknown as { pressedAt: number }).pressedAt === 0,
      );
    }
    const all = await page.evaluate(
      () => (window as unknown as { previewMs: number[] }).previewMs,
    );
    expect(all).toHaveLength(keystrokes);
    const samples = all.slice(FORMULA_WARMUP_KEYSTROKES);
    const sorted = [...samples].sort((a, b) => a - b);
    const p95 = sorted[Math.ceil(sorted.length * 0.95) - 1];
    const ratio = calibration.medianMs / FORMULA_CALIBRATION_REFERENCE_MS;
    const allowance = Math.min(Math.max(ratio, 1), FORMULA_MAX_SPEED_ALLOWANCE);
    const scaledP95 = p95 / allowance;
    const round = (value: number) => Math.round(value * 10) / 10;
    const measurement = {
      platform: platform(),
      arch: arch(),
      cpu: cpus()[0]?.model,
      warmupKeystrokes: FORMULA_WARMUP_KEYSTROKES,
      keystrokes: samples.length,
      warmupMs: all.slice(0, FORMULA_WARMUP_KEYSTROKES).map(round),
      medianMs: round(sorted[Math.floor(sorted.length / 2)]),
      rawP95Ms: round(p95),
      maxMs: round(sorted[sorted.length - 1]),
      calibrationMs: round(calibration.medianMs),
      calibrationRunsMs: calibration.runsMs.map(round),
      referenceMs: FORMULA_CALIBRATION_REFERENCE_MS,
      ratio: Math.round(ratio * 100) / 100,
      allowance: Math.round(allowance * 100) / 100,
      scaledP95Ms: round(scaledP95),
      budgetMs: FORMULA_PREVIEW_BUDGET_MS,
    };
    await info.attach("formula-preview-measurement", {
      body: JSON.stringify(measurement),
      contentType: "application/json",
    });
    console.log(JSON.stringify(measurement));
    expect(
      scaledP95,
      "keystroke to preview, p95 scaled to the reference runner",
    ).toBeLessThanOrEqual(FORMULA_PREVIEW_BUDGET_MS);
  },
);

/** Fingerprints for 200 steps, on a runner as fast as the reference. */
const FINGERPRINT_BUDGET_MS = 10;
/** Calls before the measured ones warm the JIT. */
const FINGERPRINT_WARMUP_CALLS = 10;
/** Measured calls: with 60, the p95 is the 57th. */
const FINGERPRINT_MEASURED_CALLS = 60;

test(
  "fingerprints 200 steps within 10 ms",
  { tag: "@performance" },
  async ({ page }, info) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    // The real page: Studio's code is loaded and running when it is timed.
    await openStepFixture(page, formulasFixture);
    // The runner's speed, by the formula gate's calibration and reference.
    const calibration = await calibrateFormulaWork(page, {
      text: "{{ input.customerId }}",
      context: { input: { customerId: "c-1042" }, steps: {} },
      features: [],
      ...FORMULA_CALIBRATION,
    });
    expect(calibration.evaluated).toBe(FORMULA_CALIBRATION.rounds);
    const work = await timeFingerprintWork(page, {
      steps: 200,
      warmup: FINGERPRINT_WARMUP_CALLS,
      calls: FINGERPRINT_MEASURED_CALLS,
    });
    // Every call hashed the 200 steps, or it measured something else.
    expect(work.hashed).toBe(200);
    expect(work.callsMs).toHaveLength(FINGERPRINT_MEASURED_CALLS);
    const sorted = [...work.callsMs].sort((a, b) => a - b);
    const p95 = sorted[Math.ceil(sorted.length * 0.95) - 1];
    const ratio = calibration.medianMs / FORMULA_CALIBRATION_REFERENCE_MS;
    const allowance = Math.min(Math.max(ratio, 1), FORMULA_MAX_SPEED_ALLOWANCE);
    const scaledP95 = p95 / allowance;
    const round = (value: number) => Math.round(value * 100) / 100;
    const measurement = {
      platform: platform(),
      arch: arch(),
      cpu: cpus()[0]?.model,
      steps: 200,
      calls: sorted.length,
      medianMs: round(sorted[Math.floor(sorted.length / 2)]),
      p95Ms: round(p95),
      maxMs: round(sorted[sorted.length - 1]),
      medianCalibrationMs: round(calibration.medianMs),
      referenceMs: FORMULA_CALIBRATION_REFERENCE_MS,
      ratio: round(ratio),
      allowance: round(allowance),
      scaledP95Ms: round(scaledP95),
      budgetMs: FINGERPRINT_BUDGET_MS,
    };
    await info.attach("fingerprint-measurement", {
      body: JSON.stringify(measurement),
      contentType: "application/json",
    });
    console.log(JSON.stringify(measurement));
    expect(
      scaledP95,
      "fingerprints of 200 steps, p95 scaled to the reference runner",
    ).toBeLessThanOrEqual(FINGERPRINT_BUDGET_MS);
  },
);

test(
  "executes a 200-step workflow within 2 s",
  { tag: "@performance" },
  async ({ page }, info) => {
    test.setTimeout(300_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    await withLanguageFeatures(page, ["text.concat", "text.join"]);
    const { details, calls } = await openExecuteWorkflow(page, longWorkflow());
    await pinSample(details, "check");
    // The runner's speed, measured as the formula gate measures it.
    const calibration = await calibrateFormulaWork(page, {
      text: "Review order of {{ input.amount }}",
      context: { input: { amount: 1500 }, steps: {} },
      features: ["text.concat", "text.join"],
      ...FORMULA_CALIBRATION,
    });
    const status = page
      .getByRole("region", { name: "Run log" })
      .locator(".run-log-status");
    const button = page
      .locator(".editor-bar")
      .getByRole("button", { name: "Execute workflow" });
    // From the click to the run log reading the end of the run.
    await page.emulateMedia({ reducedMotion: "reduce" });
    const once = async () => {
      const before = executions(calls).length;
      const answered = page.waitForResponse("**/studio/local/debug/execute");
      const started = Date.now();
      await button.click();
      await answered;
      await expect(status).toHaveText("Completed · 200 steps", {
        timeout: 60_000,
      });
      expect(executions(calls).length).toBe(before + 1);
      return Date.now() - started;
    };
    await once();
    const times: number[] = [];
    for (let run = 0; run < EXECUTE_RUNS; run++) times.push(await once());
    const median = [...times].sort((a, b) => a - b)[
      Math.floor(times.length / 2)
    ];
    const measurement = scaledMs(
      median,
      calibration.medianMs,
      FORMULA_CALIBRATION_REFERENCE_MS,
      FORMULA_MAX_SPEED_ALLOWANCE,
    );
    await info.attach("execute-measurement", {
      body: JSON.stringify({ ...measurement, times }, null, 2),
      contentType: "application/json",
    });
    expect(measurement.scaledMs).toBeLessThanOrEqual(EXECUTE_BUDGET_MS);
  },
);
