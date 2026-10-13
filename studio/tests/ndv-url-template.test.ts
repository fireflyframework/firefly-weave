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
import { pathPlaceholders } from "../src/app/editor/ndv/owned/owned-actions";
import {
  insertPlaceholder,
  placeholderKey,
  reconcilePathParts,
  stalePathKeys,
  urlParts,
} from "../src/app/editor/ndv/params/url-template";

describe("the URL field", () => {
  it("splits a path into text and placeholders", () => {
    expect(urlParts("/customers/{customerId}/orders")).toEqual([
      { text: "/customers/" },
      { name: "customerId" },
      { text: "/orders" },
    ]);
    expect(urlParts("")).toEqual([]);
    expect(urlParts("/{a}/{a}")).toEqual([
      { text: "/" },
      { name: "a" },
      { text: "/" },
      { name: "a" },
    ]);
    // Braces that aren't a placeholder stay text.
    expect(urlParts("/a/{2x}/{ b }")).toEqual([{ text: "/a/{2x}/{ b }" }]);
  });

  it("names a placeholder after the dropped field", () => {
    expect(placeholderKey("/input/customerId", new Set())).toBe("customerId");
    expect(placeholderKey("/input/customerId", new Set(["customerId"]))).toBe(
      "customerId-2",
    );
    expect(placeholderKey("/input/2nd value", new Set())).toBe("p2nd_value");
    // Escaped pointer keys name the field, not its escapes.
    expect(placeholderKey("/input/a~1b~0c", new Set())).toBe("a_b_c");
    // A step's whole output is named after the step.
    expect(placeholderKey("/steps/check-customer/output", new Set())).toBe(
      "check-customer",
    );
  });

  it("keeps generated names within the path grammar, suffix included", () => {
    const long = "k".repeat(200);
    const first = placeholderKey(`/input/${long}`, new Set());
    expect(first).toHaveLength(128);
    const second = placeholderKey(`/input/${long}`, new Set([first]));
    expect(second).toHaveLength(128);
    expect(second.endsWith("-2")).toBe(true);
    for (const name of [first, second])
      expect(pathPlaceholders(`/{${name}}`)).toEqual([name]);
  });

  it("inserts a placeholder at the caret and keeps the path absolute", () => {
    expect(insertPlaceholder("/customers/", null, "customerId")).toBe(
      "/customers/{customerId}",
    );
    expect(insertPlaceholder("/customers//orders", 11, "id")).toBe(
      "/customers/{id}/orders",
    );
    expect(insertPlaceholder("", null, "id")).toBe("/{id}");
  });

  it("makes each placeholder a whole path segment, as the action builder requires", () => {
    expect(insertPlaceholder("/customers", null, "id")).toBe("/customers/{id}");
    expect(insertPlaceholder("customers", null, "id")).toBe("/customers/{id}");
    // A caret inside a segment moves to the segment's end.
    expect(insertPlaceholder("/cust/orders", 3, "id")).toBe(
      "/cust/{id}/orders",
    );
    expect(insertPlaceholder("/customers/{customerId}", 14, "x")).toBe(
      "/customers/{customerId}/{x}",
    );
    // At a segment's start the placeholder comes before it.
    expect(insertPlaceholder("/a/bc", 3, "id")).toBe("/a/{id}/bc");
    expect(insertPlaceholder("/orders", 0, "id")).toBe("/{id}/orders");
    for (const path of [
      insertPlaceholder("/a/b", 2, "x"),
      insertPlaceholder("/a/b/", 4, "x"),
    ])
      expect(pathPlaceholders(path)).toEqual(["x"]);
  });

  it("finds path values that no placeholder uses any more", () => {
    expect(
      stalePathKeys("/customers/{customerId}", ["customerId", "orderId"]),
    ).toEqual(["orderId"]);
  });

  it("removes only the bindings an edit orphaned, and brings them back when the placeholder returns", () => {
    const snapshot = new Map([
      ["customerId", "A"],
      ["orderId", "B"],
    ]);
    expect(
      reconcilePathParts(
        snapshot,
        "/customers/{customerId}/orders/{orderId",
        new Set(["customerId", "orderId", "imported"]),
      ),
    ).toEqual({ remove: ["orderId"], restore: [] });
    expect(
      reconcilePathParts(
        snapshot,
        "/customers/{customerId}/orders/{orderId}",
        new Set(["customerId", "imported"]),
      ),
    ).toEqual({ remove: [], restore: [["orderId", "B"]] });
    // A value the path never used when editing began is left alone.
    expect(
      reconcilePathParts(snapshot, "/x", new Set(["imported", "customerId"])),
    ).toEqual({ remove: ["customerId"], restore: [] });
  });
});
