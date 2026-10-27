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
// A simulated run on the left-to-right canvas, as Execute workflow plays it
// back: which step is live, waiting, finished or failed, and which edges
// the run took. The host's answers are scripted here (the real host is
// tested on its own); with reduced motion the canvas shows each run's end
// at once.
import { expect, test, type Page } from "@playwright/test";
import { CanvasPage, useNewEditor } from "./canvas-po";
import { DesignerPage } from "./designer-po";
import { simulatedSource } from "./simulation-artifact";
import { allCapabilities, connected, newWorkflow, tokenColor } from "./support";

type Status = "completed" | "waiting" | "failed";
interface Answer {
  status: "completed" | "blocked" | "failed";
  trace: [string, Status][];
  blocked?: { nodeId: string; message: string };
}

/** Opens `source` on the new canvas and executes it, the host answering `answer`. */
async function simulate(page: Page, source: string, answer: Answer) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await useNewEditor(page);
  await connected(page, { capabilities: [...allCapabilities, "compile"] });
  await page.route("**/studio/local/debug/execute", (route) =>
    route.fulfill({
      json: {
        status: answer.status,
        compile: { ok: true, sliced: false, diagnostics: [], stubs: [] },
        trace: answer.trace.map(([nodeId, status], index) => ({
          index,
          nodeId,
          instanceKey: "",
          kind: "transform",
          status,
          ...(status === "completed" ? { output: {} } : {}),
        })),
        ...(answer.blocked
          ? {
              blocked: {
                reason: "signal",
                instanceKey: "",
                ...answer.blocked,
              },
            }
          : {}),
      },
    }),
  );
  await newWorkflow(page);
  await new DesignerPage(page).setSource(source);
  const canvas = new CanvasPage(page);
  await canvas.ready();
  await canvas.closeInspector();
  await page
    .locator(".editor-bar")
    .getByRole("button", { name: "Execute workflow" })
    .click();
  return canvas;
}

/** A decision whose second path waits, then a step after it. */
const routed = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: routed, version: 1.0.0}
spec:
  inputSchema:
    type: object
    properties:
      amount: {type: number, title: Amount}
  outputSchema: {type: object}
  steps:
    - {id: check, kind: transform, value: {ref: /input}}
    - id: route
      kind: switch
      cases:
        - when: {op: {name: gt, args: [{ref: /input/amount}, {literal: 1000}]}}
          steps:
            - {id: review, kind: wait, durationSeconds: 300}
          output: {literal: {}}
      default:
        steps:
          - {id: auto-approve, kind: transform, value: {literal: {approved: true}}}
        output: {literal: {}}
    - {id: record, kind: transform, value: {literal: {}}}
  output: {literal: {}}
