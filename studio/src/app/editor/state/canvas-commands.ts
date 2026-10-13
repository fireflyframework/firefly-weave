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
// canvas offers today, read for the platform Studio runs on, and a focused
// sticky note's own keys. Rows for features still to come (pins, test runs)
// stay in the table and do nothing here yet.
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
    "copy",
    "cut",
    "paste",
    "addStickyNote",
    "tidyLayout",
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
    "copy",
    "cut",
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

/** The Add a step panel's keys. */
export const PANEL_COMMANDS: ReadonlySet<KeymapCommand> =
  new Set<KeymapCommand>([
    "panelUp",
    "panelDown",
    "panelInsert",
    "panelOpen",
    "panelBack",
  ]);

/** A focused sticky note's keys (not while its text is edited). */
export const NOTE_COMMANDS: ReadonlySet<KeymapCommand> = new Set<KeymapCommand>(
  [
    "editNote",
    "deleteNote",
    "nudgeNoteLeft",
    "nudgeNoteRight",
    "nudgeNoteUp",
    "nudgeNoteDown",
  ],
);

export function noteCommand(
  event: KeyEventLike,
  platform: KeyPlatform,
): KeymapCommand | null {
  const command = commandFor(eventCombo(event, platform), "note", { platform });
  return command && NOTE_COMMANDS.has(command) ? command : null;
}

/** The "?" sheet for the canvas: what works everywhere, on the canvas and in Add a step. */
export function canvasSheet(platform: KeyPlatform): SheetSection[] {
  return shortcutSheet(platform, {
    surfaces: ["any", "canvas", "note", "addStep"],
    available: new Set([
      ...CANVAS_COMMANDS,
      ...NOTE_COMMANDS,
      ...PANEL_COMMANDS,
    ]),
  });
}
