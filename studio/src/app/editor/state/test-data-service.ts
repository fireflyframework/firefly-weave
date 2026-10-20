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
  fitsTestData,
  pruneEntries,
  renameEntries,
} from "./test-data-ops";
import { readTestData } from "./test-data-read";
import type { StudioTestData } from "./test-data";

export type TestDataPlace =
  | { kind: "local"; draftId: string }
  | { kind: "connected"; scope: AccountScope; draftId: string }
  | { kind: "memory" };
export interface TestDataHost {
  api: Pick<StudioApi, "request">;
  backend: StoreBackend;
  now(): Date;
  inputs(): CheckInputs;
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
  private problem: string | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private writing: Promise<void> = Promise.resolve();

  constructor(private readonly host: TestDataHost) {
    this.current = emptyTestData(host.now().toISOString());
  }
  data(): StudioTestData {
    return this.current;
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
    this.where = key ? place : { kind: "memory" };
    this.key = key;
    this.problem = null;
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
    this.set(data);
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
    const { data, removed } = pruneEntries(this.current, exists, this.stamp());
    if (!removed.length) return [];
    this.set(data);
    this.keep();
    return removed;
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
      await this.host.backend.set(key, JSON.stringify(data));
    });
  }
}
