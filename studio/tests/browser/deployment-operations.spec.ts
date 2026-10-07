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
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { test, expect } from "@playwright/test";
import { connected, offline, selectChoice } from "./support";

const schema = JSON.parse(
  execFileSync(
    ".venv/bin/python",
    [
      "-c",
      "import json; from firefly_weave.contracts.deployments import DeploymentRequest; print(json.dumps(DeploymentRequest.model_json_schema()))",
    ],
    { cwd: "..", encoding: "utf8" },
  ),
);
const targetUpdateSchema = JSON.parse(
  execFileSync(
    ".venv/bin/python",
    [
      "-c",
      "import json; from firefly_weave.contracts.deployments import TargetUpdate; print(json.dumps(TargetUpdate.model_json_schema()))",
    ],
    { cwd: "..", encoding: "utf8" },
  ),
);
const targetId = "11111111-1111-4111-8111-111111111111";
const deploymentId = "22222222-2222-4222-8222-222222222222";
const scope = {
  tenant_id: "tenant",
  project_id: "project",
  environment_id: "development",
};
const component = {
  name: "api",
  kind: "api",
  image: "example/api@sha256:" + "a".repeat(64),
  configuration: "api-config",
  replicas: 1,
  cpu_millis: 500,
  memory_mib: 1024,
  worker_release_id: null,
};
const target = {
  id: targetId,
  name: "owned-target",
  adapter: "docker-compose",
  external_identity: "owned-docker",
  boundary: "weave-owned",
  runner_principal_id: "44444444-4444-4444-8444-444444444444",
  capabilities: ["observe", "deploy", "update"],
  scope,
  revision: 1,
  disabled: false,
  created_at: "2026-10-03T10:00:00Z",
};
const deployment = {
  id: deploymentId,
  target_id: targetId,
  name: "owned-api",
  ownership: "imported",
  components: [component],
  scope,
  revision: 3,
  created_at: "2026-10-03T10:00:00Z",
};
const future = () => new Date(Date.now() + 600000).toISOString();
const observation = () => ({
  id: "55555555-5555-4555-8555-555555555555",
  target_id: targetId,
  target_revision: 1,
  observed_at: new Date().toISOString(),
  expires_at: future(),
  digest: "b".repeat(64),
  resources: [],
  complete: true,
  settled: true,
});
for (const width of [1440, 390])
  test.describe(`Operations at ${width}`, () => {
    test.use({ viewport: { width, height: 900 } });
    test("local mode explains the platform boundary without fake health", async ({
      page,
    }) => {
      await offline(page);
      await page
        .getByRole("button", { name: "Operations", exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: "Operations", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", {
          name: "Connect to manage deployment targets",
          exact: true,
        }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Import target", exact: true }),
      ).toHaveCount(0);
      await page
        .getByRole("button", { name: "Connect to a platform", exact: true })
        .click();
      await expect(page.locator("weave-connection-wizard")).toBeVisible();
    });
    test("registration reviews authority and observation remains a queued job", async ({
      page,
    }, testInfo) => {
      await connected(page, {
        capabilities: ["deployment.read", "target.manage", "deployment.plan"],
      });
      const targetId = "11111111-1111-4111-8111-111111111111";
      let request: any;
      const scope = {
        tenant_id: "tenant",
        project_id: "project",
        environment_id: "development",
      };
      await page.route("**/deployment-targets", async (route) => {
        request = route.request().postDataJSON();
        await route.fulfill({
          json: {
            ...request,
            id: targetId,
            scope,
            revision: 1,
            disabled: false,
            created_at: "2026-10-03T10:00:00Z",
          },
        });
      });
      await page.route("**/deployment-jobs/*", (route) =>
        route.fulfill({
          json: {
            id: "22222222-2222-4222-8222-222222222222",
            scope,
            target_id: targetId,
            target_revision: 1,
            kind: "observe",
            plan_id: null,
            plan_digest: null,
            state: "queued",
            revision: 1,
            operation_key: "33333333-3333-4333-8333-333333333333",
            created_at: "2026-10-03T10:00:00Z",
            deadline: "2026-10-03T10:05:00Z",
            receipt: null,
            observation_id: null,
          },
        }),
      );
      await page.route("**/deployment-observations", async (route) => {
        expect(route.request().postDataJSON()).toEqual({ target_id: targetId });
        await route.fulfill({
          json: {
            id: "22222222-2222-4222-8222-222222222222",
            scope,
            target_id: targetId,
            target_revision: 1,
            kind: "observe",
            plan_id: null,
            plan_digest: null,
            state: "queued",
            revision: 1,
            operation_key: "33333333-3333-4333-8333-333333333333",
            created_at: "2026-10-03T10:00:00Z",
            deadline: "2026-10-03T10:05:00Z",
            receipt: null,
            observation_id: null,
          },
        });
      });
      await page
        .getByRole("button", { name: "Operations", exact: true })
        .click();
      await page
        .getByRole("button", { name: "Register target", exact: true })
        .click();
      const wizard = page.locator("weave-target-wizard");
      await wizard
        .getByLabel("Target name", { exact: true })
        .fill("local-team");
      await wizard
        .getByLabel("External identity", { exact: true })
        .fill("owned-docker");
      await wizard
        .getByLabel("Allowed boundary", { exact: true })
        .fill("weave-owned");
      await wizard
        .getByRole("button", { name: "Continue to authority", exact: true })
        .click();
      await expect(
        wizard.getByRole("heading", {
          name: "Bind a dedicated outbound runner",
          exact: true,
        }),
      ).toBeFocused();
      await wizard
        .getByText("Use an administrator-provided application ID", {
          exact: true,
        })
        .click();
      await wizard
        .getByLabel("Runner principal ID", { exact: true })
        .fill("44444444-4444-4444-8444-444444444444");
      await wizard
        .getByRole("button", { name: "Review target", exact: true })
        .click();
      expect(request).toBeUndefined();
      await page.screenshot({
        path: testInfo.outputPath("target-review.png"),
        fullPage: true,
      });
      await wizard
        .getByRole("button", { name: "Register target", exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: "local-team", exact: true }),
      ).toBeVisible();
      expect(request).toEqual({
        name: "local-team",
        adapter: "docker-compose",
        external_identity: "owned-docker",
        boundary: "weave-owned",
        runner_principal_id: "44444444-4444-4444-8444-444444444444",
        capabilities: ["observe"],
      });
      await expect(
        page.getByText(
          "No observation yet. Request one after installing the authorized runner.",
          { exact: true },
        ),
      ).toBeVisible();
      await page
        .getByRole("button", { name: "Observe target", exact: true })
        .click();
      await expect(
        page.locator("weave-operations-view").getByRole("status"),
      ).toHaveText("queued");
      await expect(
        page.getByRole("heading", { name: "Operation receipt", exact: true }),
      ).toHaveCount(0);
    });
    test("desired edits stay local until reviewed and planning pins the selected revision", async ({
      page,
    }, testInfo) => {
      await connected(page, {
        capabilities: ["deployment.read", "target.manage", "deployment.plan"],
      });
      await page.route("**/studio/contracts/deployment", (route) =>
        route.fulfill({ json: schema }),
      );
      await page.route("**/deployment-targets?*", (route) =>
        route.fulfill({ json: { items: [target], next_cursor: null } }),
      );
      await page.route("**/deployment-targets/" + targetId, (route) =>
        route.fulfill({ json: target }),
      );
      await page.route("**/deployments?*", (route) =>
        route.fulfill({ json: { items: [deployment], next_cursor: null } }),
      );
      const observed = observation();
      await page.route("**/deployment-observations?*", (route) =>
        route.fulfill({ json: { items: [observed], next_cursor: null } }),
      );
      let saved: any;
      await page.route("**/deployments/" + deploymentId, async (route) => {
        expect(route.request().method()).toBe("PUT");
        expect(route.request().headers()["if-match"]).toBe('"3"');
        saved = route.request().postDataJSON();
        await route.fulfill({ json: { ...deployment, ...saved, revision: 4 } });
      });
      await page.route("**/deployment-plans/*/approval", (route) =>
        route.fulfill({ json: null }),
      );
      let planRequest: any;
      await page.route("**/deployment-plans", async (route) => {
        planRequest = route.request().postDataJSON();
        await route.fulfill({
          json: {
            id: "66666666-6666-4666-8666-666666666666",
            scope,
            target_id: targetId,
            target_revision: 1,
            deployment_id: deploymentId,
            deployment_revision: 4,
            adapter: "docker-compose",
            adapter_version: "1",
            intent: "update",
            observation_id: observed.id,
            observation_digest: observed.digest,
            steps: [{ action: "update", component, expected_version: "v1" }],
            risks: ["external_effects", "adoption"],
            created_at: new Date().toISOString(),
            expires_at: future(),
            digest: "c".repeat(64),
          },
        });
      });
      await page
        .getByRole("button", { name: "Operations", exact: true })
        .click();
      await page
        .getByRole("button", { name: "owned-api", exact: true })
        .click();
      await page
        .getByRole("button", { name: "Edit desired deployment", exact: true })
        .click();
      const editor = page.locator("weave-deployment-editor");
      await editor
        .getByRole("button", { name: "Continue to components", exact: true })
        .click();
      await editor
        .getByRole("button", { name: "Review desired state", exact: true })
        .click();
      expect(saved).toBeUndefined();
      await editor
        .getByRole("button", { name: "Save desired deployment", exact: true })
        .click();
      await expect(
        page.getByText("Revision 4 · imported", { exact: true }),
      ).toBeVisible();
      expect(saved).toEqual({
        target_id: targetId,
        name: "owned-api",
        ownership: "imported",
        components: [component],
      });
      await page
        .getByRole("button", { name: "Create plan", exact: true })
        .click();
      const planner = page.locator("weave-deployment-plan-builder");
      await selectChoice(
        planner.getByRole("combobox", { name: "Operation", exact: true }),
        "Update deployment",
      );
      await selectChoice(
        planner.getByRole("combobox", { name: "Observation", exact: true }),
        { value: observed.id },
      );
      await planner
        .getByRole("button", { name: "Generate reviewable plan", exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: "Review update", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("heading", { name: "Review update", exact: true }),
      ).toBeFocused();
      await page.screenshot({
        path: testInfo.outputPath("plan-review.png"),
        fullPage: true,
      });
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      expect(planRequest).toEqual({
        deployment_id: deploymentId,
        deployment_revision: 4,
        observation_id: observed.id,
        intent: "update",
        ttl_seconds: 300,
      });
      await expect(
        page.getByText("Not available", { exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Apply approved plan", exact: true }),
      ).toHaveCount(0);
    });
    test("planning refuses incomplete expired and different-revision observations", async ({
      page,
    }) => {
      await connected(page, {
        capabilities: ["deployment.read", "deployment.plan"],
      });
      await page.route("**/deployment-targets/" + targetId, (route) =>
        route.fulfill({ json: target }),
      );
      await page.route("**/deployments?*", (route) =>
        route.fulfill({ json: { items: [deployment], next_cursor: null } }),
      );
      const item = observation();
      await page.route("**/deployment-observations?*", (route) =>
        route.fulfill({
          json: {
            items: [
              { ...item, complete: false },
              {
                ...item,
                id: "99999999-9999-4999-8999-999999999999",
                expires_at: "2020-01-01T00:00:00Z",
              },
              {
                ...item,
                id: "88888888-8888-4888-8888-888888888888",
                target_revision: 2,
              },
            ],
            next_cursor: null,
          },
        }),
      );
      let plans = 0;
      await page.route("**/deployment-plans", (route) => {
        plans++;
        return route.fulfill({ status: 500, json: {} });
      });
      await page
        .getByRole("button", { name: "Operations", exact: true })
        .click();
      await page
        .getByRole("button", { name: "owned-api", exact: true })
        .click();
      await page
        .getByRole("button", { name: "Create plan", exact: true })
        .click();
      await expect(
        page.getByText(
          "No complete, current observation is available. Observe the target before planning.",
          { exact: true },
        ),
      ).toBeVisible();
      await expect(
        page.getByRole("button", {
          name: "Generate reviewable plan",
          exact: true,
        }),
      ).toBeDisabled();
      expect(plans).toBe(0);
    });
    test("new desired deployment uses canonical component fields without provisioning", async ({
      page,
    }) => {
      await connected(page, {
        capabilities: ["deployment.read", "target.manage"],
      });
      await page.route("**/studio/contracts/deployment", (route) =>
        route.fulfill({ json: schema }),
      );
      await page.route("**/deployment-targets?*", (route) =>
        route.fulfill({ json: { items: [target], next_cursor: null } }),
      );
      await page.route("**/deployment-targets/" + targetId, (route) =>
        route.fulfill({ json: target }),
      );
      let saved: any;
      await page.route("**/deployments", async (route) => {
        saved = route.request().postDataJSON();
        await route.fulfill({ json: { ...deployment, ...saved, revision: 1 } });
      });
      await page
        .getByRole("button", { name: "Operations", exact: true })
        .click();
      await page
        .getByRole("button", { name: "owned-target", exact: true })
        .click();
      await page
        .getByRole("button", { name: "Record desired deployment", exact: true })
        .click();
      const editor = page.locator("weave-deployment-editor");
      await editor
        .getByRole("textbox", { name: "Name", exact: true })
        .fill("fresh-api");
      await editor
        .getByRole("button", { name: "Continue to components", exact: true })
        .click();
      await editor
        .getByRole("button", { name: "Add Components item", exact: true })
        .click();
      const row = editor.getByRole("group", {
        name: "Components item 1",
        exact: true,
      });
      await row.getByRole("textbox", { name: "Name", exact: true }).fill("api");
      await selectChoice(
        row.getByRole("combobox", { name: "Kind", exact: true }),
        "api",
      );
      await row
        .getByRole("textbox", { name: "Image", exact: true })
        .fill(component.image);
      await row
        .getByRole("textbox", { name: "Configuration", exact: true })
        .fill("api-config");
      await editor
        .getByRole("button", { name: "Review desired state", exact: true })
        .click();
      expect(saved).toBeUndefined();
      await editor
        .getByRole("button", { name: "Save desired deployment", exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: "fresh-api", exact: true }),
      ).toBeVisible();
      expect(saved.target_id).toBe(targetId);
      expect(saved.ownership).toBe("imported");
      expect(saved.components[0]).toMatchObject({
        name: "api",
        kind: "api",
        image: component.image,
        configuration: "api-config",
        replicas: 1,
      });
      await expect(
        page.getByRole("button", { name: "Apply approved plan", exact: true }),
      ).toHaveCount(0);
    });
    test("expired plan cannot be approved or applied through a direct link", async ({
      page,
    }) => {
      await connected(page, {
        capabilities: [
          "deployment.read",
          "deployment.approve",
          "deployment.apply",
        ],
      });
      const id = "66666666-6666-4666-8666-666666666666";
      const plan = {
        id,
        scope,
        target_id: targetId,
        target_revision: 1,
        deployment_id: deploymentId,
        deployment_revision: 3,
        adapter: "docker-compose",
        adapter_version: "1",
        intent: "update",
        observation_id: observation().id,
        observation_digest: "b".repeat(64),
        steps: [{ action: "update", component, expected_version: "v1" }],
        risks: ["external_effects"],
        created_at: "2020-01-01T00:00:00Z",
        expires_at: "2020-01-01T00:05:00Z",
        digest: "c".repeat(64),
      };
      await page.route("**/deployment-plans/" + id, (route) =>
        route.fulfill({ json: plan }),
      );
      await page.route("**/deployment-plans/" + id + "/approval", (route) =>
        route.fulfill({
          json: {
            plan_id: id,
            digest: plan.digest,
            principal_id: "human",
            approved_at: plan.created_at,
          },
        }),
      );
      await page.goto("/operations/plans/" + id);
      await expect(
        page.getByRole("heading", { name: "Review update", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", {
          name: "Approve reviewed plan",
          exact: true,
        }),
      ).toBeDisabled();
      await expect(
        page.getByRole("button", { name: "Apply approved plan", exact: true }),
      ).toBeDisabled();
      await expect(
        page.getByText("Approval recorded for " + plan.digest, { exact: true }),
      ).toBeVisible();
    });
    test("ambiguous apply needs fresh settled evidence and explicit acknowledgement", async ({
      page,
    }) => {
      await connected(page, {
        capabilities: [
          "deployment.read",
          "deployment.apply",
          "deployment.approve",
        ],
      });
      const observed = observation();
      const job = {
        id: "77777777-7777-4777-8777-777777777777",
        scope,
        target_id: targetId,
        target_revision: 1,
        kind: "apply",
        plan_id: "66666666-6666-4666-8666-666666666666",
        plan_digest: "c".repeat(64),
        state: "reconciliation_required",
        revision: 5,
        operation_key: "88888888-8888-4888-8888-888888888888",
        created_at: new Date(Date.now() - 60000).toISOString(),
        deadline: future(),
        receipt: {
          code: "ambiguous",
          changed_resources: [],
          external_effects_may_continue: true,
        },
        observation_id: null,
        reconciliation_started_at: new Date(Date.now() - 30000).toISOString(),
      };
      await page.route("**/deployment-jobs?*", (route) =>
        route.fulfill({ json: { items: [job], next_cursor: null } }),
      );
      await page.route("**/deployment-observations?*", (route) =>
        route.fulfill({
          json: {
            items: [
              {
                ...observed,
                id: "99999999-9999-4999-8999-999999999999",
                settled: false,
              },
              observed,
            ],
            next_cursor: null,
          },
        }),
      );
      await page.route("**/deployment-jobs/" + job.id, (route) =>
        route.fulfill({ json: job }),
      );
      await page.route("**/deployment-targets/" + targetId, (route) =>
        route.fulfill({ json: target }),
      );
      let command: any;
      await page.route(
        "**/deployment-jobs/" + job.id + "/reconcile",
        async (route) => {
          command = route.request().postDataJSON();
          await route.fulfill({
            json: {
              ...job,
              state: "failed",
              revision: 6,
              receipt: {
                code: "reconciled",
                changed_resources: [],
                external_effects_may_continue: false,
              },
            },
          });
        },
      );
      await page
        .getByRole("button", { name: "Operations", exact: true })
        .click();
      await page
        .getByRole("button", { name: "apply · " + job.created_at, exact: true })
        .click();
      const reconcile = page.locator("weave-deployment-reconciliation");
      await expect(
        reconcile.getByRole("button", {
          name: "Record reconciliation",
          exact: true,
        }),
      ).toBeDisabled();
      await selectChoice(
        reconcile.getByRole("combobox", {
          name: "Settled observation",
          exact: true,
        }),
        { value: observed.id },
      );
      await expect(
        reconcile.getByRole("button", {
          name: "Record reconciliation",
          exact: true,
        }),
      ).toBeDisabled();
      await reconcile
        .getByLabel("I verified the external operation has stopped", {
          exact: true,
        })
        .check();
      await reconcile
        .getByRole("button", { name: "Record reconciliation", exact: true })
        .click();
      expect(command).toEqual({
        plan_digest: job.plan_digest,
        observation_id: observed.id,
        external_operation_stopped: true,
      });
      await expect(
        page.locator("weave-operations-view").getByRole("status"),
      ).toHaveText("failed");
      await expect(page.getByText("reconciled", { exact: true })).toBeVisible();
    });
    test("target authority edits are reviewed and disabling uses the exact revision", async ({
      page,
    }) => {
      await connected(page, {
        capabilities: ["deployment.read", "target.manage", "deployment.plan"],
      });
      await page.route(
        "**/studio/contracts/deployment-target-update",
        (route) => route.fulfill({ json: targetUpdateSchema }),
      );
      await page.route("**/deployment-targets?*", (route) =>
        route.fulfill({ json: { items: [target], next_cursor: null } }),
      );
      let command: any;
      await page.route("**/deployment-targets/" + targetId, async (route) => {
        expect(route.request().method()).toBe("PUT");
        expect(route.request().headers()["if-match"]).toBe('"1"');
        command = route.request().postDataJSON();
        await route.fulfill({ json: { ...target, ...command, revision: 2 } });
      });
      await page
        .getByRole("button", { name: "Operations", exact: true })
        .click();
      await page
        .getByRole("button", { name: "owned-target", exact: true })
        .click();
      await page
        .getByRole("button", { name: "Edit target authority", exact: true })
        .click();
      const wizard = page.locator("weave-target-wizard");
      await expect(
        wizard.getByRole("textbox", { name: "Target name", exact: true }),
      ).toHaveCount(0);
      await wizard
        .getByRole("button", { name: "Continue to authority", exact: true })
        .click();
      await wizard.getByLabel("Disabled", { exact: true }).check();
      await wizard
        .getByRole("button", { name: "Review target", exact: true })
        .click();
      expect(command).toBeUndefined();
      await expect(
        wizard.getByText("Enabled → Disabled", { exact: true }),
      ).toBeVisible();
      await expect(
        wizard.getByText(/invalidates current runner leases/),
      ).toBeVisible();
      await wizard
        .getByRole("button", { name: "Save target authority", exact: true })
        .click();
      expect(command).toEqual({
        runner_principal_id: target.runner_principal_id,
        capabilities: target.capabilities,
        disabled: true,
      });
      await expect(
        page.getByText("This target is disabled. No new operation can start.", {
          exact: true,
        }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Observe target", exact: true }),
      ).toBeDisabled();
    });
    test("target revision conflict requires reload and a new review", async ({
      page,
    }) => {
      await connected(page, {
        capabilities: ["deployment.read", "target.manage"],
      });
      await page.route(
        "**/studio/contracts/deployment-target-update",
        (route) => route.fulfill({ json: targetUpdateSchema }),
      );
      await page.route("**/deployment-targets?*", (route) =>
        route.fulfill({ json: { items: [target], next_cursor: null } }),
      );
      const revisions: string[] = [];
      let reloads = 0;
      await page.route("**/deployment-targets/" + targetId, async (route) => {
        if (route.request().method() === "GET") {
          reloads++;
          if (reloads === 1) {
            await route.fulfill({
              status: 503,
              json: { message: "Fixture reload unavailable" },
            });
            return;
          }
          await route.fulfill({ json: { ...target, revision: 5 } });
          return;
        }
        revisions.push(route.request().headers()["if-match"]);
        if (revisions.length === 1) {
          await route.fulfill({
            status: 409,
            json: { code: "WV-REVISION", message: "Deployment target changed" },
          });
          return;
        }
        await route.fulfill({
          json: { ...target, ...route.request().postDataJSON(), revision: 6 },
        });
      });
      await page
        .getByRole("button", { name: "Operations", exact: true })
        .click();
      await page
        .getByRole("button", { name: "owned-target", exact: true })
        .click();
      const review = async () => {
        await page
          .getByRole("button", { name: "Edit target authority", exact: true })
          .click();
        const wizard = page.locator("weave-target-wizard");
        await wizard
          .getByRole("button", { name: "Continue to authority", exact: true })
          .click();
        await wizard.getByLabel("Disabled", { exact: true }).check();
        await wizard
          .getByRole("button", { name: "Review target", exact: true })
          .click();
        await wizard
          .getByRole("button", { name: "Save target authority", exact: true })
          .click();
      };
      await review();
      await expect(
        page.getByRole("button", {
          name: "Save target authority",
          exact: true,
        }),
      ).toBeDisabled();
      expect(revisions).toEqual(['"1"']);
      await page
        .getByRole("button", {
          name: "Reload current target and discard my draft",
          exact: true,
        })
        .click();
      await expect(
        page.getByText("Fixture reload unavailable", { exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", {
          name: "Save target authority",
          exact: true,
        }),
      ).toBeDisabled();
      await page
        .getByRole("button", {
          name: "Reload current target and discard my draft",
          exact: true,
        })
        .click();
      await review();
      await expect(
        page.getByText("This target is disabled. No new operation can start.", {
          exact: true,
        }),
      ).toBeVisible();
      expect(revisions).toEqual(['"1"', '"5"']);
    });
    test("manual retry recovers the original apply job after its response is lost", async ({
      page,
    }) => {
      await page.clock.install();
      await connected(page, {
        capabilities: ["deployment.read", "deployment.apply"],
      });
      const id = "66666666-6666-4666-8666-666666666666";
      const plan = {
        id,
        scope,
        target_id: targetId,
        target_revision: 1,
        deployment_id: deploymentId,
        deployment_revision: 3,
        adapter: "docker-compose",
        adapter_version: "1",
        intent: "update",
        observation_id: observation().id,
        observation_digest: "b".repeat(64),
        steps: [{ action: "update", component, expected_version: "v1" }],
        risks: ["external_effects"],
        created_at: new Date().toISOString(),
        expires_at: future(),
        digest: "c".repeat(64),
      };
      await page.route("**/deployment-plans/" + id, (route) =>
        route.fulfill({ json: plan }),
      );
      await page.route("**/deployment-plans/" + id + "/approval", (route) =>
        route.fulfill({
          json: {
            plan_id: id,
            digest: plan.digest,
            principal_id: "human",
            approved_at: plan.created_at,
          },
        }),
      );
      const job = {
        id: "77777777-7777-4777-8777-777777777777",
        scope,
        target_id: targetId,
        target_revision: 1,
        kind: "apply",
        plan_id: id,
        plan_digest: plan.digest,
        state: "queued",
        revision: 1,
        operation_key: "88888888-8888-4888-8888-888888888888",
        created_at: new Date().toISOString(),
        deadline: future(),
        receipt: null,
        observation_id: null,
        reconciliation_started_at: null,
      };
      const keys: string[] = [];
      let created = 0;
      await page.route(
        "**/deployment-plans/" + id + "/apply",
        async (route) => {
          const key = route.request().headers()["idempotency-key"];
          if (!keys.includes(key)) created++;
          keys.push(key);
          if (keys.length === 1) {
            await route.abort("connectionreset");
            return;
          }
          await route.fulfill({ json: job });
        },
      );
      await page.route("**/deployment-jobs/" + job.id, (route) =>
        route.fulfill({ json: job }),
      );
      await page.goto("/operations/plans/" + id);
      await page
        .getByRole("button", { name: "Apply approved plan", exact: true })
        .click();
      await expect(
        page.getByText(
          /The response was lost; the command may have been accepted/,
        ),
      ).toBeVisible();
      expect(keys).toHaveLength(1);
      await page.clock.fastForward(610000);
      await page
        .getByRole("button", { name: "Recover apply result", exact: true })
        .click();
      await expect(
        page.locator("weave-operations-view").getByRole("status"),
      ).toHaveText("queued");
      expect(keys).toHaveLength(2);
      expect(keys[1]).toBe(keys[0]);
      expect(created).toBe(1);
      await expect(page).toHaveURL(
        new RegExp("/operations/jobs/" + job.id + "$"),
      );
    });
    test("browser Back restores the full authorized inventory after a target filter", async ({
      page,
    }) => {
      await connected(page, { capabilities: ["deployment.read"] });
      await page.route("**/deployment-targets?*", (route) =>
        route.fulfill({ json: { items: [target], next_cursor: null } }),
      );
      await page.route("**/deployments?*", (route) =>
        route.fulfill({
          json: {
            items: route.request().url().includes("target_id=")
              ? [deployment]
              : [
                  deployment,
                  {
                    ...deployment,
                    id: "99999999-9999-4999-8999-999999999999",
                    name: "other-authorized",
                  },
                ],
            next_cursor: null,
          },
        }),
      );
      await page
        .getByRole("button", { name: "Operations", exact: true })
        .click();
      await expect(
        page.getByRole("button", { name: "other-authorized", exact: true }),
      ).toBeVisible();
      await page
        .getByRole("button", { name: "owned-target", exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: "owned-target", exact: true }),
      ).toBeVisible();
      await page.goBack();
      await expect(
        page.getByRole("heading", { name: "Deployment targets", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "other-authorized", exact: true }),
      ).toBeVisible();
    });
    test("a reader cannot start registration or observation", async ({
      page,
    }) => {
      await connected(page, { capabilities: ["deployment.read"] });
      await page
        .getByRole("button", { name: "Operations", exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: "Deployment targets", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Register target", exact: true }),
      ).toHaveCount(0);
    });
    test("worker registration does not claim the process is healthy", async ({
      page,
    }) => {
      await connected(page);
      await page.route("**/environments/development/workers?*", (route) =>
        route.fulfill({
          json: {
            items: [
              {
                id: "11111111-1111-4111-8111-111111111111",
                principal_id: "22222222-2222-4222-8222-222222222222",
                release_id: "33333333-3333-4333-8333-333333333333",
                task_types: ["test@1.0.0"],
                capacity: 8,
                revoked: false,
              },
            ],
            next_cursor: null,
          },
        }),
      );
      await page.getByRole("button", { name: "Workers", exact: true }).click();
      await expect(
        page.getByRole("table", { name: "Workers", exact: true }),
      ).toContainText("Registered");
      await expect(
        page.getByRole("table", { name: "Workers", exact: true }),
      ).not.toContainText("Active");
    });
  });

for (const width of [1440, 390])
  test(`existing target onboarding at ${width} selects applications, limits adapter actions and supplies local setup`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await connected(page, {
      capabilities: [
        "deployment.read",
        "target.manage",
        "deployment.plan",
        "grant.admin",
      ],
    });
    await page.route("**/admin/principals?*", (route) =>
      route.fulfill({
        json: {
          items: [
            {
              id: target.runner_principal_id,
              kind: "application",
              active: true,
              display_name: "Preproduction runner",
            },
            {
              id: "human",
              kind: "human",
              active: true,
              display_name: "Person",
            },
          ],
          next_cursor: null,
        },
      }),
    );
    await page.route("**/deployment-targets", async (route) =>
      route.fulfill({ json: { ...target, ...route.request().postDataJSON() } }),
    );
    await page.getByRole("button", { name: "Operations", exact: true }).click();
    await page
      .getByRole("button", { name: "Register target", exact: true })
      .click();
    const wizard = page.locator("weave-target-wizard");
    await wizard
      .getByLabel("Target name", { exact: true })
      .fill("existing-cluster");
    await selectChoice(
      wizard.getByRole("combobox", { name: "Container runtime" }),
      "kubernetes",
    );
    await wizard
      .getByLabel("External identity", { exact: true })
      .fill("namespace-uid");
    await wizard.getByLabel("Allowed boundary", { exact: true }).fill("weave");
    await wizard
      .getByRole("button", { name: "Continue to authority", exact: true })
      .click();
    await selectChoice(
      wizard.getByRole("combobox", { name: "Runner application" }),
      target.runner_principal_id,
    );
    await expect(
      wizard.getByRole("checkbox", { name: "Deploy", exact: true }),
    ).toHaveCount(0);
    await expect(
      wizard.getByLabel("Runner principal ID", { exact: true }),
    ).not.toBeVisible();
    await wizard
      .getByRole("button", { name: "Review target", exact: true })
      .click();
    await expect(
      wizard.getByText("Preproduction runner", { exact: true }),
    ).toBeVisible();
    await wizard
      .getByRole("button", { name: "Register target", exact: true })
      .click();
    await expect(
      page.getByRole("heading", {
        name: "Connect the outbound runner",
        exact: true,
      }),
    ).toBeVisible();
    const downloaded = page.waitForEvent("download");
    await page
      .getByRole("button", { name: "Download setup template", exact: true })
      .click();
    const download = await downloaded;
    const templatePath = testInfo.outputPath("runner-template.json");
    await download.saveAs(templatePath);
    const template = JSON.parse(readFileSync(templatePath, "utf8"));
    expect(template.destination.target_id).toBe(targetId);
    expect(template.destination.capabilities).toEqual(["observe"]);
    expect(template.scope.environment_id).toBe("development");
    await page.getByText("Local setup template", { exact: true }).click();
    await expect(
      page.getByLabel("Nonsecret runner configuration"),
    ).toContainText("namespace-uid");
    await expect(
      page.getByLabel("Nonsecret runner configuration"),
    ).toContainText("development");
    await expect(
      page.getByText(
        "weave operations runner check --config /opt/weave/private/runner.json",
        { exact: true },
      ),
    ).toBeVisible();
  });

for (const width of [1440, 390])
  test(`observed worker import at ${width} keeps the local alias explicit and offers admitted releases`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await connected(page, {
      capabilities: [
        "deployment.read",
        "target.manage",
        "deployment.plan",
        "catalog.read",
      ],
    });
    const image = "example/worker@sha256:" + "a".repeat(64);
    const release = "66666666-6666-4666-8666-666666666666";
    let saved: any;
    await page.route("**/deployments", async (route) => {
      saved = route.request().postDataJSON();
      await route.fulfill({ json: { ...deployment, ...saved } });
    });
    await page.route("**/studio/contracts/deployment", (route) =>
      route.fulfill({ json: schema }),
    );
    await page.route("**/deployment-targets?*", (route) =>
      route.fulfill({ json: { items: [target], next_cursor: null } }),
    );
    await page.route("**/deployment-targets/" + targetId, (route) =>
      route.fulfill({ json: target }),
    );
    await page.route("**/deployment-observations?*", (route) =>
      route.fulfill({
        json: {
          items: [
            {
              ...observation(),
              resources: [
                {
                  name: "integration-worker",
                  kind: "worker",
                  image,
                  replicas: 2,
                  ready_replicas: 2,
                  external_identity: "worker-external",
                  version: "v1",
                  ownership: "imported",
                  state: "ready",
                },
              ],
            },
          ],
          next_cursor: null,
        },
      }),
    );
    await page.route("**/worker-releases?*", (route) =>
      route.fulfill({
        json: {
          items: [
            {
              id: release,
              image_digest: "sha256:" + "b".repeat(64),
              capabilities: [
                { taskType: "integration.execute", taskVersion: "1.0.0" },
              ],
            },
          ],
          next_cursor: null,
        },
      }),
    );
    await page.getByRole("button", { name: "Operations", exact: true }).click();
    await page.getByRole("button", { name: target.name, exact: true }).click();
    await page
      .getByRole("button", { name: "Record desired deployment", exact: true })
      .click();
    const editor = page.locator("weave-deployment-editor");
    await editor.getByLabel("Name", { exact: true }).fill("imported-workers");
    await editor
      .getByRole("button", { name: "Continue to components", exact: true })
      .click();
    await editor
      .getByRole("checkbox", { name: "Import integration-worker", exact: true })
      .check();
    await editor
      .getByRole("button", {
        name: "Use selected observed components",
        exact: true,
      })
      .click();
    await expect(
      editor.getByLabel("Configuration", { exact: true }),
    ).toHaveValue("");
    await selectChoice(
      editor.getByRole("combobox", { name: "Admitted worker release" }),
      release,
    );
    await expect(
      editor.getByRole("combobox", { name: "Admitted worker release" }),
    ).toHaveValue(/integration\.execute@1\.0\.0/);
    await expect(
      editor.getByRole("button", { name: "Review desired state", exact: true }),
    ).toBeDisabled();
    await editor
      .getByLabel("Image", { exact: true })
      .fill("example/worker@sha256:" + "c".repeat(64));
    await expect(
      editor.getByRole("combobox", { name: "Admitted worker release" }),
    ).toHaveValue("");
    await editor.getByLabel("Image", { exact: true }).fill(image);
    await selectChoice(
      editor.getByRole("combobox", { name: "Admitted worker release" }),
      release,
    );
    await editor.getByLabel("Configuration", { exact: true }).fill("pre");
    await expect(
      editor.getByRole("button", { name: "Review desired state", exact: true }),
    ).toBeDisabled();
    await editor.getByRole("spinbutton", { name: "CPU millis" }).fill("750");
    await expect(
      editor.getByRole("button", { name: "Review desired state", exact: true }),
    ).toBeDisabled();
    await editor.getByRole("spinbutton", { name: "Memory mib" }).fill("1536");
    await editor
      .getByRole("button", { name: "Review desired state", exact: true })
      .click();
    await page.screenshot({
      path: testInfo.outputPath("observed-import-review.png"),
      fullPage: true,
    });
    await editor
      .getByRole("button", { name: "Save desired deployment", exact: true })
      .click();
    await expect
      .poll(() => saved?.components?.[0]?.worker_release_id)
      .toBe(release);
    expect(saved.components[0]).toMatchObject({
      name: "integration-worker",
      configuration: "pre",
      replicas: 2,
      cpu_millis: 750,
      memory_mib: 1536,
      image,
    });
  });

