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
// The editor's one shortcut table. The key handler and the "?" sheet both
// read it, so they can't disagree. Each key combination appears in one row
// per platform; a row names what the keys do on each surface. Shortcuts act
// when focus is on the canvas, the outline or step details, never while typing
// in a field, except the rows marked whileTyping. `Mod` is Ctrl on Windows and
// Linux and Cmd on macOS.

export type KeyPlatform = "mac" | "other";
/** Where focus is, from the most specific: a Fixed field or the formula editor sit inside step details. */
export type KeymapSurface =
  | "canvas"
  | "outline"
  | "stepDetails"
  | "fixedField"
  | "formula"
  | "addStep"
  | "decisionGrid"
  | "runLog";
export type KeymapCommand =
  | "zoomIn"
  | "zoomOut"
  | "zoomReset"
  | "fitView"
  | "openStepDetails"
  | "renameStep"
  | "openAddStep"
  | "searchAddStep"
  | "addStickyNote"
  | "togglePin"
  | "selectAll"
  | "copy"
  | "cut"
  | "paste"
  | "duplicate"
  | "deleteSelection"
  | "undo"
  | "redo"
  | "previousStep"
  | "nextStep"
  | "laneAbove"
  | "laneBelow"
  | "extendUpstream"
  | "extendDownstream"
  | "executeWorkflow"
  | "executeStep"
  | "save"
  | "tidyLayout"
  | "extractSubWorkflow"
  | "replaceAction"
  | "copyWebhookUrl"
  | "copyWebhookTestUrl"
  | "toggleRunLog"
  | "focusEntryInput"
  | "focusEntryOutput"
  | "toggleBreakpoint"
  | "previousStepDetails"
  | "nextStepDetails"
  | "switchToMapped"
  | "escape"
  | "showShortcuts"
  | "panelUp"
  | "panelDown"
  | "panelInsert"
  | "panelOpen"
  | "panelBack"
  | "cellUp"
  | "cellDown"
  | "cellLeft"
  | "cellRight"
  | "nextCell"
  | "previousCell"
  | "editCell"
  | "clearCell"
  | "addRule"
  | "moveRuleUp"
  | "moveRuleDown"
  | "duplicateRule"
  | "nextRegion"
  | "previousRegion"
  | "searchPane"
  | "moveRowUp"
  | "moveRowDown"
  | "mapTo";

export interface KeymapRow {
  readonly id: string;
  /** Canonical combinations: modifiers in the order Mod, Ctrl, Meta, Alt, Shift, then the key. */
  readonly keys: readonly string[];
  /** What the keys do per surface; `any` applies where no surface entry exists; null means not bound there. */
  readonly commands: Readonly<
    Partial<Record<KeymapSurface | "any", KeymapCommand | null>>
  >;
  /** Acts while typing in a field (Ctrl/Cmd+S, Ctrl/Cmd+Enter, undo and redo). */
  readonly whileTyping?: boolean;
  /** Only on this platform. */
  readonly platform?: KeyPlatform;
  /** What n8n does with the keys, when it differs. */
  readonly n8n?: string;
  /** Shown for rows that bind no command. */
  readonly note?: string;
}

const typing = { whileTyping: true } as const;

