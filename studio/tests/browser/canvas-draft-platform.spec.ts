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
// A draft on a platform keeps its canvas (notes, trigger choices, step notes
// and the settings of the actions it owns) on this computer for the account
// that made it, after the workflow closes and after a reload.
import { expect, test, type Download, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { stringify } from "yaml";
import { CanvasPage, useNewEditor } from "./canvas-po";
import { command } from "./support";

const tenant = "0b9b5a59-6c63-4f3e-9d58-6a3c8f0e2b11";
const project = "5f0c2d4e-7a1b-4c3d-8e9f-0a1b2c3d4e5f";
const definition = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "order-intake", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [
      { id: "check-customer", kind: "transform", value: { ref: "/input" } },
    ],
    output: { literal: {} },
  },
};
const canvas = {
  schemaVersion: 2,
  kind: "weave.studio/canvas",
  stepNotes: { "check-customer": { text: "Uses the CRM sandbox" } },
  triggers: [
    { id: "trigger-0a1b2c3d", kind: "manual" },
    { id: "trigger-1b2c3d4e", kind: "webhook" },
  ],
};

/** A platform whose workspace IDs are UUIDs, signed in as `principal`. */
async function platform(page: Page, principal: string) {
  const profile = {
    name: "Test platform",
    baseUrl: "https://weave.invalid",
    tenantId: tenant,
    projectId: project,
    environmentId: "development",
  };
  await page.route("**/studio/session", (route) =>
    route.fulfill({
      json: {
        paired: true,
        csrfToken: "test-csrf",
        version: "1",
        mode: "connected",
        profile,
      },
    }),
  );
  await page.route("**/studio/api/api/v1/**", (route) =>
    route.fulfill({ json: { items: [], next_cursor: null } }),
  );
  await page.route("**/studio/api/api/v1/identity", (route) =>
    route.fulfill({
      json: {
        principal_id: principal,
        kind: "human",
        grants: [
          {
            role: "test",
            scope: {
              tenant_id: tenant,
              project_id: project,
              environment_id: "development",
            },
            resources: [],
            capabilities: ["catalog.read", "definition.write"],
          },
        ],
        workspaces: [],
        truncated: false,
      },
    }),
  );
  await page.route(`**/projects/${project}/drafts/*`, (route) =>
    route.fulfill({
      json: {
        id: route.request().url().split("/").pop(),
        revision: 1,
        document: definition,
      },
    }),
  );
  await page.route("**/studio/local/validate", (route) =>
    route.fulfill({
      json: {
        validationOk: true,
        errorCount: 0,
        diagnostics: [],
        partial: true,
      },
    }),
  );
}
async function savedCanvas(page: Page): Promise<Record<string, unknown>> {
  const downloads: Download[] = [];
  const listener = (download: Download) => downloads.push(download);
  page.on("download", listener);
  await command(page, "Save to file");
  await expect.poll(() => downloads.length).toBe(2);
  page.off("download", listener);
  const file = downloads.find((download) =>
    download.suggestedFilename().endsWith(".canvas.json"),
  )!;
  return JSON.parse(await readFile((await file.path())!, "utf8"));
}

test("a platform draft keeps its canvas on this computer after it closes, for its account only", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await useNewEditor(page);
  await platform(page, "human");
  const signedIn = page.waitForResponse("**/studio/api/api/v1/identity");
  await page.goto("/");
  await signedIn;
  await page.getByLabel("Choose a workflow file").setInputFiles([
    {
      name: "order-intake.yaml",
      mimeType: "application/yaml",
      buffer: Buffer.from(stringify(definition)),
    },
    {
      name: "order-intake.canvas.json",
      mimeType: "application/json",
      buffer: Buffer.from(JSON.stringify(canvas)),
    },
  ]);
  const view = new CanvasPage(page);
  await view.ready();
  const id = /\/workflows\/([^/]+)\/designer/.exec(page.url())![1];
  await expect
    .poll(() =>
      page.evaluate(
        (draft) =>
          Object.keys(localStorage).filter((key) =>
            key.endsWith(`:canvas.${draft}`),
          ).length,
        id,
      ),
    )
    .toBe(1);
  // Reopen the draft from the platform, as after closing Studio.
  await page.goto(`/workflows/${id}/designer`);
  await view.ready();
  await expect
    .poll(async () => (await savedCanvas(page))["stepNotes"])
    .toEqual(canvas.stepNotes);
  // Another account on this computer gets nothing of it.
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await platform(page, "someone-else");
  await page.goto(`/workflows/${id}/designer`);
  await view.ready();
  const other = await savedCanvas(page);
  expect(other["stepNotes"]).toBeUndefined();
  expect(other["triggers"]).toBeUndefined();
});
