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
// The shell: landmarks, a skip link, a title per page, Build/Operate
// navigation, the platform indicator and menu, Home's "Needs you", template
// rows, connections, Start a run and Settings.
import { selectChoice } from "./support";
import { test, expect, Page } from "@playwright/test";
import { allCapabilities, connected, offline } from "./support";
import {
  acmePlatform,
  globexPlatform,
  platformHost,
  type PlatformHost,
} from "./platform-host";

const environment =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(
    `${viewport.width}x${viewport.height}`,
    { tag: "@xplat" },
    () => {
      test.use({ viewport });

      test("one main landmark, a skip link first and a title per page", async ({
        page,
      }) => {
        await offline(page);
        await expect(page.locator("main#main")).toHaveCount(1);
        await expect(page.locator("aside.sidebar")).toHaveCount(0);
        await expect(
          page.getByRole("link", { name: "Firefly Weave Studio home" }),
        ).toBeVisible();
        await expect(page).toHaveTitle("Home | Firefly Weave Studio");
        // The skip link is the first stop and lands on the page heading.
        await page.keyboard.press("Tab");
        const skip = page.getByRole("link", { name: "Skip to content" });
        await expect(skip).toBeFocused();
        await expect(skip).toBeVisible();
        await page.keyboard.press("Enter");
        await expect(page.locator("#main h1")).toBeFocused();
        // A view change names the page and moves focus to its heading.
        await page.getByRole("button", { name: "Runs", exact: true }).click();
        await expect(page).toHaveTitle("Runs | Firefly Weave Studio");
        await expect(page.locator("#main h1")).toHaveText("Runs");
        await expect(page.locator("#main h1")).toBeFocused();
        await page
          .getByRole("button", { name: "My tasks", exact: true })
          .click();
        await expect(page).toHaveTitle("My tasks | Firefly Weave Studio");
        await page
          .getByRole("button", { name: "Settings", exact: true })
          .click();
        await expect(page).toHaveTitle("Settings | Firefly Weave Studio");
      });

      test("navigation groups Build, Work and Operate, with Settings at the bottom", async ({
        page,
      }) => {
        await offline(page);
        const nav = page.getByRole("navigation", { name: "Main navigation" });
        const labels = await nav
          .locator("button span, .nav-group")
          .evaluateAll((items) =>
            items
              .filter((item) => getComputedStyle(item).display !== "none")
              .map((item) => item.textContent?.trim()),
          );
        if (viewport.width > 1280)
          expect(labels).toEqual([
            "Home",
            "Build",
            "Workflows",
            "Connections",
            "Work",
            "My tasks",
            "Email",
            "Operate",
            "Runs",
            "Incidents",
            "Workers",
            "Clusters",
            "Settings",
            "Collapse sidebar",
          ]);
        await expect(nav).not.toContainText("Build a clear path.");
        await expect(page.locator(".breadcrumbs")).toHaveCount(0);
        const settings = (await nav
          .getByRole("button", { name: "Settings" })
          .boundingBox())!;
        const workers = (await nav
          .getByRole("button", { name: "Workers" })
          .boundingBox())!;
        expect(settings.y).toBeGreaterThan(workers.y + workers.height);
      });

      test("Home leads with what needs you, and templates are rows", async ({
        page,
      }) => {
        await page.setViewportSize(
          viewport.width === 1440 ? { width: 1280, height: 720 } : viewport,
        );
        await connected(page, {
          capabilities: [...allCapabilities, "human_task.claim"],
        });
        await page.route(`${environment}/human-tasks?*`, (r) => {
          const status = new URL(r.request().url()).searchParams.get("status");
          return r.fulfill({
            json: {
              items:
                status === "ready"
                  ? [
                      {
                        id: "task-1",
                        run_id: "run-1",
                        status: "ready",
                        title: "Review expense 104",
                      },
                    ]
                  : [],
              next_cursor: null,
            },
          });
        });
        await page.route(`${environment}/runs?*`, (r) =>
          r.fulfill({
            json: {
              items: [
                { id: "run-failed", state: { status: "failed" } },
                { id: "run-ok", state: { status: "succeeded" } },
              ],
              next_cursor: null,
            },
          }),
        );
        await page.getByRole("button", { name: "Runs", exact: true }).click();
        await page.getByRole("button", { name: "Home", exact: true }).click();
        const needs = page.getByRole("region", { name: "Needs you" });
        const first = needs.locator(".preview-row").first();
        await expect(first).toContainText("Review expense 104");
        await expect(first.locator(".status-pill")).toHaveText(
          "Ready to claim",
        );
        await expect(needs.locator(".preview-row")).toHaveCount(2);
        await expect(needs.locator(".preview-row").last()).toContainText(
          "Failed",
        );
        if (viewport.width === 1440)
          expect((await first.boundingBox())!.y).toBeLessThan(400);
        await expect(page.locator("weave-home-dashboard")).not.toContainText(
          "Welcome to Weave Studio",
        );
        await expect(
          page.getByRole("button", { name: "Platform settings" }),
        ).toHaveCount(0);
        await expect(page.locator("weave-home-dashboard")).not.toContainText(
          "Ready To Claim",
        );
        const gallery = page.locator("weave-template-gallery");
        await expect(gallery.locator("[data-template]")).toHaveCount(5);
        await expect(gallery.locator("button.primary")).toHaveCount(0);
        await expect(
          gallery
            .locator('[data-template="api-call"]')
            .locator('.status-pill[data-tone="warning"]'),
        ).toHaveText("You choose the action");
        // The row opens the task itself.
        await first.click();
        await expect(page.getByRole("heading", { level: 1 })).toHaveText(
          "My tasks",
        );
      });

      test("the platform indicator and menu name the workspace and account", async ({
        page,
      }) => {
        await platformHost(page, {
          platforms: [acmePlatform(), globexPlatform()],
          active: "Acme",
        });
        const indicator = page.locator(".platform-indicator");
        if (viewport.width > 767) {
          await expect(indicator).toContainText("Payments / Production");
          // The top bar has the room: the account line shows whole on a laptop.
          const line = indicator.locator("small");
          await expect(line).toContainText("jane@acme.example");
          expect(
            await line.evaluate((e) => e.scrollWidth <= e.clientWidth),
          ).toBe(true);
        }
        await expect(indicator).not.toContainText("Selected");
        await indicator.click();
        const menu = page.locator("#platform-menu");
        await expect(menu.locator(".platform-menu-heading")).toContainText(
          "Payments / Production · jane@acme.example",
        );
        await expect(menu.getByRole("button")).toHaveText([
          "Switch workspace",
          "Switch account",
          "Sign out",
          "Platform settings",
        ]);
      });
    },
  );