export const KEYMAP: readonly KeymapRow[] = [
  { id: "zoom-in", keys: ["+"], commands: { canvas: "zoomIn" } },
  {
    id: "zoom-in-or-map",
    keys: ["="],
    commands: { canvas: "zoomIn", fixedField: "switchToMapped" },
  },
  { id: "zoom-out", keys: ["-", "_"], commands: { canvas: "zoomOut" } },
  { id: "zoom-reset", keys: ["0", "Mod+0"], commands: { canvas: "zoomReset" } },
  { id: "fit-view", keys: ["1"], commands: { canvas: "fitView" } },
  {
    id: "open",
    keys: ["Enter"],
    commands: {
      canvas: "openStepDetails",
      outline: "openStepDetails",
      addStep: "panelInsert",
      decisionGrid: "editCell",
      stepDetails: "mapTo",
    },
  },
  {
    id: "rename",
    keys: ["F2"],
    commands: {
      canvas: "renameStep",
      outline: "renameStep",
      decisionGrid: "editCell",
      stepDetails: "renameStep",
    },
  },
  {
    id: "add-step",
    keys: ["N"],
    commands: { canvas: "openAddStep", outline: "openAddStep" },
  },
  {
    id: "search-steps",
    keys: ["/"],
    commands: {
      canvas: "searchAddStep",
      outline: "searchAddStep",
      stepDetails: "searchPane",
    },
    n8n: "The Add a step panel also opens with N.",
  },
  {
    id: "sticky-note",
    keys: ["Shift+S"],
    commands: { canvas: "addStickyNote" },
  },
  {
    id: "pin",
    keys: ["P"],
    commands: { canvas: "togglePin", outline: "togglePin" },
  },
  {
    id: "select-all",
    keys: ["Mod+A"],
    commands: { canvas: "selectAll", outline: "selectAll" },
  },
  {
    id: "copy",
    keys: ["Mod+C"],
    commands: { canvas: "copy", outline: "copy" },
  },
  { id: "cut", keys: ["Mod+X"], commands: { canvas: "cut", outline: "cut" } },
  {
    id: "paste",
    keys: ["Mod+V"],
    commands: { canvas: "paste", outline: "paste" },
  },
  {
    id: "duplicate",
    keys: ["Mod+D"],
    commands: {
      canvas: "duplicate",
      outline: "duplicate",
      decisionGrid: "duplicateRule",
    },
  },
  {
    id: "delete",
    keys: ["Delete"],
    commands: {
      canvas: "deleteSelection",
      outline: "deleteSelection",
      decisionGrid: "clearCell",
    },
  },
  {
    id: "delete-backspace",
    keys: ["Backspace"],
    commands: { canvas: "deleteSelection", outline: "deleteSelection" },
  },
  {
    id: "undo",
    keys: ["Mod+Z"],
    commands: { any: "undo", formula: null },
    ...typing,
  },
  {
    id: "redo",
    keys: ["Mod+Shift+Z"],
    commands: { any: "redo", formula: null },
    ...typing,
  },
  {
    id: "redo-windows",
    keys: ["Mod+Y"],
    commands: { any: "redo", formula: null },
    platform: "other",
    ...typing,
  },
  {
    id: "left",
    keys: ["ArrowLeft"],
    commands: {
      canvas: "previousStep",
      addStep: "panelBack",
      decisionGrid: "cellLeft",
    },
  },
  {
    id: "right",
    keys: ["ArrowRight"],
    commands: {
      canvas: "nextStep",
      addStep: "panelOpen",
      decisionGrid: "cellRight",
    },
  },
  {
    id: "up",
    keys: ["ArrowUp"],
    commands: {
      canvas: "laneAbove",
      addStep: "panelUp",
      decisionGrid: "cellUp",
    },
  },
  {
    id: "down",
    keys: ["ArrowDown"],
    commands: {
      canvas: "laneBelow",
      addStep: "panelDown",
      decisionGrid: "cellDown",
    },
  },
  {
    id: "extend-upstream",
    keys: ["Shift+ArrowLeft"],
    commands: { canvas: "extendUpstream" },
  },
  {
    id: "extend-downstream",
    keys: ["Shift+ArrowRight"],
    commands: { canvas: "extendDownstream" },
  },
  {
    id: "execute",
    keys: ["Mod+Enter"],
    commands: {
      canvas: "executeWorkflow",
      outline: "executeWorkflow",
      stepDetails: "executeStep",
      decisionGrid: null,
    },
    ...typing,
  },
  { id: "save", keys: ["Mod+S"], commands: { any: "save" }, ...typing },
  { id: "tidy", keys: ["Alt+Shift+T"], commands: { canvas: "tidyLayout" } },
  {
    id: "extract",
    keys: ["Alt+X"],
    commands: { canvas: "extractSubWorkflow", outline: "extractSubWorkflow" },
  },
  { id: "replace", keys: ["R"], commands: { canvas: "replaceAction" } },
  {
    id: "webhook-url",
    keys: ["Alt+U"],
    commands: { canvas: "copyWebhookUrl" },
  },
  {
    id: "webhook-test-url",
    keys: ["Alt+Shift+U"],
    commands: { canvas: "copyWebhookTestUrl" },
  },
  {
    id: "run-log",
    keys: ["L"],
    commands: {
      canvas: "toggleRunLog",
      outline: "toggleRunLog",
      runLog: "toggleRunLog",
    },
  },
  { id: "entry-input", keys: ["I"], commands: { runLog: "focusEntryInput" } },
  { id: "entry-output", keys: ["O"], commands: { runLog: "focusEntryOutput" } },
  {
    id: "breakpoint",
    keys: ["F9"],
    commands: { canvas: "toggleBreakpoint", outline: "toggleBreakpoint" },
    n8n: "Not in n8n.",
  },
  {
    id: "previous-details",
    keys: ["Mod+Alt+Shift+ArrowLeft"],
    commands: { stepDetails: "previousStepDetails" },
  },
  {
    id: "next-details",
    keys: ["Mod+Alt+Shift+ArrowRight"],
    commands: { stepDetails: "nextStepDetails" },
  },
  {
    id: "next-region",
    keys: ["F6"],
    commands: { stepDetails: "nextRegion" },
    ...typing,
  },
  {
    id: "previous-region",
    keys: ["Shift+F6"],
    commands: { stepDetails: "previousRegion" },
    ...typing,
  },
  { id: "escape", keys: ["Escape"], commands: { any: "escape" } },
  {
    id: "shortcuts",
    keys: ["?"],
    commands: {
      canvas: "showShortcuts",
      outline: "showShortcuts",
      stepDetails: "showShortcuts",
    },
    n8n: "Not in n8n.",
  },
  {
    id: "command-bar",
    keys: ["Mod+K"],
    commands: {},
    note: "Reserved for a command bar.",
    n8n: "Opens the command bar in n8n.",
  },
  {
    id: "deactivate",
    keys: ["D"],
    commands: {},
    note: "Not assigned: Weave has no deactivated steps.",
    n8n: "Deactivates a step in n8n.",
  },
  {
    id: "grid-next-cell",
    keys: ["Tab"],
    commands: { decisionGrid: "nextCell" },
  },
  {
    id: "grid-previous-cell",
    keys: ["Shift+Tab"],
    commands: { decisionGrid: "previousCell" },
  },
  {
    id: "grid-add-rule",
    keys: ["Alt+Enter"],
    commands: { decisionGrid: "addRule" },
  },
  {
    id: "grid-rule-up",
    keys: ["Alt+ArrowUp"],
    commands: { decisionGrid: "moveRuleUp", stepDetails: "moveRowUp" },
  },
  {
    id: "grid-rule-down",
    keys: ["Alt+ArrowDown"],
    commands: { decisionGrid: "moveRuleDown", stepDetails: "moveRowDown" },
  },
];

