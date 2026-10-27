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
// The new editor: a per-viewer preference stored under the ui: prefix, on
// unless the person turned it off. `?editor=classic` and `?editor=next` in the
// URL choose for that page, which is how tests open either editor.
import { uiKey } from "./browser-store";
import { studioStorage } from "./studio-storage";

export const EDITOR_NEXT_KEY = uiKey("weave.editorNext");
export type FlagStorage = Pick<Storage, "getItem" | "setItem">;
const defaultStorage = (): FlagStorage | null => studioStorage();
const currentSearch = () =>
  typeof location === "undefined" ? "" : location.search;

/**
 * The true or false choice browser storage holds under `key`; null when
 * nothing is stored, the value is anything else, or storage refuses.
 */
export function readUiChoice(
  key: string,
  storage: () => FlagStorage | null = defaultStorage,
): boolean | null {
  try {
    const value = storage()?.getItem(key);
    return value === "true" ? true : value === "false" ? false : null;
  } catch {
    return null;
  }
}

/** True when browser storage holds "true" under `key`; false for anything else, or when storage refuses. */
export function readUiFlag(
  key: string,
  storage: () => FlagStorage | null = defaultStorage,
): boolean {
  return readUiChoice(key, storage) === true;
}

/** What Studio says when browser storage refuses a per-viewer choice. */
export const CHOICE_NOT_KEPT =
  "Studio couldn't keep this choice in this browser, so it lasts until Studio closes.";

/** Saves a true/false preference under `key`; false when browser storage refuses it. */
export function writeUiFlag(
  key: string,
  value: boolean,
  storage: () => FlagStorage | null = defaultStorage,
): boolean {
  try {
    const store = storage();
    if (!store) return false;
    store.setItem(key, value ? "true" : "false");
    return true;
  } catch {
    return false;
  }
}

/** True when this viewer uses the new editor: always, unless they turned it off. */
export function editorNextEnabled(
  search: string = currentSearch(),
  storage: () => FlagStorage | null = defaultStorage,
): boolean {
  const asked = new URLSearchParams(search).get("editor");
  if (asked === "next") return true;
  if (asked === "classic") return false;
  return readUiChoice(EDITOR_NEXT_KEY, storage) !== false;
}

/** Saves the preference; false when browser storage refuses it. */
export function setEditorNext(
  enabled: boolean,
  storage: () => FlagStorage | null = defaultStorage,
): boolean {
  return writeUiFlag(EDITOR_NEXT_KEY, enabled, storage);
}
