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
// Test data and the canvas on a REAL running local platform (no route mocks):
// they persist across a reload and reach a second browser profile through the
// platform; the platform refuses a secret and nothing keeps a copy; signing
// out leaves no keys of the platform on this computer. Skipped unless
// WEAVE_E2E_PLATFORM_DIR and WEAVE_E2E_PERSON_FILE are set, as
// real-platform.spec.ts describes; build the app first (`npm run build`).
//
// The platform has no published action whose output a pin could name, so
// pins are covered by the mocked specs; here the trigger's test input is the
// test data that travels.
import { createHash, randomBytes } from "node:crypto";
import { resolve } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { setTestInput } from "./formula-po";
import {
  connectAndSignIn,
  expectNoCredentialsLeft,
  indicator,
  pair,
  removePlatform,
  session,
  setup,
  shot,
  uniqueName,
  useRealPlatformSuite,
} from "./real-platform-support";
import { StepDetailsPage } from "./step-details-po";

useRealPlatformSuite("real-test-data");

const fixture = resolve("tests/fixtures/real-test-data.yaml");
const sha = (text: string) => createHash("sha256").update(text).digest("hex");

/** The new editor, before the page loads. */
async function newEditor(page: Page) {
  await page.addInitScript(() =>
    localStorage.setItem("ui:weave.editorNext", "true"),
  );
}
/** A platform call through the page's own Studio session, as any client makes it. */
async function bridge(
  page: Page,
  path: string,
  method = "GET",
  body?: unknown,
  revision?: number,
) {
  return page.evaluate(
    async ({ path, method, body, revision }) => {
      const session = (await (
        await fetch("/studio/session", { credentials: "same-origin" })
      ).json()) as { csrfToken?: string };
      const response = await fetch(`/studio/api${path}`, {
        method,
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-Weave-CSRF": session.csrfToken ?? "",
          ...(method === "GET"
            ? {}
            : { "Idempotency-Key": crypto.randomUUID() }),
          ...(revision === undefined ? {} : { "If-Match": `"${revision}"` }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const text = await response.text();
      return { status: response.status, text };
    },
    { path, method, body, revision },
  );
}
/** The trigger's Test event shows this Customer ID (the test data reads from the platform first). */
async function expectCustomerId(page: Page, value: string) {
  const details = new StepDetailsPage(page);
  await details.trigger().focus();
  await page.keyboard.press("Enter");
  const dialog = details.dialogFor("Manual form trigger");
  await expect(dialog).toBeVisible();
  await expect(
    dialog.locator(".sd-input").getByLabel("Customer ID", { exact: true }),
  ).toHaveValue(value, { timeout: 30_000 });
  await details.close();
}

test("test data and the canvas persist across reload and a second browser profile; the platform refuses a secret and Studio keeps no copy; signing out leaves no data keys", async ({
  page,
  browser,
}) => {
  test.setTimeout(420_000);
  const { person, environment } = setup();
  const project = `/api/v1/tenants/${environment.tenant_id}/projects/${environment.project_id}`;
  await newEditor(page);
  const s = await session(page);
  const name = uniqueName("e2e-test-data");
  await pair(page, s.host!, "td-01-pairing");
  await connectAndSignIn(page, name, person);

  // 1. The workflow as a platform draft, its test input and a note.
  await page.getByRole("button", { name: "Home", exact: true }).click();
  await page.getByLabel("Choose a workflow file").setInputFiles(fixture);
  await expect(page.locator(".editor-bar")).toBeVisible();
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect(page.locator(".editor-bar")).toContainText(/Saved \d/, {
    timeout: 30_000,
  });
  const draftId = new URL(page.url()).pathname.split("/")[2];
  const details = new StepDetailsPage(page);
  const testData = page.waitForResponse(
    (r) =>
      r.url().endsWith(`/drafts/${draftId}/studio/test-data`) &&
      r.request().method() === "PUT",
    { timeout: 60_000 },
  );
  await setTestInput(page, details, {
    "Customer ID": "c-1042",
    Amount: "1500",
  });
  expect((await testData).status()).toBeLessThan(300);
  // The canvas save that holds the note and its text.
  const canvas = page.waitForResponse(
    (r) =>
      r.url().endsWith(`/drafts/${draftId}/studio/canvas`) &&
      r.request().method() === "PUT" &&
      (r.request().postData() ?? "").includes("Kept on the platform"),
    { timeout: 60_000 },
  );
  await page.locator('[data-step="shape"]').first().click();
  await page.keyboard.press("Shift+S");
  await page.keyboard.insertText("Kept on the platform");
  await page.keyboard.press("Escape");
  expect((await canvas).status()).toBeLessThan(300);
  await shot(page, "td-02-saved");

  // 2. Reload: the values and the note come back.
  await page.reload();
  await expect(page.locator('[data-step="shape"]').first()).toBeVisible({
    timeout: 30_000,
  });
  await expectCustomerId(page, "c-1042");
  await expect(page.locator(".sticky-note-body")).toHaveText(
    "Kept on the platform",
    { timeout: 30_000 },
  );

  // 3. A second browser profile, its own host and sign-in: the same draft,
  // from the platform (this profile's storage starts empty).
  const other = await browser.newContext({
    viewport: { width: 1440, height: 900 },
  });
  const second = await other.newPage();
  await newEditor(second);
  const s2 = await session(second);
  await pair(second, s2.host!, "td-03-second-pairing");
  await connectAndSignIn(second, name, person);
  await second.goto(`${s2.host!.origin}/workflows/${draftId}/designer`);
  await expect(second.locator('[data-step="shape"]').first()).toBeVisible({
    timeout: 30_000,
  });
  await expectCustomerId(second, "c-1042");
  await expect(second.locator(".sticky-note-body")).toHaveText(
    "Kept on the platform",
    { timeout: 30_000 },
  );
  await shot(second, "td-03-second-profile");

  // 4. A secret value: Studio never lets it be typed, and the platform
  // refuses it from any client; no copy holds it anywhere.
  const canary = `canary-${randomBytes(6).toString("hex")}`;
  const draft = JSON.parse(
    (await bridge(page, `${project}/drafts/${draftId}`)).text,
  ) as { revision: number };
  const stored = JSON.parse(
    (await bridge(page, `${project}/drafts/${draftId}/studio/test-data`)).text,
  ) as { revision: number; document: Record<string, unknown> };
  const refused = await bridge(
    page,
    `${project}/drafts/${draftId}/studio/test-data`,
    "PUT",
    {
      draft_revision: draft.revision,
      document: {
        ...stored.document,
        input: {
          value: { customerId: "c-1042", amount: 1500, password: canary },
          source: "manual",
        },
      },
    },
    stored.revision,
  );
  expect(refused.status, refused.text).toBe(422);
  expect(refused.text).toContain("WV-STUDIO-DOCUMENT-SECRET");
  expect(refused.text).not.toContain(canary);
  const after = await bridge(
    page,
    `${project}/drafts/${draftId}/studio/test-data`,
  );
  expect(after.text).not.toContain(canary);
  for (const view of [page, second])
    expect(
      await view.evaluate(() => JSON.stringify(localStorage)),
    ).not.toContain(canary);

  // 5. Sign out: no key of this platform stays on this computer.
  await indicator(page).click();
  await page
    .locator("#platform-menu")
    .getByRole("button", { name: "Sign out" })
    .click();
  await page
    .getByRole("dialog", { name: `Sign out of ${name}?` })
    .getByRole("button", { name: "Sign out" })
    .click();
  await expect(indicator(page)).toContainText("Signed out", {
    timeout: 30_000,
  });
  const prefix = `p${sha(name).slice(0, 16)}:`;
  expect(
    (await page.evaluate(() => Object.keys(localStorage))).filter((key) =>
      key.startsWith(prefix),
    ),
  ).toEqual([]);
  await shot(page, "td-05-signed-out");

  // 6. Remove the platform from both profiles; no credential stays.
  await removePlatform(page, name);
  await removePlatform(second, name);
  await other.close();
  expectNoCredentialsLeft();
  expect(s.errors).toEqual([]);
});