export const COMMAND_LABELS: Readonly<Record<KeymapCommand, string>> = {
  zoomIn: "Zoom in",
  zoomOut: "Zoom out",
  zoomReset: "Reset zoom to 100%",
  fitView: "Fit view",
  openStepDetails: "Open step details",
  renameStep: "Rename the step",
  openAddStep: "Open the Add a step panel",
  searchAddStep: "Search in the Add a step panel",
  addStickyNote: "Add a sticky note",
  togglePin: "Pin output or unpin",
  selectAll: "Select all steps",
  copy: "Copy",
  cut: "Cut",
  paste: "Paste",
  duplicate: "Duplicate",
  deleteSelection: "Delete the selection",
  undo: "Undo",
  redo: "Redo",
  previousStep: "Previous step in the sequence",
  nextStep: "Next step, or a group's first lane",
  laneAbove: "Nearest step in the lane above",
  laneBelow: "Nearest step in the lane below",
  extendUpstream: "Extend the selection upstream",
  extendDownstream: "Extend the selection downstream",
  executeWorkflow: "Execute workflow",
  executeStep: "Execute step",
  save: "Save the draft, or save to file",
  tidyLayout: "Tidy layout",
  extractSubWorkflow: "Extract the selection to a sub-workflow",
  replaceAction: "Replace the action, keeping compatible mappings",
  copyWebhookUrl: "Copy the webhook production URL",
  copyWebhookTestUrl: "Copy the webhook test URL",
  toggleRunLog: "Show or hide the run log",
  focusEntryInput: "Show the entry's input",
  focusEntryOutput: "Show the entry's output",
  toggleBreakpoint: "Toggle a breakpoint",
  previousStepDetails: "Open the previous step's details",
  nextStepDetails: "Open the next step's details",
  switchToMapped: "Switch an empty Fixed field to Mapped",
  escape: "Close, cancel, then clear the selection",
  showShortcuts: "Show keyboard shortcuts",
  panelUp: "Previous entry",
  panelDown: "Next entry",
  panelInsert: "Insert the entry",
  panelOpen: "Open the category or app",
  panelBack: "Go back",
  cellUp: "Cell above",
  cellDown: "Cell below",
  cellLeft: "Cell to the left",
  cellRight: "Cell to the right",
  nextCell: "Next cell in the row",
  previousCell: "Previous cell in the row",
  editCell: "Edit the cell",
  clearCell: "Clear the cell",
  addRule: "Add a rule below",
  moveRuleUp: "Move the rule up",
  moveRuleDown: "Move the rule down",
  duplicateRule: "Duplicate the rule",
  nextRegion: "Next region",
  previousRegion: "Previous region",
  searchPane: "Search the focused pane",
  moveRowUp: "Move the row up",
  moveRowDown: "Move the row down",
  mapTo: "Map the focused input field to a parameter",
};

