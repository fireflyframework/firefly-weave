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
// Settings › AI models and its AI connection side sheet against a mocked
// platform, with real clicks at 1440x900 and 600x500.
import { expect, test, type Page } from "@playwright/test";
import { connected } from "./support";
import {
  AI_CAPABILITIES,
  CONNECTOR_ID,
  OLLAMA_REVISION,
  mockAiPlatform,
  ollamaConnection,
  openAiModels,
  sheet,
  type AiPlatform,
} from "./ai-models-po";

async function start(page: Page, options: Partial<AiPlatform> = {}) {
  await connected(page, {
    capabilities: options.capabilities ?? AI_CAPABILITIES,
  });
  const platform = await mockAiPlatform(page, options);
  await openAiModels(page);
  return platform;
}
/** Opens a new AI connection from the screen's Connect button. */
async function newConnection(page: Page, provider: "Ollama" | "OpenAI") {
  await page.getByRole("button", { name: `Connect ${provider}` }).click();
  return sheet(page);
}

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`AI connection sheet at ${size.width}x${size.height}`, () => {
    test.use({ viewport: size });

    test("connects Ollama from an approved preset without a key, then tests a tool-capable model", async ({
      page,
    }) => {
      const platform = await start(page);
      const form = await newConnection(page, "Ollama");
      await expect(
        form.getByRole("radio", { name: /Ollama \(Weave-managed\)/ }),
      ).toBeChecked();
      await expect(form).toContainText("Development only");
      await expect(form.getByLabel("Secret handle")).toHaveCount(0);
      await expect(form).toContainText(
        "Local endpoints use no credential. Weave never sends credentials to a model over plain HTTP.",
      );
      await expect(
        form.getByRole("button", { name: "Test connection" }),
      ).toBeDisabled();
      await form.getByRole("button", { name: "Save connection" }).click();
      await expect(form.getByLabel("Model", { exact: true })).toHaveValue(
        "qwen3:4b",
      );
      expect(platform.creates).toEqual([
        {
          key: expect.any(String),
          body: {
            name: "ollama-local",
            connector_version_id: CONNECTOR_ID,
            config: {
              provider: "openai-chat",
              endpoint: "http://ollama:11434/v1",
              secretSlot: "apiKey",
            },
            secretRef: { apiKey: "no-credential" },
            allowed_destinations: ["http://ollama:11434"],
          },
        },
      ]);
      await form.getByRole("button", { name: "Test connection" }).click();
      await expect(
        form.getByRole("status").filter({ hasText: "Connected." }),
      ).toHaveText("Connected. qwen3:4b answered in 2.4 s and can call tools.");
      expect(platform.tests).toEqual([
        {
          url: expect.stringContaining(
            `/ai/connections/${OLLAMA_REVISION}/test`,
          ),
          body: { model: "qwen3:4b", probe_tools: true },
        },
      ]);
      expect(
        await page.evaluate(() => JSON.stringify(localStorage)),
      ).not.toContain("qwen3:4b");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
    });

    test("refuses a custom plain HTTP address before sending anything", async ({
      page,
    }) => {
      const platform = await start(page);
      const form = await newConnection(page, "Ollama");
      await form.getByRole("radio", { name: "Custom address…" }).check();
      await form
        .getByLabel("Endpoint address")
        .fill("http://10.0.0.5:11434/v1");
      await form.getByRole("button", { name: "Save connection" }).click();
      await expect(form.getByRole("alert")).toHaveText(
        "This address is not approved for plain HTTP. A platform operator must add it to the private-origin policy.",
      );
      await expect(form.getByLabel("Endpoint address")).toHaveAttribute(
        "aria-invalid",
        "true",
      );
      expect(platform.creates).toEqual([]);
    });
  });

