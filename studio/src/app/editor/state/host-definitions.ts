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
// What Studio sends its own host with every check and execution: the
// documents of the actions the workflow uses (published contracts and its own
// actions), then the action drafts, table drafts and copies of published
// tables kept on this computer, for exactly the versions its steps use.
import type { JsonObject } from "../../forms/core/json";
import { connectedPrefix, type AccountScope } from "./browser-store";
import { studioBackend } from "./studio-storage";
import { TableDrafts } from "./table-drafts";

/** The host's body bound. */
export const MAX_DEFINITION_BYTES = 2 * 1024 * 1024;
export const TOO_MANY_DRAFTS =
  "This workflow uses more drafts than Studio can send at once; publish some first.";

export interface DefinitionSources {
  owned: JsonObject[];
  actions(uses: string[]): Promise<JsonObject[]>;
  tables(uses: string[]): Promise<JsonObject[]>;
}

/** The `uses` of every step, in every branch, body and nesting, in order. */
export function usesOf(workflow: { spec: { steps?: unknown } }): string[] {
  const found: string[] = [];
  const visit = (value: unknown) => {
    if (Array.isArray(value)) return value.forEach(visit);
    if (!value || typeof value !== "object") return;
    const record = value as Record<string, unknown>;
    if (
      typeof record["uses"] === "string" &&
      typeof record["kind"] === "string"
    )
      found.push(record["uses"]);
    for (const item of Object.values(record)) visit(item);
  };
  visit(workflow.spec.steps);
  return [...new Set(found)];
}

const keyOf = (document: JsonObject) => {
  const metadata = document["metadata"] as
    | { name?: unknown; version?: unknown }
    | undefined;
  return `${String(document["kind"])}:${String(metadata?.name)}@${String(metadata?.version)}`;
};

/** Owned documents first, then action drafts, then tables; each once; 2 MiB at most together. */
export async function hostDefinitions(
  workflow: { spec: { steps?: unknown } },
  sources: DefinitionSources,
): Promise<JsonObject[]> {
  const uses = usesOf(workflow);
  const [actions, tables] = await Promise.all([
    sources.actions(uses),
    sources.tables(uses),
  ]);
  const seen = new Set<string>();
  const found: JsonObject[] = [];
  for (const document of [...sources.owned, ...actions, ...tables]) {
    const key = keyOf(document);
    if (seen.has(key)) continue;
    seen.add(key);
    found.push(document);
  }
  if (
    new TextEncoder().encode(JSON.stringify(found)).length >
    MAX_DEFINITION_BYTES
  )
    throw new Error(TOO_MANY_DRAFTS);
  return found;
}

export interface AccountHost {
  readonly profile: unknown;
  readonly runAccount?: AccountScope | null;
}

/** The decision tables the account keeps on this computer; null when no account can own them. */
export async function accountTables(
  host: AccountHost,
): Promise<TableDrafts | null> {
  const scope = host.runAccount ?? null;
  const prefix = !host.profile
    ? "local:"
    : scope?.subject
      ? await connectedPrefix(scope).catch(() => null)
      : null;
  if (prefix === null) return null;
  const tables = new TableDrafts(studioBackend(), prefix);
  await tables.ready();
  return tables;
}

export interface StudioDefinitionsHost extends AccountHost {
  readonly model: { definition: { spec: { steps?: unknown } } };
  readonly catalogContracts: ReadonlyMap<string, Record<string, unknown>>;
}

/** Studio's definitions for a host request: what the editor has cached for the workflow's uses, then what it keeps on this computer. */
export async function studioDefinitions(
  host: StudioDefinitionsHost,
): Promise<JsonObject[]> {
  const workflow = host.model.definition;
  const uses = new Set(usesOf(workflow));
  const owned = [...host.catalogContracts]
    .filter(([reference]) => uses.has(reference))
    .map(([, document]) => document as JsonObject);
  return hostDefinitions(workflow, {
    owned,
    actions: async () => [],
    tables: async (wanted) =>
      ((await accountTables(host))?.definitionsFor(wanted) ??
        []) as unknown as JsonObject[],
  });
}
