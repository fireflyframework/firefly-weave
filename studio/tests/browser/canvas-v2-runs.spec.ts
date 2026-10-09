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
// A simulation on the left-to-right canvas: which step is live, waiting,
// finished or failed, which edges the run took, and that nothing can change
// while it runs. The host's replies are scripted; the artifact comes from the
// repository's own compiler, so these tests need its Python environment.
import {
  expect,
  test,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test";
import { CanvasPage, useNewEditor } from "./canvas-po";
import { DesignerPage } from "./designer-po";
import {
  debugSession,
  later,
  simulatedSource,
  simulationArtifact,
} from "./simulation-artifact";
import {
  allCapabilities,
  command,
  connected,
  newWorkflow,
  tokenColor,
} from "./support";

const project = "**/studio/api/api/v1/tenants/tenant/projects/project";
type Session = ReturnType<typeof debugSession>;

/** Opens `source` on the new canvas and starts today's simulation, answered by `replies`. */
async function simulate(page: Page, source: string, replies: Session[]) {
  const artifact = simulationArtifact();
  test.skip(!artifact, "Needs the repository's Python environment.");
  await page.setViewportSize({ width: 1440, height: 900 });
  await useNewEditor(page);
  await connected(page, { capabilities: [...allCapabilities, "compile"] });
  await page.route(`${project}/compiler/compile`, (r) =>
    r.fulfill({
      json: {
        ok: true,
        validationOk: true,
        partial: false,
        errorCount: 0,
        diagnostics: [],
        artifact,
      },
    }),
  );
  await page.route(`${project}/debug/sessions`, (r: Route) =>
    r.fulfill({ status: 201, json: replies[0] }),
  );
  let next = 1;
  await page.route(`${project}/debug/sessions/*/commands`, (r: Route) =>
    r.fulfill({ json: replies[next++] }),
  );
  await newWorkflow(page);
  await new DesignerPage(page).setSource(source);
  const canvas = new CanvasPage(page);
  await canvas.ready();
  await canvas.closeInspector();
  await command(page, "Simulate");
  const setup = page.getByRole("dialog", { name: "Simulate this workflow" });
  await setup.getByLabel(/customer ?id/i).fill("c-104");
  await setup.getByRole("button", { name: "Start simulation" }).click();
  return canvas;
}

/** Clicks the middle of a button the way a person does, also when it is aria-disabled. */
async function press(page: Page, button: Locator) {
  const box = (await button.boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
}

const waitingAtWait = () =>
  debugSession(1, "waiting", ["wait-1"], ["action-1"], {
    "wait-1": later(60),
  });

test("a simulation draws the waiting step's border without a pulse, the finished steps with a check and the edges it took", async ({
  page,
}) => {
  const canvas = await simulate(page, simulatedSource, [
    waitingAtWait(),
    debugSession(2, "waiting", ["approval"], ["action-1", "wait-1"]),
  ]);
  const live = canvas.tile("wait-1").locator(".tile-shape");
  await expect(live).toHaveClass(/\blive\b/);
  await expect(live).not.toHaveClass(/\bpulse\b/);
  await expect(live).toHaveCSS("animation-name", "none");
  await expect(canvas.tileBody("wait-1")).toHaveAccessibleName(
    "wait-1, Wait for time, 1 min, Waiting, step 2 of 4 in Main sequence",
  );
  await expect(canvas.edgeLine("action-1>wait-1")).toHaveClass(/\blive\b/);
  await expect(canvas.edgeLine("wait-1>approval")).toHaveClass(/\bidle\b/);
  await expect(canvas.edgeLine("action-1>wait-1")).toHaveCSS(
    "stroke",
    await tokenColor(page, "--live"),
  );
  const simulation = page
    .getByRole("region", { name: "Simulation", exact: true })
    .or(page.getByRole("dialog", { name: "Simulation", exact: true }));
  await simulation
    .getByRole("button", { name: "Finish the wait at wait-1 (1 min)" })
    .click();
  await expect(canvas.tile("approval").locator(".tile-shape")).toHaveClass(
    /\blive\b/,
  );
  await expect(
    canvas.tile("wait-1").locator('.tile-badge[data-badge="done"]'),
  ).toBeVisible();
  await expect(canvas.tileBody("approval")).toHaveAccessibleName(
    /, Waiting for a signal, step 3 of 4 in Main sequence$/,
  );
});

test("a simulation that failed marks its step with the danger border and the failed badge, not the live one", async ({
  page,
}) => {
  const canvas = await simulate(page, simulatedSource, [
    debugSession(1, "failed", ["wait-1"], ["action-1"]),
  ]);
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

test("while a simulation runs the canvas offers no + and moves nothing, but still pans", async ({
  page,
}) => {
  const canvas = await simulate(page, simulatedSource, [waitingAtWait()]);
  await expect(canvas.root).toHaveClass(/\blocked\b/);
  await expect(canvas.root.locator(".plus-stub, .edge-plus")).toHaveCount(0);
  const tile = (await canvas.tileBody("approval").boundingBox())!;
  await page.mouse.move(tile.x + tile.width / 2, tile.y + tile.height / 2);
  await page.mouse.down();
  await page.mouse.move(tile.x + 80, tile.y + 80, { steps: 6 });
  await expect(canvas.root).not.toHaveClass(/\brevealing\b/);
  await page.mouse.up();
  await canvas.tileBody("review").hover();
  await expect(
    canvas.root.getByRole("button", { name: "Delete review", exact: true }),
  ).toHaveAttribute("aria-disabled", "true");
  const before = (await canvas.tileBody("action-1").boundingBox())!.y;
  await page.mouse.wheel(0, 120);
  await expect
    .poll(async () => (await canvas.tileBody("action-1").boundingBox())!.y)
    .not.toBe(before);
});

test("while a simulation runs the selection toolbar and the keys change no step, and say why", async ({
  page,
}) => {
  const canvas = await simulate(page, simulatedSource, [waitingAtWait()]);
  const steps = canvas.root.locator(".tile-node[data-step]");
  await expect(steps).toHaveCount(4);
  await canvas.tileBody("approval").click();
  await canvas.tileBody("review").click({ modifiers: ["Shift"] });
  const bar = canvas.root.getByRole("toolbar", { name: "Selected steps" });
  await expect(bar).toContainText("2 steps");
  for (const name of ["Duplicate", "Delete"]) {
    const button = bar.getByRole("button", { name, exact: true });
    await expect(button).toHaveAttribute("aria-disabled", "true");
    await expect(button).toHaveAccessibleDescription(
      "Editing is paused while the simulation runs.",
    );
  }
  const toast = page.locator(".toast");
  await press(page, bar.getByRole("button", { name: "Delete", exact: true }));
  await expect(toast).toContainText(
    "Editing is paused while the simulation runs.",
  );
  await expect(steps).toHaveCount(4);
  await canvas.tileBody("approval").focus();
  for (const key of ["Delete", "ControlOrMeta+d"]) {
    await page.keyboard.press(key);
    await expect(steps).toHaveCount(4);
  }
  await expect(canvas.tile("approval")).toHaveCount(1);
  await expect(canvas.tile("review")).toHaveCount(1);
});

test("a finished simulation draws the edges it took in success and the path it didn't take dashed", async ({
  page,
}) => {
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
  const canvas = await simulate(page, routed, [
    debugSession(
      1,
      "succeeded",
      [],
      ["check", "route", "auto-approve", "record"],
    ),
  ]);
  const success = await tokenColor(page, "--success");
  for (const key of [
    "$trigger:manual>check",
    "check>route",
    "route:default>auto-approve",
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
  for (const key of ["route:case 1>review", "review>$join:route"]) {
    await expect(canvas.edgeLine(key)).toHaveClass(/\bskipped\b/);
    await expect(canvas.edgeLine(key)).toHaveCSS("stroke", dim);
    await expect(canvas.edgeLine(key)).toHaveCSS("stroke-width", "2px");
    await expect(canvas.edgeLine(key)).toHaveCSS(
      "stroke-dasharray",
      "4px, 4px",
    );
  }
});
