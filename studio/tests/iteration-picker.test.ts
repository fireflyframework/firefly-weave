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
// What the iteration picker says for each loop around a step in step
// details: the item shown, whether Previous and Next can move, and the line
// under the row.
import { describe, expect, it } from "vitest";
import {
  goToItem,
  pickerLoops,
  pickerView,
  type PickerLoop,
} from "../src/app/editor/ndv/panes/iteration-picker";
import {
  loopSources,
  rootOf,
  sourceRows,
  type PaneEntry,
} from "../src/app/editor/ndv/panes/views";
import {
  OUTSIDE_LOOP,
  outsideLoop,
  planDrop,
} from "../src/app/editor/ndv/params/drop";
import type { ParamSpec } from "../src/app/editor/ndv/registry";

const loop = (extra: Partial<PickerLoop> = {}): PickerLoop => ({
  loopId: "orders",
  role: "Loop",
  index: 2,
  count: 20,
  known: true,
  limit: 1000,
  ...extra,
});

describe("the iteration picker's row", () => {
  it("names the item and the count once the count is known", () => {
    expect(pickerView(loop())).toEqual({
      item: "Item 3 of 20",
      name: "Item 3 of 20. Choose an item",
      first: false,
      last: false,
      empty: false,
      note: null,
    });
    expect(pickerView(loop({ index: 0 }))).toMatchObject({
      first: true,
      last: false,
    });
    expect(pickerView(loop({ index: 19 }))).toMatchObject({
      item: "Item 20 of 20",
      first: false,
      last: true,
    });
  });

  it("says why the count is missing until the steps before have run", () => {
    expect(pickerView(loop({ index: 0, known: false, count: 0 }))).toEqual({
      item: "Item 1",
      name: "Item 1. Choose an item",
      first: true,
      last: false,
      empty: false,
      note: "Execute previous steps to see the items.",
    });
    expect(pickerView(loop({ known: false, count: 0 }))).toMatchObject({
      item: "Item 3",
      last: false,
      note: "The number of items shows once the previous steps have run.",
    });
  });

  it("says an empty list runs nothing, with every control off", () => {
    expect(pickerView(loop({ index: 0, count: 0 }))).toEqual({
      item: "No items",
      name: "No items",
      first: true,
      last: true,
      empty: true,
      note: "The list is empty: the steps inside don't run.",
    });
  });

  it("stops at Up to, and says how many items the list had", () => {
    expect(pickerView(loop({ count: 2345, limit: 1000 }))).toMatchObject({
      item: "Item 3 of 1,000",
      note: "Up to 1,000 of 2,345 items.",
    });
    expect(
      pickerView(loop({ index: 999, count: 2345, limit: 1000 })).last,
    ).toBe(true);
  });
});

describe("the picker's loops", () => {
  it("gives one row per loop around the step, the innermost last", () => {
    const rows = pickerLoops(
      [
        { loopId: "orders", index: 1 },
        { loopId: "lines", index: 0 },
      ],
      (loopId) =>
        loopId === "orders"
          ? { count: 20, known: true }
          : { count: 0, known: false },
      (loopId) => (loopId === "orders" ? 1000 : 50),
    );
    expect(rows).toEqual([
      {
        loopId: "orders",
        role: "Outer loop",
        index: 1,
        count: 20,
        known: true,
        limit: 1000,
      },
      {
        loopId: "lines",
        role: "Loop",
        index: 0,
        count: 0,
        known: false,
        limit: 50,
      },
    ]);
    expect(
      pickerLoops(
        [],
        () => ({ count: 0, known: false }),
        () => 1,
      ),
    ).toEqual([]);
  });
});

describe("Go to item", () => {
  it("takes an item number people count from 1, inside the list", () => {
    expect(goToItem("4", loop())).toBe(3);
    expect(goToItem(" 20 ", loop())).toBe(19);
    expect(goToItem("21", loop())).toBeNull();
    expect(goToItem("0", loop())).toBeNull();
    expect(goToItem("2.5", loop())).toBeNull();
    expect(goToItem("x", loop())).toBeNull();
    // Past Up to, nothing runs.
    expect(goToItem("1001", loop({ count: 2345, limit: 1000 }))).toBeNull();
    // Without a count, any item number goes.
    expect(goToItem("40", loop({ known: false, count: 0 }))).toBe(39);
  });
});

