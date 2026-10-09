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
import {
  readUiChoice,
  readUiFlag,
  writeUiFlag,
  type FlagStorage,
} from "./editor-flag";

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

export const EDITOR_NAV_KEY = uiKey("weave.editor.navExpanded");
/** From this window width the editor opens with the full navigation, until the viewer chooses. */
export const EDITOR_NAV_WIDE = 1440;

/**
 * Whether the editor's app navigation is expanded: the viewer's saved
 * choice at any width, otherwise expanded from 1440 px.
 */
export function editorNavExpanded(
  width: number,
  storage?: () => FlagStorage | null,
): boolean {
  return readUiChoice(EDITOR_NAV_KEY, storage) ?? width >= EDITOR_NAV_WIDE;
}

/** Saves the choice; false when browser storage refuses it. */
export function setEditorNavExpanded(
  expanded: boolean,
  storage?: () => FlagStorage | null,
): boolean {
  return writeUiFlag(EDITOR_NAV_KEY, expanded, storage);
}
