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
import type { StudioApi } from "../api";

export const aiConnector = "weave-agentic-provider@1.0.0";
export interface AiConnection {
  id: string;
  name: string;
  revision: number;
  connector: string;
  config: { provider: string; endpoint: string; apiVersion?: string };
}
export interface AiConnectionDraft {
  name: string;
  provider: string;
  endpoint: string;
  apiVersion: string;
  handle: string;
}
export const aiProviders = [
  { value: "openai-responses", label: "OpenAI Responses" },
  { value: "openai-chat", label: "OpenAI Chat" },
  { value: "azure-responses", label: "Azure OpenAI Responses" },
  { value: "azure-chat", label: "Azure OpenAI Chat" },
  { value: "anthropic", label: "Anthropic" },
];
export function aiConnectionRequest(
  draft: AiConnectionDraft,
  connector: string,
) {
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(draft.name.trim()))
    throw Error(
      "Enter a connection name using letters, numbers, dots, dashes or underscores.",
    );
  if (!aiProviders.some((p) => p.value === draft.provider))
    throw Error("Choose a provider.");
  let endpoint: URL;
  try {
    endpoint = new URL(draft.endpoint.trim());
  } catch {
    throw Error("Enter the provider's trusted HTTPS endpoint.");
  }
  if (
    endpoint.protocol !== "https:" ||
    endpoint.username ||
    endpoint.password ||
    endpoint.search ||
    endpoint.hash
  )
    throw Error(
      "Use an HTTPS endpoint without credentials, query parameters or fragments.",
    );
  if (draft.provider.startsWith("azure-") && !draft.apiVersion.trim())
    throw Error(
      "Azure requires an API version supplied by your platform operator.",
    );
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(draft.handle.trim()))
    throw Error(
      "Enter an operator-approved API key secret handle, never the key itself.",
    );
  if (!connector)
    throw Error(
      `${aiConnector} must be published in this project before creating this connection.`,
    );
  return {
    name: draft.name.trim(),
    connector_version_id: connector,
    config: {
      provider: draft.provider,
      endpoint: draft.endpoint.trim(),
      secretSlot: "apiKey",
      ...(draft.provider.startsWith("azure-")
        ? { apiVersion: draft.apiVersion.trim() }
        : {}),
    },
    secretRef: { apiKey: draft.handle.trim() },
    allowed_destinations: [endpoint.origin],
  };
}
/** Bounded lists fail visibly rather than hiding an older pinned revision. */
export async function aiSetupRows(
  api: StudioApi,
  collection: string,
  environment: boolean,
) {
  const items: Record<string, unknown>[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < 20; page++) {
    const result = await api.page(collection, environment, cursor);
    items.push(...result.items);
    if (!result.next_cursor) return items;
    cursor = result.next_cursor;
  }
  throw Error(
    "Too many results to list here. Ask an administrator to narrow the environment's configuration.",
  );
}
export function aiConnections(
  items: Record<string, unknown>[],
): AiConnection[] {
  return items.filter(
    (item) =>
      !item["unavailable"] &&
      item["connector"] === aiConnector &&
      typeof item["id"] === "string" &&
      item["config"] &&
      typeof item["config"] === "object",
  ) as unknown as AiConnection[];
}
