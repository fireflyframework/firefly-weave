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
// The rules Settings › AI models applies, kept out of the components so they
// can be tested alone: which endpoints a provider offers, how a connection
// request is built, which model to suggest, what a test result means and how
// the readiness checklist reads. The platform checks all of it again.
import { ApiError } from "../api";
import { describeError } from "../errors";
import type { SelectOption } from "../forms/ui/select";
import { plainServerMessage } from "./connection-copy";
import { secretRefs, type AiConnection } from "./ai-provider-connection";
import type {
  AiEndpoint,
  AiModel,
  AiProvider,
  AiTestResult,
  BindableConnection,
  ConnectionRequestBody,
  LumiConfiguration,
  Readiness,
  ReadinessFix,
  ReadinessId,
  ReadinessStatus,
} from "./ai-setup-api";

/** The reserved handle of connections that use no credential. */
export const NO_CREDENTIAL = "no-credential";
export const SUGGESTED_MODEL = "qwen3:4b";
export const PLAIN_HTTP_REFUSED =
  "This address is not approved for plain HTTP. A platform operator must add it to the private-origin policy.";
export const GATEWAY_MISSING =
  "Tests need the AI gateway. Run `weave platform ai enable`, or ask an operator to deploy it.";
export const RATE_LIMITED =
  "Too many AI connection tests. Wait a minute, then test again.";
const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

export type ProviderCard = "ollama" | "openai" | "anthropic" | "azure";
export interface CardInfo {
  id: ProviderCard;
  title: string;
  kind: string;
  icon: "model" | "cloud";
  providers: readonly AiProvider[];
}
export const PROVIDER_CARDS: readonly CardInfo[] = [
  {
    id: "ollama",
    title: "Ollama",
    kind: "Local models",
    icon: "model",
    providers: ["openai-chat"],
  },
  {
    id: "openai",
    title: "OpenAI",
    kind: "Cloud",
    icon: "cloud",
    providers: ["openai-responses", "openai-chat"],
  },
  {
    id: "anthropic",
    title: "Anthropic",
    kind: "Cloud",
    icon: "cloud",
    providers: ["anthropic"],
  },
  {
    id: "azure",
    title: "Azure OpenAI",
    kind: "Cloud",
    icon: "cloud",
    providers: ["azure-responses", "azure-chat"],
  },
];
export const cardInfo = (card: ProviderCard): CardInfo =>
  PROVIDER_CARDS.find((item) => item.id === card)!;
export const API_STYLES: Readonly<
  Record<"openai" | "azure", readonly { value: AiProvider; label: string }[]>
> = {
  openai: [
    { value: "openai-responses", label: "Responses" },
    { value: "openai-chat", label: "Chat Completions" },
  ],
  azure: [
    { value: "azure-responses", label: "Responses" },
    { value: "azure-chat", label: "Chat Completions" },
  ],
};
const STANDARD: Partial<Record<ProviderCard, string>> = {
  openai: "https://api.openai.com/v1",
  anthropic: "https://api.anthropic.com",
};
const DEFAULT_NAME: Record<ProviderCard, string> = {
  ollama: "ollama-local",
  openai: "openai",
  anthropic: "anthropic",
  azure: "azure-openai",
};
const DEFAULT_HANDLE: Record<ProviderCard, string> = {
  ollama: "",
  openai: "openai-api-key",
  anthropic: "anthropic-api-key",
  azure: "azure-openai-api-key",
};

