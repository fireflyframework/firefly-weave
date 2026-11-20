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
// The platform Clusters' Kubernetes wizard reads, standing in as routes a test
// changes while the wizard is open, and the wizard's parts by their roles.
import { expect, type Locator, type Page } from "@playwright/test";
import { connected } from "./support";

export const scope = {
  tenant_id: "tenant",
  project_id: "project",
  environment_id: "development",
};
export const targetId = "11111111-1111-4111-8111-111111111111";
export const hour = 3_600_000;

export function clusterTarget(overrides: Record<string, unknown> = {}) {
  return {
    id: targetId,
    name: "kind-weave-test",
    adapter: "kubernetes",
    external_identity: "6f1c7e1a-4b55-4a8e-9a7e-0d6c1f0a2b3c",
    boundary: "weave-workers",
    runner_principal_id: "44444444-4444-4444-8444-444444444444",
    capabilities: ["observe", "update", "scale_workers", "restart_workers"],
    approval_policy: { distinct_approver: false },
    scope,
    revision: 1,
    disabled: false,
    created_at: "2026-11-19T11:59:00Z",
    ...overrides,
  };
}
/** A runner whose contact is fresh (online) or long gone (offline). */
export function clusterRunner(online: boolean) {
  const now = Date.now();
  return {
    id: "71111111-7777-4777-8777-777777777777",
    principal_id: "55555555-5555-4555-8555-555555555555",
    target_id: targetId,
    adapter: "kubernetes",
    adapter_version: "2",
    capabilities: ["observe", "update", "scale_workers", "restart_workers"],
    last_seen: new Date(now - (online ? 5_000 : 2 * hour)).toISOString(),
    expires_at: new Date(now + (online ? 24 : -1) * hour).toISOString(),
    revoked: false,
  };
}
export function observation(ageMs: number) {
  return {
    id: "91111111-9999-4999-8999-999999999999",
    target_id: targetId,
    target_revision: 1,
    observed_at: new Date(Date.now() - ageMs).toISOString(),
    expires_at: new Date(Date.now() + hour).toISOString(),
    digest: "sha256:0",
    resources: [],
    complete: true,
    settled: true,
  };
}

/** What the platform answers, which a test changes as the cluster connects. */
export interface Backend {
  targets: Record<string, unknown>[];
  runners: Record<string, unknown>[];
  observations: Record<string, unknown>[];
  /** Answer target reads with 503 while set. */
  unreachable: boolean;
  /** Hold the next target read until the test releases it. */
  hold: Promise<void> | null;
  targetReads: number;
  /** Every request to the platform: method and path with its query. */
  requests: string[];
}

export async function openClusters(
  page: Page,
  backend: Partial<Backend> = {},
  capabilities = ["deployment.read", "deployment.plan", "target.manage"],
): Promise<Backend> {
  const state: Backend = {
    targets: [],
    runners: [],
    observations: [],
    unreachable: false,
    hold: null,
    targetReads: 0,
    requests: [],
    ...backend,
  };
  await connected(page, { capabilities });
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/studio/api/"))
      state.requests.push(`${request.method()} ${url.pathname}${url.search}`);
  });
  const filtered = (items: Record<string, unknown>[], url: string) => {
    const target = new URL(url).searchParams.get("target_id");
    return target
      ? items.filter((item) => item["target_id"] === target)
      : items;
  };
  await page.route("**/deployment-targets?*", async (route) => {
    state.targetReads++;
    const held = state.hold;
    state.hold = null;
    if (held) await held;
    if (state.unreachable)
      return route.fulfill({
        status: 503,
        json: {
          code: "WV-UNAVAILABLE",
          message: "The platform didn't answer in time.",
        },
      });
    return route.fulfill({
      json: { items: state.targets, next_cursor: null },
    });
  });
  await page.route("**/deployment-runners?*", (route) =>
    route.fulfill({
      json: {
        items: filtered(state.runners, route.request().url()),
        next_cursor: null,
      },
    }),
  );
  await page.route("**/deployment-observations?*", (route) =>
    route.fulfill({
      json: {
        items: filtered(state.observations, route.request().url()),
        next_cursor: null,
      },
    }),
  );
  await page.getByRole("button", { name: "Clusters", exact: true }).click();
  return state;
}

export async function openWizard(page: Page) {
  await page.getByRole("button", { name: "Add target" }).click();
  await page.getByRole("button", { name: "Connect a cluster" }).click();
  await expect(
    page.getByRole("heading", { name: "Connect a kind or k3d cluster" }),
  ).toBeFocused();
}

export const status = (page: Page) =>
  page.getByRole("complementary", { name: "What Studio sees" });
export const command = (page: Page, which: "Create" | "Connect") =>
  page.getByRole("group", { name: `${which} command` });
/** Copy reads Copied for two seconds; its name says which command. */
export const copyButton = (page: Page, which: "create" | "connect") =>
  page.getByRole("button", {
    name: new RegExp(`^Cop(y|ied) ${which} command$`),
  });
/** A name in "What Studio sees", by its label. */
export const field = (page: Page, term: string) =>
  status(page).locator(`dt:text-is("${term}") + dd`);
/** Waits until Clusters has finished loading, so its next poll is scheduled. */
export const settled = (page: Page) =>
  expect(page.getByRole("button", { name: "Refresh" })).not.toHaveAttribute(
    "aria-disabled",
    "true",
  );
/** Makes a change to the inputs and waits for Studio's read of targets for them. */
export async function lookedFor(page: Page, change: () => Promise<unknown>) {
  const read = page.waitForResponse((response) =>
    response.url().includes("/deployment-targets?"),
  );
  await change();
  await read;
}
/** The command as the page shows it, one space between words. */
export const shown = async (block: Locator) =>
  (await block.locator("pre").innerText()).replace(/\s+/g, " ").trim();

export const CONNECT =
  "weave platform cluster connect --provider kind --name weave-test --namespace weave-workers";
export const CREATE =
  "weave platform cluster create --provider kind --name weave-test";
