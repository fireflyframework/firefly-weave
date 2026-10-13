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
// Apps and published actions in Add a step: an app opens on Actions (n) and
// Triggers (n), versions open newest first, workers group by task, and an
// entry the platform can't run stays visible, disabled, with the reason.
import { expect, test, type Download, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { AddStepPanel, languageFeatures } from "./add-step-po";
import { CanvasPage, openNewWorkflow, useNewEditor } from "./canvas-po";
import {
  command,
  connected,
  lookupAction,
  newWorkflow,
  sourceText,
  workerAction,
} from "./support";

/** A workflow with one step, and Add a step open after it. */
async function panelAfterFirstStep(page: Page, canvas: CanvasPage) {
  await canvas.addFirstStep("Transform");
  await canvas.closeInspector();
  await canvas.insertTarget("Add a step after transform-1").click();
  const panel = new AddStepPanel(page);
  await expect(panel.title).toHaveText("What happens next?");
  return panel;
}

test.describe("apps in Add a step", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  test("Action in an app starts with Call an API; an app opens on its actions and triggers", async ({
    page,
  }) => {
    const panel = await panelAfterFirstStep(page, await openNewWorkflow(page));
    await panel.option("Action in an app").click();
    await expect(panel.title).toHaveText("Action in an app");
    const apps = await panel.labels();
    expect(apps.slice(0, 2)).toEqual(["Call an API", "Call an action"]);
    expect(apps).toEqual(
      expect.arrayContaining(["HTTP APIs", "Email", "SFTP"]),
    );
    await panel.option("Email").click();
    await expect(panel.title).toHaveText("Email");
    await expect
      .poll(() => panel.headings())
      .toEqual(["Actions (2)", "Triggers (1)"]);
    await expect
      .poll(() => panel.labels())
      .toEqual(["Send email", "Reply to an email", "Email"]);
    await expect(panel.list).toContainText(
      "Connect to a platform to see its published actions.",
    );
    await panel.option("Send email").click();
    await expect(panel.root).toHaveCount(0);
    await expect(
      page.getByRole("dialog", { name: "Step details: send-email" }),
    ).toBeVisible();
  });

  test("a file step chosen under FTP calls an action on the FTP server", async ({
    page,
  }) => {
    const canvas = await openNewWorkflow(page);
    const panel = await panelAfterFirstStep(page, canvas);
    await panel.option("Action in an app").click();
    await panel.option("FTP").click();
    await panel.option("Write a file").click();
    await expect(
      page.getByRole("dialog", { name: "Step details: write-file" }),
    ).toBeVisible();
    await canvas.closeInspector();
    expect(await sourceText(page)).toMatch(
      /uses: untitled-workflow\.write-file@1\.0\.0/,
    );
    // The step's own action keeps its file server in the canvas file.
    const downloads: Download[] = [];
    page.on("download", (download) => downloads.push(download));
    await command(page, "Save to file");
    await expect.poll(() => downloads.length).toBe(2);
    const file = downloads.find((download) =>
      download.suggestedFilename().endsWith(".canvas.json"),
    )!;
    const saved = JSON.parse(await readFile((await file.path())!, "utf8"));
    expect(saved.actionRecipes["untitled-workflow.write-file"]).toMatchObject({
      kind: "connector",
      connector: "weave-ftp@1.0.0",
      action: "write",
    });
  });

  test("installed apps list their published actions; All versions opens every version, newest first", async ({
    page,
  }) => {
    await useNewEditor(page);
    await connected(page, {
      catalog: [
        { id: "a1", document: lookupAction },
        {
          id: "a4",
          document: {
            ...lookupAction,
            metadata: { name: "sql.lookup", version: "1.1.0" },
          },
        },
        { id: "a2", document: workerAction },
      ],
    });
    await page.route("**/projects/project/connector-descriptors?*", (route) =>
      route.fulfill({
        json: {
          items: [
            { adapter: "weave-http", reference: "weave-http@2.0.0" },
            { adapter: "weave-email", reference: "weave-email@1.0.0" },
            {
              adapter: "weave-postgresql",
              reference: "weave-postgresql@1.0.0",
            },
          ],
          next_cursor: null,
        },
      }),
    );
    await newWorkflow(page);
    const canvas = new CanvasPage(page);
    await canvas.ready();
    const panel = await panelAfterFirstStep(page, canvas);
    await panel.option("Action in an app").click();
    await expect(panel.option("PostgreSQL")).toContainText("1 action");
    await expect(panel.option("Custom workers")).toBeVisible();
    await expect
      .poll(() => panel.labels())
      .toEqual([
        "Call an API",
        "Call an action",
        "HTTP APIs",
        "Email",
        "FTP",
        "FTPS",
        "Google Drive",
        "Microsoft drive",
        "PostgreSQL",
        "SFTP",
        "Custom workers",
      ]);
    await panel.option("Custom workers").click();
    await expect.poll(() => panel.headings()).toEqual(["crm-lookup"]);
    await expect.poll(() => panel.labels()).toEqual(["crm.lookup"]);
    await page.keyboard.press("ArrowLeft");
    await panel.option("PostgreSQL").click();
    await expect
      .poll(() => panel.labels())
      .toEqual(["sql.lookup", "All versions of sql.lookup"]);
    await panel.option("All versions of sql.lookup").click();
    await expect(panel.title).toHaveText("sql.lookup versions");
    await expect(panel.list.locator(".entry-description")).toHaveText([
      "Version 1.1.0 · read",
      "Version 1.0.0 · read",
    ]);
    await panel.list.getByRole("option").nth(1).click();
    await expect(panel.root).toHaveCount(0);
    await canvas.closeInspector();
    expect(await sourceText(page)).toMatch(/uses: sql\.lookup@1\.0\.0/);
  });
});