/** The address as the platform stores it: origin plus path, without a lone trailing slash. */
export function normalizedEndpoint(value: string): string {
  const text = value.trim();
  const url = new URL(text);
  return (
    url.origin +
    (url.pathname === "/" && !text.endsWith("/") ? "" : url.pathname)
  );
}
export function originOf(value: string): string {
  try {
    return new URL(value).origin;
  } catch {
    return value;
  }
}
export function endpointFor(
  url: string,
  endpoints: readonly AiEndpoint[],
): AiEndpoint | undefined {
  try {
    const wanted = normalizedEndpoint(url);
    return endpoints.find(
      (endpoint) => normalizedEndpoint(endpoint.url) === wanted,
    );
  } catch {
    return undefined;
  }
}
export function presetsFor(
  card: ProviderCard,
  endpoints: readonly AiEndpoint[],
): AiEndpoint[] {
  const providers = cardInfo(card).providers;
  return endpoints.filter((endpoint) =>
    card === "ollama"
      ? endpoint.compat === "ollama"
      : endpoint.compat === null &&
        endpoint.providers.some((provider) => providers.includes(provider)),
  );
}
export const keylessConnection = (connection: AiConnection): boolean =>
  secretRefs(connection)["apiKey"] === NO_CREDENTIAL;
export function cardOf(
  connection: AiConnection,
  endpoints: readonly AiEndpoint[],
): ProviderCard {
  if (
    keylessConnection(connection) ||
    endpointFor(connection.config.endpoint, endpoints)?.compat === "ollama"
  )
    return "ollama";
  const provider = connection.config.provider;
  return provider === "anthropic"
    ? "anthropic"
    : provider.startsWith("azure-")
      ? "azure"
      : "openai";
}
export function cardOfBindable(
  item: BindableConnection,
  endpoints: readonly AiEndpoint[],
): ProviderCard {
  if (
    endpoints.some(
      (endpoint) =>
        endpoint.compat === "ollama" &&
        originOf(endpoint.url) === item.endpoint_origin,
    )
  )
    return "ollama";
  const provider = item.provider ?? "";
  return provider === "anthropic"
    ? "anthropic"
    : provider.startsWith("azure-")
      ? "azure"
      : "openai";
}

export interface SheetDraft {
  card: ProviderCard;
  name: string;
  endpoint: string;
  /** The address was typed, not chosen from the approved endpoints. */
  custom: boolean;
  provider: AiProvider;
  apiVersion: string;
  handle: string;
}
export function draftFor(
  card: ProviderCard,
  endpoints: readonly AiEndpoint[],
  existing?: AiConnection | null,
): SheetDraft {
  const presets = presetsFor(card, endpoints);
  if (existing) {
    const handle = secretRefs(existing)["apiKey"] ?? "";
    return {
      card,
      name: existing.name,
      endpoint: existing.config.endpoint,
      custom: !endpointFor(existing.config.endpoint, presets),
      provider: existing.config.provider as AiProvider,
      apiVersion: existing.config.apiVersion ?? "",
      handle: handle === NO_CREDENTIAL ? "" : handle,
    };
  }
  const preferred =
    presets.find((endpoint) => endpoint.url === STANDARD[card]) ?? presets[0];
  return {
    card,
    name: DEFAULT_NAME[card],
    endpoint: preferred?.url ?? STANDARD[card] ?? "",
    custom: !preferred,
    provider: cardInfo(card).providers[0],
    apiVersion: "",
    handle: DEFAULT_HANDLE[card],
  };
}
/** What makes two drafts the same request. */
export const draftKey = (draft: SheetDraft): string =>
  JSON.stringify([
    draft.card,
    draft.name.trim(),
    draft.endpoint.trim(),
    draft.provider,
    draft.apiVersion.trim(),
    draft.handle.trim(),
  ]);
/** The chosen endpoint is an approved preset that sends no credential. */
export function keyless(
  draft: SheetDraft,
  endpoints: readonly AiEndpoint[],
): boolean {
  if (draft.custom) return false;
  return (
    endpointFor(draft.endpoint, presetsFor(draft.card, endpoints))
      ?.credential === "none"
  );
}

export type SheetField =
  | "name"
  | "endpoint"
  | "apiVersion"
  | "handle"
  | "model"
  | "form";
