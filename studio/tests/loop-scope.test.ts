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
// What the steps inside a loop can read, as the compiler's analyzer walks
// it: the steps before the loop, then the body's own steps in order. The
// loop's result per item sees the whole body; the steps after the loop see
// the loop, never the steps inside it.
import { describe, expect, it } from "vitest";
import { stepOutputSchema, visibleSteps } from "../src/app/forms/core/scope";

const lit = (value: unknown) => ({ literal: value });
const ref = (pointer: string) => ({ ref: pointer });
const transform = (id: string, value: unknown = lit({})) => ({
  id,
  kind: "transform",
  value,
});
const loop = (
  id: string,
  steps: unknown[],
  output: unknown,
  extra: Record<string, unknown> = {},
) => ({
  id,
  kind: "forEach",
  items: ref("/input/orders"),
  concurrency: 1,
  maxItems: 50,
  collect: "all",
  body: { steps, output },
  ...extra,
});
const workflow = (steps: unknown[]) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "w", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: {},
    steps,
    output: lit(null),
  },
});
const ids = (steps: { id: string }[] | null) =>
  steps === null ? null : steps.map((step) => step.id);

const w = workflow([
  transform("fetch"),
  loop(
    "orders",
    [
      transform("check", lit({ ok: true })),
      loop("lines", [transform("reserve")], ref("/steps/reserve/output")),
      transform("send"),
    ],
    ref("/steps/check/output"),
  ),
  transform("summarize"),
]);

describe("the scope inside a loop", () => {
  it("lets a step inside read the steps before the loop and those before it in the body", () => {
    expect(ids(visibleSteps(w, "check", "/value"))).toEqual(["fetch"]);
    expect(ids(visibleSteps(w, "send", "/value"))).toEqual([
      "fetch",
      "check",
      "lines",
    ]);
    // The steps after the loop see the loop, never the steps inside it;
    // its list reads only what runs before it.
    expect(ids(visibleSteps(w, "summarize", "/value"))).toEqual([
      "fetch",
      "orders",
    ]);
    expect(ids(visibleSteps(w, "orders", "/items"))).toEqual(["fetch"]);
  });

  it("lets a nested loop's steps read the outer body's steps before it", () => {
    expect(ids(visibleSteps(w, "reserve", "/value"))).toEqual([
      "fetch",
      "check",
    ]);
  });

  it("lets the result per item read every step of the body", () => {
    expect(ids(visibleSteps(w, "orders", "/body/output"))).toEqual([
      "fetch",
      "check",
      "lines",
      "send",
    ]);
  });

  it("gives a loop a list of its result per item as its output", () => {
    expect(stepOutputSchema(w, "orders")).toEqual({
      type: "array",
      items: {
        type: "object",
        properties: { ok: { type: "boolean" } },
        required: ["ok"],
        additionalProperties: false,
      },
      maxItems: 50,
    });
  });

  it("leaves empty results out of a loop that skips them", () => {
    const skip = (output: unknown) =>
      workflow([
        loop("orders", [], output, { collect: "nonNull", maxItems: 9 }),
      ]);
    expect(stepOutputSchema(skip(lit(null)), "orders")).toEqual({
      type: "array",
      maxItems: 0,
    });
    expect(stepOutputSchema(skip(lit("sent")), "orders")).toEqual({
      type: "array",
      items: { type: "string" },
      maxItems: 9,
    });
    // A result that may be null keeps its other types.
    const maybe = workflow([
      loop(
        "orders",
        [{ id: "look", kind: "action", uses: "lookup@1.0.0", with: lit({}) }],
        ref("/steps/look/output"),
        { collect: "nonNull", maxItems: 9 },
      ),
    ]);
    expect(
      stepOutputSchema(maybe, "orders", {
        actionOutput: () => ({ type: ["string", "null"] }),
      }),
    ).toEqual({ type: "array", items: { type: "string" }, maxItems: 9 });
  });
});