`;

test("a simulation draws the waiting step's border without a pulse, the finished steps with a check and the edges it took", async ({
  page,
}) => {
  const canvas = await simulate(page, simulatedSource, {
    status: "blocked",
    trace: [
      ["action-1", "completed"],
      ["wait-1", "completed"],
      ["approval", "waiting"],
    ],
    blocked: {
      nodeId: "approval",
      message: "Send signal customer-approved",
    },
  });
  const live = canvas.tile("approval").locator(".tile-shape");
  await expect(live).toHaveClass(/\blive\b/);
  await expect(live).not.toHaveClass(/\bpulse\b/);
  await expect(live).toHaveCSS("animation-name", "none");
  await expect(canvas.tileBody("approval")).toHaveAccessibleName(
    /, Send signal customer-approved, step 3 of 4 in Main sequence$/,
  );
  await expect(
    canvas.tile("wait-1").locator('.tile-badge[data-badge="done"]'),
  ).toBeVisible();
  await expect(canvas.edgeLine("wait-1>approval")).toHaveClass(/\blive\b/);
  await expect(canvas.edgeLine("approval>review")).toHaveClass(/\bidle\b/);
  await expect(canvas.edgeLine("wait-1>approval")).toHaveCSS(
    "stroke",
    await tokenColor(page, "--live"),
  );
});

test("a simulation that failed marks its step with the danger border and the failed badge, not the live one", async ({
  page,
}) => {
  const canvas = await simulate(page, simulatedSource, {
    status: "failed",
    trace: [
      ["action-1", "completed"],
      ["wait-1", "failed"],
    ],
  });
  const shape = canvas.tile("wait-1").locator(".tile-shape");
  await expect(shape).toHaveClass(/\berror\b/);
  await expect(shape).not.toHaveClass(/\blive\b/);
  await expect(shape).toHaveCSS(
    "border-top-color",
    await tokenColor(page, "--danger"),
  );
  await expect(
    canvas.tile("wait-1").locator('.tile-badge[data-badge="failed"]'),
  ).toBeVisible();
  await expect(canvas.tileBody("wait-1")).toHaveAccessibleName(
    "wait-1, Wait for time, 1 min, Failed, step 2 of 4 in Main sequence",
  );
});

test("a finished simulation draws the edges it took in success and the path it didn't take dashed", async ({
  page,
}) => {
  const canvas = await simulate(page, routed, {
    status: "completed",
    trace: [
      ["check", "completed"],
      ["auto-approve", "completed"],
      ["route", "completed"],
      ["record", "completed"],
    ],
  });
  const success = await tokenColor(page, "--success");
  for (const key of [
    "$trigger:manual>check",
    "check>route",
    "route:path:default>auto-approve",
    "route>record",
    "record>$end",
  ]) {
    await expect(canvas.edgeLine(key)).toHaveClass(/\btaken\b/);
    await expect(canvas.edgeLine(key)).toHaveCSS("stroke", success);
    await expect(canvas.edgeLine(key)).toHaveCSS("stroke-width", "2.5px");
  }
  // The arrowheads follow the same state even when the classic canvas's rule for
  // them comes last in the page.
  await page.addStyleTag({
    content: ".edges .edge-arrow.taken { fill: var(--live); }",
  });
  await expect(canvas.root.locator("#canvas-arrow-taken path")).toHaveCSS(
    "fill",
    success,
  );
  const dim = await tokenColor(page, "--flow-line-dim");
  for (const key of ["route:path:case 1>review", "review>$join:route"]) {
    await expect(canvas.edgeLine(key)).toHaveClass(/\bskipped\b/);
    await expect(canvas.edgeLine(key)).toHaveCSS("stroke", dim);
    await expect(canvas.edgeLine(key)).toHaveCSS("stroke-width", "2px");
    await expect(canvas.edgeLine(key)).toHaveCSS(
      "stroke-dasharray",
      "4px, 4px",
    );
  }
});

test("a simulation that waits inside a path draws the way into the decision and down that path in the live color, and the rest idle", async ({
  page,
}) => {
  const canvas = await simulate(page, routed, {
    status: "blocked",
    trace: [
      ["check", "completed"],
      ["review", "waiting"],
    ],
    blocked: { nodeId: "review", message: "Send signal review" },
  });
  const live = await tokenColor(page, "--live");
  for (const key of [
    "$trigger:manual>check",
    "check>route",
    "route>route:path:case 1",
    "route:path:case 1>review",
  ]) {
    await expect(canvas.edgeLine(key)).toHaveClass(/\blive\b/);
    await expect(canvas.edgeLine(key)).toHaveCSS("stroke", live);
  }
  for (const key of [
    "review>$join:route",
    "route>route:path:default",
    "route:path:default>auto-approve",
    "auto-approve>$join:route",
    "route>record",
    "record>$end",
  ])
    await expect(canvas.edgeLine(key)).toHaveClass(/\bidle\b/);
});

test("a simulation that failed inside a path keeps the way into the decision taken and draws the other paths as not taken", async ({
  page,
}) => {
  const canvas = await simulate(page, routed, {
    status: "failed",
    trace: [
      ["check", "completed"],
      ["review", "failed"],
    ],
  });
  const success = await tokenColor(page, "--success");
  for (const key of [
    "$trigger:manual>check",
    "check>route",
    "route>route:path:case 1",
    "route:path:case 1>review",
  ]) {
    await expect(canvas.edgeLine(key)).toHaveClass(/\btaken\b/);
    await expect(canvas.edgeLine(key)).toHaveCSS("stroke", success);
  }
  for (const key of [
    "review>$join:route",
    "route>route:path:default",
    "route:path:default>auto-approve",
    "auto-approve>$join:route",
    "route>record",
    "record>$end",
  ])
    await expect(canvas.edgeLine(key)).toHaveClass(/\bskipped\b/);
});
