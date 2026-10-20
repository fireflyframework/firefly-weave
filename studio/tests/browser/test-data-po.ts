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
// Test data in step details: the fixture, its platform (with workspace IDs
// that let Studio keep this account's data on this computer) and the Output
// pane's pin controls.
import { expect, type Locator, type Page } from "@playwright/test";
import { resolve } from "node:path";
import { connected, defaultCatalog } from "./support";
import {
  openStepFixture,
  withLanguageFeatures,
  type StepDetailsPage,
} from "./step-details-po";

export const testDataFixture = resolve("tests/fixtures/test-data.yaml");
export const TENANT = "0b9b5a59-6c63-4f3e-9d58-6a3c8f0e2b11";
export const PROJECT = "5f0c2d4e-7a1b-4c3d-8e9f-0a1b2c3d4e5f";

/** The action lookup and notify call: its output marks `token` secret. */
export const lookupContract = {
  apiVersion: "weave/v1alpha1",
  kind: "Action",
  metadata: { name: "crm.lookup", version: "1.0.0" },
  spec: {
    implementation: {
      kind: "worker",
      taskType: "crm.lookup",
      taskVersion: "1.0.0",
    },
    sideEffect: "read_only",
    timeoutSeconds: 30,
    inputSchema: {
      type: "object",
      properties: { customerId: { type: "string", title: "Customer ID" } },
    },
    outputSchema: {
      type: "object",
      properties: {
        customerId: { type: "string" },
        found: { type: "boolean" },
        tier: { type: "string" },
        token: { type: "string", "x-secret": true },
      },
    },
  },
};

/** The fixture, connected to a platform that serves lookupContract. */
export const openTestDataFixture = (page: Page) =>
  openStepFixture(page, testDataFixture, async (p) => {
    const result = await connected(p, {
      catalog: [...defaultCatalog, { id: "a3", document: lookupContract }],
      tenantId: TENANT,
      projectId: PROJECT,
    });
    await withLanguageFeatures(p);
    return result;
  });

/** The Output pane, shown (a tab where the window shows one data pane). */
export async function outputPane(details: StepDetailsPage): Promise<Locator> {
  const dialog = details.dialog;
  // A narrow sheet shows one pane, a medium window Input or Output: choose Output.
  const tabs = dialog
    .getByRole("tablist", { name: /^(Step details panes|Data)$/ })
    .getByRole("tab", { name: "Output" });
  if (await tabs.count()) await tabs.first().click();
  const pane = dialog.locator("weave-output-pane");
  await expect(pane).toBeVisible();
  return pane;
}
export const pinButton = (pane: Locator): Locator => pane.locator(".pane-pin");
export const banner = (pane: Locator): Locator =>
  pane.locator(".pinned-banner");
/** A tile's badges on the canvas. */
export const tileBadge = (page: Page, id: string): Locator =>
  page.locator(`[data-step="${id}"] .tile-badge`);

/** The platform keeps drafts: a save is answered with its next revision, and a reload reads the last one. */
export async function routeDrafts(page: Page) {
  const drafts = new Map<string, { revision: number; document: unknown }>();
  await page.route(`**/projects/${PROJECT}/drafts/*`, async (route) => {
    const request = route.request();
    const id = new URL(request.url()).pathname.split("/").pop()!;
    if (request.method() === "PUT") {
      const revision = (drafts.get(id)?.revision ?? 0) + 1;
      drafts.set(id, { revision, document: request.postDataJSON().document });
      return route.fulfill({
        json: { id, revision, document: drafts.get(id)!.document },
      });
    }
    const kept = drafts.get(id);
    return kept
      ? route.fulfill({
          json: { id, revision: kept.revision, document: kept.document },
        })
      : route.fulfill({
          status: 404,
          json: { status: 404, code: "WV-NOT-FOUND", message: "Not found" },
        });
  });
  return drafts;
}
