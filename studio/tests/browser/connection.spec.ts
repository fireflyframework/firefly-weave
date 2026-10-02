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
import { test, expect } from "@playwright/test";
test("reviewed connection exposes manual PKCE address and protects CLI authentication", async ({
  page,
}) => {
  await page.route("**/studio/session", (r) =>
    r.fulfill({
      json: {
        paired: true,
        csrfToken: "test",
        version: "test",
        mode: "offline",
        profile: null,
      },
    }),
  );
  let supported = false;
  await page.route("**/studio/connection", (r) =>
    r.fulfill({
      json: {
        configured: true,
        login_supported: supported,
        authentication: { authenticated: false },
        login: {
          state: "awaiting_user",
          id: "example",
          authorization_uri: "https://identity.example.invalid/authorize",
        },
      },
    }),
  );
  await page.goto("/settings");
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("This profile uses CLI-managed authentication.", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Copy sign-in address" }),
  ).toBeVisible();
  await expect(
    page.getByText("Open this address in your web browser.", { exact: false }),
  ).toBeVisible();
  await page
    .getByLabel("Import login configuration", { exact: true })
    .setInputFiles({
      name: "operator.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({
          provider_id: "example",
          issuer: "https://identity.example.invalid",
          client_id: "studio",
          target: "https://api.example.invalid",
          account: "operator",
        }),
      ),
    });
  await expect(
    page.getByRole("button", { name: "Save reviewed connection" }),
  ).toBeDisabled();
  await page
    .getByLabel("I trust this API target and identity provider")
    .check();
  await expect(
    page.getByRole("button", { name: "Save reviewed connection" }),
  ).toBeEnabled();
  await page.screenshot({
    path: "test-results/connection-assistant.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "test-results/connection-assistant-mobile.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 1280, height: 720 });
  await page
    .getByRole("button", { name: "Copy sign-in address" })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: "test-results/connection-sign-in.png",
    fullPage: true,
  });
  await page
    .getByLabel("Import login configuration", { exact: true })
    .setInputFiles({
      name: "secret.json",
      mimeType: "application/json",
      buffer: Buffer.from(JSON.stringify({ access_token: "never-import" })),
    });
  await expect(
    page.getByRole("button", { name: "Save reviewed connection" }),
  ).toHaveCount(0);
  await expect(
    page.getByText(
      "Tokens, secrets and credential file paths are not accepted.",
      { exact: false },
    ),
  ).toBeVisible();
});
for (const status of [401, 403, 502])
  test(`connection check explains HTTP ${status}`, async ({ page }) => {
    await page.route("**/studio/session", (r) =>
      r.fulfill({
        json: {
          paired: true,
          csrfToken: "test",
          version: "test",
          mode: "offline",
          profile: null,
        },
      }),
    );
    await page.route("**/studio/connection", (r) =>
      r.fulfill({
        json: {
          configured: true,
          login_supported: true,
          authentication: { authenticated: false },
          login: { state: "idle" },
        },
      }),
    );
    await page.route("**/studio/connection/test", (r) =>
      r.fulfill({
        status,
        json: { code: "WV-TEST", message: "private technical detail" },
      }),
    );
    await page.goto("/settings");
    await page
      .getByRole("button", { name: "Check connection and discover workspaces" })
      .click();
    await expect(
      page.getByText(
        status === 401
          ? "Sign in again. If provider sign-in succeeds"
          : status === 403
            ? "Ask an administrator to grant the required scoped permissions"
            : "Check the reviewed URLs, network connection and TLS configuration",
        { exact: false },
      ),
    ).toBeVisible();
    await expect(
      page.getByText("private technical detail", { exact: false }),
    ).toHaveCount(0);
  });

test("configure, device login cancellation and authenticated scope discovery", async ({
  page,
}) => {
  const session = {
    paired: true,
    csrfToken: "owned",
    version: "test",
    mode: "connected",
    profile: {
      name: "Reviewed",
      baseUrl: "https://api.example.invalid",
      tenantId: null,
      projectId: null,
      environmentId: null,
    },
  };
  let configured = false,
    state = "idle",
    starts = 0;
  const identity = {
    principal_id: "actor",
    kind: "human",
    grants: [],
    workspaces: [
      {
        id: "tenant",
        name: "Tenant",
        projects: [
          {
            id: "project",
            name: "Project",
            environments: [{ id: "env", name: "Environment" }],
          },
        ],
      },
    ],
    truncated: false,
  };
  await page.route("**/studio/session", (r) =>
    r.fulfill({ json: { ...session, mode: "offline", profile: null } }),
  );
  await page.route(/\/studio\/connection(?:\/.*)?$/, async (r) => {
    const path = new URL(r.request().url()).pathname;
    if (path.endsWith("/configure")) {
      expect(r.request().postDataJSON().trust_confirmed).toBe(true);
      expect(r.request().headers()["x-weave-csrf"]).toBe("owned");
      configured = true;
      return r.fulfill({ json: session });
    }
    if (path.endsWith("/start")) {
      starts++;
      state = "awaiting_user";
      return r.fulfill({
        json: {
          id: "login",
          state,
          verification_uri: "https://identity.example.invalid/device",
          user_code: "EXAMPLE",
        },
      });
    }
    if (path.endsWith("/cancel")) {
      state = "cancelled";
      return r.fulfill({ json: { id: "login", state } });
    }
    if (path.endsWith("/test"))
      return r.fulfill({ json: { session, identity } });
    if (path.endsWith("/login/login"))
      return r.fulfill({ json: { id: "login", state: "authenticated" } });
    return r.fulfill({
      json: {
        configured,
        login_supported: true,
        authentication: { authenticated: false },
        login: { state },
      },
    });
  });
  await page.goto("/settings");
  await page
    .getByLabel("Import login configuration", { exact: true })
    .setInputFiles({
      name: "login.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({
          provider_id: "provider",
          issuer: "https://identity.example.invalid",
          client_id: "studio",
          target: "https://api.example.invalid",
          account: "operator",
        }),
      ),
    });
  await page
    .getByLabel("I trust this API target and identity provider")
    .check();
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Save reviewed connection" }).click();
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByLabel("Device code", { exact: true })).toHaveValue(
    "EXAMPLE",
  );
  await page.getByRole("button", { name: "Cancel sign-in" }).click();
  await expect(page.getByText("Login status: cancelled")).toBeVisible();
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByLabel("Workspace and environment").locator("option"),
  ).toHaveCount(2, { timeout: 5000 });
  expect(starts).toBe(2);
});