/** Pointer gestures, shown on the sheet; the canvas handles them itself. */
export const POINTER_GESTURES: readonly { keys: string; label: string }[] = [
  { keys: "Mod+Wheel", label: "Zoom" },
  { keys: "Wheel", label: "Pan vertically" },
  { keys: "Shift+Wheel", label: "Pan horizontally" },
  { keys: "Space+Drag", label: "Pan" },
  { keys: "Mod+Drag", label: "Pan" },
  { keys: "Middle drag", label: "Pan" },
  { keys: "Drag on empty canvas", label: "Select steps with a box" },
];

const MODIFIERS = ["Mod", "Ctrl", "Meta", "Alt", "Shift"] as const;

/** Splits "Mod+Shift+Z", "+" and "Mod++" into modifiers and key. */
function parts(combo: string): { modifiers: string[]; key: string } {
  if (combo === "+") return { modifiers: [], key: "+" };
  const plusKey = combo.endsWith("++");
  const pieces = (plusKey ? combo.slice(0, -2) : combo).split("+");
  const key = plusKey ? "+" : (pieces.pop() ?? "");
  return { modifiers: pieces, key };
}

/** The canonical form of a written combination: known modifiers in order, letters upper case. */
export function canonicalCombo(combo: string): string {
  const { modifiers, key } = parts(combo);
  for (const modifier of modifiers)
    if (!(MODIFIERS as readonly string[]).includes(modifier))
      throw new RangeError(`Unknown modifier "${modifier}" in "${combo}".`);
  const name = /^[a-z]$/.test(key) ? key.toUpperCase() : key;
  return [...MODIFIERS.filter((m) => modifiers.includes(m)), name].join("+");
}

export interface KeyEventLike {
  key: string;
  code: string;
  ctrlKey: boolean;
  metaKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
}
/** The combination a key press means on this platform, in canonical form. */
export function eventCombo(event: KeyEventLike, platform: KeyPlatform): string {
  let key = event.key === " " ? "Space" : event.key;
  // Option on macOS changes the character (Alt+X types "≈"); the physical key names the shortcut.
  if (event.altKey && /^Key[A-Z]$/.test(event.code)) key = event.code.slice(3);
  else if (event.altKey && /^Digit[0-9]$/.test(event.code))
    key = event.code.slice(5);
  const letter = /^[a-z]$/i.test(key);
  if (letter) key = key.toUpperCase();
  // Shift is already part of a typed symbol such as "?", "+" or "_".
  const symbol = key.length === 1 && !letter;
  const modifiers: string[] = [];
  if (platform === "mac" ? event.metaKey : event.ctrlKey) modifiers.push("Mod");
  if (platform === "mac" && event.ctrlKey) modifiers.push("Ctrl");
  if (platform === "other" && event.metaKey) modifiers.push("Meta");
  if (event.altKey) modifiers.push("Alt");
  if (event.shiftKey && !symbol) modifiers.push("Shift");
  return [...modifiers, key].join("+");
}

const appliesTo = (row: KeymapRow, platform: KeyPlatform) =>
  !row.platform || row.platform === platform;

/** Combinations that more than one row binds on a platform; empty when the table is sound. */
export function duplicateCombos(
  rows: readonly KeymapRow[],
  platform: KeyPlatform,
): string[] {
  const seen = new Set<string>();
  const twice = new Set<string>();
  for (const row of rows.filter((r) => appliesTo(r, platform)))
    for (const combo of row.keys) {
      if (seen.has(combo)) twice.add(combo);
      seen.add(combo);
    }
  return [...twice];
}

