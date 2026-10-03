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
import { execFileSync } from "node:child_process";
import { test, expect, type Page } from "@playwright/test";
import { parse } from "yaml";
import {
  allCapabilities,
  connected,
  newWorkflow,
  sourceText,
  selectChoice,
} from "./support";
import { DesignerPage } from "./designer-po";

const schema = (module: string, name: string) =>
  JSON.parse(
    execFileSync(
      ".venv/bin/python",
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
        json: { code: "WV-NOT-FOUND", message: "Lumi is not configured" },
      });
  });
  return { connections, configurations, creates };
}
for (const width of [1440, 600])
  test.describe(`AI setup at ${width}`, () => {
    test.use({ viewport: { width, height: 800 } });
    test("fresh Azure provider connection and Lumi settings use named revisions and approved handles", async ({
      page,
    }) => {
      const capture = await setup(page);
      await page
        .getByRole("button", { name: "Connections", exact: true })
        .click();
      await page
        .getByRole("button", { name: "New AI connection", exact: true })
        .click();
      const form = page.getByRole("dialog", {
        name: "New AI connection",
        exact: true,
      });
      await form
        .getByLabel("Connection name", { exact: true })
        .fill("azure-models");
      await selectChoice(
        form.getByLabel("Provider", { exact: true }),
        "azure-responses",
      );
      await form.getByRole("button", { name: "Continue", exact: true }).click();
      await form
        .getByLabel("Provider endpoint", { exact: true })
        .fill("https://approved-models.openai.azure.com/");
      await form
        .getByLabel("API key secret handle", { exact: true })
        .fill("azure-model-key");
      await form
        .getByRole("button", { name: "Review connection", exact: true })
        .click();
      await expect(form).toContainText("Azure requires an API version");
      expect(capture.creates).toHaveLength(0);
      await form
        .getByLabel("Azure API version", { exact: true })
        .fill("2025-04-01-preview");
      await form
        .getByRole("button", { name: "Review connection", exact: true })
        .click();
      await form
        .getByRole("button", { name: "Create AI connection", exact: true })
        .click();
      await expect(form).toContainText("Connection created");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      expect(capture.creates[0]).toEqual({
        name: "azure-models",
        connector_version_id: connectorId,
        config: {
          provider: "azure-responses",
          endpoint: "https://approved-models.openai.azure.com/",
          secretSlot: "apiKey",
          apiVersion: "2025-04-01-preview",
        },
        secretRef: { apiKey: "azure-model-key" },
        allowed_destinations: ["https://approved-models.openai.azure.com"],
      });
      await form.getByRole("button", { name: "Done", exact: true }).click();
      await page.getByRole("button", { name: "Ask Lumi", exact: true }).click();
      const lumi = page.getByRole("dialog", { name: "Ask Lumi", exact: true });
      await lumi
        .getByRole("button", { name: "Lumi settings", exact: true })
        .click();
      await selectChoice(
        lumi.getByLabel("Provider", { exact: true }),
        "azure-responses",
      );
      await lumi
        .getByLabel("Model", { exact: true })
        .fill("assistant-deployment");
      await lumi
        .getByRole("button", { name: "Continue to connection", exact: true })
        .click();
      await selectChoice(
        lumi.getByLabel("Provider connection", { exact: true }),
        connectionId,
      );
      await expect(
        lumi.getByLabel("Connection revision id", { exact: true }),
      ).toHaveCount(0);
      await lumi
        .getByRole("button", { name: "Review settings", exact: true })
        .click();
      await lumi
        .getByRole("button", { name: "Save Lumi settings", exact: true })
        .click();
      await expect(lumi.getByLabel("Message to Lumi")).toBeVisible();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: `../.superpowers/editor-ux/ai-setup-${width}.png`,
      });
      expect(capture.configurations).toHaveLength(1);
      expect(capture.configurations[0].connection_revision_id).toBe(
        connectionId,
      );
      expect(capture.configurations[0].profile.model).toBe(
        "assistant-deployment",
      );
      expect(capture.configurations[0].profile.outputSchema).toEqual(
        lumiSchema.$defs.LumiProfile.properties.outputSchema.const,
      );
      await expect(lumi).toContainText("gateway");
    });
    test("fresh workflow AI profile and connection slot remain separate from Lumi", async ({
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
        .getByLabel("Property value", { exact: true })
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
test("Lumi manager without connection management sees the required grant instead of an editable UUID", async ({
  page,
}) => {
  const capture = await setup(page, ["lumi.use", "lumi.manage"]);
  await page.getByRole("button", { name: "Ask Lumi", exact: true }).click();
  const lumi = page.getByRole("dialog", { name: "Ask Lumi", exact: true });
  await lumi
    .getByRole("button", { name: "Lumi settings", exact: true })
    .click();
  await expect(lumi).toContainText("connection.manage");
  await selectChoice(
    lumi.getByLabel("Provider", { exact: true }),
    "openai-responses",
  );
  await lumi.getByLabel("Model", { exact: true }).fill("test-model");
  await lumi
    .getByRole("button", { name: "Continue to connection", exact: true })
    .click();
  await expect(
    lumi.getByRole("button", { name: "Review settings", exact: true }),
  ).toBeDisabled();
  await expect(
    lumi.getByLabel("Provider connection", { exact: true }),
  ).toBeDisabled();
  expect(capture.configurations).toEqual([]);
  expect(capture.creates).toEqual([]);
  await expect(
    lumi.getByLabel("Connection revision id", { exact: true }),
  ).toHaveCount(0);
});

test("Settings exposes admin AI setup while viewers cannot configure or spend", async ({
  page,
}) => {
  await setup(page);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page
    .getByRole("button", { name: "Configure Lumi", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Choose Lumi's model",
      exact: true,
    }),
  ).toBeVisible();
  const lumi = page.getByRole("dialog", { name: "Ask Lumi", exact: true });
  await selectChoice(
    lumi.getByLabel("Provider", { exact: true }),
    "openai-responses",
  );
  await lumi.getByLabel("Model", { exact: true }).fill("test-model");
  await lumi
    .getByRole("button", { name: "Continue to connection", exact: true })
    .click();
  await expect(
    lumi.getByText(/No matching provider connections/),
  ).toBeVisible();
});

test("viewers see setup guidance without manager controls", async ({
  page,
}) => {
  const capture = await setup(page, ["catalog.read"]);
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Configure Lumi", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "New AI connection", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Ask Lumi", exact: true }).click();
  const panel = page.getByRole("dialog", { name: "Ask Lumi", exact: true });
  await expect(
    panel.getByRole("button", { name: "Lumi settings", exact: true }),
  ).toHaveCount(0);
  await expect(
    panel.getByRole("button", { name: "Send message", exact: true }),
  ).toBeDisabled();
  expect(capture.creates).toEqual([]);
  expect(capture.configurations).toEqual([]);
});