for (const width of [1440, 390]) {
  test(`unsupported migration and ACA API changes stop before review at ${width}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await connected(page, {
      capabilities: ["deployment.read", "deployment.plan", "target.manage"],
    });
    const azure = {
      ...target,
      adapter: "azure-container-apps",
      capabilities: ["observe", "update", "scale_workers"],
    };
    await page.route("**/studio/contracts/deployment", (route) =>
      route.fulfill({ json: schema }),
    );
    await page.route("**/deployment-targets?*", (route) =>
      route.fulfill({ json: { items: [azure], next_cursor: null } }),
    );
    await page.route("**/deployment-targets/" + targetId, (route) =>
      route.fulfill({ json: azure }),
    );
    await page.route("**/deployments?*", (route) =>
      route.fulfill({ json: { items: [deployment], next_cursor: null } }),
    );
    await page.route("**/studio/api/**/deployments/" + deploymentId, (route) =>
      route.fulfill({ json: deployment }),
    );
    await page.route("**/deployment-observations?*", (route) =>
      route.fulfill({ json: { items: [observation()], next_cursor: null } }),
    );
    let requests = 0;
    await page.route("**/deployment-plans", (route) => {
      requests++;
      return route.fulfill({ json: {} });
    });
    await page.goto("/operations/deployments/" + deploymentId);
    await page
      .getByRole("button", { name: "Create plan", exact: true })
      .click();
    const planner = page.locator("weave-deployment-plan-builder");
    await expect(
      planner.getByText(/Container Apps updates support worker and Lumi apps/),
    ).toBeVisible();
    await expect(
      planner.getByRole("button", {
        name: "Generate reviewable plan",
        exact: true,
      }),
    ).toBeDisabled();
    expect(requests).toBe(0);
    await planner
      .getByRole("button", { name: "Cancel plan", exact: true })
      .click();
    await page
      .getByRole("button", { name: "Edit desired deployment", exact: true })
      .click();
    const editor = page.locator("weave-deployment-editor");
    await editor
      .getByRole("button", { name: "Continue to components", exact: true })
      .click();
    await editor.getByRole("combobox", { name: "Kind", exact: true }).click();
    await expect(
      page.getByRole("option", { name: "migration", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("option", { name: "api", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Explain with Lumi", exact: true }),
    ).toHaveCount(0);
  });

  test(`Explain with Lumi offers only unchecked selected target context at ${width}`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await connected(page, { capabilities: ["deployment.read", "lumi.use"] });
    await page.route("**/deployment-targets?*", (route) =>
      route.fulfill({ json: { items: [target], next_cursor: null } }),
    );
    await page.route("**/deployment-targets/" + targetId, (route) =>
      route.fulfill({ json: target }),
    );
    await page.route("**/deployment-observations?*", (route) =>
      route.fulfill({ json: { items: [observation()], next_cursor: null } }),
    );
    await page.route("**/lumi/status", (route) =>
      route.fulfill({
        json: {
          configured: true,
          provider: "openai",
          model: "test",
          revision: 1,
        },
      }),
    );
    let asks = 0;
    await page.route("**/lumi/ask", (route) => {
      asks++;
      return route.fulfill({
        json: { answer: "test", proposals: [], followUps: [] },
      });
    });
    await page.goto("/operations/targets/" + targetId);
    await page
      .getByRole("button", { name: "Explain with Lumi", exact: true })
      .click();
    await expect(
      page.getByRole("checkbox", {
        name: "Include selected target",
        exact: true,
      }),
    ).not.toBeChecked();
    await expect(
      page.getByRole("checkbox", {
        name: "Include selected observation",
        exact: true,
      }),
    ).not.toBeChecked();
    await expect(
      page.getByRole("checkbox", { name: /draft|source|selected run/i }),
    ).toHaveCount(0);
    expect(asks).toBe(0);
  });
}
