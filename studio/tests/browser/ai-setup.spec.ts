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
import { execFileSync } from "node:child_process";
import { expect, test, type Page } from "@playwright/test";
import { python } from "../python-path";
import { connected, newWorkflow, selectChoice } from "./support";
import { DesignerPage } from "./designer-po";
import {
  AI_CAPABILITIES,
  CONNECTOR_ID,
  OLLAMA_REVISION,
  OPENAI_REVISION,
  mockAiPlatform,
  ollamaConnection,
  openAiModels,
  openaiConnection,
  readiness,
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

test("Build › Connections opens the same sheet and points AI connections to Settings", async ({
  page,
}) => {
  await start(page, { connections: [ollamaConnection] });
  await page.getByRole("button", { name: "Connections", exact: true }).click();
  await page
    .getByRole("button", { name: "New AI connection", exact: true })
    .click();
  const form = sheet(page);
  await expect(
    form
      .getByRole("group", { name: "Provider" })
      .getByRole("radio", { name: /^Ollama/ }),
  ).toBeChecked();
  await form.getByRole("button", { name: "Close AI connection" }).click();
  await expect(form).toHaveCount(0);
  await page
    .getByRole("button", { name: /ollama-local/ })
    .first()
    .click();
  await page
    .getByRole("button", { name: "Manage in Settings › AI models" })
    .click();
  await expect(page).toHaveURL(/\/settings\/ai$/);
});

test("an AI task's connection step opens the same sheet over the editor", async ({
  page,
}) => {
  await start(page);
  // The profile editor reads its fields from the real LLM profile schema.
  const llmSchema = execFileSync(
    python,
    [
      "-c",
      "import json; from firefly_weave.contracts.llm import LLMProfile; print(json.dumps(LLMProfile.model_json_schema(by_alias=True)))",
    ],
    { cwd: "..", encoding: "utf8" },
  );
  await page.route("**/studio/contracts/llm-profile", (r) =>
    r.fulfill({ contentType: "application/json", body: llmSchema }),
  );
  await page.route("**/studio/contracts/llm-profile/validate", (r) =>
    r.fulfill({ json: { valid: true, issues: [] } }),
  );
  await page.getByRole("button", { name: "Home", exact: true }).click();
  await newWorkflow(page);
  await page
    .locator(".palette")
    .getByRole("button", { name: "AI task", exact: true })
    .click();
  await new DesignerPage(page).selectStep("ask-ai-1");
  const inspector = page.locator("weave-llm-inspector");
  await inspector
    .getByRole("button", {
      name: "Configure workflow AI profiles",
      exact: true,
    })
    .click();
  await selectChoice(
    inspector.getByLabel("Provider", { exact: true }),
    "openai-chat",
  );
  await inspector.getByLabel("Model", { exact: true }).fill("qwen3:4b");
  await inspector.getByLabel("Max tokens", { exact: true }).fill("512");
  await inspector
    .getByRole("button", { name: "Continue to connection", exact: true })
    .click();
  await inspector
    .getByRole("button", { name: "New AI connection", exact: true })
    .click();
  await expect(sheet(page)).toBeVisible();
  await sheet(page)
    .getByRole("button", { name: "Close AI connection" })
    .click();
  await expect(inspector).toBeVisible();
});

test("Home offers Connect a model to people who manage connections", async ({
  page,
}) => {
  await start(page);
  await page.getByRole("button", { name: "Home", exact: true }).click();
  await page.getByRole("button", { name: "Connect a model" }).click();
  await expect(page).toHaveURL(/\/settings\/ai$/);
});

for (const size of [
  { width: 1440, height: 900 },
  { width: 600, height: 500 },
])
  test.describe(`AI models page at ${size.width}x${size.height}`, () => {
    test.use({ viewport: size });

    test("shows providers, connections and the checklist", async ({ page }) => {
      await start(page, { connections: [ollamaConnection, openaiConnection] });
      const cards = page.locator(".provider-card");
      await expect(cards).toHaveCount(4);
      await expect(cards.nth(0)).toContainText("Ollama");
      await expect(cards.nth(0)).toContainText("Connected · 1");
      await expect(cards.nth(2)).toContainText("Not set up");
      await expect(
        page.getByRole("button", { name: "Manage Ollama" }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Connect Anthropic" }),
      ).toBeVisible();
      const row = page
        .locator(".connection-row")
        .filter({ hasText: "ollama-local" });
      await expect(row).toContainText("http://ollama:11434/v1");
      await expect(row).toContainText("Development only");
      await expect(row).toContainText("Not tested in this session");
      const items = page.locator(".readiness-row");
      await expect(items).toHaveCount(8);
      await expect(items.nth(4)).toContainText("Key available · Not needed");
      await expect(items.nth(7)).toContainText("Weave AI · Not set up");
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      if (size.width < 1024) {
        const readinessBox = await page.locator(".ai-readiness").boundingBox();
        const providersBox = await page.locator(".ai-providers").boundingBox();
        expect(readinessBox!.y + readinessBox!.height).toBeLessThanOrEqual(
          providersBox!.y,
        );
      }
    });

    test("Publish and Authorize run once and the checklist rechecks", async ({
      page,
    }) => {
      const platform = await start(page, {
        connections: [ollamaConnection, openaiConnection],
        readiness: readiness({
          actions_published: {
            status: "action",
            detail: "actions_missing",
            fix: {
              kind: "operation",
              label: "Publish",
              operation: "ai_setup.publish",
            },
          },
          key_available: { status: "done", detail: "key_grant_available" },
          connection_authorized: {
            status: "action",
            detail: "authorization_missing",
            fix: {
              kind: "operation",
              label: "Authorize",
              operation: "ai_setup.grant",
            },
          },
        }),
      });
      const publish = page.getByRole("button", {
        name: "Publish",
        exact: true,
      });
      await publish.dblclick();
      await expect(page.locator(".readiness-row").nth(1)).toContainText(
        "AI actions published · Published",
      );
      expect(platform.publishes).toBe(1);
      await page
        .getByRole("button", { name: "Authorize", exact: true })
        .click();
      await expect(page.locator(".readiness-row").nth(5)).toContainText(
        "Connection authorized · Authorized",
      );
      expect(platform.grants).toEqual([
        { connection_revision_id: OPENAI_REVISION },
      ]);
    });
  });

test("a viewer sees the checklist without setup controls", async ({ page }) => {
  await start(page, {
    capabilities: ["catalog.read", "status.read"],
    connections: [ollamaConnection],
    readiness: readiness({
      connection: {
        status: "unknown",
        detail: "requires_deployer",
        fix: { kind: "ask", label: "Requires deployer" },
      },
      key_available: {
        status: "unknown",
        detail: "requires_tenant_admin",
        fix: { kind: "ask", label: "Requires tenant_admin" },
      },
      connection_authorized: {
        status: "unknown",
        detail: "requires_tenant_admin",
        fix: { kind: "ask", label: "Requires tenant_admin" },
      },
      model_responds: {
        status: "unknown",
        detail: "requires_tenant_admin",
        fix: { kind: "ask", label: "Requires tenant_admin" },
      },
      weave_ai: {
        status: "unknown",
        detail: "requires_lumi_user",
        fix: { kind: "ask", label: "Requires lumi_user" },
      },
    }),
  });
  await expect(page.locator(".readiness-row").nth(4)).toContainText(
    "Key available · Requires Tenant administrator",
  );
  await expect(page.locator(".readiness-row").nth(7)).toContainText(
    "Weave AI · Requires Weave AI user",
  );
  expect(await page.locator(".ai-readiness").textContent()).not.toContain(
    "tenant_admin",
  );
  await expect(page.getByRole("button", { name: /^Connect / })).toHaveCount(0);
  await expect(page.locator(".ai-providers")).toContainText(
    "Seeing AI connections requires the Deployer role.",
  );
});

test("the operator command is copied and the workers link opens Operate", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await start(page, {
    readiness: readiness({
      worker_installed: {
        status: "action",
        detail: "worker_missing",
        fix: {
          kind: "command",
          label: "Ask an operator to enable AI",
          command: "weave platform ai enable --ollama auto",
        },
      },
      worker_online: {
        status: "action",
        detail: "worker_offline",
        fix: {
          kind: "navigate",
          label: "Open workers",
          route: "operate/workers",
        },
      },
    }),
  });
  await page
    .getByRole("button", {
      name: "Copy weave platform ai enable --ollama auto",
    })
    .click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    "weave platform ai enable --ollama auto",
  );
  await page.getByRole("button", { name: "Open workers" }).click();
  await expect(page).toHaveURL(/\/operate\/workers$/);
});