test("shows running, canceled, no-tools, failed and gateway-missing test states", async ({
  page,
}) => {
  const platform = await start(page, {
    connections: [ollamaConnection],
    testDelay: 60_000,
  });
  await page.getByRole("button", { name: "Test ollama-local" }).click();
  const form = sheet(page, "ollama-local");
  await form.getByRole("button", { name: "Test connection" }).click();
  await expect(
    form.getByRole("status").filter({ hasText: "Calling" }),
  ).toHaveText("Calling qwen3:4b on ollama-local…");
  await form.getByRole("button", { name: "Cancel test" }).click();
  await expect(
    form.getByRole("status").filter({ hasText: "canceled" }),
  ).toHaveText("Test canceled.");
  platform.testDelay = 0;
  platform.testAnswer = {
    ok: true,
    code: "ok",
    latency_ms: 800,
    model: "gemma3:270m",
    tool_calling: "unsupported",
  };
  await form.getByLabel("Model", { exact: true }).click();
  await page.getByRole("option", { name: /gemma3:270m/ }).click();
  await form.getByRole("button", { name: "Test connection" }).click();
  const status = form.locator(".test-status");
  await expect(status).toHaveAttribute("data-tone", "warning");
  await expect(status).toContainText(
    "cannot call tools. Use it for AI tasks, not AI agents.",
  );
  platform.testAnswer = {
    ok: false,
    code: "LLM_MODEL_NOT_FOUND",
    latency_ms: 30,
    model: "gemma3:270m",
    tool_calling: "unknown",
  };
  await form.getByRole("button", { name: "Test connection" }).click();
  await expect(status).toContainText(
    "gemma3:270m is not installed on http://ollama:11434.",
  );
  await form.getByText("Show details").click();
  await expect(form.locator(".test-details")).toContainText(
    "LLM_MODEL_NOT_FOUND",
  );
  platform.testAnswer = {
    status: 503,
    body: {
      code: "WV-AI-GATEWAY-MISSING",
      message: "body text that must not show",
    },
  };
  await form.getByRole("button", { name: "Test connection" }).click();
  await expect(status).toContainText("Tests need the AI gateway.");
  await expect(form).not.toContainText("body text that must not show");
});

test("asks for an OpenAI secret handle, never a key, and shows the operator command", async ({
  page,
}) => {
  const platform = await start(page);
  const form = await newConnection(page, "OpenAI");
  await expect(form.getByRole("radio", { name: "Responses" })).toBeChecked();
  await expect(form.getByLabel("Secret handle")).toHaveValue("openai-api-key");
  await expect(form).toContainText(
    "Weave stores a reference to your key, never the key.",
  );
  await expect(
    form
      .locator("code")
      .filter({ hasText: "weave platform secret set --handle openai-api-key" }),
  ).toBeVisible();
  await form.getByLabel("Secret handle").fill("no-credential");
  await form.getByRole("button", { name: "Save connection" }).click();
  await expect(form.getByRole("alert")).toContainText(
    "no-credential is reserved",
  );
  await form.getByLabel("Secret handle").fill("team-openai");
  await form.getByRole("button", { name: "Save connection" }).click();
  await expect.poll(() => platform.creates.length).toBe(1);
  expect(platform.creates[0].body).toMatchObject({
    name: "openai",
    config: {
      provider: "openai-responses",
      endpoint: "https://api.openai.com/v1",
    },
    secretRef: { apiKey: "team-openai" },
  });
});

test("asks before closing with unsaved changes, and an uncertain save reuses its key", async ({
  page,
}) => {
  const platform = await start(page, {
    createAnswers: [
      {
        status: 502,
        body: {
          code: "WV-STUDIO-UPSTREAM",
          message: "Platform request failed",
        },
      },
    ],
  });
  const form = await newConnection(page, "Ollama");
  await form.getByLabel("Connection name").fill("team-ollama");
  await page.keyboard.press("Escape");
  const question = page.getByRole("dialog", { name: "Discard your changes?" });
  await question.getByRole("button", { name: "Keep editing" }).click();
  await expect(form).toBeVisible();
  await form.getByRole("button", { name: "Save connection" }).click();
  await expect(form.getByRole("alert")).toContainText(
    "Platform request failed",
  );
  await form.getByRole("button", { name: "Save connection" }).click();
  await expect.poll(() => platform.creates.length).toBe(2);
  expect(platform.creates[1].key).toBe(platform.creates[0].key);
  await form.getByRole("button", { name: "Close AI connection" }).click();
  await expect(form).toHaveCount(0);
});

test("is read-only for people who cannot manage connections", async ({
  page,
}) => {
  await start(page, {
    capabilities: ["catalog.read", "status.read", "connection.bind"],
    connections: [ollamaConnection],
  });
  await page.getByRole("button", { name: "View ollama-local" }).click();
  const form = sheet(page, "ollama-local");
  await expect(form).toContainText(
    "Creating AI connections requires the tenant administrator role.",
  );
  await expect(
    form.getByRole("button", { name: "Save connection" }),
  ).toHaveCount(0);
  await expect(
    form.getByRole("button", { name: "Test connection" }),
  ).toHaveCount(0);
  await expect(form.getByLabel("Connection name")).toBeDisabled();
});
