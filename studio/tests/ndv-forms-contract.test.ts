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
import { formProblems } from "../src/app/editor/ndv/form-contract";
import {
  expressionRoot,
  pathKey,
  pathStartsWith,
  samePath,
} from "../src/app/editor/ndv/params/paths";
import type {
  FormSpec,
  KindContext,
  ParamSpec,
  StepKindDescriptor,
} from "../src/app/editor/ndv/registry";
import { createStep, freshWorkflow, type Step } from "../src/app/model";

const ctx: KindContext = {
  workflow: freshWorkflow(),
  features: [],
  actionContract: () => null,
  tableContract: () => null,
  workflowContract: () => null,
};
const kind = (
  name: string,
  form?: FormSpec,
  settings?: FormSpec,
): StepKindDescriptor => ({
  kind: name,
  label: "Probe",
  description: "A kind for this test",
  keywords: "",
  icon: "action",
  role: "app",
  category: "app",
  idPrefix: "probe",
  create: (id) => createStep("transform", id),
  fields: () => [{ path: ["value"], label: "Value" }],
  summary: () => "",
  outputSchema: () => null,
  pinnable: false,
  ...(form ? { form: () => form } : {}),
  ...(settings ? { settings: () => settings } : {}),
});
const text = (
  id: string,
  path: (string | number)[],
  extra: Partial<ParamSpec> = {},
): ParamSpec => ({
  id,
  path,
  type: "text",
  label: id,
  ...extra,
});
const step: Step = createStep("transform", "probe-1");

describe("paths", () => {
  it("compares paths by their segments", () => {
    expect(samePath(["cases", 0, "when"], ["cases", "0", "when"])).toBe(true);
    expect(pathStartsWith(["with", "query", "limit"], ["with"])).toBe(true);
    expect(pathStartsWith(["without"], ["with"])).toBe(false);
    expect(pathKey(["cases", 1, "output"])).toBe("cases/1/output");
  });
  it("finds the longest expression field a path is in", () => {
    const roots = [["cases", 0, "when"], ["cases", 0, "output"], ["with"]];
    expect(expressionRoot(["with", "body", "note"], roots)).toEqual(["with"]);
    expect(expressionRoot(["cases", 0, "output", "a"], roots)).toEqual([
      "cases",
      0,
      "output",
    ]);
    expect(expressionRoot(["uses"], roots)).toBeNull();
  });
});

describe("form contract", () => {
  it("reports a kind without a Parameters form", () => {
    expect(formProblems(kind("bare"), step, ctx)).toEqual([
      "bare has no Parameters form.",
    ]);
  });

  it("reports a field ID used twice, also in options, children and settings", () => {
    const form: FormSpec = {
      fields: [
        text("value", ["value"]),
        {
          ...text("group", ["value", "group"]),
          type: "fields",
          children: () => [text("note", ["value", "group", "note"])],
        },
      ],
      options: [text("note", ["value", "note"])],
    };
    const settings: FormSpec = { fields: [text("value", ["timeoutSeconds"])] };
    expect(formProblems(kind("twice", form, settings), step, ctx)).toEqual([
      'twice uses the field ID "note" twice.',
      'twice uses the field ID "value" twice.',
    ]);
  });

  it("reports Fixed and Mapped on a field that isn't an expression", () => {
    const form: FormSpec = {
      fields: [
        text("name", ["name"], { mapping: "both" }),
        text("method", ["method"], { scope: "action", mapping: "both" }),
        text("version", ["metadata", "version"], {
          scope: "workflow",
          mapping: "both",
        }),
      ],
    };
    expect(formProblems(kind("plain", form), step, ctx)).toEqual([
      'plain offers Fixed and Mapped for "name", which isn\'t an expression.',
      'plain offers Fixed and Mapped for "method", which isn\'t an expression.',
      'plain offers Fixed and Mapped for "version", which isn\'t an expression.',
    ]);
  });

  it("allows Fixed and Mapped inside expressions, their data paths and the workflow result", () => {
    const form: FormSpec = {
      fields: [
        text("value", ["value"], { mapping: "both" }),
        text("note", ["value", "note"], { mapping: "both" }),
        text("result", ["spec", "output", "total"], {
          scope: "workflow",
          mapping: "both",
        }),
        {
          ...text("rows", ["value", "rows"]),
          type: "list",
          item: text("row", [], { mapping: "both" }),
        },
      ],
    };
    expect(formProblems(kind("mapped", form), step, ctx)).toEqual([]);
  });

  it("allows custom components only for the decision table grid and the AI agent slots", () => {
    const grid: FormSpec = {
      fields: [
        { ...text("grid", ["rules"]), type: "custom", component: "grid" },
      ],
    };
    expect(formProblems(kind("decisionTable", grid), step, ctx)).toEqual([]);
    expect(formProblems(kind("transform", grid), step, ctx)).toEqual([
      'transform uses a custom component for "grid"; only the decision table grid and the AI agent slots may.',
    ]);
  });
});
