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
// The values Input holds while the table's input columns change.
import { signal } from "@angular/core";
import { describe, expect, it } from "vitest";
import {
  TableTry,
  type TryGrid,
} from "../src/app/editor/decision-grid/table-try";

const grid = (): TryGrid => ({
  spec: signal(null),
  view: signal(null),
  shown: signal(null),
  readOnly: signal(false),
  goToRule: () => undefined,
});

describe("the values being tried when the input columns change", () => {
  it("drops a removed column's value, so nothing hidden is tried", () => {
    const tried = new TableTry(grid());
    tried.start(null);
    tried.change({ tier: "gold" });
    tried.reshape(["amount", "tier"], ["amount"]);
    expect(tried.value()).toEqual({});
    expect(tried.initial()).toEqual({});
    expect(tried.empty()).toBe(true);
  });

  it("carries a renamed column's value to its new name", () => {
    const tried = new TableTry(grid());
    tried.start({ amount: 2000, tier: "gold" });
    tried.reshape(["amount", "tier"], ["amount", "level"]);
    expect(tried.value()).toEqual({ amount: 2000, level: "gold" });
    expect(tried.initial()).toEqual({ amount: 2000, level: "gold" });
    // The step's test data follows the rename: nothing reads as edited.
    expect(tried.edited()).toBe(false);
  });

  it("keeps every value when a column is added", () => {
    const tried = new TableTry(grid());
    tried.start(null);
    tried.change({ amount: 500, tier: "silver" });
    tried.reshape(["amount", "tier"], ["amount", "tier", "region"]);
    expect(tried.value()).toEqual({ amount: 500, tier: "silver" });
    expect(tried.initial()).toEqual({ amount: 500, tier: "silver" });
  });
});