test("saves Weave AI on a cloud connection with a suggested model, keeping its limits", async ({
  page,
}) => {
  const platform = await start(page, {
    connections: [ollamaConnection, openaiConnection],
    models: [
      {
        name: "gpt-5-mini",
        approved: true,
        available: true,
        tools: "yes",
        context_tokens: 128000,
      },
    ],
  });
  const card = page.locator("weave-ai-card");
  await expect(card.getByRole("heading", { name: "Weave AI" })).toBeVisible();
  await card.getByLabel("Connection", { exact: true }).click();
  await expect(
    page.getByRole("option", { name: /ollama-local/ }),
  ).toHaveAttribute("aria-disabled", "true");
  await page.getByRole("option", { name: /openai · revision 1/ }).click();
  await expect(card.getByLabel("Model", { exact: true })).toHaveValue(
    "gpt-5-mini",
  );
  await card.getByRole("button", { name: "Save Weave AI settings" }).click();
  await expect.poll(() => platform.lumiWrites.length).toBe(1);
  expect(platform.lumiWrites[0]).toEqual({
    ifMatch: null,
    body: {
      enabled: true,
      connection_revision_id: OPENAI_REVISION,
      profile: {
        options: { max_tokens: 1024 },
        reasoning: { pattern: "none", maxSteps: 6 },
        maxCalls: 8,
        timeoutSeconds: 180,
        provider: "openai-responses",
        model: "gpt-5-mini",
        outputSchema: { type: "object" },
      },
    },
  });
  await expect.poll(() => platform.readinessReads).toBeGreaterThan(1);
});

