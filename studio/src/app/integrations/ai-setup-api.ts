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
// Calls to the platform's AI setup operations. The types mirror the platform's
// payloads and keep their snake_case. The platform checks every permission;
// Studio only shows what a person may do.
import { ApiError, type StudioApi } from "../api";
import {
  aiConnections,
  aiConnector,
  aiSetupRows,
  latestByName,
  type AiConnection,
} from "./ai-provider-connection";

export type AiProvider =
  | "openai-chat"
  | "openai-responses"
  | "azure-chat"
  | "azure-responses"
  | "anthropic";
export interface AiEndpoint {
  id: string;
  label: string;
  url: string;
  providers: AiProvider[];
  compat: "ollama" | null;
  credential: "none" | "required";
  models: "served" | "listed";
  context_tokens: number | null;
  development_only: boolean;
}
export interface AiEndpoints {
  policy: "loaded" | "absent";
  endpoints: AiEndpoint[];
}
export interface AiModel {
  name: string;
  approved: boolean;
  available: boolean;
  tools: "yes" | "no" | "unknown";
  context_tokens?: number;
  size_bytes?: number;
  family?: string;
}
export interface AiModels {
  approval: "served" | "listed" | "unknown";
  discovery: string;
  discovered_at: string | null;
  models: AiModel[];
}
export interface AiTestResult {
  ok: boolean;
  code: string;
  latency_ms: number | null;
  model: string;
  tool_calling: "supported" | "unsupported" | "unknown";
  context_tokens?: number;
}
export type ReadinessId =
  | "worker_installed"
  | "actions_published"
  | "worker_online"
  | "connection"
  | "key_available"
  | "connection_authorized"
  | "model_responds"
  | "weave_ai";
export type ReadinessStatus = "done" | "action" | "not_needed" | "unknown";
export interface ReadinessFix {
  kind: "operation" | "command" | "navigate" | "ask";
  label: string;
  operation?:
    | "ai_setup.publish"
    | "ai_setup.grant"
    | "ai_connections.test"
    | "lumi.configuration.write"
    | null;
  command?: string | null;
  route?: "settings/ai" | "operate/workers" | null;
}
export interface ReadinessItem {
  id: ReadinessId;
  status: ReadinessStatus;
  detail: string;
  fix?: ReadinessFix;
}
export interface Readiness {
  items: ReadinessItem[];
}
export interface BindableConnection {
  connection_id: string;
  revision_id: string;
  name: string;
  connector: string;
  label: string;
  enabled: boolean;
  provider?: string;
  endpoint_origin?: string;
}
interface BindablePage {
  items: BindableConnection[];
  next_cursor: string | null;
}
export interface ConnectionRequestBody {
  name: string;
  connector_version_id: string;
  config: {
    provider: AiProvider;
    endpoint: string;
    secretSlot: "apiKey";
    apiVersion?: string;
  };
  secretRef: { apiKey: string };
  allowed_destinations: string[];
}
export interface LumiConfiguration {
  enabled: boolean;
  connection_revision_id: string;
  profile: Record<string, unknown>;
  revision: number;
  /** Absent when Weave AI chooses the context size from the model. */
  context_profile?: "auto" | "compact" | "standard" | "full";
}
export interface LumiStatus {
  configured: boolean;
  provider?: string | null;
  model?: string | null;
}

/** A model test: the platform stops it after 130 seconds; Studio waits a little longer. */
export const TEST_TIMEOUT_MS = 150_000;
/** Model discovery: the AI gateway gives a server 15 seconds. */
export const DISCOVERY_TIMEOUT_MS = 30_000;
/** An ordinary platform call: the Studio host stops waiting after 35 seconds. */
export const BRIDGE_TIMEOUT_MS = 35_000;

