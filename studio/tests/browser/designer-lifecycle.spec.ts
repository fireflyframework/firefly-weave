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
// The designer's editor bar: one 52 px bar, one primary command that follows
// the workflow's lifecycle, reasons a person can read, and a canvas that keeps
// the room the chrome gives back.
import { test, expect, type Page } from "@playwright/test";
import {
  allCapabilities,
  closeSheet,
  command,
  connected,
  insertStep,
  newWorkflow,
  offline,
} from "./support";
import { DesignerPage } from "./designer-po";

const project = "**/studio/api/api/v1/tenants/tenant/projects/project";
const environment = `${project}/environments/development`;
const versionId = "11111111-1111-4111-8111-111111111111";

const toolbar = (page: Page) =>
  page.getByRole("toolbar", { name: "Workflow commands" });
/** Exactly one primary command, and what it says. */
async function primary(page: Page) {
  const primaries = page.locator(".editor-toolbar button.primary");
  await expect(primaries).toHaveCount(1);
  return primaries;
}
/** Save, publish and activate answer like a platform does. */
async function lifecyclePlatform(page: Page) {
  const calls: string[] = [];
  await page.route("**/drafts/*", (r) => {
    calls.push("save");
    return r.fulfill({
      json: { id: "draft-1", revision: 1, document: {} },
    });
  });
  await page.route(`${project}/workflows`, (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    calls.push("publish");
    return r.fulfill({
      status: 201,
      json: {
        id: versionId,
        name: "untitled-workflow",
        version: "1.0.0",
        digest: "sha256:abc",
      },
    });
  });
  await page.route(`${environment}/activations`, (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    calls.push("activate");
    return r.fulfill({
      status: 201,
      json: {
        id: "act-1",
        version_id: versionId,
        workflow: "untitled-workflow",
        version: "1.0.0",
      },
    });
  });
  return calls;
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`${viewport.width}x${viewport.height}`, () => {
    test.use({ viewport });

    test("working locally, Save to file leads and the bar says what needs a platform", async ({
      page,
    }) => {
      await offline(page);
      await newWorkflow(page);
      await insertStep(page, "Wait for time");
      await closeSheet(page);
      // The autosave settles on its own: the chip doesn't stay on "Saving…".
      await expect(page.locator(".editor-identity .status-chip")).toHaveText(
        /Draft saved \d{2}:\d{2}/,
      );
      await expect(await primary(page)).toHaveText(/Save to file/);
      await toolbar(page).locator(".toolbar-help > summary").click();
      await expect(toolbar(page)).toContainText(
        "Save to file downloads a copy.",
      );
      await expect(
        toolbar(page).getByRole("button", { name: "Connect to a platform" }),
      ).toBeVisible();
      await toolbar(page).locator(".toolbar-help > summary").click();
      // Simulate stays focusable and says why it can't run.
      await command(page, "Simulate");
      await expect(page.locator(".command-note")).toHaveText(
        "Connect to a platform to run simulations.",
      );
      await expect(page.locator(".command-note")).toHaveAttribute(
        "role",
        "status",
      );
      // ⌘/Ctrl S saves to file locally (a download starts).
      const download = page.waitForEvent("download");
      await page.locator(".canvas").focus();
      await page.keyboard.press("ControlOrMeta+s");
      expect((await download).suggestedFilename()).toMatch(
        /untitled-workflow\.(yaml|layout\.json)$/,
      );
    });

    test("connected, the one primary follows save, publish, activate and run", async ({
      page,
    }) => {
      await connected(page);
      const calls = await lifecyclePlatform(page);
      await newWorkflow(page);
      await insertStep(page, "Wait for time");
      await closeSheet(page);
      const chip = page.locator(".editor-identity .status-chip");
      await expect(await primary(page)).toHaveText("Save draft");
      await expect(chip).toHaveText("Unsaved");

      await (await primary(page)).click();
      await expect.poll(() => calls).toEqual(["save"]);
      await expect(await primary(page)).toHaveText("Publish…");
      await expect(chip).toHaveText(/Draft saved \d{2}:\d{2}/);
      await expect(page.locator(".toast")).toContainText("Draft saved.");

      await (await primary(page)).click();
      await page
        .getByRole("dialog", { name: "Publish untitled-workflow 1.0.0?" })
        .getByRole("button", { name: "Publish version" })
        .click();
      await expect.poll(() => calls).toEqual(["save", "publish"]);
      await expect(await primary(page)).toHaveText("Activate…");
      await expect(chip).toHaveText("Published 1.0.0");
      const toast = page.locator(".toast");
      await expect(toast).toContainText("Published untitled-workflow 1.0.0.");
      await expect(
        toast.getByRole("button", { name: "Activate" }),
      ).toBeVisible();
      // The primary opens the activation; the toast it supersedes goes, so
      // nothing sits over the dialog.
      await (await primary(page)).click();
      const activate = page.getByRole("dialog", {
        name: "Activate untitled-workflow 1.0.0",
      });
      await expect(activate).toBeVisible();
      await expect(toast).toHaveCount(0);
      const submit = activate.getByRole("button", { name: "Activate version" });
      await expect(submit).toBeEnabled();
      await submit.click();
      await expect(activate).toHaveCount(0);
      await expect.poll(() => calls).toEqual(["save", "publish", "activate"]);
      await expect(await primary(page)).toHaveText("Start run…");
      await expect(chip).toHaveText(/^Active in /);
      await expect(page.locator(".toast")).toContainText(
        "Activated untitled-workflow 1.0.0 in",
      );
      // Save to file is always one menu away.
      await toolbar(page).getByRole("button", { name: "More" }).click();
      await expect(
        page.getByRole("menuitem", { name: "Save to file" }),
      ).toBeVisible();
      await page.keyboard.press("Escape");
      // An edit makes saving the next step again.
      await insertStep(page, "Transform");
      await closeSheet(page);
      await expect(await primary(page)).toHaveText("Save draft");
    });

    test("a blocked command stays focusable and says why when pressed", async ({
      page,
    }) => {
      await connected(page, {
        capabilities: allCapabilities.filter((c) => c !== "definition.publish"),
      });
      await newWorkflow(page);
      await toolbar(page).getByRole("button", { name: "More" }).click();
      const publish = page.getByRole("menuitem", { name: /^Publish…/ });
      await expect(publish).toContainText(
        "Your account can't publish workflows in this workspace.",
      );
      await publish.click();
      await expect(page.locator(".command-note")).toHaveText(
        "Your account can't publish workflows in this workspace.",
      );
      // Start run needs an active version first.
      await command(page, "Start run…");
      await expect(page.locator(".command-note")).toHaveText(
        "Activate this version to start runs.",
      );
      // Validate keeps focus while and after it runs.
      const validate = toolbar(page).getByRole("button", { name: "Validate" });
      await validate.click();
      await expect(page.locator(".toast")).toContainText("No problems found.");
      await expect(validate).toBeFocused();
      expect(
        await page.evaluate(() => document.activeElement !== document.body),
      ).toBe(true);
    });
  });

