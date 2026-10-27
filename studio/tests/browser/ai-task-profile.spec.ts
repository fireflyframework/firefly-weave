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
// The AI task's workflow profiles. AI connections themselves are covered by ai-setup.spec.ts.
import { execFileSync } from "node:child_process";
import { test, expect, type Page, type Route } from "@playwright/test";
import { parse } from "yaml";
import {
  withClassicDesigner,
  allCapabilities,
  connected,
  newWorkflow,
  sourceText,
  selectChoice,
} from "./support";
import { DesignerPage } from "./designer-po";
import { python } from "../python-path";

test.beforeEach(async ({ page }) => {
  await withClassicDesigner(page);
});

const schema = (module: string, name: string) =>
  JSON.parse(
    execFileSync(
      python,
      [
        "-c",
        `import json; from firefly_weave.contracts.${module} import ${name}; print(json.dumps(${name}.model_json_schema(by_alias=True)))`,
      ],
      { cwd: "..", encoding: "utf8" },
    ),
  );
const lumiSchema = schema("lumi", "LumiConfigurationRequest");
const llmSchema = schema("llm", "LLMProfile");
const connectorId = "11111111-1111-4111-8111-111111111111";
const connectionId = "22222222-2222-4222-8222-222222222222";
async function setup(
  page: Page,
  capabilities = [...allCapabilities, "lumi.use", "lumi.manage"],
) {
  await connected(page, { capabilities });
  const connections: Record<string, unknown>[] = [];
  const configurations: any[] = [];
  const creates: any[] = [];
  await page.route("**/studio/contracts/lumi-configuration", (r) =>
    r.fulfill({ json: lumiSchema }),
  );
  await page.route("**/studio/contracts/llm-profile", (r) =>
    r.fulfill({ json: llmSchema }),
  );
  await page.route("**/studio/contracts/llm-profile/validate", (r) =>
    r.fulfill({ json: { valid: true, issues: [] } }),
  );
  await page.route("**/projects/project/connectors?*", (r) =>
    r.fulfill({
      json: {
        items: [
          { id: connectorId, name: "weave-agentic-provider", version: "1.0.0" },
        ],
        next_cursor: null,
      },
    }),
  );
  await page.route("**/environments/development/connections?*", (r) =>
    r.fulfill({ json: { items: connections, next_cursor: null } }),
  );
  await page.route("**/environments/development/connections", async (r) => {
    const body = r.request().postDataJSON();
    creates.push(body);
    const connection = {
      ...body,
      id: connectionId,
      revision: 1,
      connector: "weave-agentic-provider@1.0.0",
    };
    connections.push(connection);
    await r.fulfill({ json: connection });
  });
  await page.route("**/lumi/status", (r) =>
    r.fulfill({ json: { configured: false } }),
  );
  await page.route("**/lumi/configuration", async (r) => {
    if (r.request().method() === "PUT") {
      const body = r.request().postDataJSON();
      configurations.push(body);
      await r.fulfill({ json: { ...body, revision: 1 } });
    } else
      await r.fulfill({
        status: 404,
        json: { code: "WV-NOT-FOUND", message: "Weave AI is not configured" },
      });
  });
  return { connections, configurations, creates };
}
for (const width of [1440, 600])
  test.describe(`AI setup at ${width}`, () => {
    test.use({ viewport: { width, height: 800 } });
    test("fresh workflow AI profile and connection slot remain separate from Weave AI", async ({
      page,
    }) => {
      const capture = await setup(page);
      await newWorkflow(page);
      const designer = new DesignerPage(page);
      if (width <= 767)
        await page
          .getByRole("button", { name: "Insert step", exact: true })
          .click();
      await page
        .locator(".palette")
        .getByRole("button", { name: "AI task", exact: true })
        .click();
      await designer.selectStep("ask-ai-1");
      const form = page.locator("weave-llm-inspector");
      await form
        .getByRole("button", {
          name: "Configure workflow AI profiles",
          exact: true,
        })
        .click();
      await selectChoice(
        form.getByLabel("Provider", { exact: true }),
        "azure-responses",
      );
      await form
        .getByLabel("Model", { exact: true })
        .fill("workflow-deployment");
      await form.getByLabel("Max tokens", { exact: true }).fill("512");
      await form
        .getByRole("button", { name: "Continue to connection", exact: true })
        .click();
      await form
        .getByRole("button", { name: "Add AI connection slot", exact: true })
        .click();
      await expect(
        form.getByLabel("AI connection slot", { exact: true }),
      ).toHaveValue("ai");
      await form
        .getByRole("button", { name: "Review settings", exact: true })
        .click();
      await form
        .getByRole("button", {
          name: "Apply workflow AI settings",
          exact: true,
        })
        .click();
      await form
        .locator('[data-field="prompt"]')
        .getByLabel("AI prompt", { exact: true })
        .fill("Summarize input");
      await expect(
        form.getByLabel("Workflow AI profile", { exact: true }),
      ).toHaveValue("default");
      const doc = parse(await sourceText(page));
      expect(doc.spec.llmProfiles.default.provider).toBe("azure-responses");
      expect(doc.spec.llmProfiles.default.model).toBe("workflow-deployment");
      expect(doc.spec.connections.ai.connector).toBe(
        "weave-agentic-provider@1.0.0",
      );
      expect(doc.spec.steps[0].connection).toBe("ai");
      expect(capture.configurations).toEqual([]);
    });
  });

