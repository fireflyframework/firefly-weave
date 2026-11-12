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
// The canvas file: notes, trigger choices, step notes and the recipes of
// the actions a workflow owns travel with its workflow file. Save to file
// writes version 2; Import reads version 2, version 1 layout files and
// drafts saved before the canvas file existed.
import { expect, test, type Download, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { stringify } from "yaml";
import { CanvasPage, openStoredWorkflow, useNewEditor } from "./canvas-po";
import { command, offline, withClassicDesigner } from "./support";

const workflow = stringify({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "order-intake", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [
      { id: "check-customer", kind: "transform", value: { ref: "/input" } },
      // Calls the action this workflow owns: Studio drops the recipes of
      // owned actions no step calls when a workflow opens.
      {
        id: "get-customer-orders",
        kind: "action",
        uses: "order-intake.get-customer-orders@1.0.0",
        with: { object: {} },
      },
    ],
    output: { literal: {} },
  },
});
const canvas = {
  schemaVersion: 2,
  kind: "weave.studio/canvas",
  notes: [
    {
      id: "note-1a2b3c4d",
      text: "**Reviewed** with ops",
      color: "blue",
      x: 40,
      y: 260,
      width: 240,
      height: 120,
      anchor: { stepId: "check-customer", dx: 0, dy: 168 },
    },
  ],
  stepNotes: {
    "check-customer": { text: "Uses the CRM sandbox", showOnCanvas: true },
  },
  triggers: [
    { id: "trigger-0a1b2c3d", kind: "manual" },
    {
      id: "trigger-1b2c3d4e",
      kind: "webhook",
      name: "orders-in",
      secretRef: "orders-webhook",
    },
  ],
  ownedActions: ["order-intake.get-customer-orders"],
  actionRecipes: {
    "order-intake.get-customer-orders": {
      kind: "http",
      method: "GET",
      pathTemplate: "/orders",
      statuses: [200],
      timeoutSeconds: 30,
    },
  },
};
const digest = expect.stringMatching(/^[0-9a-f]{64}$/);
const file = (name: string, content: string) => ({
  name,
  mimeType: name.endsWith(".json") ? "application/json" : "application/yaml",
  buffer: Buffer.from(content),
});

/** Save to file, then the files it wrote, by name. */
async function exported(page: Page): Promise<Record<string, string>> {
  const downloads: Download[] = [];
  const listener = (download: Download) => downloads.push(download);
  page.on("download", listener);
  await command(page, "Save to file");
  await expect.poll(() => downloads.length).toBe(2);
  page.off("download", listener);
  const files: Record<string, string> = {};
  for (const download of downloads)
    files[download.suggestedFilename()] = await readFile(
      (await download.path())!,
      "utf8",
    );
  return files;
}
async function importFiles(
  page: Page,
  files: { name: string; mimeType: string; buffer: Buffer }[],
) {
  await page.getByLabel("Choose a workflow file").setInputFiles(files);
  await new CanvasPage(page).ready();
}
const toast = (page: Page, text: string) =>
  page.locator(".toast").filter({ hasText: text });

