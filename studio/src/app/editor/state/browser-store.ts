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
// Every key Studio keeps on this computer goes through this module: connected
// work under `<profileId>:<subjectHash>:<tenantId>:<projectId>:`, local work
// under `local:`, preferences that hold no workflow data under `ui:`. Signing
// out of a platform, or removing it, purges that profile's keys. The desktop
// app keeps the same keys in the Studio host store instead of the browser.

export const STORE_KEY_PATTERN = /^[A-Za-z0-9:._-]{1,256}$/;
export const LOCAL_PREFIX = "local:";
export const UI_PREFIX = "ui:";
const NAME_PATTERN = /^[A-Za-z0-9._-]+$/;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Who and where connected work belongs to. */
export interface AccountScope {
  /** The saved platform's name. */
  profileName: string;
  /** The signed-in subject; null when the identity provider shared none. */
  subject: string | null;
  tenantId: string;
  projectId: string;
}

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(text),
  );
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/** "p" and 16 hex digits of the profile name's SHA-256: key-safe, and never "local" or "ui". */
export async function profileId(profileName: string): Promise<string> {
  return "p" + (await sha256Hex(profileName)).slice(0, 16);
}
/** The first 16 hex digits of the SHA-256 of the signed-in subject. */
export async function subjectHash(subject: string): Promise<string> {
  return (await sha256Hex(subject)).slice(0, 16);
}
/** The prefix of every key a saved platform owns, across accounts and workspaces. */
export async function profilePrefix(profileName: string): Promise<string> {
  return `${await profileId(profileName)}:`;
}
/**
 * The prefix of connected work, or null when the subject is unknown: data
 * nobody can attribute to an account is kept in memory only.
 */
export async function connectedPrefix(
  scope: AccountScope,
): Promise<string | null> {
  if (!scope.subject) return null;
  const ids = [scope.tenantId, scope.projectId].map((id) => {
    if (!UUID_PATTERN.test(id))
      throw new RangeError(`Expected a tenant or project UUID, not "${id}".`);
    return id.toLowerCase();
  });
  return `${await profileId(scope.profileName)}:${await subjectHash(scope.subject)}:${ids.join(":")}:`;
}
/** A full key: a prefix from this module and a name of letters, digits, dots, underscores and hyphens. */
export function storeKey(prefix: string, name: string): string {
  const key = prefix + name;
  if (!NAME_PATTERN.test(name) || !STORE_KEY_PATTERN.test(key))
    throw new RangeError(`"${key}" isn't a valid Studio storage key.`);
  return key;
}
export const localKey = (name: string): string => storeKey(LOCAL_PREFIX, name);
export const uiKey = (name: string): string => storeKey(UI_PREFIX, name);

/** Where keys live: browser storage, or the desktop's Studio host store. */
export interface StoreBackend {
  keys(prefix: string): Promise<string[]>;
  get(key: string): Promise<string | null>;
  set(key: string, value: string): Promise<boolean>;
  remove(key: string): Promise<boolean>;
}
export type BrowserStorage = Pick<
  Storage,
  "getItem" | "setItem" | "removeItem" | "key" | "length"
>;
const defaultStorage = (): BrowserStorage | null =>
  typeof localStorage === "undefined" ? null : localStorage;

/** Browser storage as a backend; storage that refuses reads as empty and fails writes. */
export function browserBackend(
  storage: () => BrowserStorage | null = defaultStorage,
): StoreBackend {
  const guard = <T>(work: (store: BrowserStorage) => T, fallback: T) => {
    try {
      const store = storage();
      return Promise.resolve(store ? work(store) : fallback);
    } catch {
      return Promise.resolve(fallback);
    }
  };
  return {
    keys: (prefix) =>
      guard((store) => {
        const keys: string[] = [];
        for (let index = 0; index < store.length; index++) {
          const key = store.key(index);
          if (key !== null && key.startsWith(prefix)) keys.push(key);
        }
        return keys;
      }, []),
    get: (key) => guard((store) => store.getItem(key), null),
    set: (key, value) =>
      guard((store) => {
        store.setItem(key, value);
        return true;
      }, false),
    remove: (key) =>
      guard((store) => {
        store.removeItem(key);
        return true;
      }, false),
  };
}

/** Deletes every key of one saved platform; returns how many went. */
export async function purgeProfile(
  backend: StoreBackend,
  profileName: string,
): Promise<number> {
  let removed = 0;
  for (const key of await backend.keys(await profilePrefix(profileName)))
    if (await backend.remove(key)) removed++;
  return removed;
}

/** Keys Studio wrote before prefixes existed. */
export const LEGACY_KEYS: {
  readonly local: readonly string[];
  readonly localDraftPrefix: string;
  readonly ui: readonly string[];
  readonly runInputPrefix: string;
} = {
  local: ["weave.localDrafts.v1"],
  localDraftPrefix: "weave.localDraft.",
  ui: ["weave.studio.inspectorWidth", "weave.studio.inspectorSections"],
  runInputPrefix: "weave-studio-run-input:",
};
export interface LegacyMigration {
  moved: number;
  deleted: number;
  kept: number;
}
/**
 * Moves keys from before prefixes once: local drafts under `local:`, pane
 * preferences under `ui:`. Run inputs can't be attributed to an account, so
 * they are deleted. An existing new key is never overwritten; a key that
 * can't move (the new key would be invalid, or the write failed) is kept.
 */
export async function migrateLegacyKeys(
  backend: StoreBackend,
): Promise<LegacyMigration> {
  const result: LegacyMigration = { moved: 0, deleted: 0, kept: 0 };
  for (const key of await backend.keys("")) {
    if (key.startsWith(LEGACY_KEYS.runInputPrefix)) {
      if (await backend.remove(key)) result.deleted++;
      continue;
    }
    const prefix =
      LEGACY_KEYS.local.includes(key) ||
      key.startsWith(LEGACY_KEYS.localDraftPrefix)
        ? LOCAL_PREFIX
        : LEGACY_KEYS.ui.includes(key)
          ? UI_PREFIX
          : null;
    if (!prefix) continue;
    const target = prefix + key;
    if (!STORE_KEY_PATTERN.test(target)) {
      result.kept++;
      continue;
    }
    if ((await backend.get(target)) !== null) {
      if (await backend.remove(key)) result.deleted++;
      continue;
    }
    const value = await backend.get(key);
    if (value === null || !(await backend.set(target, value))) {
      result.kept++;
      continue;
    }
    await backend.remove(key);
    result.moved++;
  }
  return result;
}
