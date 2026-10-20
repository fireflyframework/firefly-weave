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
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import type { AiConnection } from "../src/app/integrations/ai-provider-connection";
import type {
  AiEndpoint,
  AiModel,
  Readiness,
} from "../src/app/integrations/ai-setup-api";
import {
  AI_COPY,
  FieldError,
  PLAIN_HTTP_REFUSED,
  aiMessage,
  allReady,
  cardOf,
  connectionRequest,
  defaultModel,
  draftFor,
  fixAction,
  keyless,
  secretSetCommand,
  modelOptions,
  presetsFor,
  readinessRows,
  refusal,
  sheetProblems,
  testView,
  weaveAiBody,
  weaveAiChoices,
} from "../src/app/integrations/ai-models-model";
import { python, pythonAvailable } from "./python-path";

const ollama: AiEndpoint = {
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
const openai: AiEndpoint = {
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
const endpoints = [ollama, openai];
const keylessConnection: AiConnection = {
  id: "r1",
  name: "ollama-local",
  revision: 1,
  connector: "weave-agentic-provider@1.0.0",
  config: { provider: "openai-chat", endpoint: "http://ollama:11434/v1" },
  secretRef: { apiKey: "no-credential" },
};
const cloudConnection: AiConnection = {
  id: "r2",
  name: "openai",
  revision: 3,
  connector: "weave-agentic-provider@1.0.0",
  config: {
    provider: "openai-responses",
    endpoint: "https://api.openai.com/v1",
  },
  secretRef: { apiKey: "openai-api-key" },
};
const qwen: AiModel = {
  name: "qwen3:4b",
  approved: true,
  available: true,
  tools: "yes",
  context_tokens: 8192,
  size_bytes: 2_500_000_000,
};
const gemma: AiModel = {
  name: "gemma3:270m",
  approved: true,
  available: true,
  tools: "no",
  context_tokens: 8192,
  size_bytes: 291_000_000,
};

describe("provider presets and connection requests", () => {
  it("offers only Ollama endpoints on the Ollama card and cloud endpoints elsewhere", () => {
    expect(presetsFor("ollama", endpoints)).toEqual([ollama]);
    expect(presetsFor("openai", endpoints)).toEqual([openai]);
    expect(presetsFor("anthropic", endpoints)).toEqual([]);
  });
  it("builds a keyless Ollama connection from an approved preset", () => {
    const draft = draftFor("ollama", endpoints);
    expect(keyless(draft, endpoints)).toBe(true);
    expect(connectionRequest(draft, "connector", endpoints, "loaded")).toEqual({
      name: "ollama-local",
      connector_version_id: "connector",
      config: {
        provider: "openai-chat",
        endpoint: "http://ollama:11434/v1",
        secretSlot: "apiKey",
      },
      secretRef: { apiKey: "no-credential" },
      allowed_destinations: ["http://ollama:11434"],
    });
  });
  it("shows the operator command only for a handle, never for a pasted key", () => {
    expect(secretSetCommand("team-openai")).toBe(
      "weave platform secret set --handle team-openai",
    );
    const key = `sk-proj-${"Ab3dE".repeat(19)}x`;
    expect(secretSetCommand(key)).toBe(
      "weave platform secret set --handle HANDLE",
    );
    expect(secretSetCommand("")).toBe(
      "weave platform secret set --handle HANDLE",
    );
  });
  it("asks for a handle on a custom address even when it equals a keyless preset", () => {
    const draft = {
      ...draftFor("ollama", endpoints),
      custom: true,
      endpoint: "http://ollama:11434/v1",
    };
    expect(keyless(draft, endpoints)).toBe(false);
  });
  it.each([
    "http://10.0.0.5:11434/v1",
    "http://localhost:11434/v1",
    "http://ollama.example.com/v1",
  ])("refuses plain HTTP that no preset approves: %s", (endpoint) => {
    const draft = { ...draftFor("ollama", endpoints), custom: true, endpoint };
    expect(() =>
      connectionRequest(draft, "connector", endpoints, "loaded"),
    ).toThrow(PLAIN_HTTP_REFUSED);
  });
  it("asks for a secret handle on credentialed endpoints and never sends the reserved one", () => {
    const draft = draftFor("openai", endpoints);
    expect(draft).toMatchObject({
      name: "openai",
      provider: "openai-responses",
      endpoint: "https://api.openai.com/v1",
      handle: "openai-api-key",
    });
    expect(
      connectionRequest(draft, "connector", endpoints, "loaded").secretRef,
    ).toEqual({ apiKey: "openai-api-key" });
    expect(
      connectionRequest(
        { ...draft, handle: "a".repeat(64) },
        "connector",
        endpoints,
        "loaded",
      ).secretRef,
    ).toEqual({ apiKey: "a".repeat(64) });
    // The platform's handle rule: 1 to 64 lowercase letters, digits, ".", "_"
    // or "-". Real keys (mixed case, long) never leave the browser.
    for (const handle of [
      "no-credential",
      "sk live key",
      "",
      `sk-proj-${"Ab3dE".repeat(19)}x`,
      `sk-ant-api03-${"Zq9".repeat(30)}`,
      "OpenAI-Key",
      "a".repeat(65),
    ]) {
      const error = (() => {
        try {
          connectionRequest(
            { ...draft, handle },
            "connector",
            endpoints,
            "loaded",
          );
        } catch (caught) {
          return caught;
        }
      })();
      expect(error).toBeInstanceOf(FieldError);
      expect((error as FieldError).field).toBe("handle");
    }
  });
  it("accepts only approved cloud endpoints while a policy is loaded", () => {
    const draft = {
      ...draftFor("openai", endpoints),
      custom: true,
      endpoint: "https://proxy.example.com/v1",
    };
    expect(() =>
      connectionRequest(draft, "connector", endpoints, "loaded"),
    ).toThrow("approved endpoint");
    expect(
      connectionRequest(draft, "connector", endpoints, "absent").config
        .endpoint,
    ).toBe("https://proxy.example.com/v1");
  });
  it.each([
    "https://user:password@model.example",
    "https://model.example/?api-key=secret",
    "https://model.example/#secret",
    "ftp://model.example",
  ])("refuses an endpoint carrying anything but an address: %s", (endpoint) => {
    const draft = { ...draftFor("openai", []), custom: true, endpoint };
    expect(() => connectionRequest(draft, "c", [], "absent")).toThrow(
      FieldError,
    );
  });
  it("requires Azure's API version and keeps it only for Azure", () => {
    const azure = {
      ...draftFor("azure", []),
      endpoint: "https://team.openai.azure.com",
      custom: true,
    };
    expect(() => connectionRequest(azure, "c", [], "absent")).toThrow(
      "API version",
    );
    expect(
      connectionRequest(
        { ...azure, apiVersion: "2025-04-01-preview" },
        "c",
        [],
        "absent",
      ).config,
    ).toMatchObject({
      provider: "azure-responses",
      apiVersion: "2025-04-01-preview",
    });
  });
  it("needs the published AI connector before saving", () => {
    expect(() =>
      connectionRequest(draftFor("ollama", endpoints), "", endpoints, "loaded"),
    ).toThrow("Publish");
  });
  it("puts each server problem on its field", () => {
    const detail = {
      code: "WV-CONNECTION",
      result: {
        diagnostics: [
          {
            path: "/config/endpoint",
            code: "WV-CONNECTION-CONFIG",
            message: "Plain HTTP needs a private-origin entry.",
          },
          {
            path: "/secretRef/apiKey",
            code: "WV-CONNECTION-SECRET",
            message: "The secret handle is not available.",
          },
          {
            path: "/name",
            code: "WV-CONNECTION-CONFIG",
            message: "Choose another name.",
          },
        ],
      },
    };
    expect(sheetProblems(detail).map((p) => p.field)).toEqual([
      "endpoint",
      "handle",
      "name",
    ]);
  });
  it("knows which card a saved connection belongs to", () => {
    expect(cardOf(keylessConnection, endpoints)).toBe("ollama");
    expect(cardOf(cloudConnection, endpoints)).toBe("openai");
    expect(
      cardOf(
        {
          ...cloudConnection,
          config: {
            provider: "anthropic",
            endpoint: "https://api.anthropic.com",
          },
        },
        endpoints,
      ),
    ).toBe("anthropic");
  });
});

describe("models", () => {
  it("suggests qwen3:4b, then a tool-capable model, and never one that is not approved or installed", () => {
    expect(defaultModel([gemma, qwen])).toBe("qwen3:4b");
    expect(defaultModel([gemma, { ...qwen, name: "llama3.2:3b" }])).toBe(
      "llama3.2:3b",
    );
    expect(defaultModel([{ ...qwen, approved: false }, gemma])).toBe(
      "gemma3:270m",
    );
    expect(defaultModel([{ ...qwen, available: false }])).toBe("");
    expect(defaultModel([gemma, qwen], "gemma3:270m")).toBe("gemma3:270m");
  });
  it("describes each model with size, tools and context, and disables the rest", () => {
    expect(modelOptions([qwen, { ...gemma, approved: false }])).toEqual([
      {
        value: "qwen3:4b",
        label: "qwen3:4b",
        description: "2.5 GB · tools · 8k context",
        disabled: false,
      },
      {
        value: "gemma3:270m",
        label: "gemma3:270m",
        description: "Not approved",
        disabled: true,
      },
    ]);
  });
});

describe("test states", () => {
  const origin = "http://ollama:11434";
  it("reads a tool-capable success, a no-tools warning and a failure", () => {
    expect(
      testView(
        {
          kind: "done",
          origin,
          result: {
            ok: true,
            code: "ok",
            latency_ms: 2400,
            model: "qwen3:4b",
            tool_calling: "supported",
          },
        },
        "ollama-local",
      ).text,
    ).toBe("Connected. qwen3:4b answered in 2.4 s and can call tools.");
    const warning = testView(
      {
        kind: "done",
        origin,
        result: {
          ok: true,
          code: "ok",
          latency_ms: 800,
          model: "gemma3:270m",
          tool_calling: "unsupported",
        },
      },
      "ollama-local",
    );
    expect(warning).toMatchObject({
      tone: "warning",
      text: "Connected. gemma3:270m answered in 0.8 s but cannot call tools. Use it for AI tasks, not AI agents.",
    });
    const failed = testView(
      {
        kind: "done",
        origin,
        result: {
          ok: false,
          code: "LLM_MODEL_NOT_FOUND",
          latency_ms: 30,
          model: "qwen3:4b",
          tool_calling: "unknown",
        },
      },
      "ollama-local",
    );
    expect(failed.text).toBe(
      "qwen3:4b is not installed on http://ollama:11434. Pull it with `ollama pull qwen3:4b`.",
    );
    expect(failed.details).toEqual([
      ["Code", "LLM_MODEL_NOT_FOUND"],
      ["Endpoint", "http://ollama:11434"],
      ["Latency", "30 ms"],
    ]);
    expect(
      testView({ kind: "running", model: "qwen3:4b" }, "ollama-local").text,
    ).toBe("Calling qwen3:4b on ollama-local…");
    expect(testView({ kind: "idle" }, "x").text).toBe("Not tested");
  });
  it("names the gateway's test budget when a model does not answer in time", () => {
    const timeout = testView(
      {
        kind: "done",
        origin,
        result: {
          ok: false,
          code: "LLM_TIMEOUT",
          latency_ms: null,
          model: "qwen3:4b",
          tool_calling: "unknown",
        },
      },
      "ollama-local",
    );
    expect(timeout.text).toContain("120 seconds");
  });
  it("says there was no response when the request never reached the platform", () => {
    const offline = refusal(new TypeError("Failed to fetch"), origin);
    expect(testView(offline, "x").details).toContainEqual([
      "HTTP status",
      "No response",
    ]);
  });
  it("explains a missing gateway and the test limit without response bodies", () => {
    const missing = refusal(
      new ApiError(503, {
        code: "WV-AI-GATEWAY-MISSING",
        message: "secret detail from a body",
      }),
      origin,
    );
    expect(missing).toMatchObject({
      kind: "refused",
      message:
        "Tests need the AI gateway. Run `weave platform ai enable`, or ask an operator to deploy it.",
    });
    expect(JSON.stringify(testView(missing, "x"))).not.toContain(
      "secret detail",
    );
    expect(testView(missing, "x").details).toContainEqual([
      "HTTP status",
      "5xx",
    ]);
    expect(
      refusal(new ApiError(429, { code: "WV-AI-RATE-LIMITED" }), origin),
    ).toMatchObject({
      message: "Too many AI connection tests. Wait a minute, then test again.",
    });
  });
});

describe.skipIf(!pythonAvailable())("error copy matches the platform", () => {
  it("has the same text and placeholders as the server's table", () => {
    const output = execFileSync(
      python,
      [
        "-c",
        "import json; from firefly_weave.contracts.ai import AI_ERRORS, ai_message; " +
          "print(json.dumps({'copy': {c: e.message for c, e in AI_ERRORS.items()}, " +
          "'filled': {c: ai_message(c, model='qwen3:4b', endpoint='http://ollama:11434', n=8192) for c in AI_ERRORS}}))",
      ],
      { cwd: "..", encoding: "utf8" },
    );
    const server = JSON.parse(output) as {
      copy: Record<string, string>;
      filled: Record<string, string>;
    };
    expect(AI_COPY).toEqual(server.copy);
    for (const [code, text] of Object.entries(server.filled))
      expect(
        aiMessage(code, {
          model: "qwen3:4b",
          endpoint: "http://ollama:11434",
          n: 8192,
        }),
      ).toBe(text);
  });
});

describe("readiness", () => {
  const item = (id: string, status: string, detail: string, fix?: object) => ({
    id,
    status,
    detail,
    ...(fix ? { fix } : {}),
  });
  const local = {
    items: [
      item("worker_installed", "done", "worker_installed"),
      item("actions_published", "action", "actions_missing", {
        kind: "operation",
        label: "Publish",
        operation: "ai_setup.publish",
      }),
      item("worker_online", "done", "worker_online"),
      item("connection", "done", "connection_available"),
      item("key_available", "not_needed", "no_credential"),
      item("connection_authorized", "not_needed", "no_credential"),
      item("model_responds", "action", "test_required", {
        kind: "operation",
        label: "Test connection",
        operation: "ai_connections.test",
      }),
      item("weave_ai", "unknown", "requires_lumi_user", {
        kind: "ask",
        label: "Requires lumi_user",
      }),
    ],
  } as Readiness;
  it("labels each item, its state and who may fix it", () => {
    const rows = readinessRows(local, (role) =>
      role === "lumi_user" ? "Weave AI user" : role,
    );
    expect(rows.map((row) => [row.label, row.detail, row.icon])).toEqual([
      ["Agentic worker installed", "Installed", "ok"],
      ["AI actions published", "Not published yet", "warning"],
      ["Worker online", "Online", "ok"],
      ["AI connection", "Set up", "ok"],
      ["Key available", "Not needed", "off"],
      ["Connection authorized", "Not needed", "off"],
      ["Model responds", "Not tested yet", "warning"],
      ["Weave AI", "Requires Weave AI user", "help"],
    ]);
    expect(
      readinessRows(
        {
          items: local.items.map((i) =>
            i.id === "model_responds"
              ? { ...i, status: "done", detail: "test_passed" }
              : i,
          ),
        } as Readiness,
        (role) => role,
        { model: "qwen3:4b", latency_ms: 2400 },
      )[6].detail,
    ).toBe("qwen3:4b · 2.4 s");
  });
  it("is ready only when every item is done or not needed", () => {
    expect(allReady(local)).toBe(false);
    const done = {
      items: local.items.map((i) => ({
        ...i,
        status: i.status === "not_needed" ? "not_needed" : "done",
      })),
    } as Readiness;
    expect(allReady(done)).toBe(true);
    expect(
      allReady(
        {
          items: done.items.map((i) =>
            i.id === "weave_ai" ? { ...i, status: "action" } : i,
          ),
        } as Readiness,
        ["weave_ai"],
      ),
    ).toBe(true);
  });
  it("names the card an AI settings link goes to on the page itself", () => {
    const settings = {
      kind: "navigate",
      label: "Open AI settings",
      route: "settings/ai",
    } as const;
    expect(fixAction(settings, () => true, "weave_ai")).toEqual({
      kind: "link",
      label: "Go to Weave AI",
    });
    expect(fixAction(settings, () => true, "connection")).toEqual({
      kind: "link",
      label: "Go to providers",
    });
    expect(
      fixAction(
        { kind: "navigate", label: "Open workers", route: "operate/workers" },
        () => true,
        "worker_online",
      ),
    ).toEqual({ kind: "link", label: "Open workers" });
  });
  it("offers an operation only to people who may run it", () => {
    const publish = {
      kind: "operation",
      label: "Publish",
      operation: "ai_setup.publish",
    } as const;
    expect(fixAction(publish, () => true)).toEqual({
      kind: "button",
      label: "Publish",
    });
    expect(fixAction(publish, (c) => c !== "definition.publish")).toBeNull();
    expect(
      fixAction(
        { kind: "operation", label: "Authorize", operation: "ai_setup.grant" },
        (c) => c !== "connection.manage",
      ),
    ).toBeNull();
    expect(
      fixAction(
        {
          kind: "command",
          label: "Ask an operator to enable AI",
          command: "weave platform ai enable --ollama auto",
        },
        () => false,
      ),
    ).toEqual({
      kind: "copy",
      label: "Ask an operator to enable AI",
      command: "weave platform ai enable --ollama auto",
    });
    expect(
      fixAction({ kind: "ask", label: "Requires lumi_user" }, () => false),
    ).toBeNull();
    expect(
      fixAction(
        { kind: "ask", label: "Ask an operator to set the connection secret" },
        () => false,
      ),
    ).toEqual({
      kind: "text",
      label: "Ask an operator to set the connection secret",
    });
  });
});

describe("Weave AI", () => {
  it("keeps the saved profile's limits and takes provider and model from the choice", () => {
    const body = weaveAiBody(
      { enabled: true, connection: "r2", model: "gpt-5-mini" },
      cloudConnection,
      {
        enabled: false,
        connection_revision_id: "old",
        revision: 4,
        profile: {
          provider: "anthropic",
          model: "x",
          options: { max_tokens: 2048 },
          reasoning: { pattern: "none", maxSteps: 6 },
          maxCalls: 4,
          timeoutSeconds: 90,
          outputSchema: { old: true },
        },
      },
      { type: "object" },
    );
    expect(body).toEqual({
      enabled: true,
      connection_revision_id: "r2",
      profile: {
        options: { max_tokens: 2048 },
        reasoning: { pattern: "none", maxSteps: 6 },
        maxCalls: 4,
        timeoutSeconds: 90,
        provider: "openai-responses",
        model: "gpt-5-mini",
        outputSchema: { type: "object" },
      },
    });
  });
  it("uses 1,024 output tokens and three minutes for a new configuration", () => {
    expect(
      weaveAiBody(
        { enabled: true, connection: "r2", model: "gpt-5-mini" },
        cloudConnection,
        null,
        {},
      ).profile,
    ).toMatchObject({
      options: { max_tokens: 1024 },
      maxCalls: 8,
      timeoutSeconds: 180,
    });
    expect(() =>
      weaveAiBody(
        { enabled: true, connection: "r2", model: " " },
        cloudConnection,
        null,
        {},
      ),
    ).toThrow("Choose a model");
  });
  it("lists every AI connection for Weave AI, keyless local ones included", () => {
    expect(weaveAiChoices([keylessConnection, cloudConnection])).toEqual([
      {
        value: "r1",
        label: "ollama-local · revision 1",
        description: "http://ollama:11434/v1 · no key",
      },
      {
        value: "r2",
        label: "openai · revision 3",
        description: "https://api.openai.com/v1",
      },
    ]);
  });
});
