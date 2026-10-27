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
// The Table view's rows, columns and note.
import { describe, expect, it } from "vitest";
import {
  cellText,
  filterTable,
  tableCount,
  tableOf,
} from "../src/app/editor/ndv/panes/table-view";

describe("the Table view", () => {
  it("puts a list of objects in rows under the union of their keys", () => {
    expect(tableOf([{ a: 1 }, { b: "x", a: { n: 1 } }])).toEqual({
      columns: ["a", "b"],
      rows: [
        ["1", ""],
        ["1 field", "x"],
      ],
      total: 2,
      note: null,
    });
  });
  it("shows at most 50 columns and 500 rows, and says so", () => {
    const wide = [
      Object.fromEntries(Array.from({ length: 60 }, (_, i) => [`k${i}`, i])),
    ];
    expect(tableOf(wide).columns).toHaveLength(50);
    const long = Array.from({ length: 1200 }, (_, i) => ({ i }));
    expect(tableOf(long).rows).toHaveLength(500);
    expect(tableOf(long).note).toBe("Showing 500 of 1,200");
  });
  it("shows an object as fields and values, and anything else in one cell", () => {
    expect(tableOf({ a: [1, 2], b: null })).toEqual({
      columns: ["Field", "Value"],
      rows: [
        ["a", "2 items"],
        ["b", "null"],
      ],
      total: 2,
      note: null,
    });
    expect(tableOf("hi")).toEqual({
      columns: ["Value"],
      rows: [["hi"]],
      total: 1,
      note: null,
    });
    expect(cellText(undefined)).toBe("");
  });
});

describe("the Table filter", () => {
  it("searches every row, then shows at most 500", () => {
    const rows = Array.from({ length: 1200 }, (_, i) => ({
      tier: i >= 1000 ? "gold" : "silver",
    }));
    expect(tableCount(rows, "gold")).toBe("200 of 1,200 items");
    expect(filterTable(tableOf(rows), "gold", rows)).toHaveLength(200);
  });
});
