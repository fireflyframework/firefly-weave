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
// What the Add a step panel reads from the target once per workspace: the
// language manifest (the features it runs, and what Studio can't edit yet).
// Connected, the platform's `language.read`; locally, the Studio host's copy.
// A read that fails is tried again the next time the panel opens.
import type { StudioApi } from "../../api";
import type { ConnectorInfo, LanguageTarget } from "./entries";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const records = (value: unknown) =>
  Array.isArray(value) ? value.filter(isRecord) : [];

/** The panel's view of a language manifest; null when the value isn't one. */
export function languageTarget(manifest: unknown): LanguageTarget | null {
  if (!isRecord(manifest) || !Array.isArray(manifest["features"])) return null;
  const pending = new Set<string>();
  for (const kind of records(manifest["step_kinds"]))
    if (kind["studio"] === "pending" && typeof kind["kind"] === "string")
      pending.add(kind["kind"]);
  for (const field of records(manifest["workflow_fields"]))
    if (field["studio"] === "pending" && typeof field["name"] === "string")
      pending.add(field["name"]);
  return {
    features: manifest["features"].filter(
      (item: unknown): item is string => typeof item === "string",
    ),
    pending,
  };
}

const languages = new Map<string, Promise<LanguageTarget | null>>();

export function loadLanguage(
  api: StudioApi,
  connected: boolean,
): Promise<LanguageTarget | null> {
  const key = connected ? api.project : "local";
  let pending = languages.get(key);
  if (!pending) {
    pending = api
      .request<unknown>(
        connected ? `${api.project}/language` : "/studio/contracts/language",
      )
      .then(languageTarget, () => null);
    languages.set(key, pending);
    void pending.then((target) => {
      if (!target) languages.delete(key);
    });
  }
  return pending;
}

/** One page of `connector_descriptors.list`; null when the answer isn't one. */
export function connectorPage(
  body: unknown,
): { items: ConnectorInfo[]; next: string | null } | null {
  if (!isRecord(body) || !Array.isArray(body["items"])) return null;
  return {
    items: records(body["items"]).flatMap((item) =>
      typeof item["adapter"] === "string" &&
      typeof item["reference"] === "string"
        ? [{ adapter: item["adapter"], reference: item["reference"] }]
        : [],
    ),
    next: typeof body["next_cursor"] === "string" ? body["next_cursor"] : null,
  };
}

const apps = new Map<string, Promise<ConnectorInfo[] | null>>();

/** The installed apps, up to ten pages; null when they can't be read. */
export function loadConnectors(
  api: StudioApi,
): Promise<ConnectorInfo[] | null> {
  const key = api.project;
  let pending = apps.get(key);
  if (!pending) {
    pending = (async (): Promise<ConnectorInfo[] | null> => {
      const found: ConnectorInfo[] = [];
      let cursor: string | undefined;
      for (let page = 0; page < 10; page++) {
        const parsed = connectorPage(
          await api.page("connector-descriptors", false, cursor),
        );
        if (!parsed) return null;
        found.push(...parsed.items);
        if (!parsed.next) return found;
        cursor = parsed.next;
      }
      return found;
    })().catch(() => null);
    apps.set(key, pending);
    void pending.then((list) => {
      if (!list) apps.delete(key);
    });
  }
  return pending;
}
