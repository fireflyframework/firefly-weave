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
  GridHistory,
  moveCursor,
  parsePaste,
  pasteInto,
} from "../src/app/editor/decision-grid/grid-keys";
import {
  gridOf,
  type TableSpec,
} from "../src/app/editor/decision-grid/rules-model";

const shape = { rows: 3, columns: 5, otherwise: true };
const ref = (name: string) => ({ ref: `/input/${name}` });
const op = (name: string, ...args: unknown[]) => ({ op: { name, args } });
const lit = (value: unknown) => ({ literal: value });
const table = (): TableSpec => ({
  inputSchema: {
    type: "object",
    properties: { amount: { type: "number" }, tier: { type: "string" } },
  },
  outputSchema: {
    type: "object",
    properties: { approve: { type: "boolean" }, limit: { type: "number" } },
  },
  hitPolicy: "first",
  rules: [
    {
      id: "a",
      when: op("gt", ref("amount"), lit(1)),
      output: lit({ approve: true }),
    },
    { id: "b", when: lit(true), output: lit({ approve: false }) },
    {
      id: "c",
      when: op(
        "or",
        op("gt", ref("amount"), lit(9)),
        op("exists", ref("tier")),
      ),
      output: lit({}),
    },
  ],
  defaultOutput: lit({ approve: false }),
});

describe("moving the grid's cursor", () => {
  it("stops at the edges with the arrows", () => {
    expect(moveCursor({ row: 0, column: 0 }, "cellUp", shape)).toEqual({
      row: 0,
      column: 0,
    });
    expect(moveCursor({ row: 0, column: 0 }, "cellLeft", shape)).toEqual({
      row: 0,
      column: 0,
    });
    expect(moveCursor({ row: 3, column: 4 }, "cellDown", shape)).toEqual({
      row: 3,
      column: 4,
    });
    expect(moveCursor({ row: 1, column: 4 }, "cellRight", shape)).toEqual({
      row: 1,
      column: 4,
    });
    expect(moveCursor({ row: 1, column: 2 }, "cellDown", shape)).toEqual({
      row: 2,
      column: 2,
    });
    // Without Otherwise the last rule is the last row.
    expect(
      moveCursor({ row: 2, column: 1 }, "cellDown", {
        ...shape,
        otherwise: false,
      }),
    ).toEqual({ row: 2, column: 1 });
  });

  it("moves along a row with Tab, wraps to the next row and leaves from the last cell", () => {
    expect(moveCursor({ row: 0, column: 2 }, "nextCell", shape)).toEqual({
      row: 0,
      column: 3,
    });
    expect(moveCursor({ row: 0, column: 4 }, "nextCell", shape)).toEqual({
      row: 1,
      column: 0,
    });
    expect(moveCursor({ row: 3, column: 4 }, "nextCell", shape)).toBe(
      "leave-forward",
    );
    expect(moveCursor({ row: 1, column: 0 }, "previousCell", shape)).toEqual({
      row: 0,
      column: 4,
    });
    expect(moveCursor({ row: 0, column: 0 }, "previousCell", shape)).toBe(
      "leave-backward",
    );
  });
});

