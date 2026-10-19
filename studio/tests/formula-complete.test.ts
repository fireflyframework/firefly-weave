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
import {
  completeAt,
  type CompletionScope,
  type HostEntry,
} from "../src/app/editor/formula/complete";
import { referenceScope } from "../src/app/forms/core/scope";

const workflow = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "completion", version: "1.0.0" },
  spec: {
    inputSchema: {
      type: "object",
      properties: {
        amount: { type: "number" },
        email: { type: "string" },
        status: { type: "string", enum: ["open", "closed"] },
        "first name": { type: "string" },
      },
    },
    outputSchema: {},
    steps: [
      {
        id: "check",
        kind: "transform",
        value: { literal: { score: 0.5, tier: "gold" } },
      },
      { id: "lookup.v2", kind: "transform", value: { literal: "x" } },
      { id: "use", kind: "transform", value: { literal: null } },
    ],
    output: { literal: null },
  },
};
const reference = referenceScope(workflow, "use", "/value");
const scope: CompletionScope = {
  entries: reference.entries,
  steps: reference.steps,
  loops: [],
  loopEntries: [],
  features: ["text.concat", "text.join"],
  inputSchema: workflow.spec.inputSchema,
};
const labels = (
  text: string,
  caret = text.length,
  mode: "formula" | "template" = "formula",
  at = scope,
) =>
  completeAt(text, caret, mode, at)?.options.map((option) => option.label) ??
  null;

describe("autocomplete", () => {
  it("offers the input's fields with their types after input.", () => {
    const list = completeAt("input.", 6, "formula", scope)!;
    expect(list.from).toBe(5);
    expect(list.options.map((o) => [o.label, o.detail, o.apply])).toEqual([
      ["amount", "Number", ".amount"],
      ["email", "Text", ".email"],
      ["status", "Choice", ".status"],
      ["first name", "Text", '["first name"]'],
    ]);
    expect(labels("input.em")).toEqual(["email"]);
  });
  it("offers visible steps, nearest first, then output, then its fields", () => {
    const steps = completeAt("steps.", 6, "formula", scope)!;
    expect(steps.options.map((o) => [o.label, o.apply])).toEqual([
      ["lookup.v2", '["lookup.v2"]'],
      ["check", ".check"],
    ]);
    expect(labels("steps.check.")).toEqual(["output"]);
    expect(
      completeAt("steps.check.output.", 19, "formula", scope)!.options.map(
        (o) => [o.label, o.detail],
      ),
    ).toEqual([
      ["score", "Number"],
      ["tier", "Text"],
    ]);
  });
  it("offers roots, functions and keywords where a value starts", () => {
    expect(labels("")).toEqual([
      "input",
      "steps",
      "exists",
      "coalesce",
      "contains",
      "notContains",
      "in",
      "notIn",
      "startsWith",
      "endsWith",
      "concat",
      "join",
      "not",
      "true",
      "false",
      "null",
    ]);
    const exists = completeAt("ex", 2, "formula", scope)!.options[0];
    expect(exists).toMatchObject({
      label: "exists",
      apply: "exists()",
      caret: 7,
    });
    expect(labels("coalesce(input.amount, ")).toContain("input");
  });
  it("offers and and or after a value", () => {
    expect(labels("input.amount ")).toEqual(["and", "or"]);
    expect(labels("input.amount a")).toEqual(["and"]);
  });
  it("offers the values of an enum field after == and !=", () => {
    expect(labels("input.status == ")?.slice(0, 2)).toEqual([
      '"open"',
      '"closed"',
    ]);
    expect(labels('input.status != "c')).toBeNull();
  });
  it("works only between a template's braces", () => {
    expect(labels("Hi {{ in", 8, "template")).toEqual([
      "input",
      "in",
      "contains",
      "notContains",
      "notIn",
      "join",
    ]);
    expect(labels("Hi in", 5, "template")).toBeNull();
    expect(labels("Hi {{ input.email }} an", 23, "template")).toBeNull();
  });
  it("offers nothing inside text", () => {
    expect(labels('"inp')).toBeNull();
    expect(labels('contains("inp', 13)).toBeNull();
  });
  it("offers the loop roots inside a loop body", () => {
    const { cases } = JSON.parse(
      readFileSync(
        resolve(import.meta.dirname, "fixtures/language/scope.json"),
        "utf8",
      ),
    ) as {
      cases: {
        name: string;
        result: { entries: (HostEntry & { source: string })[] };
      }[];
    };
    const body = cases.find((item) => item.name.startsWith("a loop body sees"))!
      .result.entries;
    const inLoop: CompletionScope = {
      ...scope,
      loops: ["notify"],
      loopEntries: body.filter(
        (entry) => entry.source === "item" || entry.source === "index",
      ),
    };
    expect(labels("", 0, "formula", inLoop)?.slice(0, 5)).toEqual([
      "input",
      "steps",
      "item",
      "index",
      "loops",
    ]);
    expect(labels("item.", 5, "formula", inLoop)).toEqual([
      "number",
      "customerEmail",
    ]);
    expect(labels("loops.", 6, "formula", inLoop)).toEqual(["notify"]);
    expect(labels("loops.notify.", 13, "formula", inLoop)).toEqual([
      "item",
      "index",
    ]);
  });
});