test("explains that connections without a key can't be used for Weave AI yet", async ({
  page,
}) => {
  await start(page, { connections: [ollamaConnection] });
  await expect(page.locator("weave-ai-card")).toContainText(
    "Weave AI needs an AI connection with a secret handle. Connections without a key can't be used for Weave AI yet.",
  );
});

test("a Weave AI manager without connection management sees why the card is read-only", async ({
  page,
}) => {
  await start(page, {
    capabilities: ["catalog.read", "status.read", "lumi.use", "lumi.manage"],
  });
  await expect(page.locator("weave-ai-card")).toContainText(
    "Choosing Weave AI's connection also requires the Tenant administrator role.",
  );
  await expect(
    page.getByRole("button", { name: "Save Weave AI settings" }),
  ).toHaveCount(0);
});

test("saving Weave AI on a configured connection keeps its limits and sends its revision", async ({
  page,
}) => {
  const platform = await start(page, {
    connections: [ollamaConnection, openaiConnection],
    models: [
      {
        name: "gpt-5-mini",
        approved: true,
        available: true,
        tools: "yes",
        context_tokens: 128000,
      },
    ],
    lumi: {
      enabled: true,
      connection_revision_id: OPENAI_REVISION,
      profile: {
        provider: "openai-responses",
        model: "gpt-5-mini",
        options: { max_tokens: 512 },
        reasoning: { pattern: "none", maxSteps: 6 },
        maxCalls: 4,
        timeoutSeconds: 120,
        outputSchema: { type: "object" },
      },
      revision: 7,
    },
  });
  const card = page.locator("weave-ai-card");
  await expect(card.getByLabel("Model", { exact: true })).toHaveValue(
    "gpt-5-mini",
  );
  await card.getByRole("switch", { name: "Turn on Weave AI" }).uncheck();
  await card.getByRole("button", { name: "Save Weave AI settings" }).click();
  await expect.poll(() => platform.lumiWrites.length).toBe(1);
  expect(platform.lumiWrites[0].ifMatch).toBe('"7"');
  expect(platform.lumiWrites[0].body).toMatchObject({
    enabled: false,
    profile: {
      options: { max_tokens: 512 },
      maxCalls: 4,
      timeoutSeconds: 120,
      model: "gpt-5-mini",
    },
  });
});

const GPT = {
  name: "gpt-5-mini",
  approved: true,
  available: true,
  tools: "yes",
  context_tokens: 128000,
};
const pinnedOpenAi = {
  enabled: true,
  connection_revision_id: OPENAI_REVISION,
  profile: {
    provider: "openai-responses",
    model: "gpt-5-mini",
    outputSchema: { type: "object" },
  },
  revision: 7,
};

