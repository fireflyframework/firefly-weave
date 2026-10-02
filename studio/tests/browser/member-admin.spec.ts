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

test("member administration respects platform versus tenant delegation gates", async ({
  page,
}) => {
  await administration(page, "tenant-admin");
  await expect(
    page.getByRole("heading", { name: "Tenant role bindings" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Principal directory" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Create principal", exact: true }),
  ).toHaveCount(0);
});
for (const mode of ["viewer", "project-admin"] as const)
  test(`${mode} cannot access tenant-wide administration controls`, async ({
    page,
  }) => {
    await administration(page, mode);
    await expect(
      page.getByRole("heading", { name: "People and access" }),
    ).toHaveCount(0);
  });

test("principal and membership forms send exact typed bodies through local CSRF", async ({
  page,
}) => {
  const commands = await administration(page);
  page.on("dialog", (dialog) => dialog.accept());
  await page
    .getByRole("button", { name: "Create principal", exact: true })
    .click();
  await expect.poll(() => commands.length).toBe(1);
  expect(commands[0].body).toEqual({ kind: "human" });
  const link = page.locator(".principal-link-form");
  await link.getByLabel("Principal UUID").fill(target);
  await link.getByLabel("Provider ID").fill("ciam-neutral");
  await link
    .getByLabel("Exact issuer URL")
    .fill("https://ciam.example.invalid/issuer");
  await link.getByLabel("Exact CIAM subject").fill("subject-from-ciam");
  await link
    .getByRole("button", { name: "Link identity", exact: true })
    .click();
  await expect.poll(() => commands.length).toBe(2);
  expect(commands[1].path).toBe(
    `/studio/api/api/v1/admin/principals/${target}/identity-links`,
  );
  expect(commands[1].body).toEqual({
    provider_id: "ciam-neutral",
    issuer: "https://ciam.example.invalid/issuer",
    subject: "subject-from-ciam",
  });
  const grant = page.locator(".member-grant-form");
  await grant.getByLabel("Principal UUID").fill(target);
  await grant.getByLabel("Role", { exact: true }).selectOption("developer");
  await grant.getByLabel("Grant scope").selectOption("environment");
  await grant.getByLabel("Restricted resource IDs").fill("action-a, action-b");
  await grant.getByRole("button", { name: "Grant role", exact: true }).click();
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

test("cancelling status and revoke confirmation sends no access change", async ({
  page,
}) => {
  const commands = await administration(page);
  await page.getByRole("button", { name: "Refresh access directory" }).click();
  await expect(
    page.getByRole("button", { name: "Deactivate", exact: true }),
  ).toBeVisible();
  page.on("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "Deactivate", exact: true }).click();
  await page
    .getByRole("button", { name: "Revoke binding", exact: true })
    .click();
  expect(commands).toHaveLength(0);
});

test("lost principal creation is held for reconciliation and never auto retried", async ({
  page,
}) => {
  await administration(page);
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
    .getByRole("button", { name: "Create principal", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Create principal", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "I reconciled the directory" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Refresh access directory" }).click();
  expect(creates).toBe(1);
  await expect(
    page.getByRole("button", { name: "Create principal", exact: true }),
  ).toBeDisabled();
});