export class AiSetupApi {
  constructor(private readonly api: StudioApi) {}
  private get environment() {
    return this.api.environment;
  }
  endpoints() {
    return this.api.request<AiEndpoints>(`${this.environment}/ai/endpoints`);
  }
  models(revision: string, refresh = false, signal?: AbortSignal) {
    return this.api.request<AiModels>(
      `${this.environment}/ai/models?connection=${encodeURIComponent(revision)}&refresh=${refresh}`,
      "GET",
      undefined,
      {},
      DISCOVERY_TIMEOUT_MS,
      signal,
    );
  }
  readiness() {
    return this.api.request<Readiness>(`${this.environment}/ai/readiness`);
  }
  test(revision: string, model: string, signal?: AbortSignal) {
    return this.api.request<AiTestResult>(
      `${this.environment}/ai/connections/${encodeURIComponent(revision)}/test`,
      "POST",
      { model, probe_tools: true },
      {},
      TEST_TIMEOUT_MS,
      signal,
    );
  }
  publish() {
    return this.api.request<Record<string, unknown>>(
      `${this.environment}/ai/setup/publish`,
      "POST",
      undefined,
      {},
      BRIDGE_TIMEOUT_MS,
    );
  }
  grant(revision: string) {
    return this.api.request<{ granted: boolean; not_needed?: boolean }>(
      `${this.environment}/ai/setup/grant`,
      "POST",
      { connection_revision_id: revision },
    );
  }
  /** The newest revision of each AI connection (needs connection.manage). */
  async connections(): Promise<AiConnection[]> {
    return latestByName(
      aiConnections(await aiSetupRows(this.api, "connections", true)),
    );
  }
  /** One connection revision by its ID, or null when the platform has no such revision. */
  async connection(revision: string): Promise<AiConnection | null> {
    const found = await this.api.request<Partial<AiConnection>>(
      `${this.environment}/connections/${encodeURIComponent(revision)}`,
    );
    return found?.id === revision && typeof found.name === "string"
      ? (found as AiConnection)
      : null;
  }
  /** AI connections this person may choose (needs connection.bind). */
  async bindable(): Promise<BindableConnection[]> {
    const items: BindableConnection[] = [];
    let cursor: string | null = null;
    for (let page = 0; page < 20; page++) {
      const query: string = `connector=${encodeURIComponent(aiConnector)}&limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
      const result: BindablePage = await this.api.request<BindablePage>(
        `${this.environment}/bindable-connections?${query}`,
      );
      items.push(...result.items);
      if (!result.next_cursor) return items;
      cursor = result.next_cursor;
    }
    throw Error(
      "Too many connections to list here. Ask an administrator to narrow the environment's configuration.",
    );
  }
  /** The published AI connector version, or "" before the AI actions are published. */
  async connectorId(): Promise<string> {
    const rows = await aiSetupRows(this.api, "connectors", false);
    return String(
      rows.find(
        (row) =>
          !row["unavailable"] &&
          !row["retired"] &&
          `${row["name"]}@${row["version"]}` === aiConnector,
      )?.["id"] ?? "",
    );
  }
  /** Creates a connection revision; a retry with the same key never adds a second one. */
  create(body: ConnectionRequestBody, key: string) {
    return this.api.mutate<AiConnection>(
      `${this.environment}/connections`,
      "POST",
      body,
      undefined,
      key,
    );
  }
  async lumiConfiguration(): Promise<LumiConfiguration | null> {
    try {
      return await this.api.request<LumiConfiguration>(
        `${this.environment}/lumi/configuration`,
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
  }
  saveLumi(body: Record<string, unknown>, revision: number | null) {
    return this.api.request<LumiConfiguration>(
      `${this.environment}/lumi/configuration`,
      "PUT",
      body,
      revision === null ? {} : { "If-Match": `"${revision}"` },
    );
  }
  lumiStatus() {
    return this.api.request<LumiStatus>(`${this.environment}/lumi/status`);
  }
  /** The fixed reply schema every Weave AI configuration carries. */
  async lumiReplySchema(): Promise<unknown> {
    const schema = await this.api.request<Record<string, unknown>>(
      "/studio/contracts/lumi-configuration",
    );
    const defs = (schema["$defs"] ?? {}) as Record<string, any>;
    const reply =
      defs["LumiProfile"]?.["properties"]?.["outputSchema"]?.["const"];
    if (!reply)
      throw Error("The host did not provide the fixed Weave AI reply schema.");
    return reply;
  }
}
