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
// WP-13: a simulation is set up from forms (no JSON), sends each command with
// the identifier the simulator expects (results by `node:<id>`, signals by
// their signal name, decisions by the human task's node ID), and highlights
// where the simulated run is on the canvas. The artifact is a real compile
// of the workflow below; the platform's debug sessions are mocked.
import { test, expect, Page, Route } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import {
  allCapabilities,
  closeSheet,
  command,
  connected,
  newWorkflow,
} from "./support";
import { DesignerPage } from "./designer-po";

const repository = resolve("..");
const python = resolve(repository, ".venv/bin/python");
const project = "**/studio/api/api/v1/tenants/tenant/projects/project";
const source = `apiVersion: weave/v1alpha1
kind: Workflow
metadata:
  name: simulated
  version: 1.0.0
spec:
  inputSchema:
    type: object
    additionalProperties: false
    required: [customerId]
    properties:
      customerId: { type: string, minLength: 1 }
  outputSchema: { type: object }
  steps:
    - id: action-1
      kind: action
      uses: onboarding.check-customer@1.0.0
      with:
        object:
          customerId: { ref: /input/customerId }
    - id: wait-1
      kind: wait
      durationSeconds: 60
    - id: approval
      kind: signal
      name: customer-approved
      timeoutSeconds: 3600
      payloadSchema:
        type: object
        additionalProperties: false
        required: [approved]
        properties:
          approved: { type: boolean }
    - id: review
      kind: humanTask
      assignment: reviewers
      title: { literal: Review the customer }
      context: { literal: {} }
      decisions: [approve, reject]
      formSchema: { type: object, properties: { note: { type: string } } }
  output: { literal: {} }
`;
/** The real compiled artifact, against the sample check-customer action. */
const artifact: Record<string, unknown> | null = existsSync(python)
  ? JSON.parse(
      execFileSync(
        python,
        [
          "-c",
          `
import json, pathlib, sys
import yaml
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
from firefly_weave.contracts.definitions import load_definition
action = yaml.safe_load(pathlib.Path("examples/definitions/check-customer.action.yaml").read_text())
spec = action["spec"]
catalog = CatalogSnapshot.from_definitions([load_definition(action)], tasks=[{
    "taskType": "onboarding.check-customer", "taskVersion": "1.0.0",
    "inputSchema": spec["inputSchema"], "outputSchema": spec["outputSchema"],
    "sideEffect": "read_only", "timeoutSeconds": 60}])
result = compile_source(sys.stdin.read(), format="yaml", catalog=catalog)
assert result.ok, [d.code for d in result.diagnostics]
print(result.artifact.to_bytes().decode())
`,
        ],
        {
          cwd: repository,
          env: { ...process.env, PYTHONPATH: resolve(repository, "src") },
          encoding: "utf8",
          input: source,
          timeout: 30000,
        },
      ),
    )
  : null;

const now = "2026-10-02T09:00:00.000Z";
const later = (seconds: number) =>
  new Date(Date.parse(now) + seconds * 1000).toISOString();
const session = (
  revision: number,
  status: string,
  active: string[],
  waits: Record<string, string> = {},
) => ({
  id: "44444444-4444-4444-8444-444444444444",
  revision,
  view: {
    status,
    current_nodes: active,
    active_nodes: active,
    variables: { waits, steps: {} },
    diagnostics: [],
    events: [],
    now,
  },
});

/**
 * The simulation panel: a region beside the canvas, or a modal sheet where it
 * covers most of the canvas (narrow or short windows).
 */
const simulationPanel = (page: Page) =>
  page
    .getByRole("region", { name: "Simulation", exact: true })
    .or(page.getByRole("dialog", { name: "Simulation", exact: true }));