export class FieldError extends Error {
  constructor(
    readonly field: SheetField,
    message: string,
  ) {
    super(message);
  }
}
/** The request for a new connection revision; throws FieldError for the field to fix. */
export function connectionRequest(
  draft: SheetDraft,
  connectorId: string,
  endpoints: readonly AiEndpoint[],
  policy: "loaded" | "absent",
): ConnectionRequestBody {
  const name = draft.name.trim();
  if (!NAME.test(name))
    throw new FieldError(
      "name",
      "Letters, numbers, dots, dashes or underscores.",
    );
  let url: URL;
  try {
    url = new URL(draft.endpoint.trim());
  } catch {
    throw new FieldError("endpoint", "Enter the model endpoint's address.");
  }
  if (url.username || url.password || url.search || url.hash)
    throw new FieldError(
      "endpoint",
      "Use an address without credentials, query parameters or fragments.",
    );
  const preset = draft.custom
    ? undefined
    : endpointFor(draft.endpoint, presetsFor(draft.card, endpoints));
  if (url.protocol === "http:") {
    if (!preset) throw new FieldError("endpoint", PLAIN_HTTP_REFUSED);
  } else if (url.protocol !== "https:")
    throw new FieldError("endpoint", "Use an https:// address.");
  else if (policy === "loaded" && draft.card !== "ollama" && !preset)
    throw new FieldError(
      "endpoint",
      "Choose an approved endpoint. A platform operator approves model endpoints.",
    );
  if (draft.card === "azure" && !draft.apiVersion.trim())
    throw new FieldError("apiVersion", "Azure requires an API version.");
  const noKey = preset?.credential === "none";
  const handle = draft.handle.trim();
  if (!noKey && handle === NO_CREDENTIAL)
    throw new FieldError(
      "handle",
      "no-credential is reserved for approved local endpoints without a key.",
    );
  if (!noKey && !NAME.test(handle))
    throw new FieldError(
      "handle",
      "Enter the secret handle your operator approved, never the key itself.",
    );
  if (!connectorId)
    throw new FieldError(
      "form",
      "Publish the AI actions first, from the readiness checklist.",
    );
  return {
    name,
    connector_version_id: connectorId,
    config: {
      provider: draft.provider,
      endpoint: normalizedEndpoint(draft.endpoint),
      secretSlot: "apiKey",
      ...(draft.card === "azure"
        ? { apiVersion: draft.apiVersion.trim() }
        : {}),
    },
    secretRef: { apiKey: noKey ? NO_CREDENTIAL : handle },
    allowed_destinations: [url.origin],
  };
}

export interface SheetProblem {
  field: SheetField;
  message: string;
  code?: string;
}
const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
function fieldFor(pointer: string): SheetField {
  if (pointer === "/name") return "name";
  if (
    pointer.startsWith("/config/endpoint") ||
    pointer.startsWith("/allowed_destinations")
  )
    return "endpoint";
  if (pointer.startsWith("/config/apiVersion")) return "apiVersion";
  if (pointer.startsWith("/secretRef")) return "handle";
  return "form";
}
/** Field problems from a 422 WV-CONNECTION answer, each on the field its pointer names. */
export function sheetProblems(detail: unknown): SheetProblem[] {
  const body = record(detail);
  const items = Array.isArray(body["diagnostics"])
    ? body["diagnostics"]
    : record(body["result"])["diagnostics"];
  if (!Array.isArray(items)) return [];
  return items.slice(0, 20).map((value) => {
    const item = record(value);
    const message = item["message"];
    const code = String(item["code"] ?? "");
    return {
      field: fieldFor(typeof item["path"] === "string" ? item["path"] : ""),
      message:
        typeof message === "string" && message.trim() && message.length <= 240
          ? plainServerMessage(message)
          : "The platform rejected this setting.",
      code: /^WV-CONNECTION-[A-Z]+$/.test(code) ? code : undefined,
    };
  });
}

