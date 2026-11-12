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
// The decision grid's keyboard model, apart from the component: where the
// cursor goes, how a block copied from a spreadsheet fills the grid, and the
// grid's own undo history. Columns count from the rule's name (0), then the
// input columns, then the result columns; rows are the rules, then Otherwise.
import type { Expression } from "../ndv/registry";
import { parseCell } from "./cell-grammar";
import { MAX_RULES } from "./grid-checks";
import { parseResult } from "./result-cells";
import {
  addRule,
  composeWhen,
  decomposeWhen,
  renameRule,
  setResult,
  type GridView,
  type TableSpec,
} from "./rules-model";

export interface GridCursor {
  row: number;
  column: number;
}
export interface GridShape {
  rows: number;
  columns: number;
  otherwise: boolean;
}
export type CursorCommand =
  | "cellUp"
  | "cellDown"
  | "cellLeft"
  | "cellRight"
  | "nextCell"
  | "previousCell";

/** Where a key moves the cursor; Tab and Shift+Tab leave the grid from its last and first cells. */
export function moveCursor(
  cursor: GridCursor,
  command: CursorCommand,
  shape: GridShape,
): GridCursor | "leave-forward" | "leave-backward" {
  const lastRow = shape.rows - 1 + (shape.otherwise ? 1 : 0);
  const lastColumn = shape.columns - 1;
  const { row, column } = cursor;
  switch (command) {
    case "cellUp":
      return { row: Math.max(0, row - 1), column };
    case "cellDown":
      return { row: Math.min(lastRow, row + 1), column };
    case "cellLeft":
      return { row, column: Math.max(0, column - 1) };
    case "cellRight":
      return { row, column: Math.min(lastColumn, column + 1) };
    case "nextCell":
      if (column < lastColumn) return { row, column: column + 1 };
      return row < lastRow ? { row: row + 1, column: 0 } : "leave-forward";
    case "previousCell":
      if (column > 0) return { row, column: column - 1 };
      return row > 0 ? { row: row - 1, column: lastColumn } : "leave-backward";
  }
}

const MAX_PASTE_ROWS = 1000;
const MAX_PASTE_COLUMNS = 64;

/**
 * Tab-separated rows as spreadsheets copy them: `\r\n` or `\n` between rows,
 * a cell in double quotes may hold tabs, newlines and doubled quotes, and the
 * trailing newline is dropped. At most 1,000 rows of 64 cells.
 */