/** Opens the workflow AI profile dialog of a new AI task. */
async function openProfiles(page: Page) {
  await newWorkflow(page);
  await page
    .locator(".palette")
    .getByRole("button", { name: "AI task", exact: true })
    .click();
  await new DesignerPage(page).selectStep("ask-ai-1");
  const form = page.locator("weave-llm-inspector");
  await form
    .getByRole("button", {
      name: "Configure workflow AI profiles",
      exact: true,
    })
    .click();
  return form;
}

test("AI model basics and advanced options preserve edits, invalid drafts and explicit clears", async ({
  page,
}) => {
  await setup(page);
  const form = await openProfiles(page);
  await selectChoice(
    form.getByLabel("Provider", { exact: true }),
    "openai-responses",
  );
  await form.getByLabel("Model", { exact: true }).fill("approved-model");
  await form.getByLabel("Max tokens", { exact: true }).fill("1024");
  await expect(
    form.getByRole("spinbutton", {
      name: "Temperature (optional)",
      exact: true,
    }),
  ).not.toBeVisible();
  await page.screenshot({
    path: "../.superpowers/editor-ux/ai-profile-basic.png",
  });
  const advanced = form.getByText("Advanced model settings", { exact: true });
  await advanced.click();
  await page.screenshot({
    path: "../.superpowers/editor-ux/ai-profile-advanced.png",
  });
  await form
    .getByRole("spinbutton", { name: "Temperature (optional)", exact: true })
    .fill("3");
  await advanced.click();
  await form.getByLabel("Model", { exact: true }).fill("updated-model");
  await expect(
    form.getByRole("button", { name: "Continue to connection", exact: true }),
  ).toBeDisabled();
  await advanced.click();
  await expect(
    form.getByRole("spinbutton", {
      name: "Temperature (optional)",
      exact: true,
    }),
  ).toHaveValue("3");
  await form
    .getByRole("spinbutton", { name: "Temperature (optional)", exact: true })
    .fill("");
  await advanced.click();
  await form.getByLabel("Max tokens", { exact: true }).fill("2048");
  await form
    .getByRole("button", { name: "Continue to connection", exact: true })
    .click();
  await form
    .getByRole("button", { name: "Add AI connection slot", exact: true })
    .click();
  await form
    .getByRole("button", { name: "Review settings", exact: true })
    .click();
  await form
    .getByRole("button", { name: "Apply workflow AI settings", exact: true })
    .click();
  const profile = parse(await sourceText(page)).spec.llmProfiles.default;
  expect(profile).toMatchObject({
    provider: "openai-responses",
    model: "updated-model",
    options: { max_tokens: 2048 },
  });
  expect(profile["options"]).not.toHaveProperty("temperature");
});

test("canonical model validation blocks pending and invalid settings and ignores stale replies", async ({
  page,
}) => {
  await setup(page);
  const requests: Route[] = [];
  await page.route("**/studio/contracts/llm-profile/validate", (route) => {
    requests.push(route);
  });
  const form = await openProfiles(page);
  await selectChoice(
    form.getByLabel("Provider", { exact: true }),
    "openai-responses",
  );
  await form.getByLabel("Max tokens", { exact: true }).fill("512");
  await form.getByLabel("Model", { exact: true }).fill("first-model");
  const first = requests.length;
  await expect.poll(() => requests.length).toBeGreaterThan(0);
  const next = form.getByRole("button", {
    name: "Continue to connection",
    exact: true,
  });
  await expect(next).toBeDisabled();
  await form.getByLabel("Model", { exact: true }).fill("second-model");
  await expect.poll(() => requests.length).toBeGreaterThan(Math.max(first, 1));
  const stale = requests.slice(0, -1);
  const latest = requests[requests.length - 1];
  const message =
    "A model call timeout cannot exceed the total AI step timeout";
  await latest.fulfill({
    json: { valid: false, issues: [{ path: [], message }] },
  });
  await expect(form.getByRole("alert")).toContainText(message);
  for (const route of stale)
    await route.fulfill({ json: { valid: true, issues: [] } });
  await expect(next).toBeDisabled();
  await expect(form.getByRole("alert")).toContainText(message);
  const before = requests.length;
  await form.getByLabel("Model", { exact: true }).fill("corrected-model");
  await expect.poll(() => requests.length).toBe(before + 1);
  const corrected = requests[before];
  expect(corrected.request().postDataJSON()).toMatchObject({
    provider: "openai-responses",
    model: "corrected-model",
    outputSchema: {},
  });
  expect(corrected.request().postDataJSON()).not.toHaveProperty("profile");
  await corrected.fulfill({ json: { valid: true, issues: [] } });
  await expect(next).toBeEnabled();
  await expect(form.getByRole("alert")).toHaveCount(0);
});
