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
// A save that belongs to one draft never lands in another: a save sent again
// after no answer, while the person opens a different draft, saves nothing
// more. Leaving while a save is still going asks first, and a draft refused
// as stale (412) opens the compare flow.
import { expect, test, type Page, type Route } from "@playwright/test";
import { CanvasPage, useNewEditor } from "./canvas-po";

const tenant = "0b9b5a59-6c63-4f3e-9d58-6a3c8f0e2b11";
const project = "5f0c2d4e-7a1b-4c3d-8e9f-0a1b2c3d4e5f";
const A = "8d7c6b5a-4f3e-4d2c-9b1a-0f9e8d7c6b5a";
const B = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
const workflow = (name: string, step: string) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name, version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [{ id: step, kind: "transform", value: { ref: "/input" } }],
    output: { literal: {} },
  },
});

interface Put {
  id: string;
  ifMatch: string | undefined;
  key: string | undefined;
  name: string;
}
interface Control {
  /** Draft A's next PUTs answered 503. */
  failA: number;
  /** Draft A's next PUTs answered only after 3.5 s. */
  holdA: number;
}

async function platform(page: Page, puts: Put[], control: Control) {
  const profile = {
    name: "Test platform",
    baseUrl: "https://weave.invalid",
    tenantId: tenant,
    projectId: project,
    environmentId: "development",
  };
  await page.route("**/studio/session", (r) =>
    r.fulfill({
      json: {
        paired: true,
        csrfToken: "t",
        version: "1",
        mode: "connected",
        profile,
      },
    }),
  );
  await page.route("**/studio/api/api/v1/**", (r) =>
    r.fulfill({ json: { items: [], next_cursor: null } }),
  );
  await page.route("**/studio/api/api/v1/identity", (r) =>
    r.fulfill({
      json: {
        principal_id: "human",
        kind: "human",
        grants: [
          {
            role: "t",
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
  await page.route(new RegExp(`/projects/${project}/drafts(\\?|$)`), (r) =>
    r.fulfill({
      json: {
        items: [
          { id: B, revision: 1, document: workflow("bravo", "bravo-step") },
        ],
        next_cursor: null,
      },
    }),
  );
  const revision: Record<string, number> = { [A]: 1, [B]: 1 };
  const document: Record<string, unknown> = {
    [A]: workflow("alpha", "alpha-step"),
    [B]: workflow("bravo", "bravo-step"),
  };
  await page.route(`**/projects/${project}/drafts/*`, async (route: Route) => {
    const request = route.request();
    const id = new URL(request.url()).pathname.split("/").pop()!;
    if (!(id in revision))
      return route.fulfill({
        status: 404,
        json: { status: 404, code: "WV-NOT-FOUND", message: "Not found" },
      });
    if (request.method() !== "PUT")
      return route.fulfill({
        json: { id, revision: revision[id], document: document[id] },
        headers: { ETag: `"${revision[id]}"` },
      });
    const body = request.postDataJSON();
    const headers = request.headers();
    puts.push({
      id,
      ifMatch: headers["if-match"],
      key: headers["idempotency-key"],
      name: body.document.metadata.name,
    });
    if (id === A && control.failA > 0) {
      control.failA--;
      return route.fulfill({
        status: 503,
        json: { status: 503, code: "WV-UNAVAILABLE", message: "Try again." },
      });
    }
    if (id === A && control.holdA > 0) {
      control.holdA--;
      await new Promise((done) => setTimeout(done, 3500));
    }
    if (headers["if-match"] && headers["if-match"] !== `"${revision[id]}"`)
      return route.fulfill({
        status: 412,
        json: {
          status: 412,
          code: "WV-ETAG",
          message: "Revision precondition failed",
        },
      });
    revision[id] += 1;
    document[id] = body.document;
    return route.fulfill({
      json: { id, revision: revision[id], document: body.document },
      headers: { ETag: `"${revision[id]}"` },
    });
  });
  await page.route("**/studio/local/validate", (r) =>
    r.fulfill({
      json: {
        validationOk: true,
        errorCount: 0,
        diagnostics: [],
        partial: true,
      },
    }),
  );
  return { revision, document };
}

async function openA(page: Page) {
  const signedIn = page.waitForResponse("**/studio/api/api/v1/identity");
  await page.goto(`/workflows/${A}/designer`);
  await signedIn;
  await new CanvasPage(page).ready();
}
/** A change to draft A: its step, duplicated. */
async function changeA(page: Page) {
  await page.locator('[data-step="alpha-step"]').first().click();
  await page.keyboard.press("ControlOrMeta+d");
}

test.use({ viewport: { width: 1440, height: 900 } });
test.beforeEach(async ({ page }) => {
  await useNewEditor(page);
});

test("a save sent again never writes another draft's content", async ({
  page,
}) => {
  const puts: Put[] = [];
  const server = await platform(page, puts, { failA: 1, holdA: 1 });
  await openA(page);
  await changeA(page);
  const retry = page.getByRole("button", { name: "Not saved: retry" });
  await expect(retry).toBeVisible({ timeout: 6000 });
  // Sent again with its own key; the answer takes 3.5 s.
  await retry.click();
  await expect.poll(() => puts.length).toBe(2);
  // The person leaves while it's saving, and opens draft B.
  await page.getByRole("button", { name: "Home", exact: true }).click();
  // The save is still going: leaving asks, and the person leaves.
  await page.getByRole("button", { name: "Leave designer" }).click();
  await page.getByRole("button", { name: /bravo/ }).first().click();
  await expect(page.locator('[data-step="bravo-step"]').first()).toBeVisible();
  // The answer to A's save comes, and B saves nothing it didn't change.
  await page.waitForTimeout(5000);
  expect(puts.filter((p) => p.id === A && p.name === "bravo")).toEqual([]);
  expect(puts.map((p) => p.id)).toEqual([A, A]);
  expect(puts[1].key).toBe(puts[0].key);
  expect(server.revision[B]).toBe(1);
  // B keeps its own revision: its next save is accepted.
  await page.locator('[data-step="bravo-step"]').first().click();
  await page.keyboard.press("ControlOrMeta+d");
  await expect.poll(() => server.revision[B], { timeout: 8000 }).toBe(2);
  expect(puts.at(-1)).toMatchObject({ id: B, ifMatch: '"1"', name: "bravo" });
  await expect(page.locator(".editor-bar")).not.toContainText("Not saved");
});

test("leaving while a draft is saving asks first", async ({ page }) => {
  const puts: Put[] = [];
  await platform(page, puts, { failA: 0, holdA: 2 });
  await openA(page);
  await changeA(page);
  await expect.poll(() => puts.length, { timeout: 6000 }).toBe(1);
  await page.getByRole("button", { name: "Home", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Leave the designer?" });
  await expect(dialog).toBeVisible({ timeout: 4000 });
  await expect(dialog).toContainText("still saving");
  await dialog.getByRole("button", { name: "Keep editing" }).click();
  await expect(page.locator('[data-step="alpha-step"]').first()).toBeVisible();
});

test("a draft refused as stale (412) opens the compare flow", async ({
  page,
}) => {
  const puts: Put[] = [];
  const server = await platform(page, puts, { failA: 0, holdA: 0 });
  await openA(page);
  // Someone else saved draft A meanwhile.
  server.revision[A] = 2;
  server.document[A] = workflow("alpha-theirs", "alpha-step");
  await changeA(page);
  await expect(page.getByText(/Someone else saved this draft/)).toBeVisible({
    timeout: 6000,
  });
  const dialog = page.getByRole("dialog", {
    name: "Draft changed on the server",
  });
  await expect(dialog).toContainText("alpha-theirs");
  // Autosave stops while the conflict is open: nothing more is sent.
  const sent = puts.length;
  await page.waitForTimeout(3000);
  expect(puts.length).toBe(sent);
});
