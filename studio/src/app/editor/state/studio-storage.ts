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
// Where Studio keeps what it keeps on this computer. In a browser that is
// localStorage. The desktop window keeps no browser storage after it quits,
// so there Studio keeps a mirror in memory, filled from the Studio host's
// store at start and written through to it, key by key, in order. Every
// module reads and writes through `studioStorage()`, so the keys are the
// same everywhere and signing out purges them in one place. After a sign-out
// the host refuses the platform's keys (409 WV-STUDIO-STORE-SIGNED-OUT) until
// the next sign-in: the mirror then drops the value instead of keeping it,
// and tells whoever listens (`onStudioStorageDropped`), so it isn't silent.
import type { StudioApi } from "../../api";
import { isDesktopShell } from "../../export-file";
import {
  browserBackend,
  migrateLegacyKeys,
  type BrowserStorage,
  type LegacyMigration,
  type StoreBackend,
} from "./browser-store";

const route = (key: string) => `/studio/local/store/${encodeURIComponent(key)}`;

/** The host's answer to a write for a platform signed out since. */
export const SIGNED_OUT = "WV-STUDIO-STORE-SIGNED-OUT";
const codeOf = (error: unknown) =>
  typeof error === "object" && error !== null && "code" in error
    ? (error as { code: unknown }).code
    : undefined;

/**
 * The Studio host's store (desktop app): refusals read as empty and fail
 * writes, as browser storage does. A write refused because its platform was
 * signed out calls `signedOut` and counts as done: the value isn't kept.
 */
export function hostBackend(
  api: Pick<StudioApi, "request">,
  signedOut?: (key: string) => void,
): StoreBackend {
  const quiet = async <T>(work: () => Promise<T>, fallback: T): Promise<T> => {
    try {
      return await work();
    } catch {
      return fallback;
    }
  };
  return {
    keys: (prefix) =>
      quiet(async () => {
        const answer = await api.request<{ keys: string[] }>(
          `/studio/local/store?prefix=${encodeURIComponent(prefix)}`,
        );
        return answer.keys;
      }, []),
    get: (key) =>
      quiet(
        async () => (await api.request<{ value: string }>(route(key))).value,
        null,
      ),
    set: async (key, value) => {
      try {
        await api.request(route(key), "PUT", { value });
        return true;
      } catch (error) {
        if (codeOf(error) !== SIGNED_OUT) return false;
        signedOut?.(key);
        return true;
      }
    },
    remove: (key) =>
      quiet(async () => {
        await api.request(route(key), "DELETE");
        return true;
      }, false),
  };
}

/** Synchronous storage in memory, written through to a backend in order. */
export class MirroredStorage implements BrowserStorage {
  private readonly values = new Map<string, string>();
  private backend: StoreBackend | null = null;
  /** The value each key will have once its write lands (null: removed). */
  private readonly pending = new Map<string, string | null>();
  private running: Promise<boolean> = Promise.resolve(true);
  failed = false;

  get length(): number {
    return this.values.size;
  }
  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null;
  }
  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.values.set(key, value);
    this.queue(key, value);
  }
  removeItem(key: string): void {
    this.values.delete(key);
    this.queue(key, null);
  }
  private readonly droppedListeners = new Set<(key: string) => void>();
  /** Drops a value the host won't keep (its platform was signed out), without writing. */
  drop(key: string): void {
    this.values.delete(key);
    for (const listener of this.droppedListeners) listener(key);
  }
  /** Hears every value dropped; returns how to stop. */
  onDropped(listener: (key: string) => void): () => void {
    this.droppedListeners.add(listener);
    return () => this.droppedListeners.delete(listener);
  }
  /** Fills the mirror from the host; returns how many keys came. */
  async hydrate(backend: StoreBackend): Promise<number> {
    this.backend = backend;
    for (const key of await backend.keys("")) {
      const value = await backend.get(key);
      if (value !== null && !this.values.has(key)) this.values.set(key, value);
    }
    return this.values.size;
  }
  private queue(key: string, value: string | null): void {
    if (!this.backend) return;
    const first = !this.pending.has(key);
    this.pending.set(key, value);
    if (!first) return;
    const backend = this.backend;
    this.running = this.running.then(async (ok) => {
      const latest = this.pending.get(key);
      this.pending.delete(key);
      const written =
        latest === null || latest === undefined
          ? await backend.remove(key)
          : await backend.set(key, latest);
      if (!written && latest !== null) this.failed = true;
      return ok && (written || latest === null);
    });
  }
  /** Waits for every write so far; false when one was refused. */
  flush(): Promise<boolean> {
    return this.running;
  }
}

let mirror: MirroredStorage | null = null;
/** The storage every module uses: localStorage in a browser, the mirror in the desktop app. */
export function studioStorage(): BrowserStorage | null {
  if (mirror) return mirror;
  return typeof localStorage === "undefined" ? null : localStorage;
}
export const studioBackend = (): StoreBackend => browserBackend(studioStorage);

/**
 * Before Studio reads anything it keeps: in the desktop app, fill the mirror
 * from the host; everywhere, move keys from before prefixes once.
 */
export async function bootStudioStorage(
  api: Pick<StudioApi, "request">,
  desktop = isDesktopShell(),
): Promise<{ migrated: LegacyMigration; desktop: boolean }> {
  if (desktop && !mirror) {
    const created = new MirroredStorage();
    mirror = created;
    await created.hydrate(hostBackend(api, (key) => created.drop(key)));
  }
  return { migrated: await migrateLegacyKeys(studioBackend()), desktop };
}
/** Hears every value the desktop host refused to keep because its platform is signed out there. */
export const onStudioStorageDropped = (
  listener: (key: string) => void,
): (() => void) => (mirror ? mirror.onDropped(listener) : () => undefined);
/** Pending desktop writes (on close, Save and quitting). */
export const flushStudioStorage = (): Promise<boolean> =>
  mirror ? mirror.flush() : Promise.resolve(true);