export function formatSize(bytes?: number): string {
  if (bytes === undefined) return "";
  return bytes >= 1e9
    ? `${(bytes / 1e9).toFixed(1)} GB`
    : `${Math.max(1, Math.round(bytes / 1e6))} MB`;
}
export function modelFacts(model: AiModel): string {
  if (!model.approved) return "Not approved";
  if (!model.available) return "Not installed on this server";
  return [
    formatSize(model.size_bytes),
    model.tools === "yes" ? "tools" : model.tools === "no" ? "no tools" : "",
    model.context_tokens
      ? `${Math.round(model.context_tokens / 1024)}k context`
      : "",
  ]
    .filter(Boolean)
    .join(" · ");
}
export function modelOptions(models: readonly AiModel[]): SelectOption[] {
  return models.map((model) => ({
    value: model.name,
    label: model.name,
    description: modelFacts(model),
    disabled: !model.approved || !model.available,
  }));
}
export function defaultModel(models: readonly AiModel[], current = ""): string {
  const usable = models.filter((model) => model.approved && model.available);
  return (
    (
      usable.find((model) => model.name === current) ??
      usable.find((model) => model.name === SUGGESTED_MODEL) ??
      usable.find((model) => model.tools === "yes") ??
      usable[0]
    )?.name ?? ""
  );
}

/** The platform's copy for each model error code (contracts/ai.py); a test keeps them equal. */
export const AI_COPY: Readonly<Record<string, string>> = {
  LLM_POLICY:
    "This model is not approved for this endpoint. Ask an operator to approve it.",
  LLM_CONNECTION:
    "The AI connection does not match this step. Choose the connection again.",
  LLM_MODEL_NOT_FOUND:
    "{model} is not installed on {endpoint}. Pull it with `ollama pull {model}`.",
  LLM_NO_TOOL_SUPPORT:
    "{model} cannot call tools. Choose a tool-capable model, such as qwen3:4b.",
  LLM_UNREACHABLE:
    "Weave could not reach the model endpoint. Check that Ollama is running.",
  LLM_TIMEOUT:
    "The model did not answer within {n} seconds. Local models can be slow on first load.",
  LLM_CONTEXT_LIMIT:
    "The prompt is too long for this model's context ({n} tokens).",
  LLM_AUTH: "The provider refused the credential.",
  LLM_RATE_LIMITED: "The provider is rate limiting requests.",
  LLM_CAPACITY: "Weave is busy; the step will retry.",
  LLM_OUTPUT: "The model's answer did not match the result fields.",
  LLM_LIMIT: "The step reached its token or request limit.",
  LLM_INPUT: "The AI step input is not valid.",
  LLM_OPTIONS: "This provider does not accept these model options.",
  LLM_REASONING: "The reasoning pattern did not reach a final answer.",
  LLM_PROVIDER: "The model provider returned an unexpected error.",
};
export function aiMessage(
  code: string,
  values: { model?: string; endpoint?: string; n?: number },
): string {
  return (AI_COPY[code] ?? AI_COPY["LLM_PROVIDER"])
    .replaceAll("{model}", values.model ?? "The model")
    .replaceAll("{endpoint}", values.endpoint ?? "the endpoint")
    .replaceAll("{n}", (values.n ?? 0).toLocaleString("en-US"));
}

export type TestState =
  | { kind: "idle" }
  | { kind: "running"; model: string }
  | { kind: "canceled" }
  | { kind: "done"; result: AiTestResult; origin: string }
  | {
      kind: "refused";
      message: string;
      code: string;
      status: number;
      origin: string;
    };
