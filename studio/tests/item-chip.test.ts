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
// What the item chip on a recorded loop says, and how its list is grouped.
import { describe, expect, it } from "vitest";
import {
  chipName,
  itemGroups,
  itemLabel,
  typedItem,
} from "../src/app/editor/canvas/item-chip";
import type {
  RecordedItem,
  RecordedLoop,
} from "../src/app/editor/canvas/run-state";

const item = (
  index: number,
  status: RecordedItem["status"],
  code: string | null = null,
): RecordedItem => ({ index, status, code });
const loop = (items: RecordedItem[], known = true): RecordedLoop => ({
  id: "orders",
  within: [],
  count: items.length,
  known,
  items,
  results: null,
});
const ORDERS = loop([
  item(0, "finished"),
  item(1, "finished"),
  item(2, "failed", "SMTP_REJECTED"),
  item(3, "not-run"),
]);

describe("the item chip", () => {
  it("names the item one-based, with the count when the run kept it", () => {
    expect(itemLabel(2, ORDERS)).toBe("Item 3 of 4");
    expect(itemLabel(2, loop(ORDERS.items as RecordedItem[], false))).toBe(
      "Item 3",
    );
    const many = loop(
      Array.from({ length: 1200 }, (_, index) => item(index, "finished")),
    );
    expect(itemLabel(811, many)).toBe("Item 812 of 1,200");
  });

  it("says the item's status and what the button does", () => {
    expect(chipName(2, ORDERS)).toBe("Item 3 of 4, Failed. Choose an item");
    expect(chipName(3, ORDERS)).toBe("Item 4 of 4, Not run. Choose an item");
    // Cut off by a cancelled run, or one that timed out.
    const ended = loop([item(0, "stopped"), item(1, "timed-out")]);
    expect(chipName(0, ended)).toBe("Item 1 of 2, Stopped. Choose an item");
    expect(chipName(1, ended)).toBe("Item 2 of 2, Timed out. Choose an item");
  });

  it("lists the failed items first, then every item", () => {
    expect(
      itemGroups(ORDERS).map((group) => ({
        title: group.title,
        indexes: group.items.map((each) => each.index),
      })),
    ).toEqual([
      { title: "Failed", indexes: [2] },
      { title: "All items", indexes: [0, 1, 2, 3] },
    ]);
    // Without a failure, only every item.
    expect(
      itemGroups(loop([item(0, "finished")])).map((group) => group.title),
    ).toEqual(["All items"]);
  });

  it("goes to an item by its number, or nowhere", () => {
    expect(typedItem("3", 4)).toBe(2);
    expect(typedItem(" 4 ", 4)).toBe(3);
    expect(typedItem("0", 4)).toBeNull();
    expect(typedItem("5", 4)).toBeNull();
    expect(typedItem("2.5", 4)).toBeNull();
    expect(typedItem("", 4)).toBeNull();
  });
});
