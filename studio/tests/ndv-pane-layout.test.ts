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
  MIN_MAIN_TABLE,
  RAIL,
  TABLE_LAYOUT_KEY,
  TABLE_PANES,
  clampWidths,
  defaultWidths,
  fitted,
  largest,
  readFolds,
  readWidths,
  resized,
  tableWidths,
  toEdge,
  writeFolds,
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

describe("the table layout", () => {
  it("starts Input at 20 % and Output at 24 % of the panes, within their bounds", () => {
    expect(tableWidths(1392)).toEqual({ input: 278, output: 334 });
    expect(tableWidths(1872)).toEqual({ input: 360, output: 440 });
    expect(tableWidths(1232)).toEqual({ input: 246, output: 296 });
    expect(tableWidths(1000)).toEqual({ input: 240, output: 280 });
  });
  it("keeps the grid's card at 640 px or more while a side pane grows", () => {
    expect(MIN_MAIN_TABLE).toBe(640);
    expect(clampWidths({ input: 600, output: 600 }, 1392, TABLE_PANES)).toEqual(
      { input: 512, output: 240 },
    );
    const grown = resized(tableWidths(1392), "output", 2000, 1392, TABLE_PANES);
    expect(1392 - grown.input - grown.output).toBe(640);
    expect(
      toEdge(tableWidths(1392), "input", "max", 1392, TABLE_PANES).input,
    ).toBe(1392 - 640 - 334);
  });
  it("keeps its widths apart from the step form's", () => {
    const storage = store();
    expect(
      writeWidths({ input: 348, output: 348 }, 1392, storage, TABLE_PANES),
    ).toBe(true);
    expect(readWidths(1392, storage, TABLE_PANES)).toEqual({
      input: 348,
      output: 348,
    });
    expect(readWidths(1392, storage)).toEqual(defaultWidths(1392));
    writeWidths({ input: 278, output: 278 }, 1392, storage);
    expect(readWidths(1392, storage, TABLE_PANES)).toEqual({
      input: 348,
      output: 348,
    });
    expect(storage.values.has(TABLE_LAYOUT_KEY)).toBe(true);
    expect(readWidths(1392, store(), TABLE_PANES)).toEqual(tableWidths(1392));
  });
  it("remembers which panes the person folded, beside the widths", () => {
    const storage = store();
    expect(readFolds(storage)).toEqual({
      input: false,
      output: false,
      data: true,
    });
    expect(
      writeFolds({ input: true, output: false, data: true }, storage),
    ).toBe(true);
    writeWidths({ input: 348, output: 348 }, 1392, storage, TABLE_PANES);
    expect(readFolds(storage)).toEqual({
      input: true,
      output: false,
      data: true,
    });
    // The Data pane (768 to 1099 px) starts folded until the person opens it.
    writeFolds({ input: true, output: true, data: false }, storage);
    expect(readFolds(storage).data).toBe(false);
    expect(readWidths(1392, storage, TABLE_PANES)).toEqual({
      input: 348,
      output: 348,
    });
    // A fold is drawn as the rail.
    expect(RAIL).toBe(48);
  });
  it.each(["{not json", '{"folded":"input"}', '{"folded":[1,null]}', "[]"])(
    "reads no fold from a malformed value: %s",
    (raw) => {
      const storage = store();
      storage.setItem(TABLE_LAYOUT_KEY, raw);
      expect(readFolds(storage)).toEqual({
        input: false,
        output: false,
        data: true,
      });
      expect(readWidths(1392, storage, TABLE_PANES)).toEqual(tableWidths(1392));
    },
  );
  it("survives denied browser storage for folds", () => {
    const denied = {
      getItem: () => {
        throw new Error("Denied");
      },
      setItem: () => {
        throw new Error("Denied");
      },
    };
    expect(readFolds(denied)).toEqual({
      input: false,
      output: false,
      data: true,
    });
    expect(writeFolds({ input: true, output: false, data: true }, denied)).toBe(
      false,
    );
    expect(writeFolds({ input: true, output: false, data: true }, null)).toBe(
      false,
    );
  });
  it("gives arrow keys, End and the separator's maximum the same largest width", () => {
    // Input folded to its rail: Output may grow up to the grid's minimum.
    const drawn = { input: RAIL, output: 334 };
    expect(largest(drawn, "output", 1392, TABLE_PANES)).toBe(704);
    expect(toEdge(drawn, "output", "max", 1392, TABLE_PANES).output).toBe(704);
    expect(resized(drawn, "output", 2000, 1392, TABLE_PANES).output).toBe(704);
    expect(toEdge(drawn, "output", "min", 1392, TABLE_PANES).output).toBe(240);
  });
  it("fits the panes beside the grid again when a folded pane opens", () => {
    // Output grew to 704 while Input was folded; Input opens at its width.
    expect(
      fitted({ input: 278, output: 704 }, "input", 1392, TABLE_PANES),
    ).toEqual({ input: 278, output: 474 });
    // Widths that already fit stay as they are.
    expect(
      fitted({ input: 278, output: 334 }, "input", 1392, TABLE_PANES),
    ).toEqual({ input: 278, output: 334 });
    // The kept side gives way too when the other can't go below 240 px.
    expect(
      fitted({ input: 600, output: 300 }, "input", 1392, TABLE_PANES),
    ).toEqual({ input: 512, output: 240 });
  });
});
