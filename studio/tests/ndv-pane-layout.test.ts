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
import { describe, expect, it } from "vitest";
import {
  LAYOUT_KEY,
  MIN_MAIN,
  MIN_SIDE,
  clampWidths,
  defaultWidths,
  readWidths,
  resized,
  toEdge,
  writeWidths,
} from "../src/app/editor/ndv/panes/layout";

const store = () => {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => void values.set(key, value),
    values,
  };
};

describe("step details panes", () => {
  it("starts at 28 % for Input and Output", () => {
    expect(defaultWidths(1392)).toEqual({ input: 390, output: 390 });
  });
  it("keeps every pane at its minimum", () => {
    expect(clampWidths({ input: 100, output: 900 }, 1392)).toEqual({
      input: MIN_SIDE,
      output: 1392 - MIN_MAIN - MIN_SIDE,
    });
    const grown = resized(defaultWidths(1392), "input", 2000, 1392);
    expect(1392 - grown.input - grown.output).toBe(MIN_MAIN);
  });
  it("goes to the limits with Home and End", () => {
    expect(toEdge(defaultWidths(1392), "input", "min", 1392).input).toBe(
      MIN_SIDE,
    );
    expect(toEdge(defaultWidths(1392), "output", "max", 1392).output).toBe(
      1392 - MIN_MAIN - 390,
    );
  });
  it("remembers proportions in browser storage and survives bad values", () => {
    const storage = store();
    expect(writeWidths({ input: 696, output: 278 }, 1392, storage)).toBe(true);
    expect(JSON.parse(storage.values.get(LAYOUT_KEY)!)).toEqual({
      input: 0.5,
      output: 0.2,
    });
    expect(readWidths(1000, storage)).toEqual({
      input: 1000 - MIN_MAIN - MIN_SIDE,
      output: MIN_SIDE,
    });
    storage.setItem(LAYOUT_KEY, "{not json");
    expect(readWidths(1392, storage)).toEqual(defaultWidths(1392));
    expect(readWidths(1392, null)).toEqual(defaultWidths(1392));
    expect(writeWidths({ input: 300, output: 300 }, 1392, null)).toBe(false);
  });
  it.each([
    "null",
    "[]",
    "true",
    '"text"',
    '{"input":"0.3","output":0.3}',
    '{"input":0,"output":0.3}',
    '{"input":-1,"output":0.3}',
    '{"input":1e400,"output":0.3}',
    '{"input":2,"output":0.3}',
  ])("falls back from malformed saved proportions: %s", (raw) => {
    const storage = store();
    storage.setItem(LAYOUT_KEY, raw);
    expect(readWidths(1392, storage)).toEqual(defaultWidths(1392));
  });
  it("keeps the other side unchanged while resizing", () => {
    const widths = defaultWidths(1392);
    for (const side of ["input", "output"] as const) {
      const other = side === "input" ? "output" : "input";
      const next = resized(widths, side, 2000, 1392);
      expect(next[other]).toBe(widths[other]);
      expect(next[side]).toBe(toEdge(widths, side, "max", 1392)[side]);
    }
  });
  it("survives denied browser storage and refuses nonfinite widths", () => {
    const storage = {
      getItem: () => {
        throw new Error("Denied");
      },
      setItem: () => {
        throw new Error("Denied");
      },
    };
    expect(readWidths(1392, storage)).toEqual(defaultWidths(1392));
    expect(writeWidths(defaultWidths(1392), 1392, storage)).toBe(false);
    expect(writeWidths({ input: NaN, output: 300 }, 1392, store())).toBe(false);
    expect(writeWidths({ input: 300, output: Infinity }, 1392, store())).toBe(
      false,
    );
    expect(writeWidths({ input: 300, output: 300 }, NaN, store())).toBe(false);
    expect(writeWidths({ input: 300, output: 300 }, 0, store())).toBe(false);
  });
});
