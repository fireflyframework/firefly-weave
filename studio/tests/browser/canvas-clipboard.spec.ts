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
// Copy, cut and paste of steps as YAML on the new canvas: between two
// workflows with renumbered IDs, the slots, profiles and own actions they
// bring, and a warning for references the new place can't read.
import { expect, test, type Download, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { stringify } from "yaml";
import { CanvasPage, openStoredWorkflow, openWorkflow } from "./canvas-po";
import { command, newWorkflow, sourceText } from "./support";

test.use({ permissions: ["clipboard-read", "clipboard-write"] });

const sourceA = stringify({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "order-intake", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    connections: { crm: { connector: "weave-http@2.0.0", required: true } },
    llmProfiles: { default: { model: "qwen3" } },
    steps: [
      {
        id: "check-customer",
        kind: "action",
        uses: "onboarding.check-customer@1.0.0",
        with: { ref: "/input" },
        connection: "crm",
      },
      {
        id: "summarize",
        kind: "llm",
        uses: "weave-agentic-generate@1.0.0",
        profile: "default",
        connection: "ai",
        prompt: { ref: "/steps/check-customer/output" },
        context: { literal: {} },
      },
      {
        id: "notify",
        kind: "transform",
        value: { ref: "/steps/summarize/output" },
      },
    ],
    output: { literal: {} },
  },
});
const toast = (page: Page, text: string) =>
  page.locator(".toast").filter({ hasText: text });

