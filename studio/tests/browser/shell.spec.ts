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
// App-wide behavior: pairing, session expiry, in-app dialogs, exports in the
// desktop shell, plain-language errors and navigation semantics.
import { test, expect, Page } from "@playwright/test";
import {
  allCapabilities,
  command,
  connected,
  insertStep,
  newWorkflow,
  offline,
} from "./support";

const desktopShell = (page: Page) =>
  page.addInitScript(() => {
    (window as unknown as Record<string, unknown>)["__TAURI_INTERNALS__"] = {};
  });

test("reloading a paired window never flashes the pairing page", async ({
  page,
}) => {
  await page.addInitScript(() => {
    new MutationObserver(() => {
      if (document.getElementById("pair-code"))
        (window as unknown as Record<string, boolean>)["pairSeen"] = true;
    }).observe(document, { childList: true, subtree: true });
  });
  await page.route("**/studio/session", async (r) => {
    await new Promise((resolve) => setTimeout(resolve, 600));
    await r.fulfill({
      json: { paired: true, version: "1", mode: "offline", profile: null },
    });
  });
  await page.goto("/");
  await expect(
    page.getByRole("status").filter({ hasText: "Opening Studio" }),
  ).toBeVisible();
  await expect(page.locator("weave-home-dashboard")).toBeVisible();
  expect(await page.evaluate(() => (window as any).pairSeen ?? false)).toBe(
    false,
  );
});

test("pairing focuses the code, ignores empty or repeated Enter and explains failures", async ({
  page,
}) => {
  const posts: unknown[] = [];
  await page.route("**/studio/session", async (r) => {
    if (r.request().method() === "GET")
      return r.fulfill({
        json: { paired: false, version: "1", mode: "offline", profile: null },
      });
    posts.push(r.request().postDataJSON());
    await new Promise((resolve) => setTimeout(resolve, 300));
    return r.fulfill({
      status: 403,
      json: {
        status: 403,
        code: "WV-STUDIO-PAIRING",
        message: "Pairing code is invalid or expired; restart Studio",
      },
    });
  });
  await page.goto("/");
  const code = page.getByLabel("Pairing code");
  await expect(code).toBeFocused();
  await code.press("Enter");
  expect(posts).toHaveLength(0);
  await code.fill("  stale-code ");
  await code.press("Enter");
  await code.press("Enter");
  await expect(page.getByRole("alert")).toContainText(
    "That pairing code is invalid or has expired.",
  );
  await expect(page.getByRole("alert")).toContainText(
    "Support code: WV-STUDIO-PAIRING",
  );
  await expect(page.getByRole("alert")).not.toContainText("{");
  expect(posts).toEqual([{ code: "stale-code" }]);
});

test("an ended session returns the browser to pairing with an explanation", async ({
  page,
}) => {
  await connected(page, {
    capabilities: [...allCapabilities, "status.read"],
  });
  await page.route("**/environments/development/workers?*", (r) =>
    r.fulfill({
      status: 401,
      json: { status: 401, code: "WV-STUDIO-SESSION", message: "Pair again" },
    }),
  );
  await page.getByRole("button", { name: "Workers", exact: true }).click();
  await expect(page.getByLabel("Pairing code")).toBeVisible();
  await expect(
    page.getByRole("status").filter({ hasText: "Your Studio session ended." }),
  ).toBeVisible();
  await expect(page.locator(".error-banner")).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("an ended session reloads the desktop shell so it can pair again", async ({
  page,
}) => {
  await desktopShell(page);
  await connected(page, {
    capabilities: [...allCapabilities, "status.read"],
  });
  let sessions = 0;
  await page.route("**/studio/session", (r) => {
    sessions++;
    return r.fulfill({
      json: {
        paired: true,
        csrfToken: "test-csrf",
        version: "1",
        mode: "connected",
        profile: {
          name: "Test platform",
          baseUrl: "https://weave.invalid",
          tenantId: "tenant",
          projectId: "project",
          environmentId: "development",
        },
      },
    });
  });
  await page.route("**/environments/development/workers?*", (r) =>
    r.fulfill({ status: 401, json: { code: "WV-STUDIO-SESSION" } }),
  );
  await page.getByRole("button", { name: "Workers", exact: true }).click();
  // The desktop shell re-pairs on load, so Studio reloads once instead of asking
  // for a code; a second failure explains instead of reloading in a loop.
  await expect.poll(() => sessions).toBe(1);
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: "Quit and reopen Firefly Weave Studio." }),
  ).toBeVisible();
  await page.waitForTimeout(1000);
  expect(sessions).toBe(1);
});

