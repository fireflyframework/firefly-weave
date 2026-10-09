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
import { beforeAll, describe, expect, it } from "vitest";
import { allParams, formProblems } from "../src/app/editor/ndv/form-contract";
import {
  expressionRoot,
  pathKey,
  pathStartsWith,
  samePath,
} from "../src/app/editor/ndv/params/paths";
import {
  ndvRegistry,
  type FormSpec,
  type KindContext,
  type ParamSpec,
  type StepKindDescriptor,
  type Json,
} from "../src/app/editor/ndv/registry";
import {
  createStep,
  freshWorkflow,
  kinds,
  type Kind,
  type Step,
} from "../src/app/model";

import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import {
  endForm,
  triggerForm,
  workflowSettingsForm,
} from "../src/app/editor/ndv/kinds/forms/workflow-forms";
import {
  formEnv,
  stepSubject,
  workflowSubject,
  WORKFLOW_STAND_IN,
} from "../src/app/editor/ndv/params/form-env";
import { formState } from "../src/app/editor/ndv/params/form-model";
import {
  newConnectorRecipe,
  newHttpRecipe,
} from "../src/app/editor/ndv/owned/owned-actions";
import { propertyFields } from "../src/app/property-grid";
import { lookupAction } from "./browser/support";

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

  it("reports a mapped list item whose list is not inside an expression", () => {
    const item = (id: string): ParamSpec => text(id, [], { mapping: "both" });
    const form: FormSpec = {
      fields: [
        { ...text("cc", ["cc"]), type: "list", item: item("cc-row") },
        {
          ...text("actions", ["rows"], { scope: "action" }),
          type: "list",
          item: item("action-row"),
        },
        {
          ...text("workflowRows", ["metadata", "rows"], { scope: "workflow" }),
          type: "list",
          item: item("workflow-row"),
        },
        {
          ...text("notes", ["notes"]),
          type: "list",
          item: {
            ...text("note-row", []),
            type: "fields",
            children: () => [text("inner", ["note"], { mapping: "both" })],
          },
        },
      ],
    };
    expect(formProblems(kind("lists", form), step, ctx)).toEqual([
      'lists offers Fixed and Mapped for "cc-row", which isn\'t an expression.',
      'lists offers Fixed and Mapped for "action-row", which isn\'t an expression.',
      'lists offers Fixed and Mapped for "workflow-row", which isn\'t an expression.',
      'lists offers Fixed and Mapped for "inner", which isn\'t an expression.',
    ]);
  });

  it("allows a mapped list item whose list is under the workflow result", () => {
    const form: FormSpec = {
      fields: [
        {
          ...text("lines", ["spec", "output", "lines"], { scope: "workflow" }),
          type: "list",
          item: text("line", [], { mapping: "both" }),
        },
      ],
    };
    expect(formProblems(kind("result", form), step, ctx)).toEqual([]);
  });

  it("allows a mapped list item that lands in one of the step's expression fields", () => {
    const form: FormSpec = {
      fields: [
        {
          ...text("cases", ["cases"]),
          type: "list",
          item: {
            ...text("case", []),
            type: "fields",
            children: () => [text("result", ["output"], { mapping: "both" })],
          },
        },
      ],
    };
    const withCases = (paths: (string | number)[][]) => ({
      ...kind("route", form),
      fields: () => paths.map((path) => ({ path, label: "Path result" })),
    });
    expect(
      formProblems(withCases([["cases", 0, "output"]]), step, ctx),
    ).toEqual([]);
    expect(formProblems(withCases([["value"]]), step, ctx)).toEqual([
      'route offers Fixed and Mapped for "result", which isn\'t an expression.',
    ]);
  });

  it("reports a default to write on removal when the field has none", () => {
    const form: FormSpec = {
      fields: [
        text("kept", ["kept"], { default: "a", whenRemoved: "default" }),
        text("lost", ["lost"], { whenRemoved: "default" }),
        text("deleted", ["deleted"], { whenRemoved: "delete" }),
      ],
    };
    expect(formProblems(kind("keep", form), step, ctx)).toEqual([
      'keep writes the default of "lost" when it is removed, but has none.',
    ]);
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

describe("allParams", () => {
  it("reports relative for list items and absolute for top-level fields", () => {
    const form: FormSpec = {
      fields: [
        text("value", ["value"]),
        {
          ...text("rows", ["rows"]),
          type: "list",
          item: text("row", [], { mapping: "both" }),
        },
      ],
      options: [text("note", ["note"])],
    };
    const entries = allParams(form, step, ctx);
    expect(entries.map(({ spec, relative }) => [spec.id, relative])).toEqual([
      ["value", false],
      ["rows", false],
      ["row", true],
      ["note", false],
    ]);
  });
});

beforeAll(() => loadKindRegistrations());

const recipes: Record<string, Json> = {
  "flow.get@1.0.0": newHttpRecipe() as unknown as Json,
  "flow.post@1.0.0": { ...newHttpRecipe(), method: "POST" } as unknown as Json,
  "flow.send@1.0.0": newConnectorRecipe(
    "weave-email@1.0.0",
    "send",
  ) as unknown as Json,
  "flow.reply@1.0.0": newConnectorRecipe(
    "weave-email@1.0.0",
    "reply",
  ) as unknown as Json,
  "flow.list@1.0.0": newConnectorRecipe(
    "weave-sftp@1.0.0",
    "list",
  ) as unknown as Json,
  "flow.read@1.0.0": newConnectorRecipe(
    "weave-sftp@1.0.0",
    "read",
  ) as unknown as Json,
  "flow.write@1.0.0": newConnectorRecipe(
    "weave-sftp@1.0.0",
    "write",
  ) as unknown as Json,
  "flow.drive-write@1.0.0": newConnectorRecipe(
    "weave-google-drive@1.0.0",
    "write",
  ) as unknown as Json,
  "flow.move@1.0.0": newConnectorRecipe(
    "weave-sftp@1.0.0",
    "move",
  ) as unknown as Json,
  "flow.delete@1.0.0": newConnectorRecipe(
    "weave-sftp@1.0.0",
    "delete",
  ) as unknown as Json,
};
const policy = {
  kind: "DecisionTable",
  spec: {
    inputSchema: {
      type: "object",
      required: ["amount"],
      properties: { amount: { type: "number" } },
    },
  },
};
const realCtx = (workflow = freshWorkflow()): KindContext => ({
  workflow,
  features: [],
  actionContract: (uses) =>
    uses === "sql.lookup@1.0.0" ? (lookupAction as Json) : null,
  tableContract: (uses) =>
    uses === "payment-policy@1.0.0" ? (policy as Json) : null,
  workflowContract: () => null,
  ownedAction: (uses) => recipes[uses] ?? null,
});
const newStep = (kind: Kind, extra: Record<string, unknown> = {}) => ({
  ...createStep(kind, `${kind}-1`),
  ...extra,
});
const variants = (): Step[] => [
  ...kinds.map((kind) => newStep(kind)),
  ...Object.keys(recipes).map((uses) => newStep("action", { uses })),
  newStep("action", { uses: "sql.lookup@1.0.0", with: { literal: {} } }),
];

describe("forms of every kind", () => {
  it("renders every ready kind through form()", () => {
    for (const step of variants()) {
      const descriptor = ndvRegistry.kind(step.kind)!;
      expect(
        formProblems(descriptor, step, realCtx()),
        `${step.kind} ${String(step["uses"] ?? "")}`,
      ).toEqual([]);
    }
  });

  it("shows the default field counts", () => {
    const count = (step: Step) => {
      const c = realCtx();
      const descriptor = ndvRegistry.kind(step.kind)!;
      const recipe = recipes[String(step["uses"] ?? "")] ?? null;
      return formState(
        descriptor.form!(step, c),
        formEnv(stepSubject(descriptor, step, c, recipe), step, c),
      ).fields.length;
    };
    const rows: [string, Step, number][] = [
      ["Call an API", newStep("action", { uses: "flow.get@1.0.0" }), 3],
      [
        "Call an API with a body",
        newStep("action", { uses: "flow.post@1.0.0" }),
        4,
      ],
      ["Decision", newStep("switch"), 1],
      ["Transform", newStep("transform"), 1],
      ["Human task", newStep("humanTask"), 3],
      ["Send email", newStep("action", { uses: "flow.send@1.0.0" }), 4],
      [
        "Published action, one required input",
        newStep("action", { uses: "sql.lookup@1.0.0", with: { literal: {} } }),
        3,
      ],
      ["Reply to an email", newStep("action", { uses: "flow.reply@1.0.0" }), 4],
      ["List files", newStep("action", { uses: "flow.list@1.0.0" }), 2],
      ["Read a file", newStep("action", { uses: "flow.read@1.0.0" }), 2],
      ["Write a file", newStep("action", { uses: "flow.write@1.0.0" }), 3],
      [
        "Write a file on a drive",
        newStep("action", { uses: "flow.drive-write@1.0.0" }),
        4,
      ],
      ["Move a file", newStep("action", { uses: "flow.move@1.0.0" }), 3],
      ["Delete a file", newStep("action", { uses: "flow.delete@1.0.0" }), 2],
      ["AI task", newStep("llm"), 2],
      ["Decision table", newStep("decisionTable"), 1],
      ["Parallel", newStep("parallel"), 1],
      ["Wait", newStep("wait"), 1],
      ["Wait for signal", newStep("signal"), 1],
      ["Stop with error", newStep("fail"), 2],
    ];
    for (const [name, step, expected] of rows)
      expect(count(step), name).toBe(expected);
    const wf = (spec: FormSpec) =>
      formState(
        spec,
        formEnv(workflowSubject(freshWorkflow()), WORKFLOW_STAND_IN, realCtx()),
      ).fields.length;
    expect(wf(endForm()), "End").toBe(1);
    expect(wf(triggerForm()), "Manual form trigger").toBe(1);
    expect(wf(workflowSettingsForm()), "Workflow settings").toBe(2);
  });

  it("keeps every field the classic editor edits", () => {
    const withContract: Partial<Record<Kind, Record<string, unknown>>> = {
      action: { uses: "sql.lookup@1.0.0", with: { literal: {} } },
      decisionTable: { uses: "payment-policy@1.0.0", with: { literal: {} } },
    };
    for (const kind of kinds) {
      const step = newStep(kind, withContract[kind] ?? {});
      const descriptor = ndvRegistry.kind(kind)!;
      const c = realCtx();
      const covered = new Set(
        [
          ...allParams(descriptor.form!(step, c), step, c),
          ...allParams(descriptor.settings?.(step, c), step, c),
        ]
          .filter(
            ({ spec, relative }) =>
              !relative && (spec.scope ?? "step") === "step",
          )
          .map(({ spec }) => String(spec.path[0] ?? "")),
      );
      for (const field of propertyFields(step))
        expect(covered, `${kind} ${field.label}`).toContain(
          String(field.path[0]),
        );
    }
    const workflowPaths = new Set(
      [triggerForm(), endForm(), workflowSettingsForm()].flatMap((form) =>
        [...form.fields, ...(form.options ?? [])].map((spec) =>
          spec.path.join("/"),
        ),
      ),
    );
    for (const field of propertyFields(freshWorkflow()))
      expect(workflowPaths, field.label).toContain(field.path.join("/"));
  });
});