test("after its connection gets a new revision, Weave AI says so and saves on the new one", async ({
  page,
}) => {
  const platform = await start(page, {
    connections: [ollamaConnection, openaiConnection],
    models: [GPT],
    lumi: pinnedOpenAi,
  });
  const card = page.locator("weave-ai-card");
  await expect(card.getByLabel("Model", { exact: true })).toHaveValue(
    "gpt-5-mini",
  );
  // Edit the connection Weave AI uses: saving adds revision 2, and the card
  // reads Weave AI's settings again.
  let reads = 0;
  page.on("request", (request) => {
    if (
      request.method() === "GET" &&
      request.url().endsWith("/lumi/configuration")
    )
      reads++;
  });
  await page.getByRole("button", { name: "Test openai" }).click();
  const form = sheet(page, "openai");
  await form.getByLabel("Secret handle").fill("openai-team-key");
  await form.getByRole("button", { name: "Save as a new revision" }).click();
  await expect(form.getByRole("status").last()).toContainText("revision 2");
  await page.keyboard.press("Escape");
  await expect(form).toHaveCount(0);
  await expect.poll(() => reads).toBeGreaterThan(0);

  await expect(card).toContainText(
    "Weave AI uses revision 1 of openai; saving moves it to revision 2.",
  );
  await expect(card.getByLabel("Connection", { exact: true })).toHaveValue(
    "openai · revision 2",
  );
  await card.getByRole("switch", { name: "Turn on Weave AI" }).uncheck();
  await card.getByRole("button", { name: "Save Weave AI settings" }).click();
  await expect.poll(() => platform.lumiWrites.length).toBe(1);
  const second = platform.connections.find(
    (c) => c["name"] === "openai" && c["revision"] === 2,
  )!;
  expect(platform.lumiWrites[0].ifMatch).toBe('"7"');
  expect(platform.lumiWrites[0].body).toMatchObject({
    enabled: false,
    connection_revision_id: second["id"],
    profile: { model: "gpt-5-mini" },
  });
});

test("when Weave AI's connection can't be found, the card asks for one before saving", async ({
  page,
}) => {
  const platform = await start(page, {
    connections: [ollamaConnection],
    models: [GPT],
    lumi: pinnedOpenAi,
  });
  const card = page.locator("weave-ai-card");
  await expect(card).toContainText(
    "Weave AI's connection is no longer available. Choose a connection, then save.",
  );
  await expect(
    card.getByRole("button", { name: "Save Weave AI settings" }),
  ).toBeDisabled();
  expect(platform.lumiWrites).toEqual([]);
});

/** The accessible name of the focused element, when it sits in the AI connection sheet. */
const focusInSheet = (page: Page) =>
  page.evaluate(() => {
    const active = document.activeElement as HTMLElement | null;
    if (!active?.closest('[role="dialog"]')) return null;
    return (
      active.getAttribute("aria-label") ??
      active.textContent?.trim() ??
      active.tagName
    );
  });

