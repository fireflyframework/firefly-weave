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
  allCapabilities,
  connected,
  newWorkflow,
  sourceText,
  selectChoice,
} from "./support";
import { DesignerPage } from "./designer-po";
import { python } from "../python-path";

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

test("AI model basics and advanced options preserve edits, invalid drafts and explicit clears", async ({
  page,
}) => {
  const capture = await setup(page);
  capture.connections.push({
    id: connectionId,
    name: "approved-ai",
    revision: 1,
    connector: "weave-agentic-provider@1.0.0",
    config: {
      provider: "openai-responses",
      endpoint: "https://api.openai.com/v1",
    },
  });
  const profile = {
    provider: "openai-responses",
    model: "approved-model",
    options: { max_tokens: 1024, temperature: 0.5, top_p: 0.8 },
    reasoning: { pattern: "none", maxSteps: 7 },
    maxCalls: 9,
    timeoutSeconds: 90,
    outputSchema: lumiSchema.$defs.LumiProfile.properties.outputSchema.const,
  };
  let saved = {
    enabled: true,
    connection_revision_id: connectionId,
    profile,
    revision: 1,
  };
  await page.route("**/lumi/configuration", async (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      capture.configurations.push(body);
      saved = { ...body, revision: saved.revision + 1 };
    }
    await route.fulfill({ json: saved });
  });
  await page.getByRole("button", { name: "Ask Weave AI", exact: true }).click();
  await page
    .getByRole("dialog", { name: "Ask Weave AI", exact: true })
    .getByRole("button", { name: "Weave AI settings", exact: true })
    .click();
  const lumi = page.getByRole("dialog", { name: "Ask Weave AI", exact: true });
  await expect(lumi.getByLabel("Model", { exact: true })).toHaveValue(
    "approved-model",
  );
  await expect(lumi.getByLabel("Max tokens", { exact: true })).toHaveValue(
    "1024",
  );
  await expect(
    lumi.getByRole("spinbutton", {
      name: "Temperature (optional)",
      exact: true,
    }),
  ).not.toBeVisible();
  await page.screenshot({
    path: "../.superpowers/editor-ux/ai-profile-basic.png",
  });
  const advanced = lumi.getByText("Advanced model settings", { exact: true });
  await advanced.click();
  await page.screenshot({
    path: "../.superpowers/editor-ux/ai-profile-advanced.png",
  });
  await lumi
    .getByRole("spinbutton", { name: "Temperature (optional)", exact: true })
    .fill("3");
  await advanced.click();
  await lumi.getByLabel("Model", { exact: true }).fill("updated-model");
  await expect(
    lumi.getByRole("button", { name: "Continue to connection", exact: true }),
  ).toBeDisabled();
  await advanced.click();
  await expect(
    lumi.getByRole("spinbutton", {
      name: "Temperature (optional)",
      exact: true,
    }),
  ).toHaveValue("3");
  await lumi
    .getByRole("spinbutton", { name: "Temperature (optional)", exact: true })
    .fill("");
  await advanced.click();
  await lumi.getByLabel("Max tokens", { exact: true }).fill("2048");
  await lumi
    .getByRole("button", { name: "Continue to connection", exact: true })
    .click();
  await lumi
    .getByRole("button", { name: "Review settings", exact: true })
    .click();
  await lumi
    .getByRole("button", { name: "Save Weave AI settings", exact: true })
    .click();
  await expect(lumi.getByLabel("Message to Weave AI")).toBeVisible();
  expect(capture.configurations[0].profile).toEqual({
    ...profile,
    model: "updated-model",
    options: { max_tokens: 2048, top_p: 0.8 },
  });
  await lumi
    .getByRole("button", { name: "Weave AI settings", exact: true })
    .click();
  await lumi.getByText("Advanced model settings", { exact: true }).click();
  await expect(
    lumi.getByRole("spinbutton", {
      name: "Temperature (optional)",
      exact: true,
    }),
  ).toHaveValue("");
});

test("canonical model validation blocks pending and invalid settings and ignores stale replies", async ({
  page,
}) => {
  await setup(page);
  const requests: Route[] = [];
  await page.route("**/studio/contracts/llm-profile/validate", (route) => {
    requests.push(route);
  });
  await page.getByRole("button", { name: "Ask Weave AI", exact: true }).click();
  await page
    .getByRole("dialog", { name: "Ask Weave AI", exact: true })
    .getByRole("button", { name: "Weave AI settings", exact: true })
    .click();
  const lumi = page.getByRole("dialog", { name: "Ask Weave AI", exact: true });
  await selectChoice(
    lumi.getByLabel("Provider", { exact: true }),
    "openai-responses",
  );
  await lumi.getByLabel("Model", { exact: true }).fill("first-model");
  await expect.poll(() => requests.length).toBe(1);
  const next = lumi.getByRole("button", {
    name: "Continue to connection",
    exact: true,
  });
  await expect(next).toBeDisabled();
  await lumi.getByLabel("Model", { exact: true }).fill("second-model");
  await expect.poll(() => requests.length).toBe(2);
  const message =
    "A model call timeout cannot exceed the total AI step timeout";
  await requests[1].fulfill({
    json: { valid: false, issues: [{ path: [], message }] },
  });
  await expect(lumi.getByRole("alert")).toContainText(message);
  await requests[0].fulfill({ json: { valid: true, issues: [] } });
  await expect(next).toBeDisabled();
  await expect(lumi.getByRole("alert")).toContainText(message);
  await lumi.getByLabel("Model", { exact: true }).fill("corrected-model");
  await expect.poll(() => requests.length).toBe(3);
  expect(requests[2].request().postDataJSON()).toMatchObject({
    provider: "openai-responses",
    model: "corrected-model",
    outputSchema: {},
  });
  expect(requests[2].request().postDataJSON()).not.toHaveProperty("profile");
  await requests[2].fulfill({ json: { valid: true, issues: [] } });
  await expect(next).toBeEnabled();
  await expect(lumi.getByRole("alert")).toHaveCount(0);
});
