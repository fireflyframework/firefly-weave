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
// A platform draft with its studio documents, for specs and the visual
// tour: the platform keeps the draft, its test data and its canvas, and can
// be told to refuse the next save.
import { type Page, type Route } from "@playwright/test";
import { createHash } from "node:crypto";
import { CanvasPage, useNewEditor } from "./canvas-po";

export const tenant = "0b9b5a59-6c63-4f3e-9d58-6a3c8f0e2b11";
export const project = "5f0c2d4e-7a1b-4c3d-8e9f-0a1b2c3d4e5f";
export const draftId = "8d7c6b5a-4f3e-4d2c-9b1a-0f9e8d7c6b5a";
export const PROFILE = "Test platform";
export const PRINCIPAL = "human";
export const sha = (text: string) =>
  createHash("sha256").update(text).digest("hex");
/** The key of this computer's copy of the draft's test data, as Studio computes it. */
export const COPY = `p${sha(PROFILE).slice(0, 16)}:${sha(PRINCIPAL).slice(0, 16)}:${tenant}:${project}:testData.${draftId}`;
export const definition = {
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
export const testData = (
  value: unknown,
  extra: Record<string, unknown> = {},
) => ({
  schemaVersion: 1,
  kind: "weave.studio/testData",
  updatedAt: "2026-10-20T10:00:00.000Z",
  input: { value, source: "manual" },
  ...extra,
});
export const PIN = {
  output: { id: "c-1" },
  source: "manual",
  pinnedAt: "2026-10-20T10:00:00.000Z",
  configHash: "fnv1a64:0000000000000001",
  contractHash: "fnv1a64:0000000000000002",
  upstreamHash: "fnv1a64:0000000000000003",
};

export interface Platform {
  /** Every save, in order: "draft", "test-data" or "canvas" with its body. */
  saves: { kind: string; body: Record<string, unknown> }[];
  documents: Map<string, { revision: number; document: unknown }>;
  /** Answers the next test-data save with this status and body instead. */
  refuse: { status: number; json: unknown }[];
  /** The same for canvas saves. */
  refuseCanvas?: { status: number; json: unknown }[];
}

export async function platform(page: Page, state: Platform) {
  const profile = {
    name: PROFILE,
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
        principal_id: PRINCIPAL,
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
            capabilities: ["catalog.read", "definition.write", "simulate"],
          },
        ],
        workspaces: [],
        truncated: false,
      },
    }),
  );
  let revision = 1;
  await page.route(`**/projects/${project}/drafts/*`, (route: Route) => {
    const request = route.request();
    if (request.method() !== "PUT")
      return route.fulfill({
        json: { id: draftId, revision, document: definition },
      });
    state.saves.push({ kind: "draft", body: request.postDataJSON() });
    revision += 1;
    return route.fulfill({
      json: {
        id: draftId,
        revision,
        document: request.postDataJSON().document,
      },
    });
  });
  await page.route(`**/projects/${project}/drafts/*/studio/*`, (route) => {
    const request = route.request();
    const kind = request.url().split("/").pop()!;
    const kept = state.documents.get(kind);
    if (request.method() === "GET")
      return kept
        ? route.fulfill({ json: envelope(kind, kept.revision, kept.document) })
        : route.fulfill({
            status: 404,
            json: { status: 404, code: "WV-NOT-FOUND", message: "Not found" },
          });
    const body = request.postDataJSON();
    state.saves.push({ kind, body });
    const refusal =
      kind === "test-data"
        ? state.refuse.shift()
        : kind === "canvas"
          ? state.refuseCanvas?.shift()
          : undefined;
    if (refusal) return route.fulfill(refusal);
    const next = (kept?.revision ?? 0) + 1;
    state.documents.set(kind, { revision: next, document: body.document });
    return route.fulfill({
      json: envelope(kind, next, body.document, body.draft_revision),
    });
  });
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
export const envelope = (
  kind: string,
  revision: number,
  document: unknown,
  draftRevision = 1,
) => ({
  draft_id: draftId,
  kind,
  revision,
  draft_revision: draftRevision,
  document,
  contains_run_data: false,
  source_environment_ids: [],
  updated_at: "2026-10-20T10:00:00Z",
  updated_by: PRINCIPAL,
});

export async function openDraft(page: Page, state: Platform, seed?: unknown) {
  await page.setViewportSize({ width: 1440, height: 900 });
  await useNewEditor(page);
  if (seed)
    await page.addInitScript(
      ([key, value]) => {
        if (!sessionStorage.getItem("seeded")) {
          sessionStorage.setItem("seeded", "1");
          localStorage.setItem(key, value);
        }
      },
      [COPY, JSON.stringify(seed)],
    );
  await platform(page, state);
  const signedIn = page.waitForResponse("**/studio/api/api/v1/identity");
  await page.goto(`/workflows/${draftId}/designer`);
  await signedIn;
  await new CanvasPage(page).ready();
}
/** A change on the canvas (the step is duplicated): the draft saves itself. */
export async function edit(page: Page) {
  await page.locator('[data-step="check-customer"]').first().click();
  await page.keyboard.press("ControlOrMeta+d");
}
export const kept = (page: Page) =>
  page.evaluate((key) => localStorage.getItem(key) ?? "", COPY);
export const fresh = (): Platform => ({
  saves: [],
  documents: new Map(),
  refuse: [],
});