test("keeps keyboard focus in the sheet while a test runs, ends and saves", async ({
  page,
}) => {
  const platform = await start(page, {
    connections: [ollamaConnection],
    testDelay: 60_000,
  });
  await page.getByRole("button", { name: "Test ollama-local" }).click();
  const form = sheet(page, "ollama-local");
  const testButton = form.getByRole("button", { name: "Test connection" });
  await expect(testButton).toBeEnabled();
  await testButton.focus();
  await page.keyboard.press("Enter");
  await expect(form.getByRole("button", { name: "Cancel test" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(
    form.getByRole("status").filter({ hasText: "canceled" }),
  ).toHaveText("Test canceled.");
  await expect(testButton).toBeFocused();
  platform.testDelay = 0;
  await page.keyboard.press("Enter");
  await expect(
    form.getByRole("status").filter({ hasText: "Connected." }),
  ).toBeVisible();
  expect(await focusInSheet(page)).not.toBeNull();
  await page.keyboard.press("Tab");
  expect(await focusInSheet(page)).not.toBeNull();
  await page.keyboard.press("Escape");
  await expect(form).toHaveCount(0);

  await page.getByRole("button", { name: "Connect OpenAI" }).click();
  const created = sheet(page);
  await created.getByRole("button", { name: "Save connection" }).focus();
  await page.keyboard.press("Enter");
  await expect(created.getByLabel("Model", { exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("opening the sheet moves keyboard focus into it, onto the chosen provider", async ({
  page,
}) => {
  await start(page, {});
  await page.getByRole("button", { name: "Connect Ollama" }).click();
  const form = sheet(page);
  await expect(
    form.getByRole("button", { name: "Save connection" }),
  ).toBeEnabled();
  await expect(
    form
      .getByRole("group", { name: "Provider" })
      .getByRole("radio", { checked: true }),
  ).toBeFocused();
  for (let press = 0; press < 3; press++) {
    await page.keyboard.press("Tab");
    expect(await focusInSheet(page)).not.toBeNull();
  }
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "Connect Ollama" }),
  ).toBeFocused();
});

test("a dialog takes focus back when its focused control is disabled", async ({
  page,
}) => {
  await start(page, { connections: [ollamaConnection], testDelay: 60_000 });
  await page.getByRole("button", { name: "Test ollama-local" }).click();
  const form = sheet(page, "ollama-local");
  await expect(
    form.getByRole("button", { name: "Test connection" }),
  ).toBeEnabled();
  // A control that disables itself while focused, as a busy button does.
  await page.evaluate(() => {
    const button = [
      ...document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button'),
    ].find((item) => item.textContent?.trim() === "Test connection")!;
    button.focus();
    button.disabled = true;
  });
  await expect.poll(() => focusInSheet(page)).not.toBeNull();
  await page.keyboard.press("Escape");
  await expect(form).toHaveCount(0);
});

test("the desktop sheet keeps its footer in one row at the bottom edge", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await start(page);
  const form = await newConnection(page, "Ollama");
  await expect(
    form.getByRole("button", { name: "Save connection" }),
  ).toBeVisible();
  const measure = () =>
    page.evaluate(() => {
      const panel = document.querySelector(".modal-panel.side")!;
      const footer = panel.querySelector(".dialog-footer")!;
      const buttons = [...footer.querySelectorAll("button")].map((button) =>
        button.getBoundingClientRect(),
      );
      return {
        panelBottom: panel.getBoundingClientRect().bottom,
        footerBottom: footer.getBoundingClientRect().bottom,
        tops: buttons.map((box) => Math.round(box.top)),
        heights: buttons.map((box) => Math.round(box.height)),
      };
    });
  for (const step of ["new", "saved"]) {
    if (step === "saved")
      await form.getByRole("button", { name: "Save connection" }).click();
    if (step === "saved")
      await expect(form.getByLabel("Model", { exact: true })).toHaveValue(
        "qwen3:4b",
      );
    const box = await measure();
    expect(box.tops).toHaveLength(2);
    expect(new Set(box.tops).size, step).toBe(1);
    expect(
      box.heights.every((height) => height === 36),
      step,
    ).toBe(true);
    expect(Math.abs(box.panelBottom - box.footerBottom), step).toBeLessThan(1);
  }
});

for (const size of [
  { width: 1440, height: 900, control: 36 },
  { width: 600, height: 500, control: 44 },
])
  test(`the Model field lines up with Refresh models at ${size.width}x${size.height}`, async ({
    page,
  }) => {
    await page.setViewportSize(size);
    await start(page, { connections: [ollamaConnection] });
    await page.getByRole("button", { name: "Test ollama-local" }).click();
    const form = sheet(page, "ollama-local");
    await expect(form.getByLabel("Model", { exact: true })).toHaveValue(
      "qwen3:4b",
    );
    const box = await page.evaluate(() => {
      const panel = document.querySelector(".modal-panel.side")!;
      const rect = (selector: string) =>
        panel.querySelector(selector)!.getBoundingClientRect();
      const field = rect(".model-row .choice-field");
      const refresh = rect('[aria-label="Refresh models"]');
      const name = rect("#ai-connection-name");
      return { field, refresh, name };
    });
    expect(Math.round(box.field.height)).toBe(size.control);
    expect(Math.round(box.refresh.height)).toBe(size.control);
    expect(Math.abs(box.field.bottom - box.refresh.bottom)).toBeLessThan(0.5);
    expect(Math.abs(box.field.left - box.name.left)).toBeLessThan(0.5);
  });

test("the sheet takes the connection's name once it is saved, and names its test section once", async ({
  page,
}) => {
  await start(page);
  const form = await newConnection(page, "Ollama");
  await expect(form.getByRole("heading", { level: 2 })).toHaveText(
    "New AI connection",
  );
  await form.getByRole("button", { name: "Save connection" }).click();
  await expect(form.getByRole("heading", { level: 2 })).toHaveText(
    "ollama-local",
  );
  await expect(form.getByRole("heading", { level: 3 })).toHaveText(
    "Test a model",
  );
  await expect(form.getByText("Model", { exact: true })).toHaveCount(1);
});