test("dialogs are labeled, trap focus, close on Escape and return focus", async ({
  page,
}) => {
  await connected(page);
  await newWorkflow(page);
  await insertStep(page, "Transform");
  // Publish waits in More until the draft is saved; More opens the dialog.
  const publish = page.getByRole("button", { name: "More", exact: true });
  await command(page, "Publish…");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toHaveAttribute("aria-modal", "true");
  const heading = await dialog.getAttribute("aria-labelledby");
  await expect(page.locator(`[id="${heading}"]`)).toHaveText(
    "Publish untitled-workflow 1.0.0?",
  );
  await expect(
    dialog.getByRole("button", { name: "Publish version" }),
  ).toBeFocused();
  for (let i = 0; i < 6; i++) {
    await page.keyboard.press("Tab");
    expect(
      await page.evaluate(
        () => !!document.activeElement?.closest(".modal-panel"),
      ),
    ).toBe(true);
  }
  await page.keyboard.press("Shift+Tab");
  expect(
    await page.evaluate(
      () => !!document.activeElement?.closest(".modal-panel"),
    ),
  ).toBe(true);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(publish).toBeFocused();
  await expect(page.locator('[data-step="transform-1"]')).toHaveCount(1);
  // A press on the backdrop keeps focus in the dialog, so Escape still closes it.
  await command(page, "Publish…");
  await expect(dialog).toBeVisible();
  await page.mouse.click(4, 450);
  expect(
    await page.evaluate(
      () => !!document.activeElement?.closest(".modal-panel"),
    ),
  ).toBe(true);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});

