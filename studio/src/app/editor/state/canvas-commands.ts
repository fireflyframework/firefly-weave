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
// What the canvas does with a key press: the rows of the shortcut table the
// canvas offers today, read for the platform Studio runs on. Rows for
// features still to come (pins, the clipboard, notes, test runs) stay in
// the table and do nothing here yet.
import {
  commandFor,
  eventCombo,
  shortcutSheet,
  type KeyEventLike,
  type KeymapCommand,
  type KeyPlatform,
  type SheetSection,
} from "./keymap";

export const CANVAS_COMMANDS: ReadonlySet<KeymapCommand> =
  new Set<KeymapCommand>([
    "zoomIn",
    "zoomOut",
    "zoomReset",
    "fitView",
    "openStepDetails",
    "renameStep",
    "openAddStep",
    "searchAddStep",
    "selectAll",
    "duplicate",
    "deleteSelection",
    "undo",
    "redo",
    "previousStep",
    "nextStep",
    "laneAbove",
    "laneBelow",
    "extendUpstream",
    "extendDownstream",
    "save",
    "escape",
    "showShortcuts",
  ]);

/** Commands about steps: only from a step or the canvas, never from a "+", a tool or a menu. */
export const STEP_COMMANDS: ReadonlySet<KeymapCommand> = new Set<KeymapCommand>(
  [
    "openStepDetails",
    "renameStep",
    "selectAll",
    "duplicate",
    "deleteSelection",
    "previousStep",
    "nextStep",
    "laneAbove",
    "laneBelow",
    "extendUpstream",
    "extendDownstream",
  ],
);

/** Cmd keys on macOS and iPadOS, Ctrl elsewhere. */
export function keyPlatform(
  platform: string = typeof navigator === "undefined" ? "" : navigator.platform,
): KeyPlatform {
  return /Mac|iPhone|iPad|iPod/i.test(platform) ? "mac" : "other";
}

export function canvasCommand(
  event: KeyEventLike,
  options: { platform: KeyPlatform; typing: boolean; onStep: boolean },
): KeymapCommand | null {
  const command = commandFor(eventCombo(event, options.platform), "canvas", {
    platform: options.platform,
    typing: options.typing,
  });
  if (!command || !CANVAS_COMMANDS.has(command)) return null;
  if (STEP_COMMANDS.has(command) && !options.onStep) return null;
  return command;
}

/** The "?" sheet for the canvas: what works everywhere and on the canvas. */
export function canvasSheet(platform: KeyPlatform): SheetSection[] {
  return shortcutSheet(platform, {
    surfaces: ["any", "canvas"],
    available: CANVAS_COMMANDS,
  });
}
