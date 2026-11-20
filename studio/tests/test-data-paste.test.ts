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
// Pasted steps bring their pinned output and their hand-set simulated
// failures into the test data. Each entry is checked as any is; one the
// host refuses is dropped, and the toast says why once.
import { describe, expect, it } from "vitest";
import { pasteTestData } from "../src/app/editor/state/paste-test-data";
import { TestDataService } from "../src/app/editor/state/test-data-service";

const AT = "2026-11-20T10:00:00Z";
const pin = {
  output: { sent: true },
  source: "manual" as const,
  pinnedAt: AT,
  configHash: "fnv1a64:0000000000000001",
  contractHash: "fnv1a64:0000000000000002",
  upstreamHash: "fnv1a64:0000000000000003",
};
/** Test data whose host refuses the pointers in `refused` and accepts the rest. */
function service(refused: string[] = []) {
  const api = {
    request: async (_url: string, _method: string, body?: unknown) => {
      const data = (body as { testData: Record<string, object | undefined> })
        .testData;
      const paths = ["pins", "errors"].flatMap((section) =>
        Object.keys(data[section] ?? {}).map((key) => `/${section}/${key}`),
      );
      return {
        entries: paths.map((path) =>
          refused.includes(path)
            ? { path, ok: false, code: "WV-SCHEMA-SECRET_VALUE" }
            : { path, ok: true },
        ),
      };
    },
  };
  return new TestDataService({
    api: api as never,
    backend: {
      keys: async () => [],
      get: async () => null,
      set: async () => true,
      remove: async () => true,
    },
    now: () => new Date(AT),
    inputs: () => ({
      source: "{}",
      definitions: [],
      schemaOf: () => ({}) as never,
    }),
  });
}

describe("test data pasted with steps", () => {
  it("brings the pins and the failures, and names the pinned steps", async () => {
    const testData = service();
    const pasted = await pasteTestData(testData, {
      pins: { "submit-1": pin },
      errors: { "submit-1": { code: "REQUEST_REFUSED" } },
    });
    expect(pasted).toEqual({ pinned: ["submit-1"], refusal: "" });
    expect(testData.data().pins).toEqual({ "submit-1": pin });
    expect(testData.data().errors).toEqual({
      "submit-1": { code: "REQUEST_REFUSED" },
    });
  });

  it("drops a failure the host refuses, keeps the rest, and says so once", async () => {
    const testData = service(["/errors/submit-1"]);
    const pasted = await pasteTestData(testData, {
      pins: { "submit-1": pin },
      errors: {
        "submit-1": { code: "REQUEST_REFUSED" },
        "submit-2": { code: "TIMEOUT" },
      },
    });
    expect(pasted.pinned).toEqual(["submit-1"]);
    expect(pasted.refusal).toMatch(/^Some simulated failures weren't pasted: /);
    expect(testData.data().errors).toEqual({ "submit-2": { code: "TIMEOUT" } });
  });

  it("refuses a malformed failure before asking the host, as it does a pin", async () => {
    const testData = service();
    const pasted = await pasteTestData(testData, {
      pins: { "submit-1": pin },
      errors: { "submit-1": { code: "request refused" } },
    });
    expect(pasted.pinned).toEqual([]);
    expect(pasted.refusal).toMatch(/^Some test data wasn't pasted: /);
    expect(testData.data().pins).toBeUndefined();
  });
});
