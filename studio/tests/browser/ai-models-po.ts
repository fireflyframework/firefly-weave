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
// A mocked platform for Settings › AI models: approved endpoints, discovery,
// readiness, setup fixes, connections and Weave AI, recording every request
// the screen sends. Routes registered here win over support.ts's catch-all.
import { expect, type Locator, type Page, type Route } from "@playwright/test";
import { allCapabilities } from "./support";

const ENV =
  "**/studio/api/api/v1/tenants/tenant/projects/project/environments/development";
const PROJECT = "**/studio/api/api/v1/tenants/tenant/projects/project";
export const AI_CAPABILITIES = [
  ...allCapabilities,
  "status.read",
  "connection.bind",
  "lumi.use",
  "lumi.manage",
];
export const CONNECTOR_ID = "11111111-1111-4111-8111-111111111111";
export const OLLAMA_REVISION = "22222222-2222-4222-8222-222222222222";
export const OPENAI_REVISION = "33333333-3333-4333-8333-333333333333";
export const OLLAMA_ENDPOINT = {
  id: "ollama-local",
  label: "Ollama (Weave-managed)",
  url: "http://ollama:11434/v1",
  providers: ["openai-chat"],
  compat: "ollama",
  credential: "none",
  models: "served",
  context_tokens: 8192,
  development_only: true,
};
export const OPENAI_ENDPOINT = {
  id: "openai",
  label: "OpenAI",
  url: "https://api.openai.com/v1",
  providers: ["openai-chat", "openai-responses"],
  compat: null,
  credential: "required",
  models: "listed",
  context_tokens: 128000,
  development_only: false,
};
const revision = (
  id: string,
  name: string,
  config: object,
  apiKey: string,
  origin: string,
) => ({
  id,
  name,
  revision: 1,
  connector: "weave-agentic-provider@1.0.0",
  connector_digest: "digest",
  adapter: "agentic",
  connector_version_id: CONNECTOR_ID,
  config: { ...config, secretSlot: "apiKey" },
  secretRef: { apiKey },
  allowed_destinations: [origin],
});
export const ollamaConnection = revision(
  OLLAMA_REVISION,
  "ollama-local",
  { provider: "openai-chat", endpoint: "http://ollama:11434/v1" },
  "no-credential",
  "http://ollama:11434",
);
export const openaiConnection = revision(
  OPENAI_REVISION,
  "openai",
  { provider: "openai-responses", endpoint: "https://api.openai.com/v1" },
  "openai-api-key",
  "https://api.openai.com",
);
export const QWEN = {
  name: "qwen3:4b",
  approved: true,
  available: true,
  tools: "yes",
  context_tokens: 8192,
  size_bytes: 2_500_000_000,
  family: "qwen3",
};
export const GEMMA = {
  name: "gemma3:270m",
  approved: true,
  available: true,
  tools: "no",
  context_tokens: 8192,
  size_bytes: 291_000_000,
  family: "gemma3",
};
type Item = {
  id: string;
  status: string;
  detail: string;
  fix?: Record<string, unknown>;
};
/** A local platform after `weave platform ai enable`: everything but Weave AI. */
export const READY_LOCAL: Item[] = [
  { id: "worker_installed", status: "done", detail: "worker_installed" },
  { id: "actions_published", status: "done", detail: "actions_published" },
  { id: "worker_online", status: "done", detail: "worker_online" },
  { id: "connection", status: "done", detail: "connection_available" },
  { id: "key_available", status: "not_needed", detail: "no_credential" },
  {
    id: "connection_authorized",
    status: "not_needed",
    detail: "no_credential",
  },
  { id: "model_responds", status: "done", detail: "test_passed" },
  {
    id: "weave_ai",
    status: "action",
    detail: "weave_ai_missing",
    fix: { kind: "navigate", label: "Open AI settings", route: "settings/ai" },
  },
];
export function readiness(overrides: Record<string, Partial<Item>> = {}) {
  return {
    items: READY_LOCAL.map((item) => ({
      ...item,
      ...(overrides[item.id] ?? {}),
    })),
  };
}
export interface AiPlatform {
  capabilities: string[];
  readiness: { items: Item[] };
  connections: Record<string, unknown>[];
  models: Record<string, unknown>[];
  /** The test answer: a result body, or a refusal with its status. */
  testAnswer:
    | Record<string, unknown>
    | { status: number; body: Record<string, unknown> };
  testDelay: number;
  /** Answers for the next connection creates, in order; then each create succeeds. */
  createAnswers: { status: number; body: Record<string, unknown> }[];
  /** Answers for the next grants, in order; then each grant succeeds. */
  grantAnswers: { status: number; body: Record<string, unknown> }[];
  lumi: Record<string, unknown> | null;
  // Recorded requests.
  creates: { body: Record<string, unknown>; key: string }[];
  tests: { url: string; body: Record<string, unknown> }[];
  modelRequests: string[];
  publishes: number;
  grants: Record<string, unknown>[];
  lumiWrites: { body: Record<string, unknown>; ifMatch: string | null }[];
  readinessReads: number;
}
export async function mockAiPlatform(
  page: Page,
  options: Partial<AiPlatform> = {},
): Promise<AiPlatform> {
  const state: AiPlatform = {
    capabilities: AI_CAPABILITIES,
    readiness: readiness(),
    connections: [],
    models: [QWEN, GEMMA],
    testAnswer: {
      ok: true,
      code: "ok",
      latency_ms: 2400,
      model: "qwen3:4b",
      tool_calling: "supported",
    },
    testDelay: 0,
    createAnswers: [],
    grantAnswers: [],
    lumi: null,
    creates: [],
    tests: [],
    modelRequests: [],
    publishes: 0,
    grants: [],
    lumiWrites: [],
    readinessReads: 0,
    ...options,
  };
  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, json: body });
  await page.route(`${ENV}/ai/endpoints`, (r) =>
    json(r, {
      policy: "loaded",
      endpoints: [OLLAMA_ENDPOINT, OPENAI_ENDPOINT],
    }),
  );
  await page.route(`${ENV}/ai/readiness`, (r) => {
    state.readinessReads++;
    return json(r, state.readiness);
  });
  await page.route(`${ENV}/ai/models?*`, (r) => {
    state.modelRequests.push(r.request().url());
    return json(r, {
      approval: "served",
      discovery: "ok",
      discovered_at: "2026-10-13T10:00:00Z",
      models: state.models,
    });
  });
  await page.route(`${ENV}/ai/connections/*/test`, async (r) => {
    state.tests.push({
      url: r.request().url(),
      body: r.request().postDataJSON(),
    });
    if (state.testDelay)
      await new Promise((resolve) => setTimeout(resolve, state.testDelay));
    const answer = state.testAnswer;
    await (
      "status" in answer && "body" in answer
        ? json(r, answer.body, answer.status as number)
        : json(r, answer)
    ).catch(() => undefined);
  });
  await page.route(`${ENV}/ai/setup/publish`, (r) => {
    state.publishes++;
    state.readiness = {
      items: state.readiness.items.map((item) =>
        item.id === "actions_published"
          ? { id: item.id, status: "done", detail: "actions_published" }
          : item,
      ),
    };
    return json(r, { connector: {}, actions: [{}] });
  });
  await page.route(`${ENV}/ai/setup/grant`, (r) => {
    state.grants.push(r.request().postDataJSON());
    const scripted = state.grantAnswers.shift();
    if (scripted) return json(r, scripted.body, scripted.status);
    state.readiness = {
      items: state.readiness.items.map((item) =>
        item.id === "connection_authorized"
          ? { id: item.id, status: "done", detail: "connection_authorized" }
          : item,
      ),
    };
    return json(r, { granted: true });
  });
  await page.route(`${ENV}/connections?*`, (r) =>
    json(r, { items: state.connections, next_cursor: null }),
  );
  await page.route(`${ENV}/connections`, (r) => {
    if (r.request().method() !== "POST") return r.fallback();
    const body = r.request().postDataJSON() as Record<string, unknown>;
    state.creates.push({
      body,
      key: r.request().headers()["idempotency-key"] ?? "",
    });
    const scripted = state.createAnswers.shift();
    if (scripted) return json(r, scripted.body, scripted.status);
    // Saving a name again adds its next revision; earlier ones stay readable.
    const earlier = state.connections.filter((c) => c["name"] === body["name"]);
    const revision =
      Math.max(0, ...earlier.map((c) => Number(c["revision"]))) + 1;
    const first =
      body["name"] === "ollama-local" ? OLLAMA_REVISION : OPENAI_REVISION;
    const created = {
      ...body,
      id:
        revision === 1
          ? first
          : `${first.slice(0, -2)}${String(revision).padStart(2, "0")}`,
      revision,
      connector: "weave-agentic-provider@1.0.0",
      connector_digest: "digest",
      adapter: "agentic",
    };
    state.connections = [...state.connections, created];
    return json(r, created, 201);
  });
  await page.route(`${ENV}/connections/*`, (r) => {
    const id = new URL(r.request().url()).pathname.split("/").pop();
    const found = state.connections.find((c) => c["id"] === id);
    if (r.request().method() !== "GET" || !found) return r.fallback();
    return json(r, found);
  });
  await page.route(`${ENV}/bindable-connections?*`, (r) =>
    json(r, {
      items: state.connections.map((c) => ({
        connection_id: c["id"],
        revision_id: c["id"],
        name: c["name"],
        connector: "weave-agentic-provider@1.0.0",
        label: c["name"],
        enabled: true,
        provider: (c["config"] as Record<string, string>)["provider"],
        endpoint_origin: new URL(
          (c["config"] as Record<string, string>)["endpoint"],
        ).origin,
      })),
      next_cursor: null,
    }),
  );
  await page.route(`${PROJECT}/connectors?*`, (r) =>
    json(r, {
      items: [
        { id: CONNECTOR_ID, name: "weave-agentic-provider", version: "1.0.0" },
      ],
      next_cursor: null,
    }),
  );
  await page.route(`${ENV}/lumi/configuration`, (r) => {
    if (r.request().method() === "PUT") {
      const body = r.request().postDataJSON() as Record<string, unknown>;
      state.lumiWrites.push({
        body,
        ifMatch: r.request().headers()["if-match"] ?? null,
      });
      state.lumi = {
        ...body,
        revision: ((state.lumi?.["revision"] as number) ?? 0) + 1,
      };
      return json(r, state.lumi);
    }
    return state.lumi
      ? json(r, state.lumi)
      : json(
          r,
          { code: "WV-NOT-FOUND", message: "Weave AI is not configured" },
          404,
        );
  });
  await page.route(`${ENV}/lumi/status`, (r) =>
    json(
      r,
      state.lumi
        ? {
            configured: true,
            provider: "openai-responses",
            model: "gpt-5-mini",
          }
        : { configured: false },
    ),
  );
  await page.route("**/studio/contracts/lumi-configuration", (r) =>
    json(r, {
      $defs: {
        LumiProfile: {
          properties: { outputSchema: { const: { type: "object" } } },
        },
      },
    }),
  );
  return state;
}
/** Settings › AI models, loaded. */
export async function openAiModels(page: Page) {
  await page.goto("/settings/ai");
  await expect(page.getByRole("tab", { name: "AI models" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
}
/** The AI connection side sheet, by its title when one is given. */
export function sheet(page: Page, name?: string): Locator {
  return name
    ? page.getByRole("dialog", { name })
    : page.locator("weave-ai-connection-sheet").getByRole("dialog");
}
