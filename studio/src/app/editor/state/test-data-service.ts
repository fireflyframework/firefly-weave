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
// One workflow's test data: what the trigger's Test event, pinned output and
// (later) scripts hold. It lives in memory while the workflow is open and on
// this computer next to the draft (a local draft's key, or a copy under the
// account's prefix for a platform draft). Every changed entry is checked
// before it is kept; a refused one is never kept anywhere. Test data is
// outside the workflow's undo history: callers offer Undo with `restore`.
//
// A platform draft's test data also lives on the platform. The platform's
// copy replaces this computer's when the draft opens, except entries the
// platform couldn't classify yet (an action not published): those are kept
// on this computer only, listed beside the copy, and sent once publishing
// makes them classifiable. A value the platform refuses as secret leaves
// every copy at once.
import { signal } from "@angular/core";
import type { StudioApi } from "../../api";
import {
  connectedPrefix,
  storeKey,
  type AccountScope,
  type StoreBackend,
} from "./browser-store";
import { checkEntries, type CheckInputs } from "./test-data-check";
import {
  TEST_DATA_TOO_LARGE,
  emptyTestData,
  entriesOf,
  fitsTestData,
  ownerOf,
  pruneEntries,
  renameEntries,
} from "./test-data-ops";
import { readTestData } from "./test-data-read";
import type { StudioDocument, StudioTestData } from "./test-data";

const unescapePointer = (segment: string) =>
  segment.replace(/~1/g, "/").replace(/~0/g, "~");
/** Where a document pointer points: a section and key, or a called workflow's mock. */
function locate(
  pointer: string,
):
  | { section: "input" }
  | { section: string; key: string }
  | { section: "workflows"; reference: string; key: string }
  | null {
  const parts = pointer.split("/").slice(1).map(unescapePointer);
  if (parts.length === 1 && parts[0] === "input") return { section: "input" };
  if (parts.length === 2) return { section: parts[0]!, key: parts[1]! };
  if (parts.length === 4 && parts[0] === "workflows" && parts[2] === "mocks")
    return { section: "workflows", reference: parts[1]!, key: parts[3]! };
  return null;
}
type Sections = Record<string, unknown>;
/** The entry at a pointer, or undefined. */
function entryAt(data: StudioTestData, pointer: string): unknown {
  const at = locate(pointer);
  const doc = data as unknown as Sections;
  if (!at) return undefined;
  if (at.section === "input") return data.input;
  if ("reference" in at) return data.workflows?.[at.reference]?.mocks[at.key];
  return (doc[at.section] as Sections | undefined)?.[
    (at as { key: string }).key
  ];
}
/** The document with the entry at `pointer` set (or removed when `entry` is undefined). */
function withEntry(
  data: StudioTestData,
  pointer: string,
  entry: unknown,
): StudioTestData {
  const at = locate(pointer);
  if (!at) return data;
  const next = { ...(data as unknown as Sections) };
  if (at.section === "input") {
    if (entry === undefined) delete next["input"];
    else next["input"] = entry;
    return next as unknown as StudioTestData;
  }
  if ("reference" in at) {
    const workflows = { ...(data.workflows ?? {}) } as Record<
      string,
      { mocks: Sections }
    >;
    const mocks = { ...(workflows[at.reference]?.mocks ?? {}) };
    if (entry === undefined) delete mocks[at.key];
    else mocks[at.key] = entry;
    workflows[at.reference] = { mocks };
    next["workflows"] = workflows;
    return next as unknown as StudioTestData;
  }
  const key = (at as { key: string }).key;
  const section = { ...((next[at.section] as Sections | undefined) ?? {}) };
  if (entry === undefined) delete section[key];
  else section[key] = entry;
  if (Object.keys(section).length) next[at.section] = section;
  else delete next[at.section];
  return next as unknown as StudioTestData;
}

/** The key of a list kept beside a copy: `<copy key>.local`. */
const storeKeyFor = (key: string, suffix: string) => `${key}.${suffix}`;

export type TestDataPlace =
  | { kind: "local"; draftId: string }
  | { kind: "connected"; scope: AccountScope; draftId: string }
  | { kind: "memory" };
export interface TestDataHost {
  api: Pick<StudioApi, "request">;
  backend: StoreBackend;
  now(): Date;
  inputs(): CheckInputs;
  /** Called after each change is kept (a platform draft then saves it with the draft). */
  changed?(): void;
}
export type ChangeOutcome =
  | { ok: true; previous: StudioTestData }
  | { ok: false; message: string; pointer?: string };

const UNREADABLE =
  "Studio couldn't read the test data kept for this workflow, so it starts empty.";
