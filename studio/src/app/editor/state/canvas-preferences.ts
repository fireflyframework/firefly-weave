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
// The canvas's choices for this viewer, kept under the ui: prefix: they
// hold no workflow data, so they stay when a platform signs out.
import { uiKey } from "./browser-store";
import { readUiFlag, writeUiFlag, type FlagStorage } from "./editor-flag";

export const MINIMAP_KEY = uiKey("weave.canvas.minimap");

/** "Show minimap" in the canvas tools keeps the minimap open. */
export function minimapPinned(storage?: () => FlagStorage | null): boolean {
  return readUiFlag(MINIMAP_KEY, storage);
}

/** Saves the choice; false when browser storage refuses it. */
export function setMinimapPinned(
  pinned: boolean,
  storage?: () => FlagStorage | null,
): boolean {
  return writeUiFlag(MINIMAP_KEY, pinned, storage);
}
