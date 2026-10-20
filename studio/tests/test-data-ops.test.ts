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
  TEST_DATA_TOO_LARGE,
  containsRunData,
  emptyTestData,
  entriesOf,
  fitsTestData,
  pinOf,
  pruneEntries,
  renameEntries,
  runDerived,
  withInput,
  withPin,
  withoutInput,
  withoutPin,
  withoutRunData,
} from "../src/app/editor/state/test-data-ops";
import { readTestData } from "../src/app/editor/state/test-data-read";
import type { PinEntry } from "../src/app/editor/state/test-data";
import { testData } from "./contracts/test-data-examples";

const NOW = "2026-10-20T10:00:00Z";
const pin = (output: unknown): PinEntry => ({
  output: output as PinEntry["output"],
  source: "manual",
  pinnedAt: NOW,
  configHash: "fnv1a64:0000000000000001",
  contractHash: "fnv1a64:0000000000000002",
  upstreamHash: "fnv1a64:0000000000000003",
});

describe("test data changes", () => {
  it("sets and clears the test input and pins, stamping the time", () => {
    const empty = emptyTestData("2026-10-20T09:00:00Z");
    const withData = withPin(
      withInput(empty, { amount: 1500 }, "manual", NOW),
      "lookup",
      pin({ found: true }),
      NOW,
    );
    expect(withData).toEqual({
      ...empty,
      updatedAt: NOW,
      input: { value: { amount: 1500 }, source: "manual" },
      pins: { lookup: pin({ found: true }) },
    });
    const cleared = withoutInput(withoutPin(withData, "lookup", NOW), NOW);
    expect(cleared).toEqual({ ...empty, updatedAt: NOW });
    expect("pins" in cleared).toBe(false);
    expect(withoutPin(cleared, "lookup", NOW)).toBe(cleared);
    expect(readTestData(withData).ok).toBe(true);
  });
  it("finds the pin of one iteration before its step's", () => {
    let data = withPin(emptyTestData(NOW), "send", pin("all"), NOW);
    data = withPin(data, "send[1]", pin("second"), NOW);
    expect(pinOf(data, "send[1]")?.entry.output).toBe("second");
    expect(pinOf(data, "send[2]")).toEqual({ key: "send", entry: pin("all") });
    expect(pinOf(data, "sender")).toBeNull();
  });
  it("follows a renamed step in keys and frames, never inside a called workflow", () => {
    const renamed = renameEntries(testData, "send", "notify", NOW);
    expect(Object.keys(renamed.pins ?? {})).toEqual([
      "check-customer",
      "notify",
      "notify[1]",
    ]);
    const frames = renameEntries(testData, "enrich", "fetch", NOW);
    expect(frames.humans?.["review"]).toEqual({
      decision: "reject",
      data: { note: "Wrong address" },
      frame: "fetch",
    });
    // review sits inside the called workflow: renaming this workflow's review moves nothing.
    expect(renameEntries(testData, "review", "check", NOW)).toBe(testData);
    expect(renamed.workflows).toEqual(testData.workflows);
  });
  it("drops the entries of deleted steps and names them", () => {
    const { data, removed } = pruneEntries(
      testData,
      (id) => id !== "send" && id !== "enrich",
      NOW,
    );
    expect(removed).toEqual(["send", "send[1]", "review"]);
    expect(Object.keys(data.pins ?? {})).toEqual(["check-customer"]);
    expect(Object.keys(data.humans ?? {})).toEqual(["approval"]);
    expect(pruneEntries(testData, () => true, NOW).data).toBe(testData);
  });
  it("tells data recorded from a run apart and can leave it out", () => {
    expect(runDerived(testData.pins?.["check-customer"])).toBe(true);
    expect(runDerived(testData.pins?.["send"])).toBe(false);
    expect(containsRunData(testData)).toBe(true);
    const clean = withoutRunData(testData, NOW);
    expect(containsRunData(clean)).toBe(false);
    expect(Object.keys(clean.pins ?? {})).toEqual(["send", "send[1]"]);
    expect(readTestData(clean).ok).toBe(true);
  });
  it("lists every entry with its pointer", () => {
    expect(entriesOf(testData).map((item) => item.path)).toEqual([
      "/input",
      "/pins/check-customer",
      "/pins/send",
      "/pins/send[1]",
      "/signals/wait-paid",
      "/humans/approval",
      "/humans/review",
      "/aiTurns/support",
      "/workflows/notify-customer@1.0.0/mocks/send-mail",
    ]);
  });
  it("measures the document as the platform does", () => {
    const big = withInput(
      emptyTestData(NOW),
      "é".repeat(524_288),
      "manual",
      NOW,
    );
    expect(fitsTestData(big)).toBe(false);
    expect(TEST_DATA_TOO_LARGE).toBe(
      "Test data is limited to 1 MB. Unpin some output to make room.",
    );
  });
});
