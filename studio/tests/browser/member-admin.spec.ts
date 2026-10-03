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
import { selectChoice } from "./support";
import { test, expect, Page } from "@playwright/test";
const tenant = "00000000-0000-0000-0000-000000000001";
const project = "00000000-0000-0000-0000-000000000002";
const environment = "00000000-0000-0000-0000-000000000003";
const actor = "00000000-0000-0000-0000-000000000004";
const target = "00000000-0000-0000-0000-000000000005";
const binding = "00000000-0000-0000-0000-000000000006";
type Mode = "viewer" | "project-admin" | "tenant-admin" | "platform-admin";
async function administration(page: Page, mode: Mode = "platform-admin") {
  const commands: {
    path: string;
    body: unknown;
    headers: Record<string, string>;
  }[] = [];
  const scope = {
    tenant_id: tenant,
    project_id: project,
    environment_id: environment,
  };
  const roles =
    mode === "platform-admin"
      ? [
          {
            role: "platform_admin",
            scope: null,
            resources: [],
            capabilities: ["grant.admin"],
          },
        ]
      : [
          {
            role: mode === "viewer" ? "viewer" : "tenant_admin",
            scope: mode === "tenant-admin" ? { tenant_id: tenant } : scope,
            resources: [],
            capabilities:
              mode === "viewer" ? ["status.read"] : ["grant.manage"],
          },
        ];
  await page.route("**/studio/session", (r) =>
    r.fulfill({
      json: {
        paired: true,
        csrfToken: "owned-csrf",
        version: "test",
        mode: "connected",
        profile: {
          name: "Administration test",
          baseUrl: "https://api.example.invalid",
          tenantId: tenant,
          projectId: project,
          environmentId: environment,
        },
      },
    }),
  );
  await page.route("**/studio/api/api/v1/**", async (r) => {
    const request = r.request(),
      path = new URL(request.url()).pathname;
    if (path.endsWith("/identity"))
      return r.fulfill({
        json: {
          principal_id: actor,
          kind: "human",
          grants: roles,
          workspaces: [],
          truncated: false,
        },
      });
    if (request.method() === "GET")
      return r.fulfill({
        json: {
          items: path.endsWith("/principals")
            ? [{ id: target, kind: "human", active: true }]
            : path.endsWith("/members")
              ? [
                  {
                    id: binding,
                    principal_id: target,
                    kind: "human",
                    active: true,
                    role: "viewer",
                    scope: { tenant_id: tenant },
                    resources: [],
                  },
                ]
              : [],
          next_cursor: null,
        },
      });
    const body = request.postData() ? request.postDataJSON() : undefined;
    commands.push({ path, body, headers: request.headers() });
    return r.fulfill({
      json: path.endsWith("/principals")
        ? { id: target, kind: "human", active: true }
        : path.endsWith("/identity-links")
          ? { ...body, principal_id: target }
          : path.endsWith("/status")
            ? { id: target, kind: "human", active: false }
            : path.endsWith("/revoke")
              ? { revoked: true }
              : {
                  id: binding,
                  principal_id: target,
                  kind: "human",
                  active: true,
                  scope,
                  resources: [],
                  ...body,
                },
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  return commands;
}

const people = (page: Page) =>
  page.getByRole("tab", { name: "People and access" }).click();

test("member administration respects platform versus tenant delegation gates", async ({
  page,
}) => {
  await administration(page, "tenant-admin");
  await people(page);
  await expect(
    page.getByRole("heading", { name: "People and access" }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "Accounts" })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Create account", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Advanced: link a sign-in to an account"),
  ).toHaveCount(0);
});
for (const mode of ["viewer", "project-admin"] as const)
  test(`${mode} cannot access tenant-wide administration controls`, async ({
    page,
  }) => {
    await administration(page, mode);
    await expect(
      page.getByRole("tab", { name: "People and access" }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("heading", { name: "People and access" }),
    ).toHaveCount(0);
  });

test("People and access reads by account and role; tables share one look", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await administration(page);
  await people(page);
  const table = page.locator(".administration table").first();
  await expect(table.locator("tbody tr")).toHaveCount(1);
  const row = table.locator("tbody tr").first();
  await expect(row).toContainText("Account 00000000");
  await expect(row).toContainText("Person");
  await expect(row).toContainText("Viewer");
  await expect(row).not.toContainText(target);
  await expect(row).not.toContainText("{");
  // Table heads are 12px captions on the sunken surface; rows are 56px.
  const head = await table
    .locator("th")
    .first()
    .evaluate((e) => {
      const s = getComputedStyle(e);
      return [s.fontSize, s.backgroundColor, s.color];
    });
  expect(head).toEqual(["12px", "rgb(248, 250, 248)", "rgb(77, 101, 93)"]);
  const cell = await table.locator("td").first().boundingBox();
  expect(cell!.height).toBeGreaterThanOrEqual(56);
  // Assign role opens a side panel; its fields stack with their labels.
  await page
    .locator(".administration .card-heading")
    .getByRole("button", { name: "Assign role", exact: true })
    .click();
  const panel = page.getByRole("complementary", { name: "Assign a role" });
  await expect(panel.getByLabel("Account ID")).toBeFocused();
  const lefts = await panel
    .locator("label")
    .evaluateAll((labels) =>
      labels.map((l) => Math.round(l.getBoundingClientRect().left)),
    );
  expect(new Set(lefts).size).toBe(1);
});

test("account and role forms send exact typed bodies through local CSRF", async ({
  page,
}) => {
  const commands = await administration(page);
  await people(page);
  await page
    .getByRole("button", { name: "Create account", exact: true })
    .click();
  await expect.poll(() => commands.length).toBe(1);
  expect(commands[0].body).toEqual({ kind: "human" });
  await expect(page.locator(".toast")).toContainText(
    "Created Account 00000000.",
  );
  await page.getByText("Advanced: link a sign-in to an account").click();
  const link = page.locator(".principal-link-form");
  await link.getByLabel("Account ID").fill(target);
  await link.getByLabel("Identity provider ID").fill("ciam-neutral");
  await link
    .getByLabel("Issuer URL (exact)")
    .fill("https://ciam.example.invalid/issuer");
  await link.getByLabel("Subject (exact)").fill("subject-from-ciam");
  await link.getByRole("button", { name: "Link sign-in", exact: true }).click();
  await expect.poll(() => commands.length).toBe(2);
  expect(commands[1].path).toBe(
    `/studio/api/api/v1/admin/principals/${target}/identity-links`,
  );
  expect(commands[1].body).toEqual({
    provider_id: "ciam-neutral",
    issuer: "https://ciam.example.invalid/issuer",
    subject: "subject-from-ciam",
  });
  await page
    .locator(".administration .card-heading")
    .getByRole("button", { name: "Assign role", exact: true })
    .click();
  const grant = page.locator(".member-grant-form");
  await grant.getByLabel("Account ID").fill(target);
  await selectChoice(grant.getByLabel("Role", { exact: true }), "developer");
  await selectChoice(grant.getByLabel("Applies to"), "environment");
  await grant.getByLabel("Limit to resources").fill("action-a, action-b");
  await grant.getByRole("button", { name: "Assign role", exact: true }).click();
  await expect.poll(() => commands.length).toBe(3);
  expect(commands[2].body).toEqual({
    principal_id: target,
    role: "developer",
    project_id: project,
    environment_id: environment,
    resources: ["action-a", "action-b"],
  });
  expect(
    commands.every(
      (c) =>
        c.headers["x-weave-csrf"] === "owned-csrf" &&
        !c.headers["authorization"],
    ),
  ).toBe(true);
});

test("cancelling deactivate and remove confirmations sends no access change", async ({
  page,
}) => {
  const commands = await administration(page);
  await people(page);
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  const actions = page.getByRole("button", {
    name: "Actions for Account 00000000",
  });
  // Confirmation is an in-app dialog; Cancel must send nothing.
  const dialog = page.getByRole("dialog");
  await actions.click();
  await page.getByRole("menuitem", { name: "Deactivate" }).click();
  await expect(dialog).toContainText("Deactivate Account 00000000?");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await actions.click();
  await page.getByRole("menuitem", { name: /^Remove Viewer/ }).click();
  await expect(dialog).toContainText("Remove this role?");
  await expect(dialog).toContainText("Account 00000000 loses the Viewer role");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  expect(commands).toHaveLength(0);
});

test("lost account creation is held for checking and never retried by itself", async ({
  page,
}) => {
  await administration(page);
  await people(page);
  let creates = 0;
  await page.route("**/api/v1/admin/principals", async (r) => {
    if (r.request().method() === "POST") {
      creates++;
      return r.abort("failed");
    }
    return r.fulfill({
      json: {
        items: [{ id: target, kind: "human", active: true }],
        next_cursor: null,
      },
    });
  });
  await page
    .getByRole("button", { name: "Create account", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Create account", exact: true }),
  ).toBeDisabled();
  await expect(page.getByRole("alert")).toContainText(
    "Studio couldn't confirm the account was created. Check the list before creating it again.",
  );
  await expect(
    page.getByRole("button", { name: "I checked the list" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  expect(creates).toBe(1);
  await expect(
    page.getByRole("button", { name: "Create account", exact: true }),
  ).toBeDisabled();
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
]) {
  test(`file and Lumi roles can be granted independently at ${viewport.width}`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const commands = await administration(page);
    await people(page);
    for (const role of [
      "file_reader",
      "file_manager",
      "lumi_user",
      "lumi_manager",
    ]) {
      await page
        .locator(".administration .card-heading")
        .getByRole("button", { name: "Assign role", exact: true })
        .click();
      const grant = page.locator(".member-grant-form");
      await grant.getByLabel("Account ID").fill(target);
      await selectChoice(grant.getByLabel("Role", { exact: true }), role);
      await selectChoice(grant.getByLabel("Applies to"), "environment");
      await grant
        .getByRole("button", { name: "Assign role", exact: true })
        .click();
      await expect
        .poll(() => commands.at(-1)?.body)
        .toEqual({
          principal_id: target,
          role,
          project_id: project,
          environment_id: environment,
          resources: [],
        });
      // A pointer user can dismiss feedback before the next form at short heights.
      await page
        .getByRole("button", { name: "Dismiss notification", exact: true })
        .click();
      await expect(page.locator(".toast")).not.toBeVisible();
    }
    expect(commands).toHaveLength(4);
  });
}
