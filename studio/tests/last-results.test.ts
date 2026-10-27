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
// The last execution's results: merged, stale, pruned, bounded and read back strictly.
import { describe, expect, it } from "vitest";
import type { ExecuteResponse } from "../src/app/editor/state/execution";
import {
  LAST_RESULTS_MAX_BYTES,
  branchVariables,
  keptText,
  lastResultsKey,
  pruneResults,
  readResults,
  staleResults,
  withExecution,
} from "../src/app/editor/state/last-results";
import { RESPONSE, print } from "./execution-fixtures";

describe("last results", () => {
  const prints = new Map([
    ["check", print(1)],
    ["send", print(2)],
  ]);
  it("keep what each step returned with the fingerprint it ran with, and what earlier executions found", () => {
    const earlier = withExecution(
      null,
      { ...RESPONSE, trace: [{ ...RESPONSE.trace[1], output: { id: "old" } }] },
      prints,
      "a",
    );
    const results = withExecution(
      {
        ...earlier,
        steps: {
          ...earlier.steps,
          other: {
            status: "completed",
            execution: print(5).execution,
            output: 5,
          },
        },
      },
      RESPONSE,
      prints,
      "b",
    );
    expect(results.at).toBe("b");
    expect(results.steps).toEqual({
      check: {
        status: "completed",
        execution: print(1).execution,
        input: { id: "c" },
        output: { ok: true },
      },
      "send[0]": {
        status: "completed",
        execution: print(2).execution,
        output: { id: "m-0" },
      },
      other: { status: "completed", execution: print(5).execution, output: 5 },
    });
  });
  it("go out of date when what the step depends on changes, and leave with their step", () => {
    const results = withExecution(null, RESPONSE, prints, "a");
    expect(staleResults(results, prints)).toEqual(new Set());
    expect(
      staleResults(
        results,
        new Map([
          ["check", print(9)],
          ["send", print(2)],
        ]),
      ),
    ).toEqual(new Set(["check"]));
    expect(
      Object.keys(pruneResults(results, (id) => id === "send").steps),
    ).toEqual(["send[0]"]);
  });
  it("are kept under the account's prefix, at most 1 MiB, and read back only when this version wrote them", () => {
    expect(lastResultsKey("local:", "d-1")).toBe(
      "local:weave.lastExecution.d-1",
    );
    const results = withExecution(null, RESPONSE, prints, "a");
    expect(readResults(keptText(results))).toEqual(results);
    const big = {
      ...results,
      steps: {
        check: {
          ...results.steps["check"],
          output: "x".repeat(LAST_RESULTS_MAX_BYTES),
        },
      },
    };
    expect(keptText(big)).toBeNull();
    for (const text of [
      "",
      "{",
      "[]",
      '{"schemaVersion":2}',
      JSON.stringify({
        ...results,
        steps: { a: { status: "done", execution: "x" } },
      }),
    ])
      expect(readResults(text)).toBeNull();
  });
});

describe("last results of a loop", () => {
  const ran = (count: number) => ({
    ...RESPONSE,
    trace: Array.from({ length: count }, (_, i) => ({
      index: i,
      nodeId: "send",
      instanceKey: `send[${i}]`,
      kind: "action",
      status: "completed" as const,
      output: { id: i },
    })),
  });
  it("forget iterations an execution no longer reached", () => {
    const prints = new Map([["send", print(2)]]);
    const three = withExecution(null, ran(3), prints, "a");
    const two = withExecution(three, ran(2), prints, "b");
    expect(Object.keys(two.steps)).toEqual(["send[0]", "send[1]"]);
  });
});

describe("last results of a decision", () => {
  const prints = new Map([
    ["route", print(1)],
    ["check", print(2)],
  ]);
  const routed: ExecuteResponse = {
    ...RESPONSE,
    trace: [
      {
        index: 0,
        nodeId: "route",
        instanceKey: "",
        kind: "switch",
        status: "completed",
        output: { tier: "big" },
      },
      {
        index: 1,
        nodeId: "route",
        instanceKey: "route[1]",
        kind: "switch",
        status: "completed",
      },
    ],
    variables: {
      branches: {
        "route/case:0": {
          owner: "route",
          name: "case:0",
          output: { tier: "big" },
        },
        "route/default[1]": { owner: "route", name: "default", output: null },
      },
    },
  };
  it("keep the path each decision took, per iteration", () => {
    const results = withExecution(null, routed, prints, "a");
    expect(results.steps["route"].branch).toEqual({
      name: "case:0",
      output: { tier: "big" },
    });
    expect(results.steps["route[1]"].branch).toEqual({
      name: "default",
      output: null,
    });
    expect(branchVariables(results, "route[1]")).toEqual({
      branches: {
        "route[1]": { owner: "route", name: "default", output: null },
      },
    });
  });
  it("keep a decision's path after executing another step, and after a reload", () => {
    const first = withExecution(null, routed, prints, "a");
    const later = withExecution(
      first,
      { ...RESPONSE, trace: [RESPONSE.trace[0]], variables: {} },
      prints,
      "b",
    );
    expect(branchVariables(later, "route")).toEqual({
      branches: {
        route: { owner: "route", name: "case:0", output: { tier: "big" } },
      },
    });
    const reloaded = readResults(keptText(later));
    expect(reloaded?.steps["route"].branch?.name).toBe("case:0");
    expect(branchVariables(reloaded, "route")).toEqual(
      branchVariables(later, "route"),
    );
  });
});
