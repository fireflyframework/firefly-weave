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
  /** The secret handle by slot; never a secret value. */
  secretRef?: Record<string, string>;
}
/** The secret handles of a connection revision, whichever spelling the platform used. */
export function secretRefs(connection: AiConnection): Record<string, string> {
  const value =
    connection.secretRef ??
    (connection as unknown as { secret_refs?: Record<string, string> })
      .secret_refs;
  return value && typeof value === "object" ? value : {};
}
/** The newest revision of each connection name, sorted by name. */
export function latestByName(connections: AiConnection[]): AiConnection[] {
  const latest = new Map<string, AiConnection>();
  for (const connection of connections) {
    const known = latest.get(connection.name);
    if (!known || connection.revision > known.revision)
      latest.set(connection.name, connection);
  }
  return [...latest.values()].sort((a, b) => a.name.localeCompare(b.name));
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
