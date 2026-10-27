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
// The words a step that ran shows: its summary line and a decision's paths.
import { describe, expect, it } from "vitest";
import { pathRows, summaryLine } from "../src/app/editor/state/run-summary";
import type { Step } from "../src/app/model";

describe("what a step that ran says", () => {
  const route = {
    id: "route",
    kind: "switch",
    cases: [
      { when: { literal: true }, steps: [], output: { literal: null } },
      { when: { literal: true }, steps: [], output: { literal: null } },
    ],
    default: { steps: [], output: { literal: null } },
  } as unknown as Step;
  const taking = (name: string) => ({
    branches: { [`route/${name}`]: { owner: "route", name } },
  });
  it("lists a decision's paths as taken, not taken or not checked", () => {
    expect(
      pathRows(route, taking("case:0"))?.map((r) => [r.label, r.outcome]),
    ).toEqual([
      ["Path 1", "taken"],
      ["Path 2", "not-checked"],
      ["Otherwise", "not-taken"],
    ]);
    expect(pathRows(route, taking("case:1"))?.map((r) => r.outcome)).toEqual([
      "not-taken",
      "taken",
      "not-taken",
    ]);
    expect(pathRows(route, taking("default"))?.map((r) => r.outcome)).toEqual([
      "not-taken",
      "not-taken",
      "taken",
    ]);
    expect(pathRows(route, {})).toBeNull();
  });
  it("sums up its output in a few words", () => {
    const step = (kind: string, extra = {}) =>
      ({ id: "s", kind, ...extra }) as unknown as Step;
    expect(summaryLine(step("action"), { a: 1, b: 2, c: 3 })).toBe("3 fields");
    expect(summaryLine(step("action"), [1, 2])).toBe("2 items");
    expect(summaryLine(step("transform"), true)).toBe("true");
    expect(
      summaryLine(step("humanTask"), { decision: "approve", data: {} }),
    ).toBe("approve");
    expect(summaryLine(step("wait", { durationSeconds: 300 }), null)).toBe(
      "Waited 5 min",
    );
    expect(summaryLine(step("signal"), {})).toBe("Signal received");
    expect(summaryLine(route, null, taking("default"))).toBe("Path: Otherwise");
    expect(summaryLine(step("action"), undefined)).toBeNull();
  });
  it("names a taken case by its condition", () => {
    const amount = {
      ...route,
      cases: [
        {
          when: {
            op: {
              name: "gt",
              args: [{ ref: "/input/amount" }, { literal: 1000 }],
            },
          },
          steps: [],
          output: { literal: null },
        },
      ],
    } as unknown as Step;
    const line = summaryLine(amount, null, taking("case:0"));
    expect(line).toMatch(/^Path: /);
    expect(line).toContain("1000");
  });
});
