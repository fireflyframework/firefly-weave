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
// A save sent for one draft never sets another draft's revision or status:
// Check now resends draft A's unanswered save while draft B is open, and A's
// first save answers after the person opened B.
import { expect, test, type Page, type Route } from "@playwright/test";
import { CanvasPage, useNewEditor } from "./canvas-po";

const tenant = "0b9b5a59-6c63-4f3e-9d58-6a3c8f0e2b11";
const project = "5f0c2d4e-7a1b-4c3d-8e9f-0a1b2c3d4e5f";
const A = "8d7c6b5a-4f3e-4d2c-9b1a-0f9e8d7c6b5a";
const B = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
const C = "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e";
const names: Record<string, string> = {
  [A]: "alpha",
  [B]: "bravo",
  [C]: "charlie",
};
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
  ifMatch?: string;
  key?: string;
  name: string;
  steps: number;
  at: number;
}
interface Server {
  revision: Record<string, number>;
  document: Record<string, unknown>;
  puts: Put[];
  /** per-draft queue of behaviors for the next PUTs: "503", "hold", "slow503" */
  plan: Record<string, string[]>;
}
function server(start: Record<string, number> = {}): Server {
  return {
    revision: { [A]: start[A] ?? 1, [B]: start[B] ?? 1, [C]: start[C] ?? 1 },
    document: {
      [A]: workflow("alpha", "alpha-step"),
      [B]: workflow("bravo", "bravo-step"),
      [C]: workflow("charlie", "charlie-step"),
    },
    puts: [],
    plan: { [A]: [], [B]: [], [C]: [] },
  };
}
async function platform(page: Page, s: Server) {
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
        items: [A, B, C].map((id) => ({
          id,
          revision: s.revision[id],
          document: s.document[id],
        })),
        next_cursor: null,
      },
    }),
  );
  await page.route(`**/projects/${project}/drafts/*`, async (route: Route) => {
    const request = route.request();
    const id = new URL(request.url()).pathname.split("/").pop()!;
    if (!(id in s.revision))
      return route.fulfill({
        status: 404,
        json: { status: 404, code: "WV-NOT-FOUND", message: "Not found" },
      });
    if (request.method() !== "PUT")
      return route.fulfill({
        json: { id, revision: s.revision[id], document: s.document[id] },
        headers: { ETag: `"${s.revision[id]}"` },
      });
    const body = request.postDataJSON();
    const headers = request.headers();
    s.puts.push({
      id,
      ifMatch: headers["if-match"],
      key: headers["idempotency-key"],
      name: body.document.metadata.name,
      steps: body.document.spec.steps.length,
      at: Date.now(),
    });
    const step = s.plan[id].shift();
    if (step === "503")
      return route.fulfill({
        status: 503,
        json: { status: 503, code: "WV-UNAVAILABLE", message: "Try again." },
      });
    if (step === "slow503") {
      await new Promise((d) => setTimeout(d, 3500));
      return route.fulfill({
        status: 503,
        json: { status: 503, code: "WV-UNAVAILABLE", message: "Try again." },
      });
    }
    if (step === "hold") await new Promise((d) => setTimeout(d, 3500));
    if (headers["if-match"] && headers["if-match"] !== `"${s.revision[id]}"`)
      return route.fulfill({
        status: 412,
        json: {
          status: 412,
          code: "WV-ETAG",
          message: "Revision precondition failed",
        },
      });
    s.revision[id] += 1;
    s.document[id] = body.document;
    return route.fulfill({
      json: { id, revision: s.revision[id], document: body.document },
      headers: { ETag: `"${s.revision[id]}"` },
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
}
async function openDeep(page: Page, id: string) {
  const signedIn = page.waitForResponse("**/studio/api/api/v1/identity");
  await page.goto(`/workflows/${id}/designer`);
  await signedIn;
  await new CanvasPage(page).ready();
}
async function change(page: Page, id: string) {
  await page.locator(`[data-step="${names[id]}-step"]`).first().click();
  await page.keyboard.press("ControlOrMeta+d");
}
/** Home, answering "Leave designer" if asked, then open a draft from the list. */
async function switchTo(page: Page, id: string) {
  await page.getByRole("button", { name: "Home", exact: true }).click();
  const leave = page.getByRole("button", { name: "Leave designer" });
  const home = page
    .getByRole("button", { name: new RegExp(names[id]) })
    .first();
  await Promise.race([
    leave.waitFor({ timeout: 5000 }),
    home.waitFor({ timeout: 5000 }),
  ]).catch(() => {});
  if (await leave.isVisible()) await leave.click();
  await page
    .getByRole("button", { name: new RegExp(names[id]) })
    .first()
    .click();
  await expect(
    page.locator(`[data-step="${names[id]}-step"]`).first(),
  ).toBeVisible();
}
/** Every PUT carries the content of the draft it names. */
function noCrossWrites(s: Server) {
  for (const p of s.puts)
    expect(p.name, `PUT /drafts/${names[p.id]} carried ${p.name}`).toBe(
      names[p.id],
    );
}

test.use({ viewport: { width: 1440, height: 900 } });
test.beforeEach(async ({ page }) => useNewEditor(page));

test("Check now for another draft's save leaves the open draft's revision and status", async ({
  page,
}) => {
  test.setTimeout(90000);
  const s = server({ [A]: 5, [B]: 1 });
  s.plan[A] = ["503", "503", "503"];
  await platform(page, s);
  await openDeep(page, A);
  await change(page, A);
  const retry = page.getByRole("button", { name: "Not saved: retry" });
  await expect(retry).toBeVisible({ timeout: 6000 });
  await retry.click();
  await expect.poll(() => s.puts.length).toBe(2);
  await switchTo(page, B);
  await change(page, B);
  await page.waitForTimeout(12000);
  const check = page.getByRole("button", { name: "Check now" });
  await expect(check).toBeVisible();
  await check.click();
  await expect.poll(() => s.revision[A]).toBe(6);
  await page.waitForTimeout(500);
  await change(page, B);
  await page.waitForTimeout(13000);
  noCrossWrites(s);
  const bPuts = s.puts.filter((p) => p.id === B);
  expect(bPuts.length).toBeGreaterThan(0);
  for (const p of bPuts)
    expect(p.ifMatch, "B's PUT used A's revision").not.toBe('"6"');
});

test("a first save answered after a switch leaves the open draft's revision", async ({
  page,
}) => {
  const s = server({ [A]: 4, [B]: 1 });
  s.plan[A] = ["hold"];
  await platform(page, s);
  await openDeep(page, A);
  await change(page, A);
  await expect.poll(() => s.puts.length, { timeout: 6000 }).toBe(1);
  await switchTo(page, B);
  await expect.poll(() => s.revision[A], { timeout: 6000 }).toBe(5);
  await page.waitForTimeout(500);
  await change(page, B);
  await expect
    .poll(() => s.puts.filter((p) => p.id === B).length, { timeout: 14000 })
    .toBe(1);
  await page.waitForTimeout(500);
  noCrossWrites(s);
  expect(s.puts.at(-1)).toMatchObject({ id: B, ifMatch: '"1"' });
  expect(s.revision[B]).toBe(2);
});
