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
import { cpus, platform, arch } from "node:os";
test("records pointer frame latency for 50/250/1000 step workflows", async ({
  page,
}, info) => {
  test.setTimeout(60000);
  await page.setViewportSize({ width: 1600, height: 1000 });
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
  await page.goto("/");
  await page.getByRole("button", { name: "New workflow", exact: true }).click();
  const result: any = {
    platform: platform(),
    arch: arch(),
    cpu: cpus()[0]?.model,
    logicalCpus: cpus().length,
    viewport: "1600x1000",
    measurements: [],
  };
  for (const count of [50, 250, 1000]) {
    await page.getByRole("tab", { name: "Source", exact: true }).click();
    await page.getByRole("textbox", { name: "Workflow source" }).fill(
      JSON.stringify({
        apiVersion: "weave/v1alpha1",
        kind: "Workflow",
        metadata: { name: "latency-measurement", version: "1.0.0" },
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
      }),
    );
    const start = Date.now();
    await page
      .getByRole("button", { name: "Apply changes", exact: true })
      .click();
    await page.getByRole("tab", { name: "Designer", exact: true }).click();
    await expect(page.locator("[data-step]")).toHaveCount(count);
    const sourceToGraphMs = Date.now() - start;
    const node = page.locator('[data-step="step-0"] .node-body');
    const bounds = await node.boundingBox();
    expect(bounds).not.toBeNull();
    await page.evaluate(() => {
      const w = window as any;
      w.frameSamples = [];
      w.sampling = true;
      let last = performance.now();
      const frame = (time: number) => {
        if (!w.sampling) return;
        w.frameSamples.push(time - last);
        last = time;
        requestAnimationFrame(frame);
      };
      requestAnimationFrame(frame);
    });
    await page.mouse.move(bounds!.x + 40, bounds!.y + 20);
    await page.mouse.down();
    const interaction = Date.now();
    await page.mouse.move(bounds!.x + 150, bounds!.y + 40, { steps: 30 });
    await page.mouse.up();
    await page.waitForTimeout(100);
    const frames = await page.evaluate(() => {
      const w = window as any;
      w.sampling = false;
      return w.frameSamples as number[];
    });
    frames.sort((a, b) => a - b);
    result.measurements.push({
      count,
      sourceToGraphMs,
      pointerGestureMs: Date.now() - interaction,
      p95FrameMs: frames[Math.floor(frames.length * 0.95)],
      samples: frames.length,
    });
  }
  await info.attach("interaction-measurements", {
    body: JSON.stringify(result, null, 2),
    contentType: "application/json",
  });
  console.log(JSON.stringify(result));
});