test("delayed old connection check cannot replace a switched scope", async ({
  page,
}) => {
  const profile = {
    name: "Selected",
    baseUrl: "https://api.example.invalid",
    tenantId: "tenant",
    projectId: "project",
    environmentId: "old",
  };
  const session = {
    paired: true,
    csrfToken: "owned",
    version: "test",
    mode: "connected",
    profile,
  };
  const identity = {
    principal_id: "actor",
    kind: "human",
    grants: [],
    workspaces: [
      {
        id: "tenant",
        name: "Tenant",
        projects: [
          {
            id: "project",
            name: "Project",
            environments: [
              { id: "old", name: "Old" },
              { id: "new", name: "New" },
            ],
          },
        ],
      },
    ],
    truncated: false,
  };
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/studio/session", (r) => r.fulfill({ json: session }));
  await page.route("**/studio/api/api/v1/identity", (r) =>
    r.fulfill({ json: identity }),
  );
  await page.route("**/studio/connection", (r) =>
    r.fulfill({
      json: {
        configured: true,
        login_supported: true,
        authentication: { authenticated: true },
        login: { state: "idle" },
      },
    }),
  );
  await page.route("**/studio/connection/test", async (r) => {
    await held;
    await r.fulfill({
      json: { session, identity: { ...identity, workspaces: [] } },
    });
  });
  await page.route("**/studio/scope", (r) =>
    r.fulfill({
      json: { ...session, profile: { ...profile, environmentId: "new" } },
    }),
  );
  await page.goto("/settings");
  await page
    .getByRole("button", { name: "Check connection and discover workspaces" })
    .click();
  await page
    .getByLabel("Workspace and environment")
    .selectOption("tenant/project/new");
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Change workspace" }).click();
  await expect(
    page.getByLabel("Workspace and environment").locator("option"),
  ).toHaveCount(3);
  release();
  await page.waitForTimeout(200);
  await expect(
    page.getByLabel("Workspace and environment").locator("option"),
  ).toHaveCount(3);
  await expect(
    page.getByText("Authenticated, but no workspace is visible", {
      exact: false,
    }),
  ).toHaveCount(0);
});

test("delayed reviewed save locks configuration edits and ignores dropped replacement", async ({
  page,
}) => {
  const session = {
    paired: true,
    csrfToken: "owned",
    version: "test",
    mode: "connected",
    profile: {
      name: "Reviewed",
      baseUrl: "https://api.example.invalid",
      tenantId: null,
      projectId: null,
      environmentId: null,
    },
  };
  await page.route("**/studio/session", (r) =>
    r.fulfill({ json: { ...session, mode: "offline", profile: null } }),
  );
  await page.route("**/studio/connection", (r) =>
    r.fulfill({
      json: {
        configured: true,
        login_supported: true,
        authentication: { authenticated: false },
        login: { state: "idle" },
      },
    }),
  );
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route("**/studio/connection/configure", async (r) => {
    await held;
    await r.fulfill({ json: session });
  });
  await page.goto("/settings");
  await page
    .getByLabel("Import login configuration", { exact: true })
    .setInputFiles({
      name: "reviewed.json",
      mimeType: "application/json",
      buffer: Buffer.from(
        JSON.stringify({
          provider_id: "provider",
          issuer: "https://identity.example.invalid",
          client_id: "studio",
          target: "https://api.example.invalid",
          account: "operator",
        }),
      ),
    });
  await page.getByText("Enter connection details", { exact: true }).click();
  await page
    .getByLabel("I trust this API target and identity provider")
    .check();
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Save reviewed connection" }).click();
  await expect(
    page.getByRole("button", { name: "Replace login configuration" }),
  ).toBeDisabled();
  await expect(page.getByLabel("API origin", { exact: true })).toBeDisabled();
  await expect(
    page.getByLabel("I trust this API target and identity provider"),
  ).toBeDisabled();
  await page.locator(".configuration-dropzone").evaluate((element) => {
    const data = new DataTransfer();
    data.items.add(
      new File(
        [
          JSON.stringify({
            provider_id: "replacement",
            issuer: "https://other.example.invalid",
            client_id: "other",
            target: "https://other.example.invalid",
            account: "other",
          }),
        ],
        "replacement.json",
        { type: "application/json" },
      ),
    );
    element.dispatchEvent(
      new DragEvent("drop", { bubbles: true, dataTransfer: data }),
    );
  });
  await expect(page.getByLabel("API origin", { exact: true })).toHaveValue(
    "https://api.example.invalid",
  );
  release();
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeEnabled();
  await expect(page.getByLabel("API origin", { exact: true })).toBeEnabled();
  await expect(
    page.getByText("reviewed.json · Imported for review", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("replacement.json", { exact: false }),
  ).toHaveCount(0);
});