export interface TestView {
  tone: "neutral" | "success" | "warning" | "danger";
  text: string;
  details: [string, string][];
}
/** The gateway's own test budget, which a timed-out test reports. */
const TEST_SECONDS = 120;
export function testView(state: TestState, connection: string): TestView {
  switch (state.kind) {
    case "idle":
      return { tone: "neutral", text: "Not tested", details: [] };
    case "canceled":
      return { tone: "neutral", text: "Test canceled.", details: [] };
    case "running":
      return {
        tone: "neutral",
        text: `Calling ${state.model} on ${connection}…`,
        details: [],
      };
    case "refused":
      return {
        tone: "danger",
        text: state.message,
        details: [
          ["Code", state.code],
          ["Endpoint", state.origin],
          ["HTTP status", `${Math.floor(state.status / 100)}xx`],
        ],
      };
    case "done": {
      const result = state.result;
      const seconds =
        result.latency_ms === null
          ? ""
          : ` in ${(result.latency_ms / 1000).toFixed(1)} s`;
      const details: [string, string][] = [
        ["Code", result.code],
        ["Endpoint", state.origin],
        [
          "Latency",
          result.latency_ms === null ? "Unknown" : `${result.latency_ms} ms`,
        ],
      ];
      if (!result.ok)
        return {
          tone: "danger",
          text: aiMessage(result.code, {
            model: result.model,
            endpoint: state.origin,
            n:
              result.code === "LLM_TIMEOUT"
                ? TEST_SECONDS
                : (result.context_tokens ?? 0),
          }),
          details,
        };
      if (result.tool_calling === "unsupported")
        return {
          tone: "warning",
          text: `Connected. ${result.model} answered${seconds} but cannot call tools. Use it for AI tasks, not AI agents.`,
          details,
        };
      return {
        tone: "success",
        text:
          result.tool_calling === "supported"
            ? `Connected. ${result.model} answered${seconds} and can call tools.`
            : `Connected. ${result.model} answered${seconds}.`,
        details,
      };
    }
  }
}
/** A test the platform refused before calling the model. Never shows the response body. */
export function refusal(error: unknown, origin: string): TestState {
  const status = error instanceof ApiError ? error.status : 0;
  const code = error instanceof ApiError ? error.code : "";
  const message =
    code === "WV-AI-GATEWAY-MISSING"
      ? GATEWAY_MISSING
      : code === "WV-AI-RATE-LIMITED"
        ? RATE_LIMITED
        : describeError(error).message;
  return {
    kind: "refused",
    message,
    code: code || "WV-STUDIO",
    status,
    origin,
  };
}

export const READINESS_LABELS: Readonly<Record<ReadinessId, string>> = {
  worker_installed: "Agentic worker installed",
  actions_published: "AI actions published",
  worker_online: "Worker online",
  connection: "AI connection",
  key_available: "Key available",
  connection_authorized: "Connection authorized",
  model_responds: "Model responds",
  weave_ai: "Weave AI",
};
const DETAILS: Readonly<Record<string, string>> = {
  worker_installed: "Installed",
  worker_missing: "Not installed",
  actions_published: "Published",
  actions_missing: "Not published yet",
  catalog_mismatch: "Published versions differ from this platform's",
  worker_online: "Online",
  worker_offline: "Offline",
  connection_available: "Set up",
  connection_missing: "No AI connection yet",
  key_grant_available: "Available",
  key_missing: "Key not found",
  no_credential: "Not needed",
  connection_authorized: "Authorized",
  authorization_missing: "Not authorized yet",
  test_passed: "Answered in the last 24 hours",
  test_failed: "The last test failed",
  test_required: "Not tested yet",
  test_stale: "Last tested more than a day ago",
  weave_ai_ready: "Ready",
  weave_ai_missing: "Not set up",
  weave_ai_disabled: "Turned off",
  gateway_missing: "Needs the AI gateway",
  record_unavailable: "Couldn't check",
  limit_exceeded: "Too many records to check",
};
const ICONS: Readonly<Record<ReadinessStatus, string>> = {
  done: "ok",
  action: "warning",
  not_needed: "off",
  unknown: "help",
};
export interface ReadinessRow {
  id: ReadinessId;
  label: string;
  status: ReadinessStatus;
  tone: "success" | "warning" | "neutral";
  icon: string;
  detail: string;
  fix?: ReadinessFix;
}
export function readinessRows(
  result: Readiness,
  roleLabel: (role: string) => string,
  lastTest?: { model: string; latency_ms: number | null },
): ReadinessRow[] {
  return result.items.map((item) => ({
    id: item.id,
    label: READINESS_LABELS[item.id],
    status: item.status,
    tone:
      item.status === "done"
        ? "success"
        : item.status === "action"
          ? "warning"
          : "neutral",
    icon: ICONS[item.status],
    detail: item.detail.startsWith("requires_")
      ? `Requires ${roleLabel(item.detail.slice("requires_".length))}`
      : item.id === "model_responds" && item.status === "done" && lastTest
        ? `${lastTest.model}${lastTest.latency_ms === null ? "" : ` · ${(lastTest.latency_ms / 1000).toFixed(1)} s`}`
        : (DETAILS[item.detail] ?? "Couldn't check"),
    fix: item.fix,
  }));
}
export function allReady(
  result: Readiness,
  except: readonly ReadinessId[] = [],
): boolean {
  return result.items.every(
    (item) =>
      except.includes(item.id) ||
      item.status === "done" ||
      item.status === "not_needed",
  );
}

