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
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { readTestData } from "../src/app/editor/state/test-data-read";
import {
  INSTANCE_KEY_PATTERN,
  instanceStep,
  isInstanceKey,
  renameInstance,
} from "../src/app/editor/state/instance-keys";
import { testData } from "./contracts/test-data-examples";

const fixture = (name: string) =>
  JSON.parse(readFileSync(resolve("tests/fixtures", name), "utf8")) as {
    [key: string]: unknown;
  };

describe("test data documents", () => {
  it("accepts and refuses every case exactly as the platform does", () => {
    const { cases } = fixture("test-data-cases.json") as {
      cases: { name: string; document: unknown; valid: boolean }[];
    };
    expect(cases.length).toBeGreaterThan(60);
    for (const { name, document, valid } of cases)
      expect(readTestData(document).ok, name).toBe(valid);
  });
  it("keeps at most 1000 pins, as the platform does", () => {
    const pin = testData.pins!["send"];
    const pins = (count: number) =>
      Object.fromEntries(
        Array.from({ length: count }, (_, index) => [`step-${index}`, pin]),
      );
    const document = (count: number) => ({ ...testData, pins: pins(count) });
    expect(readTestData(document(1000)).ok).toBe(true);
    expect(readTestData(document(1001)).ok).toBe(false);
  });
  it("reads the documented example", () => {
    expect(readTestData(structuredClone(testData))).toEqual({
      ok: true,
      data: testData,
    });
  });
  it("says why a document can't be used, in words", () => {
    expect(readTestData({ schemaVersion: 1 })).toEqual({
      ok: false,
      reason: "This test data isn't in a format Studio can read.",
    });
    const large = {
      schemaVersion: 1,
      kind: "weave.studio/testData",
      updatedAt: "2026-10-07T10:00:00Z",
      input: { value: "x".repeat(1_048_577), source: "manual" },
    };
    expect(readTestData(large)).toEqual({
      ok: false,
      reason: "This test data is larger than 1 MB.",
    });
  });
});

describe("instance keys", () => {
  it("use the platform's pattern and accept exactly its keys", () => {
    const { pattern, valid, invalid } = fixture(
      "language/instance-keys.json",
    ) as {
      pattern: string;
      valid: { key: string; node_id: string }[];
      invalid: { key: string }[];
    };
    expect(INSTANCE_KEY_PATTERN).toBe(pattern);
    for (const { key, node_id } of valid) {
      expect(isInstanceKey(key), key).toBe(true);
      expect(instanceStep(key), key).toBe(node_id);
    }
    for (const { key } of invalid) expect(isInstanceKey(key), key).toBe(false);
  });
  it("follow a renamed step and leave other steps alone", () => {
    expect(renameInstance("send[2]#1.2~3", "send", "notify")).toBe(
      "notify[2]#1.2~3",
    );
    expect(renameInstance("send", "send", "notify")).toBe("notify");
    expect(renameInstance("sender[1]", "send", "notify")).toBe("sender[1]");
  });
});