test("provider setup explains missing catalog permission without creating a connection", async ({
  page,
}) => {
  const capture = await setup(page);
  await page.route("**/projects/project/connectors?*", (r) =>
    r.fulfill({
      status: 403,
      json: { code: "WV-FORBIDDEN", message: "Access denied" },
    }),
  );
  await page.getByRole("button", { name: "Connections", exact: true }).click();
  await page
    .getByRole("button", { name: "New AI connection", exact: true })
    .click();
  const form = page.getByRole("dialog", {
    name: "New AI connection",
    exact: true,
  });
  await expect(form).toContainText("catalog.read");
  await expect(
    form.getByRole("button", { name: "Continue", exact: true }),
  ).toBeDisabled();
  expect(capture.creates).toEqual([]);
});

test("retrying an uncertain connection create reuses the same idempotency key", async ({
  page,
}) => {
  await setup(page);
  const requests: { body: unknown; key: string | undefined }[] = [];
  await page.route("**/environments/development/connections", async (r) => {
    const body = r.request().postDataJSON();
    requests.push({ body, key: r.request().headers()["idempotency-key"] });
    if (requests.length === 1)
      await r.fulfill({ status: 503, json: { message: "Temporary failure" } });
    else
      await r.fulfill({
        json: {
          ...body,
          id: connectionId,
          revision: 1,
          connector: "weave-agentic-provider@1.0.0",
        },
      });
  });
  await page.getByRole("button", { name: "Connections", exact: true }).click();
  await page
    .getByRole("button", { name: "New AI connection", exact: true })
    .click();
  const form = page.getByRole("dialog", {
    name: "New AI connection",
    exact: true,
  });
  await form.getByLabel("Connection name", { exact: true }).fill("provider");
  await selectChoice(
    form.getByLabel("Provider", { exact: true }),
    "openai-responses",
  );
  await form.getByRole("button", { name: "Continue", exact: true }).click();
  await form
    .getByLabel("Provider endpoint", { exact: true })
    .fill("https://api.openai.com/v1");
  await form
    .getByLabel("API key secret handle", { exact: true })
    .fill("model-key");
  await form
    .getByRole("button", { name: "Review connection", exact: true })
    .click();
  const submit = form.getByRole("button", {
    name: "Create AI connection",
    exact: true,
  });
  await submit.click();
  await expect(form.getByRole("alert")).toBeVisible();
  await submit.click();
  await expect(form).toContainText("Connection created");
  expect(requests).toHaveLength(2);
  expect(requests[0].key).toBeTruthy();
  expect(requests[1]).toEqual(requests[0]);
});

