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
// A platform draft's test data and canvas live on the platform: they save
// after the draft, come back on reload, ask before replacing either side on
// a conflict, leave every copy on this computer when the platform refuses a
// secret, and keep entries the platform can't classify yet on this computer.
import { expect, test, type Page, type Route } from "@playwright/test";
import { createHash } from "node:crypto";
import { CanvasPage, useNewEditor } from "./canvas-po";

const tenant = "0b9b5a59-6c63-4f3e-9d58-6a3c8f0e2b11";
const project = "5f0c2d4e-7a1b-4c3d-8e9f-0a1b2c3d4e5f";
const draftId = "8d7c6b5a-4f3e-4d2c-9b1a-0f9e8d7c6b5a";
const PROFILE = "Test platform";
const PRINCIPAL = "human";
const sha = (text: string) => createHash("sha256").update(text).digest("hex");
/** The key of this computer's copy of the draft's test data, as Studio computes it. */
const COPY = `p${sha(PROFILE).slice(0, 16)}:${sha(PRINCIPAL).slice(0, 16)}:${tenant}:${project}:testData.${draftId}`;
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
const testData = (value: unknown, extra: Record<string, unknown> = {}) => ({
  schemaVersion: 1,
  kind: "weave.studio/testData",
  updatedAt: "2026-10-20T10:00:00.000Z",
  input: { value, source: "manual" },
  ...extra,
});
const PIN = {
  output: { id: "c-1" },
  source: "manual",
  pinnedAt: "2026-10-20T10:00:00.000Z",
  configHash: "fnv1a64:0000000000000001",
  contractHash: "fnv1a64:0000000000000002",
  upstreamHash: "fnv1a64:0000000000000003",
};

interface Platform {
  /** Every save, in order: "draft", "test-data" or "canvas" with its body. */
  saves: { kind: string; body: Record<string, unknown> }[];
  documents: Map<string, { revision: number; document: unknown }>;
  /** Answers the next test-data save with this status and body instead. */
  refuse: { status: number; json: unknown }[];
}

