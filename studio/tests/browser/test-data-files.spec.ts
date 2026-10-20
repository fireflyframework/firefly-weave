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
// Test data travels with the workflow: Save to file writes it beside the
// workflow (asking first about data recorded from runs), opening the files
// offers it back, copy and paste brings pins (never run data), and Undo of a
// rename moves a pin back.
import { expect, test, type Download, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { setTestInput } from "./formula-po";
import { fitCanvas, StepDetailsPage } from "./step-details-po";
import { command, connected, defaultCatalog } from "./support";
import { withLanguageFeatures, withNewEditor } from "./step-details-po";
import {
  banner,
  lookupContract,
  openTestDataFixture,
  outputPane,
  PROJECT,
  TENANT,
} from "./test-data-po";

test.use({
  viewport: { width: 1440, height: 900 },
  permissions: ["clipboard-read", "clipboard-write"],
});

const RUN = "3f2a6c1e-0d5b-4a63-9c1f-2b7e8d9a0b11";
const ENVIRONMENT = "9b1c2d3e-4f50-4a61-8b72-c3d4e5f60718";
const AT = "2026-10-20T10:00:00.000Z";
const pin = (output: unknown, extra: Record<string, unknown> = {}) => ({
  output,
  source: "manual",
  pinnedAt: AT,
  configHash: "fnv1a64:0000000000000001",
  contractHash: "fnv1a64:0000000000000002",
  upstreamHash: "fnv1a64:0000000000000003",
  ...extra,
});
const fromRun = {
  source: "test-call",
  runId: RUN,
  runEnvironmentId: ENVIRONMENT,
};

/** Save to file, then the files it wrote, by name. */
async function saved(page: Page, count: number) {
  const downloads: Download[] = [];
  const listener = (download: Download) => downloads.push(download);
  page.on("download", listener);
  await command(page, "Save to file");
  await expect.poll(() => downloads.length).toBe(count);
  page.off("download", listener);
  const files: Record<string, string> = {};
  for (const download of downloads)
    files[download.suggestedFilename()] = await readFile(
      (await download.path())!,
      "utf8",
    );
  return files;
}
const asFile = (name: string, content: string) => ({
  name,
  mimeType: name.endsWith(".json") ? "application/json" : "application/yaml",
  buffer: Buffer.from(content),
});
/** Opens files from Home in a fresh page of the same platform, answering Load. */
async function openFiles(page: Page, files: Record<string, string>) {
  await withNewEditor(page);
  await connected(page, {
    catalog: [...defaultCatalog, { id: "a3", document: lookupContract }],
    tenantId: TENANT,
    projectId: PROJECT,
  });
  await withLanguageFeatures(page);
  await page
    .getByLabel("Choose a workflow file")
    .setInputFiles(
      Object.entries(files).map(([name, content]) => asFile(name, content)),
    );
  const question = page.getByRole("dialog", {
    name: "Load the test data saved with this workflow?",
  });
  await expect(question).toBeVisible();
  await question.getByRole("button", { name: "Load" }).click();
  await expect(
    page
      .locator(".toast")
      .filter({ hasText: "Loaded the test data saved with this workflow." }),
  ).toHaveCount(1);
  return new StepDetailsPage(page);
}
const testDataFile = (pins: Record<string, unknown>) =>
  JSON.stringify({
    schemaVersion: 1,
    kind: "weave.studio/testData",
    updatedAt: AT,
    input: { value: { customerId: "c-5", amount: 5 }, source: "manual" },
    pins,
  });

test("Save to file writes the test data beside the workflow and opening offers it back", async ({
  page,
  context,
}) => {
  const details = await openTestDataFixture(page);
  await setTestInput(page, details, {
    "Customer ID": "c-1042",
    Amount: "1500",
  });
  await details.open("lookup");
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "Generate sample" }).click();
  await expect(banner(pane)).toBeVisible();
  await details.close();
  const files = await saved(page, 3);
  expect(Object.keys(files).sort()).toEqual([
    "tiered-orders.canvas.json",
    "tiered-orders.testdata.json",
    "tiered-orders.yaml",
  ]);
  const kept = JSON.parse(files["tiered-orders.testdata.json"]);
  expect(kept.input.value).toMatchObject({ customerId: "c-1042" });
  expect(Object.keys(kept.pins)).toEqual(["lookup"]);
  expect(files["tiered-orders.testdata.json"]).not.toContain("password");
  const other = await context.newPage();
  const reopened = await openFiles(other, files);
  await reopened.open("lookup");
  await expect(banner(await outputPane(reopened))).toBeVisible();
  await reopened.close();
  await reopened.trigger().focus();
  await other.keyboard.press("Enter");
  await expect(
    reopened.dialogFor("Manual form trigger").getByLabel("Customer ID", {
      exact: true,
    }),
  ).toHaveValue("c-1042");
});

