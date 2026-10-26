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
// The Table view of a pane: a list of objects becomes rows under the union of
// their keys (at most 50 columns, the first 500 rows), an object a two-column
// key and value table, anything else one cell. DOM-free.
import type { Json } from "../registry";

export const TABLE_COLUMNS = 50;
export const TABLE_ROWS = 500;

export interface TableData {
  columns: string[];
  rows: string[][];
  /** Rows (or keys) the value holds; `rows.length` may be fewer. */
  total: number;
  /** "Showing 500 of 1,200" when cut, otherwise null. */
  note: string | null;
}

const isObject = (value: unknown): value is Record<string, Json> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** One cell's text: scalars as they read, structures by size. */
export function cellText(value: Json | undefined): string {
  if (value === undefined) return "";
  if (value === null) return "null";
  if (Array.isArray(value))
    return `${value.length} item${value.length === 1 ? "" : "s"}`;
  if (isObject(value)) {
    const n = Object.keys(value).length;
    return `${n} field${n === 1 ? "" : "s"}`;
  }
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function tableOf(value: Json | undefined): TableData {
  const note = (shown: number, total: number) =>
    shown < total
      ? `Showing ${shown.toLocaleString("en-US")} of ${total.toLocaleString("en-US")}`
      : null;
  if (Array.isArray(value) && value.length && value.every(isObject)) {
    const columns: string[] = [];
    for (const row of value as Record<string, Json>[])
      for (const key of Object.keys(row))
        if (!columns.includes(key) && columns.length < TABLE_COLUMNS)
          columns.push(key);
    const rows = (value as Record<string, Json>[])
      .slice(0, TABLE_ROWS)
      .map((row) => columns.map((key) => cellText(row[key])));
    return {
      columns,
      rows,
      total: value.length,
      note: note(rows.length, value.length),
    };
  }
  if (isObject(value)) {
    const entries = Object.entries(value);
    const rows = entries
      .slice(0, TABLE_ROWS)
      .map(([key, item]) => [key, cellText(item)]);
    return {
      columns: ["Field", "Value"],
      rows,
      total: entries.length,
      note: note(rows.length, entries.length),
    };
  }
  return {
    columns: ["Value"],
    rows: [[cellText(value)]],
    total: 1,
    note: null,
  };
}