export type FixAction =
  | { kind: "button"; label: string }
  | { kind: "copy"; label: string; command: string }
  | { kind: "link"; label: string }
  | { kind: "text"; label: string };
const NEEDS: Readonly<Record<string, string>> = {
  "ai_setup.publish": "definition.publish",
  "ai_setup.grant": "connection.manage",
  "ai_connections.test": "connection.manage",
  "lumi.configuration.write": "lumi.manage",
};
/** What a person can do about an item; null when the fix is not theirs to run. */
export function fixAction(
  fix: ReadinessFix | undefined,
  can: (capability: string) => boolean,
): FixAction | null {
  if (!fix) return null;
  if (fix.kind === "operation")
    return fix.operation && can(NEEDS[fix.operation])
      ? { kind: "button", label: fix.label }
      : null;
  if (fix.kind === "command" && fix.command)
    return { kind: "copy", label: fix.label, command: fix.command };
  if (fix.kind === "navigate") return { kind: "link", label: fix.label };
  // "Requires {role}" repeats the row's detail, which names the role in words.
  return fix.label.startsWith("Requires ")
    ? null
    : { kind: "text", label: fix.label };
}

export interface WeaveAiDraft {
  enabled: boolean;
  connection: string;
  model: string;
}
/** Limits for a new Weave AI configuration; a saved one keeps its own. */
export const WEAVE_AI_DEFAULTS: Readonly<Record<string, unknown>> = {
  options: { max_tokens: 1024 },
  reasoning: { pattern: "none", maxSteps: 6 },
  maxCalls: 8,
  timeoutSeconds: 180,
};
export function weaveAiBody(
  draft: WeaveAiDraft,
  connection: AiConnection,
  current: LumiConfiguration | null,
  replySchema: unknown,
): Record<string, unknown> {
  const model = draft.model.trim();
  if (!model) throw new FieldError("model", "Choose a model.");
  const {
    provider: _provider,
    model: _model,
    outputSchema: _schema,
    ...kept
  } = (current?.profile ?? WEAVE_AI_DEFAULTS) as Record<string, unknown>;
  return {
    enabled: draft.enabled,
    connection_revision_id: connection.id,
    profile: {
      ...kept,
      provider: connection.config.provider,
      model,
      outputSchema: replySchema,
    },
  };
}
export function weaveAiChoices(
  connections: readonly AiConnection[],
): SelectOption[] {
  return connections.map((connection) => {
    const noKey = keylessConnection(connection);
    return {
      value: connection.id,
      label: `${connection.name} · revision ${connection.revision}`,
      description: noKey
        ? "Weave AI can't use a connection without a key yet"
        : connection.config.endpoint,
      disabled: noKey,
    };
  });
}
