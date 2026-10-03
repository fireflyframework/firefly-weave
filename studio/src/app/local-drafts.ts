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
import { isDesktopShell } from "./export-file";
// Workflows kept on this computer: the browser's localStorage holds an index
// and one document per draft. Storage can be missing, full or blocked, so
// every read and write is guarded and reports failure instead of throwing.

export const indexKey = "weave.localDrafts.v1";
export const draftKey = (id: string) => `weave.localDraft.${id}`;

/**
 * Where local work lives. The desktop app's window keeps no browser data after
 * it quits, so there a draft lasts only until Studio quits.
 */
export function localKeepLabel(desktop = isDesktopShell()) {
  return desktop ? "Kept until you quit" : "Kept on this computer";
}
/** One row of "On this computer". */
export interface LocalDraftEntry {
  id: string;
  name: string;
  version: string;
  /** ISO time of the last save. */
  savedAt: string;
}
/** What reopens a draft: its source, format and canvas layout. */
export interface LocalDraftDocument {
  source: string;
  format: "yaml" | "json";
  layout: unknown;
  savedAt: string;
}
/** A deleted draft, kept so Undo can put it back. */
export interface RemovedDraft {
  entry: LocalDraftEntry;
  document: LocalDraftDocument | null;
}

/** The storage surface the drafts use (localStorage in the browser). */
export type DraftStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown) => (typeof value === "string" ? value : "");

function entryOf(value: unknown): LocalDraftEntry | null {
  if (!isRecord(value) || !text(value["id"])) return null;
  return {
    id: text(value["id"]),
    name: text(value["name"]) || "untitled-workflow",
    version: text(value["version"]),
    savedAt: text(value["savedAt"]),
  };
}

function documentOf(value: unknown): LocalDraftDocument | null {
  if (!isRecord(value) || typeof value["source"] !== "string") return null;
  return {
    source: value["source"],
    format: value["format"] === "json" ? "json" : "yaml",
    layout: value["layout"] ?? null,
    savedAt: text(value["savedAt"]),
  };
}

/**
 * The drafts on this computer. Each method returns null or false when the
 * browser refuses storage; `failed` stays true after the first refusal so
 * the shell explains it once.
 */
export class LocalDrafts {
  failed = false;
  constructor(
    private storage: () => DraftStorage | null = () =>
      typeof localStorage === "undefined" ? null : localStorage,
  ) {}

  private guard<T>(work: (storage: DraftStorage) => T, fallback: T): T {
    try {
      const storage = this.storage();
      if (!storage) throw Error("No storage");
      return work(storage);
    } catch {
      this.failed = true;
      return fallback;
    }
  }

  /** The drafts, newest first; null when storage can't be read. */
  list(): LocalDraftEntry[] | null {
    return this.guard((storage) => {
      const raw = storage.getItem(indexKey);
      const parsed: unknown = raw ? JSON.parse(raw) : [];
      const entries = (Array.isArray(parsed) ? parsed : [])
        .map(entryOf)
        .filter((entry): entry is LocalDraftEntry => !!entry);
      return entries.sort((a, b) => (a.savedAt < b.savedAt ? 1 : -1));
    }, null);
  }

  /** The newest draft, or null. */
  latest(): LocalDraftEntry | null {
    return this.list()?.[0] ?? null;
  }

  read(id: string): LocalDraftDocument | null {
    return this.guard((storage) => {
      const raw = storage.getItem(draftKey(id));
      return raw ? documentOf(JSON.parse(raw)) : null;
    }, null);
  }

  /** Saves a draft and moves it to the top of the index; false on failure. */
  save(entry: LocalDraftEntry, document: LocalDraftDocument): boolean {
    return this.guard((storage) => {
      const entries = (this.list() ?? []).filter((e) => e.id !== entry.id);
      storage.setItem(draftKey(entry.id), JSON.stringify(document));
      storage.setItem(indexKey, JSON.stringify([entry, ...entries]));
      return true;
    }, false);
  }

  /** Deletes a draft; returns it for Undo, or null when nothing was removed. */
  remove(id: string): RemovedDraft | null {
    return this.guard((storage) => {
      const entries = this.list() ?? [];
      const entry = entries.find((e) => e.id === id);
      if (!entry) return null;
      const document = this.read(id);
      storage.removeItem(draftKey(id));
      storage.setItem(
        indexKey,
        JSON.stringify(entries.filter((e) => e.id !== id)),
      );
      return { entry, document };
    }, null);
  }

  /** Puts a deleted draft back (Undo). */
  restore(removed: RemovedDraft): boolean {
    if (!removed.document) return false;
    return this.save(removed.entry, removed.document);
  }
}