test("at 360 px a signed-out platform keeps a word beside the dot", async ({
  page,
}) => {
  await page.setViewportSize({ width: 360, height: 740 });
  await platformHost(page, {
    platforms: [acmePlatform({ signedIn: false })],
    active: "Acme",
  });
  const word = page.locator(".platform-indicator .indicator-word");
  await expect(word).toHaveText("Sign in");
  await expect(word).toBeVisible();
  const size = await word.evaluate((e) =>
    parseFloat(getComputedStyle(e).fontSize),
  );
  expect(size).toBeGreaterThanOrEqual(12);
});

test("a toast sits bottom-left beside the sidebar and pauses while hovered", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const host: PlatformHost = await platformHost(page, {
    platforms: [acmePlatform(), globexPlatform()],
    active: "Acme",
  });
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("button", { name: "Sign out of Acme" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Sign out", exact: true })
    .click();
  const toast = page.locator(".toast");
  await expect(toast).toContainText("Signed out of Acme.");
  const box = (await toast.boundingBox())!;
  const sidebar = (await page.locator(".sidebar").boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(sidebar.x + sidebar.width);
  expect(box.y + box.height).toBeGreaterThan(900 - 80);
  await toast.hover();
  await page.waitForTimeout(6500);
  await expect(toast).toBeVisible();
  await page.mouse.move(700, 300);
  await expect(toast).toHaveCount(0, { timeout: 8000 });
  expect(host.requests("/studio/connection/logout")).toHaveLength(1);
});

test("Connections: one New connection, the dialog's success and the real grant command", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await connected(page, {
    capabilities: [...allCapabilities, "connection.manage"],
  });
  const revision = "0f8fad5b-d9cb-469f-a165-70867728950e";
  await page.route("**/projects/project/capabilities", (r) =>
    r.fulfill({ json: { connectors: ["weave-http-v2"] } }),
  );
  await page.route(
    "**/projects/project/connector-descriptors/weave-http-v2",
    (r) =>
      r.fulfill({
        json: {
          adapter: "weave-http-v2",
          reference: "weave-http@2.0.0",
          digest: "e".repeat(64),
          manifest: {},
          source: "{}",
          implementation_version: "2.0.0",
          capabilities: [],
          bindings: [],
          actions: [],
          connection: { config_schema: {}, auth_schema: {} },
          published_version_id: "7a8cbeef-7d5c-483a-b926-4157ad4286d0",
        },
      }),
  );
  await page.route(`${environment}/connections`, (r) =>
    r.fulfill({
      status: 201,
      json: {
        id: revision,
        name: "pets",
        revision: 1,
        connector: "weave-http@2.0.0",
      },
    }),
  );
  await page.getByRole("button", { name: "Connections", exact: true }).click();
  await expect(page.locator(".subtitle")).toContainText(
    "You choose one for each connection slot when you activate a version.",
  );
  await expect(
    page.getByRole("button", { name: "New integration" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "New connection" }).click();
  const dialog = page.getByRole("dialog", { name: "New API connection" });
  await expect(dialog).toContainText(
    "You'll choose this name when you activate a workflow, for example pets.",
  );
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeVisible();
  await dialog.getByLabel("Connection name", { exact: true }).fill("pets");
  await dialog
    .getByLabel("API address", { exact: true })
    .fill("https://api.pets.example");
  await selectChoice(
    dialog.getByLabel("How the API checks who is calling", { exact: true }),
    "api-key",
  );
  await dialog.getByLabel("Header name", { exact: true }).fill("X-API-Key");
  await dialog.getByLabel("API key handle", { exact: true }).fill("pets-key");
  await dialog.getByRole("button", { name: "Create connection" }).click();
  const created = dialog.locator(".created-notice");
  await expect(created).toHaveText("Created pets (revision 1).");
  await expect(created).toHaveAttribute("data-tone", "success");
  await expect(
    dialog.getByRole("button", { name: "Create a new revision" }),
  ).toHaveCount(0);
  await expect(dialog).toContainText(
    "Last step: let the connector use this connection",
  );
  await expect(dialog.locator("code.grant")).toHaveText(
    "weave workers grant --request grant.json",
  );
  await expect(dialog.getByLabel("grant.json")).toHaveValue(
    new RegExp(`"connection_revision_id": "${revision}"`),
  );
  await expect(
    dialog.getByRole("button", { name: "Copy for an administrator" }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Done" }).click();
  await expect(dialog).toHaveCount(0);

  // A public API has no secrets, yet the connector still needs the grant.
  await page.getByRole("button", { name: "New connection" }).click();
  await dialog.getByLabel("Connection name", { exact: true }).fill("todos");
  await dialog
    .getByLabel("API address", { exact: true })
    .fill("https://api.todos.example");
  await selectChoice(
    dialog.getByLabel("How the API checks who is calling", { exact: true }),
    "none",
  );
  await dialog.getByRole("button", { name: "Create connection" }).click();
  await expect(created).toHaveText("Created pets (revision 1).");
  await expect(dialog).toContainText(
    "Last step: let the connector use this connection",
  );
  await expect(dialog.locator("code.grant")).toHaveText(
    "weave workers grant --request grant.json",
  );
});

test("Start a run: one version field, keys behind a disclosure, a toast with View", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await connected(page);
  const versionId = "44444444-4444-4444-8444-444444444444";
  await page.route(`${environment}/activations?*`, (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: "act-1",
            name: "todo-reader",
            revision: 2,
            request: { version_id: versionId },
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/projects/project/workflows?*", (r) =>
    r.fulfill({
      json: {
        items: [{ id: versionId, name: "todo-reader", version: "1.0.0" }],
        next_cursor: null,
      },
    }),
  );
  await page.route(`**/projects/project/workflows/${versionId}/export`, (r) =>
    r.fulfill({
      json: {
        document: {
          apiVersion: "weave/v1alpha1",
          kind: "Workflow",
          metadata: { name: "todo-reader", version: "1.0.0" },
          spec: { inputSchema: { type: "object" }, steps: [] },
        },
      },
    }),
  );
  const starts: unknown[] = [];
  await page.route(`${environment}/runs`, (r) => {
    starts.push(r.request().postDataJSON());
    return r.fulfill({
      status: 201,
      json: {
        id: "5e6f7a8b-0000-4000-8000-000000000001",
        state: { status: "queued" },
      },
    });
  });
  await page.route(
    `${environment}/runs/5e6f7a8b-0000-4000-8000-000000000001`,
    (r) =>
      r.fulfill({
        json: {
          id: "5e6f7a8b-0000-4000-8000-000000000001",
          state: { status: "queued" },
        },
      }),
  );
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  await page.getByRole("button", { name: "Start run", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Start a run" });
  await expect(dialog).toContainText(
    "Starts the active version in Workspace development with the input below.",
  );
  const version = dialog.getByLabel("Version to run");
  await expect(version.locator("option").nth(1)).toHaveText(
    "todo-reader 1.0.0 · revision 2",
  );
  // Starting without a version explains it at the field.
  await dialog.getByRole("button", { name: "Start run" }).click();
  await expect(dialog.getByText("Choose a version to run.")).toBeVisible();
  await expect(version).toHaveAttribute("aria-invalid", "true");
  await selectChoice(version, "act-1");
  await expect(dialog.getByLabel("Business key")).toBeHidden();
  await dialog.getByText("Add a business key (optional)").click();
  await dialog.getByLabel("Business key").fill("order-7");
  await expect(dialog).toContainText(
    "Your own reference, such as an order number.",
  );
  await dialog.getByRole("button", { name: "Start run" }).click();
  await expect(dialog).toHaveCount(0);
  expect(starts).toEqual([
    { activation_id: "act-1", input: {}, business_key: "order-7" },
  ]);
  const toast = page.locator(".toast");
  await expect(toast).toContainText("Started run 5e6f7a8b.");
  await toast.getByRole("button", { name: "View" }).click();
  await expect(
    page.locator(".record-detail").getByRole("heading", { level: 2 }),
  ).toHaveText("Run 5e6f7a8b");
});

test("Settings: one Platforms section, People and access in its own tab", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const tenant = acmePlatform().workspace!.tenant_id;
  const account = "1a2b3c4d-0000-4000-8000-000000000005";
  const commands: { path: string; body: unknown }[] = [];
  const administrator = (host: PlatformHost) => ({
    ...host.identity(),
    principal_id: "me",
    grants: [
      {
        role: "platform_admin",
        scope: null,
        resources: [],
        capabilities: ["grant.admin"],
      },
      {
        role: "tenant_admin",
        scope: { tenant_id: tenant },
        resources: [],
        capabilities: ["grant.manage", ...allCapabilities],
      },
    ],
  });
  const host = await platformHost(page, {
    platforms: [acmePlatform(), globexPlatform({ signedIn: false })],
    active: "Acme",
    test: (host) => ({
      json: {
        session: host.session(),
        identity: administrator(host),
        workspaces: host.current?.workspaces ?? [],
        truncated: false,
        workspace_revoked: false,
      },
    }),
  });
  await page.route("**/studio/api/api/v1/identity", (r) =>
    r.fulfill({ json: administrator(host) }),
  );
  await page.route("**/studio/api/api/v1/admin/principals?*", (r) =>
    r.fulfill({
      json: {
        items: [{ id: account, kind: "human", active: true }],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/members?*", (r) =>
    r.fulfill({
      json: {
        items: [
          {
            id: "binding-1",
            principal_id: account,
            kind: "human",
            active: true,
            role: "developer",
            scope: { tenant_id: tenant },
            resources: [],
          },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/members", (r) => {
    commands.push({
      path: new URL(r.request().url()).pathname,
      body: r.request().postDataJSON(),
    });
    return r.fulfill({ json: { id: "binding-2" } });
  });
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Current platform" }),
  ).toHaveCount(0);
  await expect(page.locator(".platforms-card")).toContainText(
    "Studio keeps your sign-ins in this computer's credential store. This window never sees your password or tokens.",
  );
  const globex = page.locator(".platform-row").filter({ hasText: "Globex" });
  await expect(globex).toContainText("Not signed in");
  await expect(globex.locator(".state-chip")).toHaveCount(0);
  await expect(
    globex.getByRole("button", { name: "Switch to this platform" }),
  ).toBeVisible();
  await expect(globex.locator("button.danger")).toHaveCount(0);
  await globex.getByRole("button", { name: "More actions for Globex" }).click();
  await expect(
    page.getByRole("menuitem", { name: "Remove Globex" }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  const acme = page.locator(".platform-row").filter({ hasText: "In use" });
  await expect(
    acme.getByRole("button", { name: "Switch workspace" }),
  ).toBeVisible();
  // People and access.
  await page.getByRole("tab", { name: "People and access" }).click();
  await expect(
    page.getByRole("tab", { name: "People and access" }),
  ).toHaveAttribute("aria-selected", "true");
  const table = page.locator(".administration table");
  await expect(table.locator("th")).toHaveText([
    "Account",
    "Type",
    "Roles",
    "Applies to",
    "Actions",
  ]);
  const row = table.locator("tbody tr").first();
  await expect(row).toContainText("Account 1a2b3c4d");
  await expect(row).toContainText("Person");
  await expect(row).toContainText("Workflow editor");
  await expect(row).toContainText("All of Acme");
  await row
    .getByRole("button", { name: "Actions for Account 1a2b3c4d" })
    .click();
  await page.getByRole("menuitem", { name: "Assign role" }).click();
  const panel = page.getByRole("complementary", {
    name: "Assign a role to Account 1a2b3c4d",
  });
  await selectChoice(panel.getByLabel("Role", { exact: true }), "viewer");
  await selectChoice(panel.getByLabel("Applies to"), "project");
  await panel.getByRole("button", { name: "Assign role", exact: true }).click();
  await expect.poll(() => commands.length).toBe(1);
  expect(commands[0].body).toEqual({
    principal_id: account,
    role: "viewer",
    project_id: acmePlatform().workspace!.project_id,
    resources: [],
  });
  await expect(page.locator(".toast")).toContainText(
    "Assigned Viewer to Account 1a2b3c4d.",
  );
  await expect(panel).toHaveCount(0);
  await expect(page.locator(".administration")).not.toContainText("Principal");
});
