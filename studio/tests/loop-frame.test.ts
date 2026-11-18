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
// What a loop's frame says in its header: the list it goes over, printed as
// a formula, and how many items run at a time when that's more than one.
import { describe, expect, it } from "vitest";
import { loopHeader } from "../src/app/editor/canvas/loop-frame";
import { createStep, type Step } from "../src/app/model";

const loop = (extra: Record<string, unknown>): Step => ({
  ...createStep("forEach", "orders"),
  ...extra,
});

describe("a loop's header", () => {
  it("names the list and how many items run at a time", () => {
    expect(
      loopHeader(loop({ items: { ref: "/input/orders" }, concurrency: 4 })),
    ).toBe("For each item in input.orders · up to 4 at a time");
  });

  it("leaves out the items at a time when they run one by one", () => {
    expect(loopHeader(loop({ items: { ref: "/input/orders" } }))).toBe(
      "For each item in input.orders",
    );
    expect(
      loopHeader(loop({ items: { ref: "/input/orders" }, concurrency: 1 })),
    ).toBe("For each item in input.orders");
  });

  it("waits for the list while none is chosen", () => {
    expect(loopHeader(loop({}))).toBe("For each item in …");
    expect(loopHeader(loop({ concurrency: 4 }))).toBe(
      "For each item in … · up to 4 at a time",
    );
  });

  it("prints the list as the formula editor writes it", () => {
    expect(loopHeader(loop({ items: { ref: "/item/lines" } }))).toBe(
      "For each item in item.lines",
    );
    expect(
      loopHeader(loop({ items: { ref: "/steps/fetch/output/open orders" } })),
    ).toBe('For each item in steps.fetch.output["open orders"]');
    // A tree outside the language reads as not chosen, never as raw JSON.
    expect(loopHeader(loop({ items: { script: "orders()" } }))).toBe(
      "For each item in …",
    );
  });
});