describe("a loop's item in the Input pane", () => {
  const entry = (ref: string, source: string, loop?: string) =>
    ({
      ref,
      source,
      loop,
      path: [],
      label: "",
      breadcrumb: "",
      schema: {},
      types: [],
      typeLabel: "",
      optional: false,
    }) as PaneEntry;
  const field = (ref: string, source: string, path: string[], loop?: string) =>
    ({ ...entry(ref, source, loop), path }) as PaneEntry;

  it("lists the loops' items first, then the workflow input, then the steps before", () => {
    const entries = [
      entry("/input", "input"),
      entry("/steps/fetch/output", "step"),
      entry("/loops/orders/item", "item", "orders"),
      entry("/loops/orders/index", "index", "orders"),
      entry("/item", "item", "lines"),
      entry("/index", "index", "lines"),
    ];
    const sources = loopSources(entries, [
      { id: "fetch", kind: "transform" },
      { id: "check", kind: "transform" },
    ]);
    expect(sources.map((source) => [source.id, source.label])).toEqual([
      ["item", "Current item"],
      ["index", "Item position"],
      ["loop:orders", "Loop orders › Current item"],
      ["loop-index:orders", "Loop orders › Item position"],
      ["input", "Workflow input"],
      ["step:check", "check"],
      ["step:fetch", "fetch"],
    ]);
    expect(sources.map((source) => rootOf(source.id))).toEqual([
      "/item",
      "/index",
      "/loops/orders/item",
      "/loops/orders/index",
      "/input",
      "/steps/check/output",
      "/steps/fetch/output",
    ]);
  });

  it("drags the current item's fields as the loop's own references", () => {
    const rows = sourceRows(
      [
        entry("/item", "item", "lines"),
        field("/item/customerEmail", "item", ["customerEmail"], "lines"),
        field(
          "/loops/orders/item/customerId",
          "item",
          ["customerId"],
          "orders",
        ),
      ],
      "item",
    );
    expect(rows.map((row) => [row.ref, row.breadcrumb])).toEqual([
      ["/item/customerEmail", "Current item › customerEmail"],
    ]);
    const outer = sourceRows(
      [
        field(
          "/loops/orders/item/customerId",
          "item",
          ["customerId"],
          "orders",
        ),
      ],
      "loop:orders",
    );
    expect(outer.map((row) => [row.ref, row.breadcrumb])).toEqual([
      [
        "/loops/orders/item/customerId",
        "Loop orders › Current item › customerId",
      ],
    ]);
  });

  it("refuses an item dropped on a field the loop isn't around", () => {
    expect(outsideLoop("/item/customerEmail", [])).toBe(true);
    expect(outsideLoop("/index", [])).toBe(true);
    expect(outsideLoop("/item/customerEmail", ["lines", "orders"])).toBe(false);
    expect(outsideLoop("/loops/orders/item/id", ["lines"])).toBe(true);
    expect(outsideLoop("/loops/orders/item/id", ["lines", "orders"])).toBe(
      false,
    );
    expect(outsideLoop("/input/orders", [])).toBe(false);
    const spec = { id: "to", path: ["with", "to"], type: "text", label: "To" };
    const drag = { ref: "/item/customerEmail", breadcrumb: "", schema: {} };
    expect(
      planDrop(spec as ParamSpec, { mode: "absent" }, drag, {
        caret: null,
        templates: false,
        mode: "mapped",
        parse: { features: [], loops: [] },
      }),
    ).toEqual({ kind: "refuse", reason: OUTSIDE_LOOP });
    expect(OUTSIDE_LOOP).toBe("Only steps inside the loop can read its items");
    expect(
      planDrop(spec as ParamSpec, { mode: "absent" }, drag, {
        caret: null,
        templates: false,
        mode: "mapped",
        parse: { features: [], loops: ["orders"] },
      }),
    ).toMatchObject({ kind: "write" });
  });
});
