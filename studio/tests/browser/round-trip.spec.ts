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
// A configured integration step survives YAML and JSON export, re-import and
// a draft save without losing or reshaping any value.
import { readFile } from "node:fs/promises";
import { test, expect, Page, Download } from "@playwright/test";
import { parse } from "yaml";
import {
  command,
  connected,
  insertStep,
  newWorkflow,
  sourceText,
} from "./support";
import { actionPicker, chooseAction } from "./integrations-po";

async function exported(page: Page, trigger: () => Promise<void>) {
  const downloads: Download[] = [];
  const listener = (download: Download) => downloads.push(download);
  page.on("download", listener);
  await trigger();
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
async function reimport(
  page: Page,
  name: string,
  content: string,
  edited: boolean,
) {
  await page.getByRole("button", { name: "Home", exact: true }).click();
  // Leaving asks first only when the designer holds edits since it was opened.
  if (edited)
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Leave designer" })
      .click();
  await page.locator("weave-home-dashboard input[type=file]").setInputFiles({
    name,
    mimeType: name.endsWith(".json") ? "application/json" : "text/yaml",
    buffer: Buffer.from(content),
  });
  await expect(page.locator('[data-step="call-action-1"]')).toBeVisible();
  await page.locator('[data-step="call-action-1"] .node-body').click();
}
async function expectConfigured(page: Page) {
  await expect(actionPicker(page)).toHaveValue("sql.lookup@1.0.0");
  await expect(page.getByLabel(/^Customer ID/)).toHaveValue("customer-104");
  await expect(page.getByLabel(/^Region(\s*\(optional\))?$/)).toHaveValue("1");
  await expect(page.getByLabel(/^Limit(\s*\(optional\))?$/)).toHaveValue("25");
  await expect(page.getByLabel(/^Mode(\s*\(optional\))?$/)).toHaveValue("1");
  await expect(page.getByLabel(/^Label(\s*\(optional\))?$/)).toHaveValue(
    "Quarterly review",
  );
  await expect(page.getByLabel("Tags item 1", { exact: true })).toHaveValue(
    "priority",
  );
  await expect(page.getByLabel(/^Dry run(\s*\(optional\))?$/)).toHaveValue(
    "true",
  );
  // Options is an open object: named entries, each any JSON value.
  await expect(page.getByLabel("Options name 1", { exact: true })).toHaveValue(
    "retries",
  );
  expect(
    JSON.parse(
      await page.getByLabel("Options value 1", { exact: true }).inputValue(),
    ),
  ).toEqual(2);
  await expect(page.getByLabel("Connection slot", { exact: true })).toHaveValue(
    "orders",
  );
  await expect(page.locator(".apply-state")).toHaveCount(0);
  await expect(page.locator(".integration-issues")).toHaveCount(0);
}

test("integration configuration survives YAML and JSON export, import and draft save", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await connected(page);
  await newWorkflow(page);
  await insertStep(page, "Call an action");
  await chooseAction(page, "sql.lookup@1.0.0");
  await page.getByLabel(/^Customer ID/).fill("customer-104");
  await page.getByLabel(/^Region(\s*\(optional\))?$/).selectOption("us");
  await page.getByLabel(/^Limit(\s*\(optional\))?$/).fill("25");
  await page.getByLabel(/^Mode(\s*\(optional\))?$/).selectOption("safe");
  await page.getByLabel(/^Label(\s*\(optional\))?$/).fill("Quarterly review");
  await page.getByRole("button", { name: "Add Tags item" }).click();
  await page.getByLabel("Tags item 1", { exact: true }).fill("priority");
  await page.getByLabel(/^Dry run(\s*\(optional\))?$/).selectOption("true");
  await page.getByRole("button", { name: "Add Options entry" }).click();
  const entry = page.getByLabel("Options name 1", { exact: true });
  await entry.fill("retries");
  await entry.press("Tab");
  await page.getByLabel("Options value 1", { exact: true }).fill("2");
  await page.locator(".add-slot").getByLabel("Slot name").fill("orders");
  await page.getByRole("button", { name: "Add slot", exact: true }).click();
  await page
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await expectConfigured(page);
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  await expect(page.locator(".diagnostics")).toContainText("No problems found");

  const yamlFiles = await exported(page, () => command(page, "Save to file"));
  const yamlSource = yamlFiles["untitled-workflow.yaml"];
  expect(yamlSource).toContain("customerId: customer-104");
  expect(Object.keys(yamlFiles)).toContain("untitled-workflow.layout.json");
  const definition = parse(yamlSource);
  expect(definition.spec.steps[0]).toEqual({
    id: "call-action-1",
    kind: "action",
    uses: "sql.lookup@1.0.0",
    with: {
      literal: {
        parameters: { customerId: "customer-104", region: "us" },
        limit: 25,
        mode: "safe",
        label: "Quarterly review",
        tags: ["priority"],
        dryRun: true,
        options: { retries: 2 },
      },
    },
    connection: "orders",
  });
  expect(definition.spec.connections).toEqual({
    orders: { connector: "weave-postgresql@1.0.0", required: true },
  });

  await page.getByRole("tab", { name: "Source", exact: true }).click();
  await page.getByLabel("Source format", { exact: true }).selectOption("json");
  await page.getByRole("tab", { name: "Designer", exact: true }).click();
  const jsonFiles = await exported(page, () => command(page, "Save to file"));
  const jsonSource = jsonFiles["untitled-workflow.json"];
  expect(JSON.parse(jsonSource)).toEqual(definition);

  await reimport(page, "untitled-workflow.yaml", yamlSource, true);
  await expectConfigured(page);
  expect(parse(await sourceText(page))).toEqual(definition);

  await reimport(page, "untitled-workflow.json", jsonSource, false);
  await expectConfigured(page);
  expect(JSON.parse(await sourceText(page))).toEqual(definition);

  let saved: any = null;
  await page.route("**/projects/project/drafts/*", (r) => {
    saved = r.request().postDataJSON();
    return r.fulfill({
      json: { id: "draft", revision: 1, document: saved.document },
    });
  });
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect.poll(() => saved).not.toBeNull();
  expect(saved.document).toEqual(definition);
  await expect(page.locator(".editor-identity .status-chip")).toHaveText(
    "Draft saved",
  );
});