export function parsePaste(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  let atStart = true;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (quoted) {
      if (char === '"' && text[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (char === '"') quoted = false;
      else cell += char;
      continue;
    }
    if (char === '"' && atStart) {
      quoted = true;
      atStart = false;
      continue;
    }
    atStart = false;
    if (char === "\t") {
      row.push(cell);
      cell = "";
      atStart = true;
    } else if (char === "\n" || char === "\r") {
      if (char === "\r" && text[i + 1] === "\n") i++;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
      atStart = true;
    } else cell += char;
  }
  if (cell !== "" || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows
    .slice(0, MAX_PASTE_ROWS)
    .map((cells) => cells.slice(0, MAX_PASTE_COLUMNS));
}

export interface PastedProblem {
  row: number;
  column: number;
  text: string;
  message: string;
}

/**
 * A block pasted from the cursor, as one edit: conditions read by the cell
 * language, results by their column's type, rules added below up to 1,000,
 * columns never added. Cells that don't read are left out and flagged.
 */
export function pasteInto(
  spec: TableSpec,
  cursor: GridCursor,
  block: string[][],
  view: GridView,
): { spec: TableSpec; flagged: PastedProblem[]; notes: string[] } {
  const inputs = view.inputs.length;
  const columns = 1 + inputs + view.results.length;
  const flagged: PastedProblem[] = [];
  const notes: string[] = [];
  let next = spec;
  // On Otherwise, the first pasted row fills its results; it is the last row.
  if (view.otherwise && cursor.row >= spec.rules.length) {
    block[0]?.forEach((text, at) => {
      const column = cursor.column + at;
      if (column <= inputs || column >= columns) return;
      const index = column - 1 - inputs;
      const reading = parseResult(text, view.results[index]);
      if (reading.kind === "error" || reading.kind === "formula") {
        flagged.push({
          row: spec.rules.length,
          column,
          text,
          message:
            reading.kind === "error"
              ? reading.message
              : "Write a formula in the formula editor.",
        });
        return;
      }
      next = setResult(
        next,
        "otherwise",
        index,
        reading.kind === "empty" ? undefined : reading.expression,
      );
    });
    const left = block.length - 1;
    if (left > 0)
      notes.push(
        `Otherwise is the last row: ${left} pasted row${left === 1 ? " was" : "s were"} left out.`,
      );
    return { spec: next, flagged, notes };
  }
  block.forEach((cells, offset) => {
    const row = cursor.row + offset;
    while (row >= next.rules.length && next.rules.length < MAX_RULES)
      next = addRule(next, next.rules.length - 1);
    if (row >= next.rules.length) return;
    const testable = view.inputs.map((column) => ({
      name: column.name,
      type: column.type ?? ("text" as const),
    }));
    let conditions: (Expression | null)[] | null = null;
    cells.forEach((text, at) => {
      const column = cursor.column + at;
      if (column >= columns) return;
      if (column === 0) {
        next = renameRule(next, row, text.trim());
        return;
      }
      if (column <= inputs) {
        const index = column - 1;
        if (view.inputs[index].type === null) {
          flagged.push({
            row,
            column,
            text,
            message: "Test this field in a formula: = …",
          });
          return;
        }
        // A rule shown as one formula is replaced by the cells pasted over it.
        if (!conditions) {
          const cells = decomposeWhen(next.rules[row].when, testable);
          if (!cells)
            notes.push(`Pasted cells replaced the formula of rule ${row + 1}.`);
          conditions = cells ?? testable.map(() => null);
        }
        const reading = parseCell(text, testable[index]);
        if (reading.kind === "error" || reading.kind === "formula") {
          flagged.push({
            row,
            column,
            text,
            message:
              reading.kind === "error"
                ? reading.message
                : "Write a formula in the formula editor.",
          });
          return;
        }
        conditions[index] =
          reading.kind === "empty" ? null : reading.expression;
        return;
      }
      const index = column - 1 - inputs;
      const reading = parseResult(text, view.results[index]);
      if (reading.kind === "error" || reading.kind === "formula") {
        flagged.push({
          row,
          column,
          text,
          message:
            reading.kind === "error"
              ? reading.message
              : "Write a formula in the formula editor.",
        });
        return;
      }
      try {
        next = setResult(
          next,
          row,
          index,
          reading.kind === "empty" ? undefined : reading.expression,
        );
      } catch (error) {
        flagged.push({
          row,
          column,
          text,
          message: error instanceof Error ? error.message : String(error),
        });
      }
    });
    if (conditions) {
      const rule = next.rules[row];
      const when = composeWhen(conditions);
      if (JSON.stringify(when) !== JSON.stringify(rule.when))
        next = {
          ...next,
          rules: next.rules.map((item, i) =>
            i === row ? { ...item, when } : item,
          ),
        };
    }
  });
  return { spec: next, flagged, notes };
}

const MERGE_WINDOW = 600;
const HISTORY_LIMIT = 100;

/** The grid's own undo history: the table before each edit, 100 at most; quick edits of one cell merge. */
export class GridHistory {
  private past: TableSpec[] = [];
  private future: TableSpec[] = [];
  private last: { key: string; at: number } | null = null;

  /** Records the table as it was before an edit of `key` (a cell, or a command). */
  record(before: TableSpec, key: string, now: number): void {
    const merge =
      this.last?.key === key &&
      now - this.last.at < MERGE_WINDOW &&
      this.past.length;
    this.last = { key, at: now };
    this.future = [];
    if (merge) return;
    this.past.push(before);
    if (this.past.length > HISTORY_LIMIT) this.past.shift();
  }
  /** The table before the last edit, or null; `current` becomes redoable. */
  undo(current: TableSpec): TableSpec | null {
    const previous = this.past.pop();
    if (!previous) return null;
    this.future.push(current);
    this.last = null;
    return previous;
  }
  redo(current: TableSpec): TableSpec | null {
    const next = this.future.pop();
    if (!next) return null;
    this.past.push(current);
    this.last = null;
    return next;
  }
  clear(): void {
    this.past = [];
    this.future = [];
    this.last = null;
  }
}
