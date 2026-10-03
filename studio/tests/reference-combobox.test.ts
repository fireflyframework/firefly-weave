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
import "@angular/compiler";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parse } from "yaml";
import {
  moveActive,
  referenceHint,
  referenceView,
  type ReferenceOption,
} from "../src/app/forms/ui/reference-combobox";
import { visibleRefs } from "../src/app/forms/core/scope";

const onboarding = parse(
  readFileSync(
    resolve(
      import.meta.dirname,
      "../../examples/definitions/customer-onboarding.workflow.yaml",
    ),
    "utf8",
  ),
);

const options: ReferenceOption[] = [
  {
    ref: "/input",
    source: "input",
    label: "Workflow input",
    breadcrumb: "Workflow input",
    typeLabel: "Object",
    optional: false,
    schema: { type: "object" },
  },
  {
    ref: "/input/customerId",
    source: "input",
    label: "Customer ID",
    breadcrumb: "Workflow input › Customer ID",
    typeLabel: "Text",
    optional: false,
    schema: { type: "string" },
  },
  {
    ref: "/steps/check/output",
    source: "step",
    stepId: "check",
    stepKind: "action",
    label: "check",
    breadcrumb: "check",
    typeLabel: "Object",
    optional: false,
    schema: { type: "object" },
  },
  {
    ref: "/steps/check/output/eligible",
    source: "step",
    stepId: "check",
    stepKind: "action",
    label: "eligible",
    breadcrumb: "check › eligible",
    typeLabel: "Yes or no",
    optional: true,
    schema: { type: "boolean" },
  },
  {
    ref: "/steps/approval/output/decision",
    source: "step",
    stepId: "approval",
    stepKind: "humanTask",
    label: "decision",
    breadcrumb: "approval › decision",
    typeLabel: "Choice",
    optional: false,
    schema: { enum: ["approve", "reject"] },
  },
];

describe("reference suggestions", () => {
  it("groups by workflow input and then each step, in scope order", () => {
    const view = referenceView(options, "");
    expect(view.groups.map((g) => [g.label, g.detail])).toEqual([
      ["Workflow input", undefined],
      ["check", "Call an action"],
      ["approval", "Human task"],
    ]);
    expect(view.flat.map((o) => o.option.ref)).toEqual(
      [options[1], options[0], options[3], options[2], options[4]].map(
        (o) => o.ref,
      ),
    );
    // Indexes are positions in the flat list, used for option ids.
    expect(view.groups[1].options.map((o) => o.index)).toEqual([2, 3]);
  });

  it("matches every query word against the path, label, breadcrumb and type", () => {
    const refs = (query: string) =>
      referenceView(options, query).flat.map((o) => o.option.ref);
    expect(refs("/steps/ch")).toEqual([
      "/steps/check/output/eligible",
      "/steps/check/output",
    ]);
    expect(refs("customer")).toEqual(["/input/customerId"]);
    expect(refs("check yes")).toEqual(["/steps/check/output/eligible"]);
    expect(refs("CHOICE")).toEqual(["/steps/approval/output/decision"]);
    expect(refs("nothing-like-this")).toEqual([]);
    expect(referenceView(options, "nope").groups).toEqual([]);
  });

  it("puts values that fit the target first and marks the ones that may not", () => {
    const view = referenceView(options, "", { type: "boolean" });
    const fits = view.flat.map((o) => [o.option.ref, o.fit]);
    expect(fits).toEqual([
      ["/steps/check/output/eligible", "compatible"],
      ["/steps/check/output", "incompatible"],
      ["/input/customerId", "incompatible"],
      ["/input", "incompatible"],
      ["/steps/approval/output/decision", "incompatible"],
    ]);
    // A group with a compatible leaf comes first; its container remains reachable.
    expect(view.groups[0].options.map((o) => o.option.ref)).toEqual([
      "/steps/check/output/eligible",
      "/steps/check/output",
    ]);
    expect(view.flat.map((o) => o.index)).toEqual([0, 1, 2, 3, 4]);
  });

  it("works on the compiler-accurate scope of the onboarding example", () => {
    const entries = visibleRefs(onboarding, "decision", "/value");
    const view = referenceView(entries, "");
    expect(view.groups.map((g) => [g.label, g.detail])).toEqual([
      ["Workflow input", undefined],
      ["check", "Call an action"],
      ["approval", "Wait for signal"],
    ]);
    expect(view.flat.length).toBe(entries.length);
  });
});

describe("keyboard movement", () => {
  it("wraps around and starts from either end when nothing is active", () => {
    expect(moveActive(-1, 1, 3)).toBe(0);
    expect(moveActive(-1, -1, 3)).toBe(2);
    expect(moveActive(2, 1, 3)).toBe(0);
    expect(moveActive(0, -1, 3)).toBe(2);
    expect(moveActive(1, 1, 3)).toBe(2);
    expect(moveActive(0, 1, 0)).toBe(-1);
  });
});

describe("hints for typed references", () => {
  it("accepts any well-formed pointer and explains the rest in plain words", () => {
    expect(referenceHint("", options)).toBeNull();
    expect(referenceHint("/input/customerId", options)).toEqual({
      tone: "info",
      message: "Text",
    });
    expect(referenceHint("/steps/check/output/eligible", options)).toEqual({
      tone: "info",
      message: "Yes or no · may be absent",
    });
    expect(referenceHint("input/customerId", options)?.tone).toBe("error");
    expect(referenceHint("input/customerId", options)?.message).toMatch(
      /^Start with a slash/,
    );
    expect(referenceHint("/a~2", options)?.tone).toBe("error");
    expect(referenceHint("/customer", options)).toEqual({
      tone: "warning",
      message: "Data references start with /input or /steps/<step ID>/output.",
    });
    expect(referenceHint("/steps/check", options)?.message).toBe(
      "Step data is under /steps/check/output.",
    );
    expect(referenceHint("/steps/later/output", options)).toEqual({
      tone: "warning",
      message:
        "No step named “later” is available here. Validate to check this reference.",
    });
    expect(referenceHint("/input/other", options)).toEqual({
      tone: "info",
      message: "Not in the suggestions. Validate to check this reference.",
    });
    for (const text of ["input/x", "/x", "/steps/later/output"])
      expect(referenceHint(text, options)?.message).not.toMatch(/WV-/);
  });
});

it("ranks compatible leaf groups before incompatible containers", () => {
  const view = referenceView(options, "", { type: "boolean" });
  expect(view.flat[0].option.ref).toBe("/steps/check/output/eligible");
  expect(referenceView(options, "").groups[0].options[0].option.ref).toBe(
    "/input/customerId",
  );
});