async function simulator(page: Page) {
  const creates: Record<string, unknown>[] = [];
  const commands: { body: Record<string, unknown>; match: string }[] = [];
  const replies = [
    session(2, "waiting", ["approval"], { approval: later(3600) }),
    session(3, "waiting", ["review"]),
    session(4, "succeeded", []),
  ];
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
  await page.route(`${project}/debug/sessions`, (r: Route) => {
    creates.push(r.request().postDataJSON());
    return r.fulfill({
      status: 201,
      json: session(1, "waiting", ["wait-1"], { "wait-1": later(60) }),
    });
  });
  await page.route(`${project}/debug/sessions/*/commands`, (r: Route) => {
    commands.push({
      body: r.request().postDataJSON(),
      match: r.request().headers()["if-match"],
    });
    return r.fulfill({ json: replies[commands.length - 1] });
  });
  return { creates, commands };
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });
    test.skip(!artifact, "Needs the repository's Python environment.");

    test("a workflow simulates from forms with the identifiers the simulator expects", async ({
      page,
    }) => {
      await connected(page, { capabilities: [...allCapabilities, "compile"] });
      const { creates, commands } = await simulator(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      await designer.setSource(source);
      const close = page.getByRole("button", { name: "Close inspector" });
      if (await close.isVisible()) await close.click();
      await command(page, "Simulate");
      const setup = page.getByRole("dialog", {
        name: "Simulate this workflow",
      });
      await expect(setup).toBeVisible();
      await setup.getByLabel(/customer ?id/i).fill("c-104");
      await setup.getByRole("tab", { name: /Action results/ }).click();
      const result = setup.locator('[data-node="action-1"]');
      await expect(result).toContainText("action-1");
      await result.getByLabel(/eligible/i).check();
      await setup.getByRole("button", { name: "Start simulation" }).click();
      await expect.poll(() => creates.length).toBe(1);
      expect(creates[0]["input"]).toEqual({ customerId: "c-104" });
      expect(creates[0]["mocks"]).toEqual({
        "node:action-1": { eligible: true },
      });
      expect(creates[0]["artifact"]).toEqual(artifact);

      const simulation = simulationPanel(page);
      await expect(simulation).toContainText("Waiting");
      await expect(page.locator('[data-step="wait-1"]')).toHaveClass(
        /\bis-live\b/,
      );
      await expect(
        page.locator('[data-step="wait-1"] .node-body'),
      ).toHaveAccessibleName(/The simulation is here/);

      await simulation
        .getByRole("button", { name: "Finish the wait at wait-1 (1 min)" })
        .click();
      await expect.poll(() => commands.length).toBe(1);
      expect(commands[0].body).toEqual({ kind: "advance_time", seconds: 60 });
      expect(commands[0].match).toBe('"1"');
      await expect(page.locator('[data-step="approval"]')).toHaveClass(
        /\bis-live\b/,
      );
      await expect(page.locator('[data-step="wait-1"]')).not.toHaveClass(
        /\bis-live\b/,
      );

      await simulation
        .getByRole("button", { name: "Send customer-approved" })
        .click();
      await expect.poll(() => commands.length).toBe(2);
      expect(commands[1].body).toEqual({
        kind: "signal",
        name: "customer-approved",
        payload: { approved: false },
      });

      await simulation.getByRole("radio", { name: "approve" }).check();
      await simulation.getByRole("button", { name: "Submit decision" }).click();
      await expect.poll(() => commands.length).toBe(3);
      expect(commands[2].body).toEqual({
        kind: "human_decision",
        name: "review",
        payload: { decision: "approve", data: {} },
      });
      await expect(simulation).toContainText("Finished");
      await simulation
        .getByRole("button", { name: "Close simulation" })
        .click();
      await expect(simulation).toHaveCount(0);
      await expect(page.locator(".graph-node.is-live")).toHaveCount(0);
    });

    test("while another command runs, the simulation's controls wait instead of dropping a click", async ({
      page,
    }) => {
      await connected(page, { capabilities: [...allCapabilities, "compile"] });
      const { commands } = await simulator(page);
      let release = () => {};
      const held = new Promise<void>((resolve) => (release = resolve));
      await page.route(`${project}/drafts/*`, async (r) => {
        await held;
        return r.fulfill({
          json: { id: "draft", revision: 1, document: {} },
        });
      });
      await newWorkflow(page);
      await new DesignerPage(page).setSource(source);
      const close = page.getByRole("button", { name: "Close inspector" });
      if (await close.isVisible()) await close.click();
      await command(page, "Simulate");
      const setup = page.getByRole("dialog", {
        name: "Simulate this workflow",
      });
      await setup.getByLabel(/customer ?id/i).fill("c-1");
      await setup.getByRole("button", { name: "Start simulation" }).click();
      const simulation = simulationPanel(page);
      await expect(simulation).toContainText("Waiting");
      const save = page.getByRole("button", {
        name: "Save draft",
        exact: true,
      });
      // Where the panel is a modal sheet over the toolbar, Escape folds it
      // away; it opens again to show its waiting controls.
      const modal = (await simulation.getAttribute("aria-modal")) === "true";
      if (modal) {
        await page.keyboard.press("Escape");
        await expect(simulation).not.toHaveAttribute("aria-modal", "true");
      }
      await save.scrollIntoViewIfNeeded();
      await save.click();
      if (modal)
        await simulation
          .getByRole("button", { name: "Expand simulation" })
          .click();
      const finish = simulation.getByRole("button", {
        name: "Finish the wait at wait-1 (1 min)",
      });
      // The save is still running: the panel says its controls are waiting.
      await expect(finish).toHaveAttribute("aria-disabled", "true");
      release();
      await expect(finish).not.toHaveAttribute("aria-disabled", "true");
      await finish.click();
      await expect.poll(() => commands.length).toBe(1);
      expect(commands[0].body).toEqual({ kind: "advance_time", seconds: 60 });
    });

    test("the simulation panel is a modal sheet only where it covers the canvas", async ({
      page,
    }) => {
      await connected(page, { capabilities: [...allCapabilities, "compile"] });
      await simulator(page);
      await newWorkflow(page);
      await new DesignerPage(page).setSource(source);
      await closeSheet(page);
      await command(page, "Simulate");
      const setup = page.getByRole("dialog", {
        name: "Simulate this workflow",
      });
      await setup.getByLabel(/customer ?id/i).fill("c-1");
      await setup.getByRole("button", { name: "Start simulation" }).click();
      const simulation = simulationPanel(page);
      await expect(simulation).toContainText("Waiting");
      const title = simulation.getByRole("heading", { name: "Simulation" });
      await expect(title).toBeFocused();
      if (viewport.width > 1024) {
        // Docked beside the canvas: a region, and the page stays usable
        // (only the step palette waits until the simulation stops).
        await expect(simulation).not.toHaveAttribute("aria-modal", "true");
        await expect(page.locator("[inert]:not(.palette)")).toHaveCount(0);
        await page.getByRole("button", { name: "Fit all" }).click();
        return;
      }
      await expect(simulation).toHaveAttribute("role", "dialog");
      await expect(simulation).toHaveAttribute("aria-modal", "true");
      // The canvas and the toolbar under it are inert.
      for (const covered of [".canvas-panel", ".editor-toolbar"])
        expect(
          await page.locator(covered).evaluate((e) => !!e.closest("[inert]")),
          covered,
        ).toBe(true);
      // Tab stays in the panel.
      for (const key of ["Tab", "Shift+Tab"])
        for (let i = 0; i < 10; i++) {
          await page.keyboard.press(key);
          expect(
            await simulation.evaluate((e) =>
              e.contains(document.activeElement),
            ),
          ).toBe(true);
        }
      // Escape folds it away (the session stays) and focuses its toggle.
      await page.keyboard.press("Escape");
      const expand = simulation.getByRole("button", {
        name: "Expand simulation",
      });
      await expect(expand).toBeFocused();
      await expect(simulation).not.toHaveAttribute("aria-modal", "true");
      // Only the step palette waits until the simulation stops.
      await expect(page.locator("[inert]:not(.palette)")).toHaveCount(0);
      // Expanded again, a click beside it folds it too.
      await expand.click();
      await expect(simulation).toHaveAttribute("aria-modal", "true");
      await page.locator(".sheet-scrim").click({ position: { x: 4, y: 4 } });
      await expect(expand).toBeFocused();
      // Closing the simulation returns focus to Simulate.
      await expand.click();
      await simulation
        .getByRole("button", { name: "Close simulation" })
        .click();
      await expect(simulation).toHaveCount(0);
      // Simulate, or the More menu that holds it on narrow layouts.
      const toolbar = page.getByRole("toolbar", { name: "Workflow commands" });
      const simulate = toolbar.getByRole("button", {
        name: "Simulate",
        exact: true,
      });
      await expect(
        (await simulate.isVisible())
          ? simulate
          : toolbar.getByRole("button", { name: "More", exact: true }),
      ).toBeFocused();
      await expect(page.locator("[inert]")).toHaveCount(0);
    });

    test("a failed command stays in the simulation panel", async ({ page }) => {
      await connected(page, { capabilities: [...allCapabilities, "compile"] });
      const { creates } = await simulator(page);
      await page.route(`${project}/debug/sessions/*/commands`, (r) =>
        r.fulfill({
          status: 409,
          json: {
            code: "WV-DEBUG-REVISION",
            message: "The debug session changed.",
          },
        }),
      );
      await newWorkflow(page);
      await new DesignerPage(page).setSource(source);
      const close = page.getByRole("button", { name: "Close inspector" });
      if (await close.isVisible()) await close.click();
      await command(page, "Simulate");
      const setup = page.getByRole("dialog", {
        name: "Simulate this workflow",
      });
      await setup.getByLabel(/customer ?id/i).fill("c-1");
      await setup.getByRole("button", { name: "Start simulation" }).click();
      await expect.poll(() => creates.length).toBe(1);
      const simulation = simulationPanel(page);
      await simulation.getByRole("button", { name: "Continue" }).click();
      await expect(simulation.getByRole("alert")).toContainText(
        "WV-DEBUG-REVISION",
      );
      // The page-level error banner stays quiet; the panel owns the failure.
      await expect(page.locator(".error-banner")).toHaveCount(0);
    });
  });