/** The row that owns a combination on a platform, or null. */
export function rowFor(combo: string, platform: KeyPlatform): KeymapRow | null {
  return (
    KEYMAP.find(
      (row) => appliesTo(row, platform) && row.keys.includes(combo),
    ) ?? null
  );
}

/**
 * The command for a combination where focus is. `surfaces` lists where focus
 * is from the most specific (["fixedField", "stepDetails"]); `typing` is true
 * while focus is in a text field.
 */
export function commandFor(
  combo: string,
  surfaces: KeymapSurface | readonly KeymapSurface[],
  options: { platform: KeyPlatform; typing?: boolean },
): KeymapCommand | null {
  const row = rowFor(combo, options.platform);
  if (!row) return null;
  for (const surface of typeof surfaces === "string" ? [surfaces] : surfaces) {
    const command = row.commands[surface];
    if (command === undefined) continue;
    if (options.typing && !row.whileTyping && surface !== "fixedField")
      return null;
    return command;
  }
  if (options.typing && !row.whileTyping) return null;
  return row.commands.any ?? null;
}

/** How a combination reads on the sheet: Cmd and Option on macOS, arrows as symbols. */
export function formatCombo(combo: string, platform: KeyPlatform): string {
  const names: Record<string, string> = {
    Mod: platform === "mac" ? "Cmd" : "Ctrl",
    Alt: platform === "mac" ? "Option" : "Alt",
    Meta: "Win",
    ArrowLeft: "←",
    ArrowRight: "→",
    ArrowUp: "↑",
    ArrowDown: "↓",
  };
  const { modifiers, key } = parts(combo);
  return [...modifiers, key].map((part) => names[part] ?? part).join("+");
}

export interface SheetSection {
  title: string;
  entries: { keys: string[]; label: string }[];
}
const SHEET_SURFACES: readonly [KeymapSurface | "any", string][] = [
  ["any", "Everywhere"],
  ["canvas", "Canvas"],
  ["outline", "Outline"],
  ["stepDetails", "Step details"],
  ["fixedField", "Fixed fields"],
  ["addStep", "Add a step panel"],
  ["decisionGrid", "Decision table grid"],
  ["runLog", "Run log"],
];

/** Ends a phrase with a period, unless it already ends with a sentence mark. */
function asSentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

export interface SheetOptions {
  /** Only these surfaces, in the sheet's order. */
  surfaces?: readonly (KeymapSurface | "any")[];
  /** Only these commands; rows without a command still explain themselves. */
  available?: ReadonlySet<KeymapCommand>;
}

/** The "?" sheet, built from the same table the handler reads. */
export function shortcutSheet(
  platform: KeyPlatform,
  options: SheetOptions = {},
): SheetSection[] {
  const offered = (command: KeymapCommand) =>
    !options.available || options.available.has(command);
  const rows = KEYMAP.filter((row) => appliesTo(row, platform));
  const surfaces = SHEET_SURFACES.filter(
    ([surface]) => !options.surfaces || options.surfaces.includes(surface),
  );
  const sections: SheetSection[] = surfaces.map(([surface, title]) => {
    const byCommand = new Map<KeymapCommand, string[]>();
    for (const row of rows) {
      const command = row.commands[surface];
      if (!command || !offered(command)) continue;
      byCommand.set(command, [
        ...(byCommand.get(command) ?? []),
        ...row.keys.map((combo) => formatCombo(combo, platform)),
      ]);
    }
    const entries = [...byCommand].map(([command, keys]) => ({
      keys,
      label: COMMAND_LABELS[command],
    }));
    if (surface === "canvas")
      entries.push(
        ...POINTER_GESTURES.map((gesture) => ({
          keys: [formatCombo(gesture.keys, platform)],
          label: gesture.label,
        })),
      );
    return { title, entries };
  });
  sections.push({
    title: "Differences from n8n",
    entries: rows
      .filter((row) => {
        if (!row.n8n) return false;
        const own = Object.values(row.commands).find(Boolean);
        return own ? offered(own) : !options.available;
      })
      .map((row) => {
        const own = Object.values(row.commands).find(Boolean);
        const meaning = row.note ?? (own ? COMMAND_LABELS[own] : "");
        return {
          keys: row.keys.map((combo) => formatCombo(combo, platform)),
          label: `${asSentence(meaning)} ${row.n8n}`,
        };
      }),
  });
  return sections.filter((section) => section.entries.length);
}
