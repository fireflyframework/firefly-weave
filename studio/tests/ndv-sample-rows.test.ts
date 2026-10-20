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
import { withSamples, type TreeRow } from "../src/app/editor/ndv/panes/views";

const row = (ref: string): TreeRow => ({
  key: ref,
  label: ref.split("/").at(-1)!,
  depth: 0,
  typeLabel: "Text",
  types: ["string"],
  ref,
});

describe("Schema rows with test data", () => {
  it("shows each field's value on one line, groups and lists by size", () => {
    const rows = [
      "/input/customerId",
      "/input/amount",
      "/input/items",
      "/input/shipping",
      "/input/express",
      "/input/missing",
    ].map(row);
    const data = {
      customerId: "c-1042",
      amount: 1250,
      items: [1, 2, 3],
      shipping: { country: "ES" },
      express: true,
    };
    expect(
      withSamples(rows, data, "/input").map((item) => item.sample),
    ).toEqual(['"c-1042"', "1250", "3 items", "1 field", "true", undefined]);
  });
  it("reads a step's output below its root, and long text is cut", () => {
    const rows = [
      row("/steps/lookup/output/note"),
      row("/steps/lookup/output/a~1b"),
    ];
    const data = { note: "x".repeat(80), "a/b": null };
    const shown = withSamples(rows, data, "/steps/lookup/output").map(
      (item) => item.sample,
    );
    expect(shown[0]).toHaveLength(60);
    expect(shown[0]!.endsWith("…")).toBe(true);
    expect(shown[1]).toBe("null");
  });
  it("leaves rows alone when there is no data", () => {
    const rows = [row("/input/a")];
    expect(withSamples(rows, undefined, "/input")).toBe(rows);
  });
});