describe("pasting cells from a spreadsheet", () => {
  it("reads a Google Sheets copy with a newline inside a quoted cell", () => {
    expect(parsePaste('> 5\t"gold\nplus"\n< 2\tsilver')).toEqual([
      ["> 5", "gold\nplus"],
      ["< 2", "silver"],
    ]);
  });

  it("reads an Excel copy with CRLF rows and a trailing newline", () => {
    expect(parsePaste("> 5\tgold\r\n< 2\tsilver\r\n")).toEqual([
      ["> 5", "gold"],
      ["< 2", "silver"],
    ]);
    expect(parsePaste('"say ""hi"""\tx')).toEqual([['say "hi"', "x"]]);
  });

  it("reads a single cell", () => {
    expect(parsePaste("> 1000")).toEqual([["> 1000"]]);
    expect(parsePaste("> 1000\n")).toEqual([["> 1000"]]);
  });

  it("fills a block from the focused cell, grows rules and flags what doesn't read", () => {
    const spec = table();
    const view = gridOf(spec);
    const { spec: next, flagged } = pasteInto(
      spec,
      { row: 1, column: 1 },
      [
        ["> 5", "gold"],
        ["abc", "silver"],
        ["< 2", "bronze"],
      ],
      view,
    );
    expect(next.rules).toHaveLength(4);
    expect(next.rules[1].when).toEqual(
      op(
        "and",
        op("gt", ref("amount"), lit(5)),
        op("eq", ref("tier"), lit("gold")),
      ),
    );
    // Rule 3 was one formula: it is replaced by the cells pasted over it.
    expect(next.rules[2].when).toEqual(op("eq", ref("tier"), lit("silver")));
    expect(next.rules[3].when).toEqual(
      op(
        "and",
        op("lt", ref("amount"), lit(2)),
        op("eq", ref("tier"), lit("bronze")),
      ),
    );
    expect(next.rules[0]).toBe(spec.rules[0]);
    expect(flagged).toEqual([
      {
        row: 2,
        column: 1,
        text: "abc",
        message: "Text test in a Number column.",
      },
    ]);
  });

  it("never adds columns and pastes results by their type", () => {
    const spec = table();
    const { spec: next, flagged } = pasteInto(
      spec,
      { row: 0, column: 3 },
      [["false", "250", "extra"]],
      gridOf(spec),
    );
    expect(next.rules[0].output).toEqual(lit({ approve: false, limit: 250 }));
    expect(Object.keys(next.outputSchema["properties"] as object)).toHaveLength(
      2,
    );
    expect(flagged).toEqual([]);
  });
});

describe("pasting at the edges", () => {
  it("keeps at most 1,000 rows of 64 cells", () => {
    const text = Array.from({ length: 1005 }, () =>
      Array.from({ length: 70 }, (_, i) => String(i)).join("\t"),
    ).join("\n");
    const block = parsePaste(text);
    expect(block).toHaveLength(1000);
    expect(block[0]).toHaveLength(64);
  });

  it("writes Otherwise's results when pasted on it, and nothing below it", () => {
    const spec = table();
    const view = gridOf(spec);
    const {
      spec: next,
      flagged,
      notes,
    } = pasteInto(
      spec,
      { row: 3, column: 3 },
      [
        ["true", "77"],
        ["false", "1"],
      ],
      view,
    );
    expect(next.rules).toHaveLength(3);
    expect(next.defaultOutput).toEqual(lit({ approve: true, limit: 77 }));
    expect(flagged).toEqual([]);
    expect(notes).toEqual([
      "Otherwise is the last row: 1 pasted row was left out.",
    ]);
  });

  it("says when pasted cells replace a rule's formula", () => {
    const spec = table();
    const { notes } = pasteInto(
      spec,
      { row: 2, column: 1 },
      [["> 5"]],
      gridOf(spec),
    );
    expect(notes).toEqual(["Pasted cells replaced the formula of rule 3."]);
  });
});

describe("the grid's history", () => {
  it("undoes and redoes table edits, merging quick edits of one cell", () => {
    const history = new GridHistory();
    const a = table();
    const b = { ...a, hitPolicy: "unique" as const };
    const c = { ...b, hitPolicy: "collect" as const };
    history.record(a, "0:1", 0);
    history.record(b, "0:1", 300);
    expect(history.undo(c)).toBe(a);
    expect(history.redo(a)).toBe(c);
    history.record(c, "0:1", 2000);
    history.record(b, "0:2", 2100);
    expect(history.undo(a)).toBe(b);
    expect(history.undo(b)).toBe(c);
  });

  it("keeps the last 100 entries", () => {
    const history = new GridHistory();
    const specs = Array.from({ length: 150 }, (_, i) => ({
      ...table(),
      rules: [{ id: `r${i}`, when: lit(true), output: lit({}) }],
    }));
    specs.forEach((spec, i) => history.record(spec, `${i}`, i * 1000));
    let current: TableSpec = table();
    let steps = 0;
    for (;;) {
      const previous = history.undo(current);
      if (!previous) break;
      current = previous;
      steps++;
    }
    expect(steps).toBe(100);
    expect(current).toBe(specs[50]);
  });
});
