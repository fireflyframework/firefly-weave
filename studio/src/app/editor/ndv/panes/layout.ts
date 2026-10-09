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
// Widths of step details' Input and Output panes: 28 % each by default,
// never below 240 px, with Parameters never below 368 px. The person's
// choice is kept as proportions under one ui: key.
import { uiKey } from "../../state/browser-store";

export interface PaneWidths {
  input: number;
  output: number;
}
export const LAYOUT_KEY = uiKey("weave.ndv.layout.v1");
export const MIN_SIDE = 240;
export const MIN_MAIN = 368;
export const KEY_STEP = 16;

const defaultStorage = (): Storage | null => {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
};

export const defaultWidths = (total: number): PaneWidths => ({
  input: Math.round(total * 0.28),
  output: Math.round(total * 0.28),
});

export function clampWidths(widths: PaneWidths, total: number): PaneWidths {
  const input = Math.max(
    MIN_SIDE,
    Math.min(widths.input, total - MIN_MAIN - MIN_SIDE),
  );
  const output = Math.max(
    MIN_SIDE,
    Math.min(widths.output, total - MIN_MAIN - input),
  );
  return { input: Math.round(input), output: Math.round(output) };
}

export function resized(
  widths: PaneWidths,
  side: keyof PaneWidths,
  delta: number,
  total: number,
): PaneWidths {
  const other = side === "input" ? widths.output : widths.input;
  const next = Math.max(
    MIN_SIDE,
    Math.min(widths[side] + delta, total - MIN_MAIN - other),
  );
  return { ...widths, [side]: Math.round(next) };
}

export function toEdge(
  widths: PaneWidths,
  side: keyof PaneWidths,
  edge: "min" | "max",
  total: number,
): PaneWidths {
  const other = side === "input" ? widths.output : widths.input;
  return clampWidths(
    { ...widths, [side]: edge === "min" ? MIN_SIDE : total - MIN_MAIN - other },
    total,
  );
}

export function readWidths(
  total: number,
  storage: Pick<Storage, "getItem"> | null = defaultStorage(),
): PaneWidths {
  try {
    const raw = JSON.parse(storage?.getItem(LAYOUT_KEY) ?? "null") as {
      input?: unknown;
      output?: unknown;
    } | null;
    if (
      raw &&
      typeof raw.input === "number" &&
      typeof raw.output === "number" &&
      Number.isFinite(raw.input) &&
      Number.isFinite(raw.output) &&
      raw.input > 0 &&
      raw.input <= 1 &&
      raw.output > 0 &&
      raw.output <= 1
    )
      return clampWidths(
        { input: raw.input * total, output: raw.output * total },
        total,
      );
  } catch {
    // A value Studio can't read means no choice was kept.
  }
  return defaultWidths(total);
}

export function writeWidths(
  widths: PaneWidths,
  total: number,
  storage: Pick<Storage, "setItem"> | null = defaultStorage(),
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
    storage.setItem(
      LAYOUT_KEY,
      JSON.stringify({
        input: round(widths.input),
        output: round(widths.output),
      }),
    );
    return true;
  } catch {
    return false;
  }
}