test("Save to file warns before writing run data", async ({ page }) => {
  await openFiles(page, {
    "tiered-orders.yaml": await readFile(
      "tests/fixtures/test-data.yaml",
      "utf8",
    ),
    "tiered-orders.testdata.json": testDataFile({
      lookup: pin({ customerId: "c-5", found: true, tier: "gold" }),
      notify: pin({ customerId: "c-6", found: false, tier: "basic" }, fromRun),
    }),
  });
  const downloads: Download[] = [];
  page.on("download", (download) => downloads.push(download));
  await command(page, "Save to file");
  const question = page.getByRole("dialog", {
    name: "Save data recorded from runs?",
  });
  await expect(question).toContainText(
    "This file will include data from run 3f2a…. Anyone with the file can read it.",
  );
  await question.getByRole("button", { name: "Save without run data" }).click();
  await expect.poll(() => downloads.length).toBe(3);
  const written = downloads.find((download) =>
    download.suggestedFilename().endsWith(".testdata.json"),
  )!;
  const kept = JSON.parse(await readFile((await written.path())!, "utf8"));
  expect(Object.keys(kept.pins)).toEqual(["lookup"]);
  expect(JSON.stringify(kept)).not.toContain(RUN);
});

test("copying steps leaves out pins recorded from runs", async ({ page }) => {
  await openFiles(page, {
    "tiered-orders.yaml": await readFile(
      "tests/fixtures/test-data.yaml",
      "utf8",
    ),
    "tiered-orders.testdata.json": testDataFile({
      lookup: pin({ customerId: "c-5", found: true, tier: "gold" }),
      notify: pin({ customerId: "c-6", found: false, tier: "basic" }, fromRun),
    }),
  });
  await fitCanvas(page);
  const copyAndPaste = async (id: string) => {
    await page.locator(`[data-step="${id}"]`).first().click();
    await page.keyboard.press("ControlOrMeta+c");
    await expect(
      page.locator(".toast").filter({ hasText: `Copied ${id}.` }),
    ).toHaveCount(1);
    await page.keyboard.press("ControlOrMeta+v");
    await expect(page.locator(`[data-step="${id}-1"]`).first()).toBeVisible();
  };
  await copyAndPaste("lookup");
  await copyAndPaste("notify");
  await expect(page.locator('[data-step="lookup-1"]').first()).toBeVisible();
  await expect(
    page.locator('[data-step="lookup-1"] .tile-badge').first(),
    // Stamped by another workflow file: out of date here, and said so plainly.
  ).toHaveAttribute("data-badge", "pinned-stale");
  await expect(
    page.locator('[data-step="lookup-1"] [title]').first(),
  ).toHaveAttribute(
    "title",
    /Pinned test data, out of date: pinned in another workflow; check it still fits/,
  );
  await expect(
    page.locator('[data-step="lookup"] [title]').first(),
  ).toHaveAttribute(
    "title",
    /Pinned test data, out of date: pinned in another workflow; check it still fits/,
  );
  await expect(page.locator('[data-step="notify-1"] .tile-badge')).toHaveCount(
    0,
  );
});

test("undoing a rename moves the pin back, and deleting a step offers its pin back", async ({
  page,
}) => {
  const details = await openTestDataFixture(page);
  await details.open("lookup");
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "Generate sample" }).click();
  await expect(banner(pane)).toBeVisible();
  await details.dialog.getByRole("button", { name: "Rename lookup" }).click();
  await details.dialog.getByRole("textbox", { name: "Step name" }).fill("find");
  await page.keyboard.press("Enter");
  await expect(details.dialogFor("find")).toBeVisible();
  await expect(banner(await outputPane(details))).toBeVisible();
  await details.close();
  await command(page, "Undo");
  await details.open("lookup");
  await expect(banner(await outputPane(details))).toBeVisible();
  await details.close();
  // Deleting a step takes its pin; the toast's Undo brings both back.
  await details.open("notify");
  const notify = await outputPane(details);
  await notify.getByRole("button", { name: "Generate sample" }).click();
  await expect(banner(notify)).toBeVisible();
  await details.close();
  await fitCanvas(page);
  await page.locator('[data-step="notify"]').first().click();
  await page.keyboard.press("Delete");
  const toast = page
    .locator(".toast")
    .filter({ hasText: "Deleted notify and its pinned output." });
  await expect(toast).toHaveCount(1);
  await toast.getByRole("button", { name: "Undo" }).click();
  await expect(
    page.locator('[data-step="notify"] .tile-badge').first(),
  ).toHaveAttribute("data-badge", "pinned");
});

test("the toolbar's Undo of a delete brings the pin back too, and Redo takes it again", async ({
  page,
}) => {
  const details = await openTestDataFixture(page);
  await details.open("notify");
  const pane = await outputPane(details);
  await pane.getByRole("button", { name: "Generate sample" }).click();
  await expect(banner(pane)).toBeVisible();
  await details.close();
  await fitCanvas(page);
  await page.locator('[data-step="notify"]').first().click();
  await page.keyboard.press("Delete");
  await expect(page.locator('[data-step="notify"]')).toHaveCount(0);
  await command(page, "Undo");
  await expect(
    page.locator('[data-step="notify"] .tile-badge').first(),
  ).toHaveAttribute("data-badge", "pinned");
  await command(page, "Redo");
  await expect(page.locator('[data-step="notify"]')).toHaveCount(0);
  await command(page, "Undo");
  await expect(
    page.locator('[data-step="notify"] .tile-badge').first(),
  ).toHaveAttribute("data-badge", "pinned");
});
