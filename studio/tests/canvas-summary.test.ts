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
  durationSeconds,
  formatDuration,
  incompleteChip,
  splitDuration,
  stepIssues,
  stepSummary,
} from "../src/app/designer/canvas-summary";
import { createStep } from "../src/app/model";

describe("durations", () => {
  it("shows the largest whole unit", () => {
    expect(formatDuration(60)).toBe("1 min");
    expect(formatDuration(90)).toBe("90 s");
    expect(formatDuration(3600)).toBe("1 h");
    expect(formatDuration(5400)).toBe("90 min");
    expect(formatDuration(86400 * 2)).toBe("2 d");
    expect(splitDuration(7200)).toEqual({ amount: 2, unit: "h" });
    expect(splitDuration(undefined)).toEqual({ amount: null, unit: "min" });
  });
  it("converts an amount and unit to whole seconds", () => {
    expect(durationSeconds("2", "h")).toBe(7200);
    expect(durationSeconds("1.5", "min")).toBe(90);
    expect(durationSeconds("0.5", "s")).toBeNaN();
    expect(durationSeconds("0", "min")).toBeNaN();
    expect(durationSeconds("", "min")).toBeUndefined();
    expect(durationSeconds("abc", "min")).toBeNaN();
  });
});

describe("node summaries", () => {
  it("summarizes each kind in plain words", () => {
    expect(
      stepSummary({
        ...createStep("action", "a"),
        uses: "sql.lookup@1.0.0",
        connection: "crm",
      }),
    ).toBe("sql.lookup@1.0.0 · crm");
    expect(stepSummary(createStep("action", "a"))).toBe("Choose an action");
    expect(
      stepSummary({ ...createStep("wait", "w"), durationSeconds: 3600 }),
    ).toBe("1 h");
    expect(
      stepSummary({
        ...createStep("signal", "s"),
        name: "customer-approved",
        timeoutSeconds: 86400,
      }),
    ).toBe("customer-approved · 1 d");
    expect(
      stepSummary({
        ...createStep("humanTask", "h"),
        assignment: "expense-reviewers",
      }),
    ).toBe("expense-reviewers · approve/reject");
    const defaults = { ...createStep("humanTask", "h"), assignment: "ops" };
    delete defaults["decisions"];
    expect(stepSummary(defaults)).toBe("ops · approve/reject");
    // A decision names each path's condition, then "otherwise".
    expect(stepSummary(createStep("switch", "d"))).toBe(
      "Condition not set · otherwise",
    );
    const routed = createStep("switch", "d");
    (routed["cases"] as { when?: unknown }[])[0].when = {
      op: { name: "gt", args: [{ ref: "/input/amount" }, { literal: 1000 }] },
    };
    expect(
      stepSummary(routed, {
        spec: {
          inputSchema: {
            properties: { amount: { type: "number", title: "Amount" } },
          },
        },
      }),
    ).toBe("Amount > 1000 · otherwise");
    expect(stepSummary(createStep("parallel", "p"))).toBe("2 branches · max 2");
    expect(stepSummary(createStep("fail", "f"))).toBe("business-error");
    expect(
      stepSummary({
        ...createStep("transform", "t"),
        value: { object: { a: { literal: 1 }, b: { literal: 2 } } },
      }),
    ).toBe("Builds 2 fields");
    expect(
      stepSummary({
        ...createStep("transform", "t"),
        value: { ref: "/input/x" },
      }),
    ).toBe("Copies /input/x");
  });
  it("flags placeholders that still need a choice", () => {
    expect(stepIssues(createStep("action", "a"))).toEqual([
      "Choose the action to call.",
    ]);
    expect(
      stepIssues({ ...createStep("action", "a"), uses: "crm.lookup@1.0.0" }),
    ).toEqual([]);
    const decision = createStep("switch", "d");
    // A new case has no condition: never a silent "always".
    expect((decision["cases"] as { when?: unknown }[])[0].when).toBeUndefined();
    expect(stepIssues(decision)).toEqual([
      "Case 1: choose when this path applies.",
    ]);
    expect(incompleteChip(decision)).toBe("Choose a condition");
    expect(incompleteChip(createStep("action", "a"))).toBe("Choose an action");
    (decision["cases"] as { when: unknown }[])[0].when = {
      ref: "/input/approved",
    };
    expect(stepIssues(decision)).toEqual([]);
    expect(incompleteChip(decision)).toBe("");
  });
});