const KEEP_MS = 800;

export class TestDataService {
  readonly version = signal(0);
  private current: StudioTestData;
  private where: TestDataPlace = { kind: "memory" };
  private key: string | null = null;
  private opened = 0;
  /** Bumped each time a document finishes opening: whose data `data()` holds changed. */
  private loaded = 0;
  private problem: string | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private writing: Promise<void> = Promise.resolve();
  /** Pointers of entries kept on this computer until the platform can classify them. */
  private waiting = new Set<string>();
  private waitingKey: string | null = null;
  /** Entries pruned with their steps, by pointer, while this opening lasts. */
  private readonly deleted = new Map<string, unknown>();
  /** Keys of accounts signed out since: a write queued for one never runs. */
  private readonly forgotten = new Set<string>();
  /** The platform revision this copy is based on. */
  private platformRevision: number | undefined;

  constructor(private readonly host: TestDataHost) {
    this.current = emptyTestData(host.now().toISOString());
  }
  data(): StudioTestData {
    return this.current;
  }
  /** Which opening `data()` comes from; it changes when another workflow or account opens. */
  generation(): number {
    return this.loaded;
  }
  place(): TestDataPlace {
    return this.where;
  }
  notice(): string | null {
    return this.problem;
  }
  async keyOf(place: TestDataPlace): Promise<string | null> {
    if (place.kind === "local")
      return storeKey("local:", `weave.localDraft.${place.draftId}.testData`);
    if (place.kind === "connected") {
      const prefix = await connectedPrefix(place.scope).catch(() => null);
      return prefix ? storeKey(prefix, `testData.${place.draftId}`) : null;
    }
    return null;
  }
  async open(place: TestDataPlace): Promise<void> {
    await this.flush();
    const opening = ++this.opened;
    const key = await this.keyOf(place);
    const raw = key ? await this.host.backend.get(key) : null;
    if (opening !== this.opened) return;
    this.deleted.clear();
    // Opened again for that account: it signed in again.
    if (key) this.forgotten.delete(key);
    this.where = key ? place : { kind: "memory" };
    this.key = key;
    this.problem = null;
    this.platformRevision = undefined;
    this.waitingKey =
      key && place.kind === "connected" ? storeKeyFor(key, "local") : null;
    if (this.waitingKey) this.forgotten.delete(this.waitingKey);
    this.waiting = new Set();
    if (this.waitingKey) {
      try {
        const list: unknown = JSON.parse(
          (await this.host.backend.get(this.waitingKey)) ?? "[]",
        );
        if (Array.isArray(list))
          this.waiting = new Set(
            list.filter((item): item is string => typeof item === "string"),
          );
      } catch {
        this.waiting = new Set();
      }
      if (opening !== this.opened) return;
    }
    let data = emptyTestData(this.host.now().toISOString());
    if (raw !== null) {
      let parsed: unknown = null;
      try {
        parsed = JSON.parse(raw);
      } catch {
        parsed = null;
      }
      const reading = readTestData(parsed);
      if (reading.ok) data = reading.data;
      else this.problem = UNREADABLE;
    }
    this.loaded++;
    this.set(data);
  }
  /**
   * The account signed out: its data leaves memory now, and nothing waiting
   * or queued is written under its key afterwards (no flush, unlike `open`).
   */
  forget(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    for (const key of [this.key, this.waitingKey])
      if (key && this.where.kind === "connected") this.forgotten.add(key);
    this.opened++;
    this.deleted.clear();
    this.where = { kind: "memory" };
    this.key = null;
    this.waitingKey = null;
    this.waiting = new Set();
    this.problem = null;
    this.platformRevision = undefined;
    this.loaded++;
    this.set(emptyTestData(this.stamp()));
  }
  /** Checks the entries at `pointers` and keeps the document if every one may be kept. */
  async change(
    next: StudioTestData,
    pointers: string[],
  ): Promise<ChangeOutcome> {
    if (!fitsTestData(next)) return { ok: false, message: TEST_DATA_TOO_LARGE };
    const opening = this.opened;
    const previous = this.current;
    const verdicts = pointers.length
      ? await checkEntries(this.host.api, next, pointers, this.host.inputs())
      : new Map();
    if (opening !== this.opened || this.current !== previous)
      return {
        ok: false,
        message: "The test data changed meanwhile. Try again.",
      };
    for (const [pointer, verdict] of verdicts)
      if (!verdict.ok) return { ok: false, message: verdict.message, pointer };
    this.set(next);
    this.keep();
    return { ok: true, previous };
  }
  restore(previous: StudioTestData, ifVersion: number): boolean {
    if (this.version() !== ifVersion) return false;
    this.set(previous);
    this.keep();
    return true;
  }
  rename(from: string, to: string): void {
    const next = renameEntries(this.current, from, to, this.stamp());
    if (next === this.current) return;
    this.set(next);
    this.keep();
  }
  prune(exists: (id: string) => boolean): string[] {
    const before = this.current;
    const { data, removed } = pruneEntries(before, exists, this.stamp());
    if (!removed.length) return [];
    // Kept for an Undo that brings the steps back (the toast's or the toolbar's).
    const left = new Set(entriesOf(data).map(({ path }) => path));
    for (const { path, entry } of entriesOf(before))
      if (!left.has(path)) this.deleted.set(path, entry);
    this.set(data);
    this.keep();
    return removed;
  }
  /**
   * Steps came back (an Undo of their deletion, a Redo of their insertion):
   * the entries their deletion took return, unless one was set since.
   * Returns their pointers.
   */
  revive(exists: (id: string) => boolean): string[] {
    let next = this.current;
    const revived: string[] = [];
    for (const [path, entry] of [...this.deleted]) {
      const at = locate(path);
      const key = at && "key" in at ? at.key : null;
      if (!key || !exists(ownerOf(key, entry))) continue;
      this.deleted.delete(path);
      if (entryAt(next, path) !== undefined) continue;
      next = withEntry(next, path, entry);
      revived.push(path);
    }
    if (!revived.length) return [];
    this.set({ ...next, updatedAt: this.stamp() });
    this.keep();
    return revived;
  }
  // --- the platform side (platform drafts) ---------------------------------
  /** The platform's document: it replaces this copy, except entries waiting for publishing. */
  syncFrom(document: StudioDocument | null): void {
    if (!document) return;
    const reading = readTestData(document.document);
    if (!reading.ok) {
      this.problem = UNREADABLE;
      return;
    }
    let next = reading.data;
    for (const pointer of this.waiting) {
      const entry = entryAt(this.current, pointer);
      if (entry !== undefined) next = withEntry(next, pointer, entry);
    }
    this.platformRevision = document.revision;
    this.set(next);
    this.keep();
  }
  /** What the platform receives: the document without the entries kept on this computer. */
  toPlatform(): { document: StudioTestData; localOnly: string[] } {
    let document = this.current;
    const localOnly = [...this.waiting].filter(
      (pointer) => entryAt(document, pointer) !== undefined,
    );
    for (const pointer of localOnly)
      document = withEntry(document, pointer, undefined);
    return { document, localOnly };
  }
  /** Entries the platform can't classify yet ("Kept on this computer until the action is published"). */
  markLocalOnly(paths: readonly string[]): void {
    for (const path of paths) this.waiting.add(path);
    this.keepWaiting();
    this.version.update((value) => value + 1);
  }
  /** Publishing made them classifiable: the next save sends them. */
  releaseLocalOnly(): void {
    if (!this.waiting.size) return;
    this.waiting.clear();
    this.keepWaiting();
    this.version.update((value) => value + 1);
  }
  localOnly(): ReadonlySet<string> {
    return this.waiting;
  }
  /** Entries the platform refused as secret: removed from this copy and every copy on this computer, now. */
  async dropSecret(paths: readonly string[]): Promise<void> {
    let next = this.current;
    for (const path of paths) {
      next = withEntry(next, path, undefined);
      this.waiting.delete(path);
    }
    this.set(next);
    this.keepWaiting();
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.write();
    await this.writing;
  }
  revision(): number | undefined {
    return this.platformRevision;
  }
  /** After a save on the platform: the revision the next save names. */
  savedOnPlatform(revision: number): void {
    this.platformRevision = revision;
  }
  private keepWaiting(): void {
    const key = this.waitingKey;
    if (!key) return;
    const list = [...this.waiting].sort();
    this.writing = this.writing.then(async () => {
      if (this.forgotten.has(key)) return;
      if (list.length) await this.host.backend.set(key, JSON.stringify(list));
      else await this.host.backend.remove(key);
    });
  }

  async flush(): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
      this.write();
    }
    await this.writing;
  }

  private stamp(): string {
    return this.host.now().toISOString();
  }
  private set(data: StudioTestData): void {
    this.current = data;
    this.version.update((value) => value + 1);
  }
  private keep(): void {
    this.host.changed?.();
    if (!this.key) return;
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.write();
    }, KEEP_MS);
  }
  private write(): void {
    const key = this.key;
    const data = this.current;
    if (!key) return;
    this.writing = this.writing.then(async () => {
      if (this.forgotten.has(key)) return;
      await this.host.backend.set(key, JSON.stringify(data));
    });
  }
}
