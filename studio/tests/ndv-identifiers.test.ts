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
  normalizeIdentifier,
  uniqueIdentifier,
} from "../src/app/editor/ndv/params/identifiers";

describe("identifiers", () => {
  it("turns free text into a step ID", () => {
    expect(normalizeIdentifier("Get customer orders!")).toBe(
      "get-customer-orders",
    );
    expect(normalizeIdentifier("  --Check  Customer__v2 ")).toBe(
      "check-customer__v2",
    );
    expect(normalizeIdentifier("orders.v1 / EU")).toBe("orders.v1-eu");
    expect(normalizeIdentifier("!!!")).toBe("");
  });
  it("keeps at most 128 characters without a trailing hyphen", () => {
    const long = normalizeIdentifier(`${"a".repeat(127)} b`);
    expect(long).toHaveLength(127);
    expect(long.endsWith("-")).toBe(false);
  });
  it("adds -2, -3 to a taken name and stays within the limit", () => {
    expect(uniqueIdentifier("check", new Set(["check"]))).toBe("check-2");
    expect(uniqueIdentifier("check", new Set(["check", "check-2"]))).toBe(
      "check-3",
    );
    expect(uniqueIdentifier("free", new Set(["check"]))).toBe("free");
    const taken = new Set(["x".repeat(128)]);
    expect(uniqueIdentifier("x".repeat(128), taken)).toBe(
      `${"x".repeat(126)}-2`,
    );
  });
});