async function platform(page: Page, state: Platform) {
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
    const refusal = kind === "test-data" ? state.refuse.shift() : undefined;
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
const envelope = (
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

async function openDraft(page: Page, state: Platform, seed?: unknown) {
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
async function edit(page: Page) {
  await page.locator('[data-step="check-customer"]').first().click();
  await page.keyboard.press("ControlOrMeta+d");
}
const kept = (page: Page) =>
  page.evaluate((key) => localStorage.getItem(key) ?? "", COPY);
const fresh = (): Platform => ({ saves: [], documents: new Map(), refuse: [] });

test("test data and the canvas save with the draft and come back on reload", async ({
  page,
}) => {
  const state = fresh();
  await openDraft(page, state, testData({ customerId: "c-1042" }));
  // A sticky note changes the canvas; the draft saves with it.
  await page.locator('[data-step="check-customer"]').first().click();
  await page.keyboard.press("Shift+S");
  await page.keyboard.press("Escape");
  await expect
    .poll(() => state.saves.map((save) => save.kind), { timeout: 6000 })
    .toEqual(["draft", "test-data", "canvas"]);
  const [, data] = state.saves;
  expect(data.body["draft_revision"]).toBe(2);
  expect(
    (data.body["document"] as { input: { value: unknown } }).input.value,
  ).toEqual({ customerId: "c-1042" });
  // Another note on the platform's canvas, and this computer's copy gone: the platform's come back.
  state.documents.set("canvas", {
    revision: 7,
    document: {
      schemaVersion: 2,
      kind: "weave.studio/canvas",
      notes: [
        {
          id: "note-1a2b3c4d",
          text: "Kept on the platform",
          color: "blue",
          x: 400,
          y: 360,
          width: 240,
          height: 120,
        },
      ],
    },
  });
  await page.evaluate((key) => localStorage.removeItem(key), COPY);
  await page.reload();
  await new CanvasPage(page).ready();
  await expect(page.locator(".sticky-note-body")).toHaveText(
    "Kept on the platform",
  );
  await expect.poll(() => kept(page)).toContain("c-1042");
});

test("a changed document on the platform asks before replacing either side", async ({
  page,
}) => {
  const state = fresh();
  await openDraft(page, state, testData({ customerId: "mine" }));
  // Someone else saved test data for this draft meanwhile: this first save is stale.
  state.refuse.push({
    status: 412,
    json: { status: 412, code: "WV-ETAG", message: "Changed" },
  });
  state.documents.set("test-data", {
    revision: 4,
    document: testData({ customerId: "theirs" }),
  });
  await edit(page);
  const dialog = page.getByRole("dialog", {
    name: "Someone else changed the test data",
  });
  await expect(dialog).toBeVisible({ timeout: 6000 });
  await expect(dialog).toContainText(
    "Keep your version, or use the one saved on the platform.",
  );
  await expect(dialog.getByRole("button", { name: "Keep mine" })).toBeFocused();
  await dialog.getByRole("button", { name: "Use theirs" }).click();
  await expect.poll(() => kept(page)).toContain('"theirs"');
  const toast = page.locator(".toast").filter({
    hasText: "Using the test data saved on the platform.",
  });
  await toast.getByRole("button", { name: "Undo" }).click();
  await expect.poll(() => kept(page)).toContain('"mine"');
});

test("Keep mine saves this computer's test data over the platform's", async ({
  page,
}) => {
  const state = fresh();
  await openDraft(page, state, testData({ customerId: "mine" }));
  state.refuse.push({
    status: 412,
    json: { status: 412, code: "WV-ETAG", message: "Changed" },
  });
  state.documents.set("test-data", {
    revision: 4,
    document: testData({ customerId: "theirs" }),
  });
  await edit(page);
  const dialog = page.getByRole("dialog", {
    name: "Someone else changed the test data",
  });
  await dialog.getByRole("button", { name: "Keep mine" }).click();
  await expect
    .poll(() => JSON.stringify(state.documents.get("test-data")))
    .toContain('"mine"');
});

test("a value the platform refuses as secret leaves every copy on this computer", async ({
  page,
}) => {
  const state = fresh();
  state.refuse.push({
    status: 422,
    json: {
      status: 422,
      code: "WV-STUDIO-DOCUMENT-SECRET",
      message: "Secret",
      result: { paths: ["/input"] },
    },
  });
  await openDraft(page, state, testData({ password: "s3cret-canary" }));
  await expect.poll(() => kept(page)).toContain("s3cret-canary");
  await edit(page);
  await expect(page.locator(".toast")).toContainText(
    "This value is marked secret in the schema. Test data can't keep secrets; leave the field out.",
  );
  await expect
    .poll(() => page.evaluate(() => JSON.stringify(localStorage)))
    .not.toContain("s3cret-canary");
});

test("an entry waiting for publishing stays on this computer and is left out of saves", async ({
  page,
}) => {
  const state = fresh();
  state.refuse.push({
    status: 422,
    json: {
      status: 422,
      code: "WV-STUDIO-DOCUMENT-UNCLASSIFIED",
      message: "Unclassified",
      result: { paths: ["/pins/check-customer"] },
    },
  });
  await openDraft(
    page,
    state,
    testData({ customerId: "c-1" }, { pins: { "check-customer": PIN } }),
  );
  await edit(page);
  await expect
    .poll(
      () => state.saves.filter((save) => save.kind === "test-data").length,
      { timeout: 6000 },
    )
    .toBe(2);
  const [first, second] = state.saves.filter(
    (save) => save.kind === "test-data",
  );
  expect(JSON.stringify(first.body)).toContain("check-customer");
  expect(JSON.stringify(second.body)).not.toContain("check-customer");
  // This computer keeps the entry, and the list of what waits for publishing.
  await expect.poll(() => kept(page)).toContain("c-1");
  expect(
    await page.evaluate((key) => localStorage.getItem(`${key}.local`), COPY),
  ).toBe('["/pins/check-customer"]');
});