test.describe("copy and paste of steps", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  test("copy and paste between two workflows renumbers IDs, brings slots and warns about references it can't read", async ({
    page,
  }) => {
    const a = await openStoredWorkflow(page, { id: "clip-a", source: sourceA });
    await a.tileBody("check-customer").focus();
    await page.keyboard.press("Shift+ArrowRight");
    // Focus follows the selection after the next render; the next key starts there.
    await expect(a.tileBody("summarize")).toBeFocused();
    await page.keyboard.press("Shift+ArrowRight");
    await expect(a.tileBody("notify")).toBeFocused();
    await page.keyboard.press("ControlOrMeta+c");
    await expect(toast(page, "Copied 3 steps.")).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toMatch(
      /^# Firefly Weave steps\. Paste into Studio\.\nweaveClipboard: 1\n/,
    );

    await page.getByRole("button", { name: "Home", exact: true }).click();
    await newWorkflow(page);
    const b = new CanvasPage(page);
    await b.ready();
    await expect(
      b.root.getByText(/or paste steps \((Ctrl|Cmd)\+V\)/),
    ).toBeVisible();
    await b.root.focus();
    await page.keyboard.press("ControlOrMeta+v");
    const dialog = page.getByRole("dialog", {
      name: "Add what the pasted steps use?",
    });
    await expect(dialog).toContainText(
      "the connection slot crm (weave-http@2.0.0)",
    );
    await expect(dialog).toContainText("the AI profile default");
    await dialog
      .getByRole("button", { name: "Paste and add", exact: true })
      .click();
    await expect(b.tile("notify")).toBeVisible();
    await b.root.focus();
    await page.keyboard.press("ControlOrMeta+v");
    await expect(b.tile("notify-1")).toBeVisible();
    await expect(
      toast(page, "3 steps got new IDs, because theirs were taken."),
    ).toBeVisible();
    const source = await sourceText(page);
    expect(source).toMatch(/crm:\s+connector: weave-http@2\.0\.0/);
    expect(source).toMatch(/ref: \/steps\/summarize-1\/output/);

    await b.tileBody("notify-1").focus();
    await page.keyboard.press("ControlOrMeta+c");
    // The clipboard is written asynchronously; its toast says it holds the step.
    await expect(toast(page, "Copied notify-1.")).toBeVisible();
    await b.tileBody("$trigger:manual").focus();
    await page.keyboard.press("ControlOrMeta+v");
    const warning = toast(
      page,
      "1 reference can't be read here (summarize-1 runs later or on another path).",
    );
    await expect(warning).toBeVisible();
    await expect(
      b.tile("notify-2").locator('.tile-badge[data-badge="setup"]'),
    ).toBeAttached();
    await expect(b.tileBody("notify-2")).toHaveAttribute(
      "aria-label",
      /Setup needed: replace the reference to summarize-1/,
    );
    await warning.getByRole("button", { name: "Review", exact: true }).click();
    await expect(b.tileBody("notify-2")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  test("a Call an API step pasted into another workflow keeps its request", async ({
    page,
  }) => {
    const a = await openStoredWorkflow(page, {
      id: "clip-owned",
      source: stringify({
        apiVersion: "weave/v1alpha1",
        kind: "Workflow",
        metadata: { name: "order-intake", version: "1.0.0" },
        spec: {
          inputSchema: { type: "object" },
          outputSchema: { type: "object" },
          steps: [
            {
              id: "call-api",
              kind: "action",
              uses: "order-intake.call-api@1.0.0",
              with: { literal: {} },
            },
          ],
          output: { literal: {} },
        },
      }),
      canvas: {
        schemaVersion: 2,
        kind: "weave.studio/canvas",
        ownedActions: ["order-intake.call-api"],
        actionRecipes: {
          "order-intake.call-api": {
            kind: "http",
            method: "GET",
            pathTemplate: "/orders",
            statuses: [200],
            timeoutSeconds: 30,
          },
        },
      },
    });
    await a.tileBody("call-api").focus();
    await page.keyboard.press("ControlOrMeta+c");
    await expect(toast(page, "Copied call-api.")).toBeVisible();
    await page.getByRole("button", { name: "Home", exact: true }).click();
    await newWorkflow(page);
    const b = new CanvasPage(page);
    await b.ready();
    await b.root.focus();
    await page.keyboard.press("ControlOrMeta+v");
    await expect(b.tile("call-api")).toBeVisible();
    expect(await sourceText(page)).toMatch(
      /uses: untitled-workflow\.call-api@1\.0\.0/,
    );
    const downloads: Download[] = [];
    page.on("download", (download) => downloads.push(download));
    await command(page, "Save to file");
    await expect.poll(() => downloads.length).toBe(2);
    const file = downloads.find((download) =>
      download.suggestedFilename().endsWith(".canvas.json"),
    )!;
    const saved = JSON.parse(await readFile((await file.path())!, "utf8"));
    expect(saved.actionRecipes["untitled-workflow.call-api"]).toMatchObject({
      kind: "http",
      method: "GET",
      pathTemplate: "/orders",
    });
  });

  test("cut and paste move a step; copying steps that aren't next to each other is refused with its reason", async ({
    page,
  }) => {
    const canvas = await openStoredWorkflow(page, {
      id: "clip-cut",
      source: stringify({
        apiVersion: "weave/v1alpha1",
        kind: "Workflow",
        metadata: { name: "three-steps", version: "1.0.0" },
        spec: {
          inputSchema: { type: "object" },
          outputSchema: { type: "object" },
          steps: ["first", "second", "third"].map((id) => ({
            id,
            kind: "transform",
            value: { literal: {} },
          })),
          output: { literal: {} },
        },
      }),
    });
    await canvas.tileBody("first").click();
    await canvas.tileBody("third").click({ modifiers: ["ControlOrMeta"] });
    await canvas.tileBody("third").focus();
    await page.keyboard.press("ControlOrMeta+c");
    await expect(
      toast(
        page,
        "Copy works on steps next to each other in one path. Select a single run of steps.",
      ),
    ).toBeVisible();
    await canvas.tileBody("third").click();
    await canvas.tileBody("third").focus();
    await page.keyboard.press("ControlOrMeta+x");
    await expect(canvas.tile("third")).toHaveCount(0);
    await canvas.tileBody("first").focus();
    await page.keyboard.press("ControlOrMeta+v");
    // The paste reads the clipboard first: wait for it before reading Source.
    await expect(toast(page, "Pasted third.")).toBeVisible();
    expect(await canvas.rootSteps()).toEqual(["first", "third", "second"]);
  });

  test("cut that can't delete leaves the clipboard as it was", async ({
    page,
  }) => {
    const canvas = await openStoredWorkflow(page, {
      id: "clip-cut-refused",
      source: stringify({
        apiVersion: "weave/v1alpha1",
        kind: "Workflow",
        metadata: { name: "read-by-output", version: "1.0.0" },
        spec: {
          inputSchema: { type: "object" },
          outputSchema: { type: "object" },
          steps: [{ id: "result", kind: "transform", value: { literal: {} } }],
          output: { ref: "/steps/result/output" },
        },
      }),
    });
    await page.evaluate(() => navigator.clipboard.writeText("kept text"));
    await canvas.tileBody("result").click();
    await canvas.tileBody("result").focus();
    await page.keyboard.press("ControlOrMeta+x");
    await expect(
      toast(
        page,
        "This step is referenced by the workflow output. Update it before deleting it.",
      ),
    ).toBeVisible();
    await expect(canvas.tile("result")).toHaveCount(1);
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
      "kept text",
    );
  });

  test("typing in editable text on the canvas never pastes steps or runs the canvas's keys", async ({
    page,
  }) => {
    const canvas = await openStoredWorkflow(page, {
      id: "clip-editable",
      source: stringify({
        apiVersion: "weave/v1alpha1",
        kind: "Workflow",
        metadata: { name: "editable", version: "1.0.0" },
        spec: {
          inputSchema: { type: "object" },
          outputSchema: { type: "object" },
          steps: [{ id: "first", kind: "transform", value: { literal: {} } }],
          output: { literal: {} },
        },
      }),
    });
    // Editable text a later canvas feature could draw: a paragraph inside
    // contenteditable, and plain-text editing.
    await canvas.root.evaluate((root) => {
      const rich = document.createElement("div");
      rich.contentEditable = "true";
      rich.className = "zz-rich";
      rich.innerHTML = "<p>rich</p>";
      const plain = document.createElement("div");
      plain.setAttribute("contenteditable", "plaintext-only");
      plain.className = "zz-plain";
      plain.textContent = "plain";
      for (const [index, element] of [rich, plain].entries())
        Object.assign(element.style, {
          position: "absolute",
          top: `${80 + index * 48}px`,
          left: "80px",
          zIndex: "50",
          width: "200px",
          minHeight: "32px",
          background: "white",
        });
      root.append(rich, plain);
    });
    await page.evaluate(() =>
      navigator.clipboard.writeText(
        "- {id: injected, kind: wait, durationSeconds: 60}",
      ),
    );
    for (const target of [".zz-rich p", ".zz-plain"]) {
      await canvas.root.locator(target).click();
      await page.keyboard.press("End");
      await page.keyboard.press("ControlOrMeta+v");
      await page.keyboard.type("n/");
      await page.keyboard.press("Shift+S");
      await expect(canvas.root.locator(target)).toContainText("injected");
      await expect(canvas.root.locator(target)).toContainText("n/S");
    }
    await expect(canvas.tile("injected")).toHaveCount(0);
    await expect(page.locator(".add-step-panel")).toHaveCount(0);
    await expect(canvas.root.locator(".sticky-note")).toHaveCount(0);
  });

  test("the step menu and the selection toolbar copy too", async ({ page }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("approval").hover();
    await canvas.root
      .getByRole("button", { name: "More commands for approval", exact: true })
      .click();
    await page.getByRole("menuitem", { name: "Copy", exact: true }).click();
    await expect(toast(page, "Copied approval.")).toBeVisible();
    await canvas.tileBody("prepare-request").click();
    await canvas.tileBody("approval").click({ modifiers: ["Shift"] });
    await canvas.root
      .getByRole("toolbar", { name: "Selected steps" })
      .getByRole("button", { name: "Copy", exact: true })
      .click();
    await expect(toast(page, "Copied 2 steps.")).toBeVisible();
  });

  test("pasting text that isn't steps changes nothing and says why", async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    const canvas = await openWorkflow(page);
    for (const [text, reason] of [
      ["https://example.com/orders", "The clipboard doesn't hold Weave steps."],
      [
        "base: &b {id: a, kind: wait}\nsteps: [*b]",
        "Studio doesn't paste YAML aliases (*name). Copy the steps without them.",
      ],
      [
        "- {id: a, kind: teleport}",
        "This paste has a step kind Studio doesn't know: teleport.",
      ],
    ]) {
      await page.evaluate(
        (value) => navigator.clipboard.writeText(value),
        text,
      );
      await canvas.root.focus();
      await page.keyboard.press("ControlOrMeta+v");
      await expect(toast(page, reason)).toBeVisible();
    }
    expect(await canvas.rootSteps()).toEqual([
      "prepare-request",
      "approval",
      "route",
      "record-result",
    ]);
    expect(errors).toEqual([]);
  });

  test("nothing is pasted while the source doesn't parse", async ({ page }) => {
    const canvas = await openWorkflow(page);
    await canvas.tileBody("approval").focus();
    await page.keyboard.press("ControlOrMeta+c");
    await page.getByRole("tab", { name: "Source", exact: true }).click();
    await page
      .getByRole("textbox", { name: "Workflow source" })
      .fill("kind: [");
    await page
      .getByRole("button", { name: "Apply changes", exact: true })
      .click();
    await page.getByRole("tab", { name: "Designer", exact: true }).click();
    await canvas.ready();
    await canvas.root.focus();
    await page.keyboard.press("ControlOrMeta+v");
    await expect(
      toast(page, "Fix the source before pasting steps."),
    ).toBeVisible();
  });
});