test.describe("1280x720", () => {
  test.use({ viewport: { width: 1280, height: 720 } });
  for (const mode of ["local", "connected"] as const)
    test(`the two-row toolbar keeps at least 470 px of canvas height (${mode})`, async ({
      page,
    }) => {
      if (mode === "local") await offline(page);
      else await connected(page);
      await newWorkflow(page);
      // Steps without problems: an open diagnostics list takes its own room.
      for (const label of ["Transform", "Wait for time", "Transform"])
        await insertStep(page, label);
      // Measure the settled bar: the status chip's longest steady text.
      await expect(page.locator(".status-chip")).toHaveAttribute(
        "title",
        mode === "local" ? /Draft saved \d{2}:\d{2}/ : "Unsaved",
      );
      const canvas = await page.locator(".canvas").boundingBox();
      const bar = await page.locator(".editor-bar").boundingBox();
      // The views and the commands never overlap, whatever the font.
      const views = await page.locator(".editor-views").boundingBox();
      const commands = await page.locator(".editor-toolbar").boundingBox();
      const overlap =
        views!.x < commands!.x + commands!.width &&
        commands!.x < views!.x + views!.width &&
        views!.y < commands!.y + commands!.height &&
        commands!.y < views!.y + views!.height;
      expect(overlap).toBe(false);
      expect(canvas!.height).toBeGreaterThanOrEqual(470);
      expect(bar!.height).toBeLessThanOrEqual(100);
      // The navigation is the 64 px rail while a workflow is open.
      const rail = await page.locator(".sidebar").boundingBox();
      expect(rail!.width).toBe(64);
    });
});

