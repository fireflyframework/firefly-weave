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
import { describe, expect, it } from "vitest";
import {
  diagnosticRows,
  fieldName,
  suggestedChange,
  withPointer,
} from "../src/app/designer/diagnostics-list";

const definition = {
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "probe", version: "1.0.0" },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    steps: [
      {
        id: "route",
        kind: "switch",
        cases: [
          {
            when: { literal: true },
            steps: [
              { id: "a", kind: "transform", value: { literal: 1 } },
              { id: "b", kind: "transform", value: { ref: "/steps/x" } },
            ],
          },
        ],
        default: { steps: [] },
      },
      { id: "stop", kind: "fail", code: "bad", message: "old" },
    ],
    output: { literal: {} },
  },
};

describe("diagnostic rows", () => {
  it("names the step and field a diagnostic belongs to, in plain words", () => {
    const { problems, notes } = diagnosticRows(
      [
        {
          code: "WV-COMP-UNAVAILABLE_REFERENCE",
          severity: "error",
          path: "/spec/steps/0/cases/0/steps/1/value/ref",
          message: "Referenced step does not dominate this expression.",
        },
        {
          code: "WV-COMP-TYPE_MISMATCH",
          severity: "warning",
          path: "/spec/inputSchema",
          message: "x",
        },
        {
          code: "WV-COMP-CATALOG_PENDING",
          severity: "info",
          path: "/spec/steps/1",
          message: "pending",
        },
        { code: "WV-PARSE-YAML", severity: "error", message: "bad yaml" },
      ],
      definition,
    );
    expect(problems.map((row) => row.where)).toEqual([
      "Step b · Value",
      "Workflow settings · Input schema",
      "",
    ]);
    expect(problems[0].location?.stepId).toBe("b");
    expect(problems[0].location?.field).toEqual(["value"]);
    expect(problems[0].view.text).toMatch(/hasn't finished/);
    expect(problems[0].view.code).toBe("WV-COMP-UNAVAILABLE_REFERENCE");
    // A diagnostic without a pointer cannot be opened.
    expect(problems[2].location).toBeNull();
    expect(notes).toHaveLength(1);
  });

  it("labels case, branch and workflow fields", () => {
    expect(fieldName(["cases", 0, "when"])).toBe("Case 1 condition");
    expect(fieldName(["cases", 1, "output"])).toBe("Case 2 output");
    expect(fieldName(["default", "output"])).toBe("Otherwise output");
    expect(fieldName(["branches", "left", "output"])).toBe(
      "Branch left output",
    );
    expect(fieldName(["metadata", "version"])).toBe("Version");
    expect(fieldName(["spec", "output"])).toBe("Workflow output");
    expect(fieldName(["with"])).toBe("Input");
  });
});

describe("suggested edits", () => {
  it("edits one step's data and leaves the definition untouched", () => {
    const change = suggestedChange(
      { path: "/spec/steps/1/message", value: "Customer not found" },
      definition,
    );
    expect(change).toEqual({
      stepId: "stop",
      value: {
        id: "stop",
        kind: "fail",
        code: "bad",
        message: "Customer not found",
      },
    });
    expect(definition.spec.steps[1]).toMatchObject({ message: "old" });
  });

  it("edits nested steps and workflow settings", () => {
    expect(
      suggestedChange(
        {
          path: "/spec/steps/0/cases/0/steps/1/value",
          value: { ref: "/input" },
        },
        definition,
      ),
    ).toEqual({
      stepId: "b",
      value: { id: "b", kind: "transform", value: { ref: "/input" } },
    });
    const workflow = suggestedChange(
      { path: "/metadata/version", value: "1.0.1" },
      definition,
    );
    expect(workflow?.stepId).toBe("$workflow");
    expect((workflow?.value as typeof definition).metadata.version).toBe(
      "1.0.1",
    );
  });

  it("refuses identity changes, stale pointers and malformed edits", () => {
    expect(
      suggestedChange({ path: "/spec/steps/1/id", value: "x" }, definition),
    ).toBeNull();
    expect(
      suggestedChange(
        { path: "/spec/steps/9/message", value: "x" },
        definition,
      ),
    ).toBeNull();
    expect(suggestedChange({ path: 3, value: "x" }, definition)).toBeNull();
    expect(suggestedChange({ path: "/metadata/name" }, definition)).toBeNull();
    expect(suggestedChange(null, definition)).toBeNull();
  });

  it("writes pointers with escapes and refuses missing parents", () => {
    expect(withPointer({ "a/b": { c: 1 } }, "/a~1b/c", 2)).toEqual({
      "a/b": { c: 2 },
    });
    expect(withPointer({ list: [1] }, "/list/-", 2)).toEqual({ list: [1, 2] });
    expect(withPointer({ a: 1 }, "/missing/x", 2)).toBeNull();
    expect(withPointer({ list: [1] }, "/list/5", 2)).toBeNull();
  });
});