for (const width of [1440, 360])
  test.describe(`AI connection wizard at ${width}`, () => {
    test.use({ viewport: { width, height: 800 } });
    test("wizard guides keyboard review without creating early and retains Back edits", async ({
      page,
    }) => {
      const capture = await setup(page);
      await page
        .getByRole("button", { name: "Connections", exact: true })
        .click();
      await page
        .getByRole("button", { name: "New AI connection", exact: true })
        .click();
      const form = page.getByRole("dialog", {
        name: "New AI connection",
        exact: true,
      });
      const progress = form.getByRole("list", { name: "AI connection setup" });
      await expect(progress.locator('[aria-current="step"]')).toHaveText(
        /Provider/,
      );
      await expect(
        form.getByLabel("Provider endpoint", { exact: true }),
      ).toHaveCount(0);
      await form.getByRole("button", { name: "Continue", exact: true }).click();
      await expect(form.getByRole("alert")).toContainText("connection name");
      expect(capture.creates).toHaveLength(0);
      await selectChoice(
        form.getByLabel("Provider", { exact: true }),
        "azure-chat",
      );
      await form
        .getByLabel("Connection name", { exact: true })
        .fill("approved-azure");
      await form.getByLabel("Connection name", { exact: true }).press("Enter");
      await expect(
        form.getByRole("heading", {
          name: "Endpoint and secret handle",
          exact: true,
        }),
      ).toBeFocused();
      await expect(progress.locator('[aria-current="step"]')).toHaveText(
        /Access/,
      );
      await expect(
        form.getByLabel("Provider endpoint", { exact: true }),
      ).toHaveAttribute(
        "placeholder",
        "https://your-resource.openai.azure.com",
      );
      await expect(form).toContainText("operator-approved endpoint");
      await form
        .getByLabel("Provider endpoint", { exact: true })
        .fill("https://approved.openai.azure.com");
      await form
        .getByLabel("Azure API version", { exact: true })
        .fill("2024-10-21");
      await form
        .getByLabel("API key secret handle", { exact: true })
        .fill("approved-key");
      await form
        .getByLabel("API key secret handle", { exact: true })
        .press("Enter");
      await expect(
        form.getByRole("heading", {
          name: "Review your connection",
          exact: true,
        }),
      ).toBeFocused();
      await expect(progress.locator('[aria-current="step"]')).toHaveText(
        /Review/,
      );
      await expect(form).toContainText("Azure OpenAI Chat");
      await expect(form).toContainText("https://approved.openai.azure.com");
      await expect(form).toContainText("approved-key");
      await expect(form).toContainText("No model request or connectivity test");
      expect(capture.creates).toHaveLength(0);
      await form.getByRole("button", { name: "Back", exact: true }).click();
      await expect(
        form.getByLabel("API key secret handle", { exact: true }),
      ).toHaveValue("approved-key");
      await form.getByRole("button", { name: "Back", exact: true }).click();
      await expect(
        form.getByLabel("Connection name", { exact: true }),
      ).toHaveValue("approved-azure");
      await form
        .getByLabel("Connection name", { exact: true })
        .fill("reviewed-azure");
      await form.getByRole("button", { name: "Continue", exact: true }).click();
      await expect(
        form.getByLabel("Azure API version", { exact: true }),
      ).toHaveValue("2024-10-21");
      await form
        .getByRole("button", { name: "Review connection", exact: true })
        .click();
      await expect(form).toContainText("reviewed-azure");
      expect(capture.creates).toHaveLength(0);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: `../.superpowers/editor-ux/ai-wizard-review-${width}.png`,
      });
      await form
        .getByRole("button", { name: "Create AI connection", exact: true })
        .press("Enter");
      await expect(form).toContainText("Connection created");
      expect(capture.creates).toHaveLength(1);
      expect(capture.creates[0].name).toBe("reviewed-azure");
    });
  });