for (const width of [1280, 1440])
  test(`working locally, the info control explains saving and connecting at ${width}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 800 });
    await offline(page);
    await newWorkflow(page);
    const note = page.locator(".toolbar-help");
    await expect(note).not.toHaveAttribute("open", "");
    await note.locator("summary").click();
    const link = note.getByRole("button", { name: "Connect to a platform" });
    await expect(link).toBeVisible();
    await expect(note).toContainText("Save to file downloads a copy.");
    await expect(note).toContainText(
      "Connect to a platform to publish, activate and run it.",
    );
    const inside = await link.evaluate((e) => {
      const r = e.getBoundingClientRect();
      return r.left >= 0 && r.right <= innerWidth;
    });
    expect(inside).toBe(true);
    await link.focus();
    await link.press("Escape");
    await expect(note).not.toHaveAttribute("open", "");
    await expect(note.locator("summary")).toBeFocused();
  });

test("the views are a tab list; Source gives the text the whole width", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await offline(page);
  await newWorkflow(page);
  const tabs = page.getByRole("tablist", { name: "Workflow views" });
  const designer = tabs.getByRole("tab", { name: "Designer" });
  await expect(designer).toHaveAttribute("aria-selected", "true");
  await designer.focus();
  await page.keyboard.press("ArrowRight");
  const source = tabs.getByRole("tab", { name: "Source" });
  await expect(source).toBeFocused();
  await expect(source).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".palette")).toBeHidden();
  await expect(page.locator(".inspector")).toBeHidden();
  const text = page.getByRole("textbox", { name: "Workflow source" });
  expect(
    await text.evaluate((e) => {
      const style = getComputedStyle(e);
      return [style.fontSize, style.lineHeight, style.whiteSpace];
    }),
  ).toEqual(["13px", "20px", "pre"]);
  const box = await text.boundingBox();
  const grid = await page.locator(".editor-grid").boundingBox();
  expect(box!.width).toBeGreaterThan(grid!.width - 60);
  await page.keyboard.press("End");
  await expect(tabs.getByRole("tab", { name: "Outline" })).toBeFocused();
  await page.keyboard.press("Home");
  await expect(designer).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".canvas")).toBeVisible();
});

test("on a narrow screen a workflow opens on its outline, with the canvas a click away", async ({
  page,
}) => {
  await page.setViewportSize({ width: 360, height: 740 });
  await offline(page);
  await page.getByRole("button", { name: "New workflow", exact: true }).click();
  await expect(
    page.getByText("Editing on the canvas works best on a wider screen."),
  ).toBeVisible();
  await expect(
    page.getByRole("tab", { name: "Outline", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
  await page.getByRole("button", { name: "Show canvas" }).click();
  await expect(new DesignerPage(page).canvas).toBeVisible();
  await expect(
    page.getByText("Editing on the canvas works best on a wider screen."),
  ).toHaveCount(0);
});