test.describe("1280x720", () => {
  test.use({ viewport: { width: 1280, height: 720 } });
  test.skip(!artifact, "Needs the repository's Python environment.");

  test("the simulation docks beside the canvas and draws the live thread", async ({
    page,
  }) => {
    await connected(page, { capabilities: [...allCapabilities, "compile"] });
    await simulator(page);
    // The run finished action-1 and waits at wait-1.
    await page.route(`${project}/debug/sessions`, (r: Route) =>
      r.fulfill({
        status: 201,
        json: {
          ...session(1, "waiting", ["wait-1"], { "wait-1": later(60) }),
          view: {
            ...session(1, "waiting", ["wait-1"], { "wait-1": later(60) }).view,
            variables: {
              waits: { "wait-1": later(60) },
              steps: { "action-1": { output: { eligible: true } } },
            },
          },
        },
      }),
    );
    await newWorkflow(page);
    await new DesignerPage(page).setSource(source);
    await command(page, "Simulate");
    const setup = page.getByRole("dialog", { name: "Simulate this workflow" });
    await setup.getByLabel(/customer ?id/i).fill("c-1");
    await setup.getByRole("button", { name: "Start simulation" }).click();
    const simulation = simulationPanel(page);
    await expect(simulation).toContainText("Waiting");
    // Docked in the inspector's place: never over the toolbar.
    const panel = await simulation.boundingBox();
    const toolbar = await page.locator(".editor-toolbar").boundingBox();
    expect(
      panel!.y >= toolbar!.y + toolbar!.height ||
        panel!.y + panel!.height <= toolbar!.y,
    ).toBe(true);
    await expect(page.locator(".inspector")).toBeHidden();
    // The live thread: one live step, the finished one checked, the rest faded.
    await expect(page.locator(".graph-node.is-live")).toHaveCount(1);
    await expect(page.locator('[data-step="wait-1"]')).toHaveClass(
      /\bis-live\b/,
    );
    await expect(page.locator('[data-step="action-1"]')).toHaveClass(
      /\bis-done\b/,
    );
    await expect(
      page.locator('[data-step="action-1"] .node-badge'),
    ).toBeVisible();
    await expect(page.locator('[data-step="review"]')).toHaveClass(
      /\bis-unreached\b/,
    );
    await expect(page.locator("path.edge.edge-taken").first()).toBeAttached();
    // Only the control for what the run waits for, as the one primary.
    await expect(simulation.locator("button.primary")).toHaveCount(1);
    await expect(simulation.locator("button.primary")).toHaveText(
      "Finish the wait at wait-1 (1 min)",
    );
    await expect(simulation).not.toContainText("Send a signal");
    await expect(simulation).toContainText("Now at wait-1");
    // Editing waits until the simulation stops.
    await expect(page.locator(".editing-paused")).toContainText(
      "Simulating. Editing is paused.",
    );
    await expect(page.locator(".insertion-target")).toHaveCount(0);
    expect(
      await page.locator(".palette").evaluate((e) => e.hasAttribute("inert")),
    ).toBe(true);
    // Selecting a step shows what the simulation recorded for it.
    await page.locator('[data-step="action-1"] .node-body').click();
    await expect(simulation).toContainText('"eligible": true');
    await page.getByRole("button", { name: "Stop simulation" }).click();
    await expect(simulation).toHaveCount(0);
    await expect(page.locator(".graph-node.is-live")).toHaveCount(0);
  });
});
