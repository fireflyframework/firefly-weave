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
// Simulate a failure's rules: which steps offer it and why it is disabled,
// the key a failure is written under (one item or every item), and the
// codes the platform accepts.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  PLATFORM_FAILURES,
  platformFailures,
} from "../src/app/editor/state/failure-codes";
import {
  codeProblem,
  codeSuggestions,
  failureKey,
  simulateBlock,
} from "../src/app/editor/ndv/panes/simulate-failure";
import type { Step } from "../src/app/model";

const step = (kind: string, extra: Record<string, unknown> = {}): Step =>
  ({ id: "submit", kind, ...extra }) as Step;
const STOPS =
  "This step's failure would stop the run. Choose Continue or Continue with error output first.";
const IN_CALL =
  "Studio can't simulate a failure inside a called workflow yet. Simulate it on the call step.";

describe("which steps offer Simulate a failure", () => {
  it("offers it to actions, AI tasks and calls that wait, whatever On error says", () => {
    for (const kind of ["action", "llm", "agent"]) {
      expect(simulateBlock(step(kind), false), kind).toBeNull();
      expect(
        simulateBlock(step(kind, { onError: "continue" }), false),
      ).toBeNull();
    }
    expect(
      simulateBlock(step("callWorkflow", { mode: "wait" }), false),
    ).toBeNull();
  });

  it("disables it for a transform or a decision table that stops the run", () => {
    for (const kind of ["transform", "decisionTable"]) {
      expect(simulateBlock(step(kind), false), kind).toBe(STOPS);
      expect(
        simulateBlock(step(kind, { onError: "continue" }), false),
      ).toBeNull();
      expect(
        simulateBlock(step(kind, { onError: "errorOutput" }), false),
      ).toBeNull();
    }
  });

  it("points a wait for a signal and a human task to their own answers", () => {
    expect(simulateBlock(step("signal"), false)).toBe(
      "Choose Time out as this step's answer in its test data; On error decides what happens next.",
    );
    expect(simulateBlock(step("humanTask"), false)).toBe(
      "Choose Expire as this step's answer in its test data; On error decides what happens next.",
    );
  });

  it("disables it inside a called workflow, and leaves it out where a step can't fail on its own", () => {
    expect(simulateBlock(step("action"), true)).toBe(IN_CALL);
    expect(simulateBlock(step("transform"), true)).toBe(IN_CALL);
    for (const kind of ["switch", "parallel", "forEach", "wait", "stop"])
      expect(simulateBlock(step(kind), false), kind).toBeUndefined();
    expect(
      simulateBlock(step("callWorkflow", { mode: "detach" }), false),
    ).toBeUndefined();
  });
});

describe("where a simulated failure is written", () => {
  it("writes one item under its own key, and every item under the step's", () => {
    expect(failureKey("submit-item[2]", false)).toBe("submit-item[2]");
    expect(failureKey("submit-item[2]", true)).toBe("submit-item");
    expect(failureKey("submit", false)).toBe("submit");
  });
});

describe("the code of a simulated failure", () => {
  it("takes what the platform accepts, and says what to change otherwise", () => {
    for (const code of ["REQUEST_REFUSED", "WV-TASK-DEADLINE", "A.B-1_C", "X"])
      expect(codeProblem(code), code).toBeNull();
    const problem =
      "Use capital letters, digits, dots, dashes and underscores, starting with a letter.";
    for (const code of [
      "",
      "request_refused",
      "1REFUSED",
      "REQUEST REFUSED",
      "_A",
    ])
      expect(codeProblem(code), code).toBe(problem);
    expect(codeProblem("A".repeat(128))).toBeNull();
    expect(codeProblem("A".repeat(129))).toBe("Use at most 128 characters.");
  });
});

describe("the platform's failure codes a step may return", () => {
  const fixture = JSON.parse(
    readFileSync(
      resolve(import.meta.dirname, "fixtures/language/step-errors.json"),
      "utf8",
    ),
  ) as { codes: { code: string; message: string; kinds: string[] }[] };

  it("are the platform's, with its messages and the kinds that return them", () => {
    expect(PLATFORM_FAILURES).toEqual(fixture.codes);
  });

  it("are suggested for a step by its kind", () => {
    const codes = (kind: string) =>
      platformFailures(kind).map((item) => item.code);
    expect(codes("transform")).toEqual([
      "WV-EXPR-MISSING",
      "WV-EXPR-TYPE",
      "WV-EXPR-RESOURCE_LIMIT",
    ]);
    expect(codes("action")).toEqual(
      expect.arrayContaining(["WV-TASK-DEADLINE", "TIMEOUT", "UNAVAILABLE"]),
    );
    expect(codes("action")).not.toContain("WV-TASK-RECONCILED-FAILURE");
    expect(codes("switch")).toEqual([]);
  });
});

describe("the codes Simulate a failure suggests", () => {
  const contract = (codes: string[]) => ({
    spec: { definiteFailures: codes },
  });

  it("lists the action's own failures first, then the platform's that fit the kind", () => {
    const groups = codeSuggestions(
      step("action"),
      contract(["REQUEST_REFUSED", "TIMEOUT"]),
    );
    expect(groups.map((group) => group.label)).toEqual([
      "This action's failures",
      "Platform failures",
    ]);
    expect(groups[0].items).toEqual([
      {
        code: "REQUEST_REFUSED",
        message: "The step failed with code REQUEST_REFUSED.",
      },
      { code: "TIMEOUT", message: "The step timed out." },
    ]);
    // A code the action lists already is not offered twice.
    const platform = groups[1].items.map((item) => item.code);
    expect(platform).toContain("UNAVAILABLE");
    expect(platform).not.toContain("TIMEOUT");
  });

  it("offers only the platform's codes when the step has no contract or lists none", () => {
    const groups = codeSuggestions(step("transform"), null);
    expect(groups.map((group) => group.label)).toEqual(["Platform failures"]);
    expect(groups[0].items[0]).toEqual({
      code: "WV-EXPR-MISSING",
      message: "A value the step reads is missing.",
    });
    expect(codeSuggestions(step("action"), contract([]))[0].label).toBe(
      "Platform failures",
    );
  });
});