test.describe("what the platform runs", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
  });

  test("entries the platform can't run stay visible, disabled, with the reason, and Enter does nothing", async ({
    page,
  }) => {
    await languageFeatures(page, ["text.concat"]);
    const canvas = await openNewWorkflow(page);
    const panel = await panelAfterFirstStep(page, canvas);
    await panel.option("Flow").click();
    const loop = panel.option("Loop over items");
    await expect(loop).toHaveAttribute("aria-disabled", "true");
    await expect(loop).toContainText(
      "Needs a platform with loops (flow.forEach)",
    );
    // A real pointer on the disabled entry, then Enter on it: nothing happens.
    const spot = (await loop.boundingBox())!;
    await page.mouse.click(spot.x + spot.width / 2, spot.y + spot.height / 2);
    await expect(panel.title).toHaveText("Flow");
    // The pointer made it the active entry; Enter from the search chooses nothing.
    await panel.search.focus();
    await expect.poll(() => panel.activeLabel()).toBe("Loop over items");
    await page.keyboard.press("Enter");
    await expect(panel.title).toHaveText("Flow");
    await expect(canvas.root.locator(".tile-node[data-step]")).toHaveCount(1);
    await page.keyboard.press("ArrowLeft");
    await panel.option("Data transformation").click();
    await expect(panel.option("Text template")).not.toHaveAttribute(
      "aria-disabled",
      "true",
    );
    await panel.option("Text template").click();
    await expect(
      page.getByRole("dialog", { name: "Step details: text-template" }),
    ).toBeVisible();
    await canvas.closeInspector();
    expect(await canvas.rootSteps()).toEqual(["transform-1", "text-template"]);
  });

  test("without text templates, Text template says what the platform needs", async ({
    page,
  }) => {
    await languageFeatures(page, []);
    const canvas = await openNewWorkflow(page);
    await canvas.insertTarget("Add first step").click();
    const panel = new AddStepPanel(page);
    await expect(panel.option("Called by a workflow")).toContainText(
      "Needs a platform with sub-workflows (flow.callWorkflow)",
    );
    await panel.search.fill("text template");
    await expect(panel.option("Text template")).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    await expect(panel.option("Text template")).toContainText(
      "Needs a platform with text templates (text.concat)",
    );
  });
});
