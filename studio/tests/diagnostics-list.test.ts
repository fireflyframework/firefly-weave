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

  it("names a decision table's version Table version, and an action's Action version", () => {
    const steps = {
      ...definition,
      spec: {
        ...definition.spec,
        steps: [
          {
            id: "score",
            kind: "decisionTable",
            uses: "payment-policy@1.2.1",
            with: { ref: "/input" },
          },
          {
            id: "lookup",
            kind: "action",
            uses: "sql.lookup@1.0.1",
            with: { literal: {} },
          },
        ],
      },
    };
    const { problems } = diagnosticRows(
      ["/spec/steps/0/uses", "/spec/steps/1/uses"].map((path) => ({
        code: "WV-COMP-DRAFT_DEPENDENCY",
        severity: "error",
        path,
        message: "Publish it before publishing this workflow.",
      })),
      steps,
    );
    expect(problems.map((row) => row.where)).toEqual([
      "Step score · Table version",
      "Step lookup · Action version",
    ]);
  });
});

describe("a problem of the Platform event trigger", () => {
  const reacting = (
    eventTrigger: unknown,
    inputSchema: unknown = { $ref: "weave.platform-event-v1" },
  ) => ({
    ...definition,
    spec: {
      ...definition.spec,
      inputSchema,
      ...(eventTrigger === undefined ? {} : { eventTrigger }),
    },
  });
  const ATTENTION = { events: ["transaction.in_doubt"] };
  const NO_EVENT = { events: [] };
  const FORM = { type: "object", required: ["reference"] };
  /** What the compiler answers, at `path`. */
  const invalid = (path: string) => ({
    code: "WV-SCHEMA-INVALID_INSTANCE",
    severity: "error",
    path,
    message: "Value does not satisfy the declared contract.",
  });
  const mismatch = {
    code: "WV-COMP-TYPE_MISMATCH",
    severity: "error",
    path: "/spec/inputSchema",
    message: "Definition violates the type mismatch contract.",
  };
  const row = (diagnostic: object, document: unknown, stepDetails = true) =>
    diagnosticRows(
      [diagnostic as Parameters<typeof diagnosticRows>[0][number]],
      document,
      stepDetails,
    ).problems[0]!;

  it.each<[string, object, unknown, string, string | undefined, string]>([
    [
      "no event",
      invalid("/spec/eventTrigger/events"),
      reacting(NO_EVENT),
      "Choose at least one event.",
      undefined,
      "Which events",
    ],
    [
      "no list of events",
      invalid("/spec/eventTrigger"),
      reacting({}),
      "Choose at least one event.",
      undefined,
      "Which events",
    ],
    [
      "no workflow named",
      invalid("/spec/eventTrigger/from"),
      reacting({ ...ATTENTION, from: [] }),
      "Add at least one workflow, or choose Any workflow in this project.",
      undefined,
      "From which workflows",
    ],
    [
      "a name that isn't a workflow's",
      invalid("/spec/eventTrigger/from/1"),
      reacting({ ...ATTENTION, from: ["sync-records", "not one"] }),
      "“not one” isn't a workflow name. Use letters, numbers, dots, underscores or hyphens, starting with a letter or number.",
      undefined,
      "From which workflows",
    ],
    [
      "an input that refuses the event",
      mismatch,
      reacting(ATTENTION, FORM),
      "This trigger can't start runs: the workflow's input doesn't accept the platform event.",
      "Reset the input in the trigger's Parameters.",
      "Input",
    ],
    [
      "an input Studio can't compare",
      {
        ...mismatch,
        code: "WV-COMP-UNKNOWN_COMPATIBILITY",
        severity: "warning",
      },
      reacting(ATTENTION, FORM),
      "Studio can't confirm that the platform event fits this workflow's input.",
      "Each start checks it.",
      "Input",
    ],
  ])(
    "reads as the trigger says it, and opens the trigger: %s",
    (_what, diagnostic, document, text, hint, part) => {
      const { view, where, location } = row(diagnostic, document);
      expect(view.text).toBe(text);
      expect(view.hint).toBe(hint);
      expect(where).toBe(`Platform event trigger · ${part}`);
      expect(location?.event).toBe(
        { "Which events": "events", "From which workflows": "from" }[part] ??
          "input",
      );
      // What support asks for stays in Details.
      expect(view).toMatchObject({
        severity: (diagnostic as { severity: string }).severity,
        code: (diagnostic as { code: string }).code,
        technical: (diagnostic as { message: string }).message,
      });
    },
  );

  it.each<[string, object, unknown, boolean, string, string]>([
    [
      "a workflow without the trigger",
      mismatch,
      reacting(undefined, FORM),
      true,
      "This value's type doesn't match what the field expects.",
      "Workflow settings · Input schema",
    ],
    [
      "a problem inside the input schema",
      { ...mismatch, path: "/spec/inputSchema/properties/reference" },
      reacting(ATTENTION, FORM),
      true,
      "This value's type doesn't match what the field expects.",
      "Workflow settings · Input schema",
    ],
    [
      "another problem at the input",
      { ...mismatch, code: "WV-SCHEMA-INVALID_SCHEMA" },
      reacting(ATTENTION, FORM),
      true,
      "This schema isn't valid.",
      "Workflow settings · Input schema",
    ],
    [
      "a field the trigger doesn't have",
      invalid("/spec/eventTrigger/every"),
      reacting({ ...ATTENTION, every: "day" }),
      true,
      "This value doesn't have the expected format.",
      "Workflow settings",
    ],
    [
      "an editor without step details",
      mismatch,
      reacting(ATTENTION, FORM),
      false,
      "This value's type doesn't match what the field expects.",
      "Workflow settings · Input schema",
    ],
  ])(
    "leaves %s as it was",
    (_what, diagnostic, document, stepDetails, text, where) => {
      const found = row(diagnostic, document, stepDetails);
      expect(found.view.text).toBe(text);
      expect(found.where).toBe(where);
      expect(found.location?.event).toBeUndefined();
    },
  );

  it("opens the trigger for a problem the trigger has no sentence for", () => {
    const twice = row(
      invalid("/spec/eventTrigger/events"),
      reacting({ events: ["transaction.in_doubt", "transaction.in_doubt"] }),
    );
    expect(twice.view.text).toBe(
      "This value doesn't have the expected format.",
    );
    expect(twice.where).toBe("Platform event trigger · Which events");
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