test("export in the desktop shell downloads both files to the shell", async ({
  page,
}) => {
  await desktopShell(page);
  await offline(page);
  await newWorkflow(page);
  await insertStep(page, "Transform");
  const names: string[] = [];
  page.on("download", (download) => names.push(download.suggestedFilename()));
  await command(page, "Save to file");
  await expect.poll(() => names.length).toBe(2);
  expect(names.sort()).toEqual([
    "untitled-workflow.layout.json",
    "untitled-workflow.yaml",
  ]);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("export offers copies when no download can start", async ({ page }) => {
  await page.addInitScript(() => {
    URL.createObjectURL = () => {
      throw new Error("blocked");
    };
  });
  await offline(page);
  await newWorkflow(page);
  await insertStep(page, "Transform");
  await command(page, "Save to file");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Save your exported files");
  await expect(
    dialog.getByRole("textbox", { name: "Contents of untitled-workflow.yaml" }),
  ).toHaveValue(/kind: Workflow/);
  await expect(
    dialog.getByRole("button", { name: "Copy untitled-workflow.yaml" }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", {
      name: "Copy untitled-workflow.layout.json",
    }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(dialog).toHaveCount(0);
});

test("browser export downloads both files without a fallback dialog", async ({
  page,
}) => {
  await offline(page);
  await newWorkflow(page);
  const names: string[] = [];
  page.on("download", (download) => names.push(download.suggestedFilename()));
  await command(page, "Save to file");
  await expect.poll(() => names.length).toBe(2);
  expect(names.sort()).toEqual([
    "untitled-workflow.layout.json",
    "untitled-workflow.yaml",
  ]);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("API failures read as plain language with a support code", async ({
  page,
}) => {
  await connected(page);
  await page.route("**/environments/development/runs?*", (r) =>
    r.fulfill({
      status: 500,
      json: { code: "WV-INTERNAL", message: '{"trace":"x"}' },
    }),
  );
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  const banner = page.locator(".error-banner");
  await expect(banner).toContainText(
    "The platform could not complete this request.",
  );
  await expect(banner).toContainText("Support code: WV-INTERNAL");
  await expect(banner).not.toContainText("trace");
});

test("connection and worker details read their own resources", async ({
  page,
}) => {
  await connected(page, {
    capabilities: [...allCapabilities, "status.read"],
  });
  const connection = {
    id: "0f8fad5b-d9cb-469f-a165-70867728950e",
    name: "orders-db",
    revision: 3,
    connector: "weave-postgresql@1.0.0",
    adapter: "postgresql",
    allowed_destinations: ["db.internal:5432"],
  };
  const worker = {
    id: "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    release_id: "11111111-1111-4111-8111-111111111111",
    task_types: ["crm-lookup"],
    capacity: 4,
    principal_id: "22222222-2222-4222-8222-222222222222",
    revoked: false,
  };
  const reads: string[] = [];
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (/\/(runs|connections|workers)\/[0-9a-f-]{36}$/.test(path))
      reads.push(path.split("/environments/development/")[1]);
  });
  await page.route("**/environments/development/connections?*", (r) =>
    r.fulfill({
      json: {
        items: [
          connection,
          { id: "33333333-3333-4333-8333-333333333333", unavailable: true },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route(`**/connections/${connection.id}`, (r) =>
    r.fulfill({ json: connection }),
  );
  await page.route("**/environments/development/workers?*", (r) =>
    r.fulfill({ json: { items: [worker], next_cursor: null } }),
  );
  await page.route(`**/workers/${worker.id}`, (r) =>
    r.fulfill({ json: worker }),
  );
  await page.getByRole("button", { name: "Connections", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Connections", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await page.locator(".resource-row").filter({ hasText: "orders-db" }).click();
  const detail = page.locator(".record-detail");
  await expect(detail).toContainText("weave-postgresql 1.0.0");
  await expect(detail).toContainText("db.internal:5432");
  await page
    .locator(".resource-row")
    .filter({ hasText: "Unavailable connection" })
    .click();
  await expect(detail).toContainText("Studio can't show this connection.");
  await page.getByRole("button", { name: "Workers", exact: true }).click();
  await page.locator(".resource-row").first().click();
  await expect(detail).toContainText("crm-lookup");
  await expect(detail).toContainText("Active");
  expect(reads).toEqual([
    `connections/${connection.id}`,
    `workers/${worker.id}`,
  ]);
});

test("the desktop shell never asks for a terminal pairing code", async ({
  page,
}) => {
  await desktopShell(page);
  await page.route("**/studio/session", (r) =>
    r.fulfill({
      json: { paired: false, version: "1", mode: "offline", profile: null },
    }),
  );
  await page.goto("/");
  const card = page.locator(".pair-card");
  await expect(card).toContainText(/quit and reopen Firefly Weave Studio/i);
  await expect(card).not.toContainText("terminal");
});

test("an ended session keeps designer edits for after pairing again", async ({
  page,
}) => {
  await connected(page);
  await newWorkflow(page);
  await insertStep(page, "Transform");
  await page.route("**/studio/local/validate", (r) =>
    r.fulfill({ status: 401, json: { code: "WV-STUDIO-SESSION" } }),
  );
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  await expect(page.getByLabel("Pairing code")).toBeVisible();
  await page.route("**/studio/session", (r) =>
    r.fulfill({
      json: {
        paired: true,
        csrfToken: "new-csrf",
        version: "1",
        mode: "offline",
        profile: null,
      },
    }),
  );
  await page.getByLabel("Pairing code").fill("fresh-code");
  await page.getByRole("button", { name: "Pair browser" }).click();
  await expect(page.locator('[data-step="transform-1"]')).toBeVisible();
  await expect(page.locator(".editor-identity .status-chip")).toHaveText(
    "Draft saved",
  );
});

test("an ended desktop session with unsaved edits offers export instead of reloading", async ({
  page,
}) => {
  await desktopShell(page);
  await connected(page);
  let sessions = 0;
  await page.route("**/studio/session", (r) => {
    sessions++;
    return r.fulfill({
      json: { paired: true, version: "1", mode: "offline", profile: null },
    });
  });
  await newWorkflow(page);
  await insertStep(page, "Transform");
  await page.route("**/studio/local/validate", (r) =>
    r.fulfill({ status: 401, json: { code: "WV-STUDIO-SESSION" } }),
  );
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  const banner = page.locator(".error-banner");
  await expect(banner).toContainText("Export your workflow to keep your edits");
  await page.waitForTimeout(500);
  expect(sessions).toBe(0);
  await expect(page.locator('[data-step="transform-1"]')).toBeVisible();
  // Later local calls keep pointing at the way out instead of failing silently.
  await banner.getByRole("button", { name: "Dismiss error" }).click();
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  await expect(banner).toContainText("Export your workflow to keep your edits");
  const names: string[] = [];
  page.on("download", (download) => names.push(download.suggestedFilename()));
  await command(page, "Save to file");
  await expect.poll(() => names.length).toBe(2);
});
