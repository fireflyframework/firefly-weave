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
// Drafts of the next version of published actions, kept on this computer
// under the account's storage prefix (`local:` offline), one per action name:
// Change in a new version starts one, and its retry, timeout and request
// change there until Publish action. A published version never changes.
import type { JsonObject } from "../../forms/core/json";
import { storeKey, type StoreBackend } from "./browser-store";
import { VERSION, nextDraftVersion } from "./table-drafts";

export interface ActionDocument {
  apiVersion: "weave/v1alpha1";
  kind: "Action";
  metadata: { name: string; version: string };
  spec: JsonObject;
}
export interface ActionDraftEntry {
  kind: "draft";
  name: string;
  version: string;
  base: string;
  document: ActionDocument;
  savedAt: string;
}

export const ACTION_DRAFT_MAX_BYTES = 1_048_576;
export const MAX_ACTION_DRAFTS = 50;
const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const KEY = "weave.actionDraft.";

export const actionDraftKey = (prefix: string, name: string): string =>
  storeKey(prefix, KEY + name);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const exactly = (value: Record<string, unknown>, keys: string[]) =>
  Object.keys(value).length === keys.length &&
  keys.every((key) => key in value);

/** A stored action draft, from its typed fields only; null for anything else. */
export function readActionEntry(raw: string): ActionDraftEntry | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (
    !isRecord(value) ||
    !exactly(value, ["kind", "name", "version", "base", "document", "savedAt"])
  )
    return null;
  const { kind, name, version, base, document, savedAt } = value;
  if (kind !== "draft" || typeof name !== "string" || !NAME.test(name))
    return null;
  if (typeof version !== "string" || !VERSION.test(version)) return null;
  if (typeof base !== "string" || !VERSION.test(base)) return null;
  if (typeof savedAt !== "string" || Number.isNaN(Date.parse(savedAt)))
    return null;
  if (
    !isRecord(document) ||
    !exactly(document, ["apiVersion", "kind", "metadata", "spec"]) ||
    document["apiVersion"] !== "weave/v1alpha1" ||
    document["kind"] !== "Action" ||
    !isRecord(document["spec"])
  )
    return null;
  const metadata = document["metadata"];
  if (
    !isRecord(metadata) ||
    !exactly(metadata, ["name", "version"]) ||
    metadata["name"] !== name ||
    metadata["version"] !== version
  )
    return null;
  return value as unknown as ActionDraftEntry;
}

export class ActionDrafts {
  private drafts = new Map<string, ActionDraftEntry>();
  private loaded: Promise<void> | null = null;
  private last = 0;

  constructor(
    private readonly backend: StoreBackend,
    /** The account's storage prefix; null keeps drafts in memory only. */
    readonly prefix: string | null,
  ) {}

  ready(): Promise<void> {
    this.loaded ??= this.load();
    return this.loaded;
  }
  private async load() {
    if (this.prefix === null) return;
    for (const key of await this.backend.keys(this.prefix + KEY)) {
      const raw = await this.backend.get(key);
      const entry = raw === null ? null : readActionEntry(raw);
      if (!entry || key !== actionDraftKey(this.prefix, entry.name)) {
        await this.backend.remove(key);
        continue;
      }
      this.drafts.set(entry.name, entry);
    }
  }

  draft(name: string): ActionDraftEntry | null {
    return this.drafts.get(name) ?? null;
  }
  entries(): ActionDraftEntry[] {
    return [...this.drafts.values()];
  }

  /** Starts the next free patch version of a published action. */
  async startDraft(
    published: ActionDocument,
    listed: readonly string[],
  ): Promise<ActionDraftEntry> {
    const { name, version } = published.metadata;
    const next = nextDraftVersion(version, listed);
    const document = JSON.parse(JSON.stringify(published)) as ActionDocument;
    document.metadata = { name, version: next };
    return this.keep({
      kind: "draft",
      name,
      version: next,
      base: version,
      document,
      savedAt: "",
    });
  }

  /** Writes a changed document into the draft; it keeps its name and version. */
  async save(
    entry: ActionDraftEntry,
    document: ActionDocument,
  ): Promise<ActionDraftEntry> {
    if (document.metadata?.name !== entry.name)
      throw new Error(`Keep the action's name: ${entry.name}.`);
    return this.keep({
      ...entry,
      document: {
        ...document,
        metadata: { name: entry.name, version: entry.version },
      },
      savedAt: "",
    });
  }

  async remove(name: string): Promise<void> {
    this.drafts.delete(name);
    if (this.prefix !== null)
      await this.backend.remove(actionDraftKey(this.prefix, name));
  }

  /** The drafts these `uses` name, once each. */
  definitionsFor(uses: readonly string[]): ActionDocument[] {
    return [...new Set(uses)]
      .map((ref) => {
        const draft = this.drafts.get(ref.slice(0, ref.lastIndexOf("@")));
        return draft && `${draft.name}@${draft.version}` === ref
          ? draft.document
          : null;
      })
      .filter((document): document is ActionDocument => document !== null);
  }

  private async keep(entry: ActionDraftEntry): Promise<ActionDraftEntry> {
    this.last = Math.max(Date.now(), this.last + 1);
    const saved = { ...entry, savedAt: new Date(this.last).toISOString() };
    const text = JSON.stringify(saved);
    if (new TextEncoder().encode(text).length > ACTION_DRAFT_MAX_BYTES)
      throw new Error(
        "This action is too large to keep on this computer (1 MB at most).",
      );
    if (!readActionEntry(text))
      throw new Error("Studio can't keep this action on this computer.");
    if (!this.drafts.has(saved.name) && this.drafts.size >= MAX_ACTION_DRAFTS)
      throw new Error(
        "Studio keeps 50 action drafts on this computer; publish or delete one first.",
      );
    this.drafts.set(saved.name, saved);
    if (this.prefix !== null)
      await this.backend.set(actionDraftKey(this.prefix, saved.name), text);
    return saved;
  }
}