test.describe("the canvas file", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await useNewEditor(page);
    await offline(page);
  });

  test("opening a workflow file is not a step Undo takes back", async ({
    page,
  }) => {
    await importFiles(page, [file("order-intake.yaml", workflow)]);
    const canvas = new CanvasPage(page);
    const before = await canvas.rootSteps();
    expect(before.length).toBeGreaterThan(0);
    await expect(
      page
        .getByRole("toolbar", { name: "Workflow commands" })
        .getByRole("button", { name: /^Undo/ }),
    ).toBeDisabled();
    await canvas.root.focus();
    await page.keyboard.press("ControlOrMeta+z");
    expect(await canvas.rootSteps()).toEqual(before);
  });

  test("opens with its workflow and saves as a version 2 canvas file, owned actions included", async ({
    page,
  }) => {
    await importFiles(page, [
      file("order-intake.yaml", workflow),
      file("order-intake.canvas.json", JSON.stringify(canvas)),
    ]);
    const files = await exported(page);
    expect(Object.keys(files).sort()).toEqual([
      "order-intake.canvas.json",
      "order-intake.yaml",
    ]);
    // The anchored note is saved where the canvas draws it, under its step,
    // so it keeps that place if its step is deleted later.
    expect(JSON.parse(files["order-intake.canvas.json"])).toEqual({
      ...canvas,
      notes: [{ ...canvas.notes[0], x: 344, y: 264 }],
      semanticDigest: digest,
    });
    await expect(toast(page, "don't keep the settings")).toHaveCount(0);
  });

  test("migrates a version 1 layout file: the viewport stays, positions go", async ({
    page,
  }) => {
    const layout = {
      draftId: "d",
      schemaVersion: 1,
      semanticDigest: "abc",
      revision: 2,
      positions: { "check-customer": { x: 400, y: 80 } },
      viewport: { x: 12, y: 34, zoom: 0.8 },
    };
    await importFiles(page, [
      file("order-intake.layout.json", JSON.stringify(layout)),
      file("order-intake.yaml", workflow),
    ]);
    const saved = JSON.parse(
      (await exported(page))["order-intake.canvas.json"],
    );
    expect(saved).toEqual({
      schemaVersion: 2,
      kind: "weave.studio/canvas",
      viewport: { x: 12, y: 34, zoom: 0.8 },
      semanticDigest: digest,
    });
  });

  test("a canvas file that doesn't parse leaves the workflow open and says why", async ({
    page,
  }) => {
    await importFiles(page, [
      file("order-intake.yaml", workflow),
      file("order-intake.canvas.json", "{"),
    ]);
    await expect(
      toast(
        page,
        "The workflow opened without its canvas file: it isn't JSON.",
      ),
    ).toBeVisible();
    await expect(new CanvasPage(page).tile("check-customer")).toBeVisible();
    await page.getByRole("button", { name: "Home", exact: true }).click();
    await importFiles(page, [
      file("order-intake.yaml", workflow),
      file("order-intake.canvas.json", JSON.stringify({ schemaVersion: 3 })),
    ]);
    await expect(
      toast(
        page,
        "The workflow opened without its canvas file: this isn't a Studio canvas file.",
      ),
    ).toBeVisible();
  });

  test("a canvas file for other steps keeps its notes without their anchors", async ({
    page,
  }) => {
    const other = {
      ...canvas,
      notes: [
        {
          ...canvas.notes[0],
          anchor: { stepId: "other-step", dx: 0, dy: 168 },
        },
      ],
      stepNotes: { "other-step": { text: "Gone" } },
    };
    await importFiles(page, [
      file("order-intake.yaml", workflow),
      file("order-intake.canvas.json", JSON.stringify(other)),
    ]);
    await expect(
      toast(
        page,
        "The canvas file mentions steps this workflow doesn't have. Their step notes were left out, and notes anchored to them stay on the canvas without an anchor.",
      ),
    ).toBeVisible();
    const saved = JSON.parse(
      (await exported(page))["order-intake.canvas.json"],
    );
    expect(saved.notes[0].anchor).toBeUndefined();
    expect(saved.stepNotes).toBeUndefined();
  });

  test("a canvas file alone asks for its workflow", async ({ page }) => {
    await page
      .getByLabel("Choose a workflow file")
      .setInputFiles([
        file("order-intake.canvas.json", JSON.stringify(canvas)),
      ]);
    await expect(
      page.getByText(
        "Choose the workflow file too: a canvas file opens only with its workflow.",
      ),
    ).toBeVisible();
  });
});

test("with the new editor off, Import takes one workflow file at a time, as before", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await withClassicDesigner(page);
  await offline(page);
  await expect(page.getByLabel("Choose a workflow file")).not.toHaveAttribute(
    "multiple",
  );
  const files = await page.evaluateHandle(() => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(["kind: Workflow"], "order-intake.yaml"));
    transfer.items.add(new File(["{}"], "order-intake.canvas.json"));
    return transfer;
  });
  await page
    .locator(".home-header")
    .dispatchEvent("drop", { dataTransfer: files });
  await expect(
    page.getByText("Choose one workflow file at a time.", { exact: true }),
  ).toBeVisible();
});

test("a draft kept on this computer before the canvas file keeps its viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openStoredWorkflow(page, {
    id: "kept-before",
    source: workflow,
    layout: { revision: 1, positions: {}, viewport: { x: 5, y: 6, zoom: 0.9 } },
  });
  const saved = JSON.parse((await exported(page))["order-intake.canvas.json"]);
  expect(saved).toEqual({
    schemaVersion: 2,
    kind: "weave.studio/canvas",
    viewport: { x: 5, y: 6, zoom: 0.9 },
    semanticDigest: digest,
  });
});
