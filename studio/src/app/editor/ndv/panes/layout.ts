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
// Widths of step details' Input and Output panes. The step form starts them
// at 28 % each, with Parameters never below 368 px. Table view (the decision
// table grid) has its own profile: Input at 20 % and Output at 24 % of the
// panes within fixed bounds, with the grid's card never below 640 px, and the
// panes the person folded to rails. Every pane stays at 240 px or more. Each
// profile keeps the person's choice as proportions under its own ui: key, so
// neither layout overwrites the other.
import { uiKey, type BrowserStorage } from "../../state/browser-store";
import { studioStorage } from "../../state/studio-storage";

export interface PaneWidths {
  input: number;
  output: number;
}
export const LAYOUT_KEY = uiKey("weave.ndv.layout.v1");
export const TABLE_LAYOUT_KEY = uiKey("weave.ndv.layout.table.v1");
export const MIN_SIDE = 240;
export const MIN_MAIN = 368;
/** The grid's card in table view: its tracks' minimums fit without scrolling. */
export const MIN_MAIN_TABLE = 640;
export const KEY_STEP = 16;
/** The width of a folded side pane. */
export const RAIL = 48;

const clampTo = (low: number, value: number, high: number) =>
  Math.min(high, Math.max(low, value));

export const defaultWidths = (total: number): PaneWidths => ({
  input: Math.round(total * 0.28),
  output: Math.round(total * 0.28),
});
/** Table view's widths before the person sets any. */
export const tableWidths = (total: number): PaneWidths => ({
  input: Math.round(clampTo(MIN_SIDE, total * 0.2, 360)),
  output: Math.round(clampTo(280, total * 0.24, 440)),
});

/** One layout's widths: where they are kept, their start, and Parameters' minimum. */
export interface PaneProfile {
  key: string;
  minMain: number;
  defaults(total: number): PaneWidths;
}
export const FORM_PANES: PaneProfile = {
  key: LAYOUT_KEY,
  minMain: MIN_MAIN,
  defaults: defaultWidths,
};
export const TABLE_PANES: PaneProfile = {
  key: TABLE_LAYOUT_KEY,
  minMain: MIN_MAIN_TABLE,
  defaults: tableWidths,
};

const defaultStorage = (): BrowserStorage | null => {
  try {
    return studioStorage();
  } catch {
    return null;
  }
};

export function clampWidths(
  widths: PaneWidths,
  total: number,
  profile: PaneProfile = FORM_PANES,
): PaneWidths {
  const input = Math.max(
    MIN_SIDE,
    Math.min(widths.input, total - profile.minMain - MIN_SIDE),
  );
  const output = Math.max(
    MIN_SIDE,
    Math.min(widths.output, total - profile.minMain - input),
  );
  return { input: Math.round(input), output: Math.round(output) };
}

export function resized(
  widths: PaneWidths,
  side: keyof PaneWidths,
  delta: number,
  total: number,
  profile: PaneProfile = FORM_PANES,
): PaneWidths {
  const other = side === "input" ? widths.output : widths.input;
  const next = Math.max(
    MIN_SIDE,
    Math.min(widths[side] + delta, total - profile.minMain - other),
  );
  return { ...widths, [side]: Math.round(next) };
}

export function toEdge(
  widths: PaneWidths,
  side: keyof PaneWidths,
  edge: "min" | "max",
  total: number,
  profile: PaneProfile = FORM_PANES,
): PaneWidths {
  const other = side === "input" ? widths.output : widths.input;
  return clampWidths(
    {
      ...widths,
      [side]: edge === "min" ? MIN_SIDE : total - profile.minMain - other,
    },
    total,
    profile,
  );
}

/** What a profile's key holds: proportions, and in table view the folded panes. */
function readStored(
  key: string,
  storage: Pick<Storage, "getItem"> | null,
): Record<string, unknown> | null {
  try {
    const raw = JSON.parse(storage?.getItem(key) ?? "null") as unknown;
    return raw && typeof raw === "object" && !Array.isArray(raw)
      ? (raw as Record<string, unknown>)
      : null;
  } catch {
    // A value Studio can't read means no choice was kept.
    return null;
  }
}

export function readWidths(
  total: number,
  storage: Pick<Storage, "getItem"> | null = defaultStorage(),
  profile: PaneProfile = FORM_PANES,
): PaneWidths {
  const raw = readStored(profile.key, storage);
  const input = raw?.["input"];
  const output = raw?.["output"];
  if (
    typeof input === "number" &&
    typeof output === "number" &&
    Number.isFinite(input) &&
    Number.isFinite(output) &&
    input > 0 &&
    input <= 1 &&
    output > 0 &&
    output <= 1
  )
    return clampWidths(
      { input: input * total, output: output * total },
      total,
      profile,
    );
  return profile.defaults(total);
}

export function writeWidths(
  widths: PaneWidths,
  total: number,
  storage: Pick<Storage, "getItem" | "setItem"> | null = defaultStorage(),
  profile: PaneProfile = FORM_PANES,
): boolean {
  if (
    !storage ||
    !Number.isFinite(total) ||
    total <= 0 ||
    !Number.isFinite(widths.input) ||
    !Number.isFinite(widths.output) ||
    widths.input <= 0 ||
    widths.output <= 0
  )
    return false;
  try {
    const round = (n: number) => Math.round((n / total) * 1000) / 1000;
    // Table view keeps its folds beside the widths.
    const folded =
      profile.key === TABLE_LAYOUT_KEY
        ? readStored(profile.key, storage)?.["folded"]
        : undefined;
    storage.setItem(
      profile.key,
      JSON.stringify({
        input: round(widths.input),
        output: round(widths.output),
        ...(Array.isArray(folded) ? { folded } : {}),
      }),
    );
    return true;
  } catch {
    return false;
  }
}

/** The side panes the person folded to rails in table view. */
export interface PaneFolds {
  input: boolean;
  output: boolean;
}

export function readFolds(
  storage: Pick<Storage, "getItem"> | null = defaultStorage(),
): PaneFolds {
  const folded = readStored(TABLE_LAYOUT_KEY, storage)?.["folded"];
  const list = Array.isArray(folded) ? folded : [];
  return { input: list.includes("input"), output: list.includes("output") };
}

export function writeFolds(
  folds: PaneFolds,
  storage: Pick<Storage, "getItem" | "setItem"> | null = defaultStorage(),
): boolean {
  if (!storage) return false;
  try {
    const kept = readStored(TABLE_LAYOUT_KEY, storage) ?? {};
    const { input, output } = kept;
    storage.setItem(
      TABLE_LAYOUT_KEY,
      JSON.stringify({
        ...(typeof input === "number" && typeof output === "number"
          ? { input, output }
          : {}),
        folded: (["input", "output"] as const).filter((side) => folds[side]),
      }),
    );
    return true;
  } catch {
    return false;
  }
}
