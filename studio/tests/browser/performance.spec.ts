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

test("records pointer frame latency for 50/250/1000 step workflows", async ({
  page,
}, info) => {
  test.setTimeout(60000);
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
});

test("the new canvas opens 200 steps within a second and pans and zooms within a frame", async ({
  page,
}, info) => {
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
      expect(openMs, "open 200 steps to interactive").toBeLessThanOrEqual(
        OPEN_BUDGET_MS,
      );
      expect(p95FrameMs, "pan and zoom p95 at 200 steps").toBeLessThanOrEqual(
        FRAME_BUDGET_MS + VSYNC_ALLOWANCE_MS,
      );
    }
  }
  await info.attach("canvas-measurements", {
    body: JSON.stringify(result, null, 2),
    contentType: "application/json",
  });
  console.log(JSON.stringify(result));
});
