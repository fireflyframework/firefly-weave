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
// Simulated failures in a workflow's test data: the `errors` section the
// platform accepts (contracts/studio_documents.py ErrorEntry), written and
// removed without touching the other sections, read for one item before
// its step, and following its step when it is renamed or deleted.
import { describe, expect, it } from "vitest";
import {
  emptyTestData,
  failureOf,
  pruneEntries,
  renameEntries,
  withFailure,
  withPin,
  withScript,
} from "../src/app/editor/state/test-data-ops";
import { readTestData } from "../src/app/editor/state/test-data-read";
import { checkEntries } from "../src/app/editor/state/test-data-check";
import type {
  ErrorEntry,
  StudioTestData,
} from "../src/app/editor/state/test-data";

const AT = "2026-11-20T10:00:00Z";
const LATER = "2026-11-20T11:00:00Z";
const REFUSED: ErrorEntry = { code: "REQUEST_REFUSED" };
const pin = {
  output: { sent: true },
  source: "manual" as const,
  pinnedAt: AT,
  configHash: "fnv1a64:0000000000000001",
  contractHash: "fnv1a64:0000000000000002",
  upstreamHash: "fnv1a64:0000000000000003",
};
/** A pin on submit and a signal's answer: what a failure must leave alone. */
function others(): StudioTestData {
  return withScript(
    withPin(emptyTestData(AT), "submit", pin, AT),
    "signals",
    "approval",
    { payload: { approved: true } },
    AT,
  );
}

describe("a simulated failure in test data", () => {
  it("is written beside every other section, and removed alone", () => {
    const before = others();
    const failed = withFailure(before, "submit", REFUSED, LATER);
    expect(failed).toEqual({
      ...before,
      updatedAt: LATER,
      errors: { submit: REFUSED },
    });
    const removed = withFailure(failed, "submit", null, LATER);
    // The last failure gone, the section goes too.
    expect(removed).toEqual({ ...before, updatedAt: LATER });
    expect(withFailure(before, "submit", null, LATER)).toBe(before);
  });

  it("is read for one item before its step's, and its step's for any other item", () => {
    const data = withFailure(
      withFailure(emptyTestData(AT), "submit-item", REFUSED, AT),
      "submit-item[2]",
      { code: "TIMEOUT", outcome: "unknown" },
      AT,
    );
    expect(failureOf(data, "submit-item[2]")).toEqual({
      key: "submit-item[2]",
      entry: { code: "TIMEOUT", outcome: "unknown" },
    });
    expect(failureOf(data, "submit-item[3]")).toEqual({
      key: "submit-item",
      entry: REFUSED,
    });
    expect(failureOf(data, "after")).toBeNull();
  });

  it("follows its step when it is renamed, and goes when the step does", () => {
    const data = withFailure(others(), "submit", REFUSED, AT);
    const renamed = renameEntries(data, "submit", "send", LATER);
    expect(renamed.errors).toEqual({ send: REFUSED });
    const pruned = pruneEntries(renamed, (id) => id !== "send", LATER);
    // Its pin and its failure both go.
    expect(pruned.removed).toEqual(["send", "send"]);
    expect(pruned.data.errors).toBeUndefined();
    expect(pruned.data.signals).toEqual(data.signals);
  });

  it("is read as the platform accepts it, and refused otherwise", () => {
    const document = (errors: unknown) => ({ ...emptyTestData(AT), errors });
    const accepted = document({
      submit: REFUSED,
      "submit-item[2]": {
        code: "REQUEST.REFUSED-2_X",
        outcome: "unknown",
        attempts: 1000,
        source: "run",
        runId: "8f0f6b0e-6a43-4f0e-9a43-6b1f7a2f9c11",
        runEnvironmentId: "1b0f6b0e-6a43-4f0e-9a43-6b1f7a2f9c12",
      },
    });
    expect(readTestData(accepted)).toMatchObject({ ok: true });
    for (const refused of [
      { code: "request_refused" },
      { code: "1REFUSED" },
      { code: "A".repeat(129) },
      { code: "REFUSED", message: "The service refused the request." },
      { code: "REFUSED", outcome: "maybe" },
      { code: "REFUSED", attempts: 1001 },
      { code: "REFUSED", attempts: 1.5 },
      { code: "REFUSED", source: "simulated" },
      { code: "REFUSED", source: "run" },
      {},
    ])
      expect(
        readTestData(document({ submit: refused })),
        JSON.stringify(refused),
      ).toMatchObject({ ok: false });
  });
});

describe("a simulated failure checked as test data", () => {
  it("is never sent to the host's value check, which has nothing to check in it", async () => {
    const asked: string[] = [];
    const verdicts = await checkEntries(
      {
        request: async <T>() => {
          asked.push("check");
          return { entries: [{ path: "/pins/after", ok: true }] } as T;
        },
      },
      withFailure(emptyTestData(AT), "submit", REFUSED, AT),
      ["/errors/submit"],
      { source: "{}", definitions: [], schemaOf: () => undefined },
    );
    expect(verdicts.get("/errors/submit")).toEqual({ ok: true });
    expect(asked).toEqual([]);
  });
});
