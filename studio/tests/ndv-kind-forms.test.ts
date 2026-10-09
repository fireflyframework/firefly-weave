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
import { formProblems } from "../src/app/editor/ndv/form-contract";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import {
  decisionTableForm,
  transformForm,
} from "../src/app/editor/ndv/kinds/forms/data-forms";
import {
  failForm,
  parallelForm,
  signalForm,
  signalSettings,
  waitForm,
} from "../src/app/editor/ndv/kinds/forms/flow-forms";
import {
  humanForm,
  humanSettings,
} from "../src/app/editor/ndv/kinds/forms/human-form";
import {
  ON_ERROR_KINDS,
  STOP_ONLY_KINDS,
  durationParam,
  wholeMapping,
} from "../src/app/editor/ndv/kinds/forms/shared";
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
import { fieldRules } from "../src/app/editor/ndv/params/field-rules";
import { formState } from "../src/app/editor/ndv/params/form-model";
import {
  fixed,
  isDefault,
  readEntries,
  readParam,
  resetChanges,
  writeParam,
} from "../src/app/editor/ndv/params/value-io";
import {
  ndvRegistry,
  type FormSpec,
  type Json,
  type KindContext,
  type NdvContext,
} from "../src/app/editor/ndv/registry";
import {
  createStep,
  freshWorkflow,
  kinds,
  type Kind,
  type Step,
  type Workflow,
} from "../src/app/model";

beforeAll(() => loadKindRegistrations());

const table = {
  kind: "DecisionTable",
  metadata: { name: "payment-policy", version: "1.0.0" },
  spec: {
    inputSchema: {
      type: "object",
      required: ["amount"],
      properties: {
        amount: { type: "number", title: "Amount" },
        tier: { type: "string", title: "Tier" },
      },
    },
  },
};
const context = (workflow: Workflow, features: string[] = []): KindContext => ({
  workflow,
  features,
  actionContract: () => null,
  tableContract: (uses) =>
    uses === "payment-policy@1.0.0" ? (table as Json) : null,
  workflowContract: () => null,
});
function shown(
  step: Step,
  form: "form" | "settings" = "form",
  features: string[] = [],
) {
  const workflow = freshWorkflow();
  workflow.spec.steps.push(step);
  workflow.spec["llmProfiles"] = {
    default: { provider: "ollama", model: "qwen3:4b" },
  };
  const ctx = context(workflow, features);
  const descriptor = ndvRegistry.kind(step.kind)!;
  const spec = descriptor[form]?.(step, ctx) ?? { fields: [] };
  const state = formState(
    spec,
    formEnv(stepSubject(descriptor, step, ctx, null), step, ctx),
  );
  return {
    spec,
    state,
    ids: state.fields.map((f) => f.spec.id),
    labels: state.fields.map((f) => f.spec.label),
  };
}
const fresh = (kind: Kind, extra: Record<string, unknown> = {}): Step => ({
  ...createStep(kind, `${kind}-1`),
  ...extra,
});
const workflowShown = (
  spec: FormSpec,
  features: string[] = [],
  edit?: (workflow: Workflow) => void,
) => {
  const workflow = freshWorkflow();
  edit?.(workflow);
  const ctx = context(workflow, features);
  return formState(
    spec,
    formEnv(workflowSubject(workflow), WORKFLOW_STAND_IN, ctx),
  );
};
const labelsOf = (state: { fields: { spec: { label: string } }[] }) =>
  state.fields.map((f) => f.spec.label);
const addableOf = (state: { addable: { spec: { label: string } }[] }) =>
  state.addable.map((o) => o.spec.label);

describe("default forms", () => {
  it("shows the default fields of each new step", () => {
    expect(shown(fresh("switch")).labels).toEqual(["Paths"]);
    expect(shown(fresh("transform")).labels).toEqual(["Fields"]);
    expect(shown(fresh("parallel")).labels).toEqual(["Branches"]);
    expect(shown(fresh("wait")).labels).toEqual(["Wait for"]);
    expect(shown(fresh("signal")).labels).toEqual(["Signal name"]);
    expect(shown(fresh("fail")).labels).toEqual(["Error code", "Message"]);
    expect(shown(fresh("humanTask")).labels).toEqual([
      "Assign to",
      "Title",
      "Answers",
    ]);
    expect(shown(fresh("llm")).labels).toEqual(["Model", "Prompt"]);
    expect(shown(fresh("decisionTable")).labels).toEqual(["Table"]);
    expect(
      workflowShown(triggerForm()).fields.map((f) => f.spec.label),
    ).toEqual(["Input fields"]);
    expect(workflowShown(endForm()).fields.map((f) => f.spec.label)).toEqual([
      "Result fields",
    ]);
    expect(
      workflowShown(workflowSettingsForm()).fields.map((f) => f.spec.label),
    ).toEqual(["Name", "Version"]);
  });
  it("adds one row per required table input and offers the optional ones", () => {
    const step = fresh("decisionTable", {
      uses: "payment-policy@1.0.0",
      with: { literal: {} },
    });
    const { state } = shown(step);
    expect(state.fields.map((f) => f.spec.label)).toEqual(["Table", "Amount"]);
    expect(state.addable.map((o) => o.spec.label)).toEqual(["Tier"]);
    const whole = shown(
      fresh("decisionTable", { uses: "payment-policy@1.0.0" }),
    );
    expect(whole.labels).toEqual(["Table", "Table input"]);
  });
  it("keeps a decision's paths, Otherwise and their results inside one Paths field", () => {
    const { spec } = shown(fresh("switch"));
    const step = fresh("switch");
    const children = spec.fields[0].children!(step, context(freshWorkflow()));
    expect(children.map((c) => [c.id, c.type])).toEqual([
      ["cases", "list"],
      ["otherwise", "fields"],
    ]);
    expect(
      children[0].item!.children!(step, context(freshWorkflow())).map((c) => [
        c.id,
        c.path,
      ]),
    ).toEqual([
      ["case.when", ["when"]],
      ["case.output", ["output"]],
    ]);
  });
  it("lists the AI profiles as models", () => {
    const { spec } = shown(fresh("llm"));
    expect(spec.fields[0].choices).toEqual([
      { value: "default", label: "default · qwen3:4b" },
    ]);
  });
  it("offers Callable by other workflows only on platforms that support calls", () => {
    expect(
      workflowShown(workflowSettingsForm()).addable.find(
        (o) => o.spec.id === "callable",
      )?.disabled,
    ).toBe("Update the platform to use this (flow.callWorkflow).");
    expect(
      workflowShown(workflowSettingsForm(), ["flow.callWorkflow"]).addable.find(
        (o) => o.spec.id === "callable",
      )?.disabled,
    ).toBeNull();
  });
});

describe("settings forms", () => {
  it("keeps a signal's timeout and a human task's deadlines on Settings", () => {
    expect(shown(fresh("signal"), "settings").labels).toEqual(["Wait at most"]);
    expect(shown(fresh("humanTask"), "settings").labels).toEqual([
      "Due after",
      "Expires after",
    ]);
    expect(
      shown(fresh("humanTask")).spec.fields.map((f) => f.id),
    ).not.toContain("due");
    expect(shown(fresh("wait"), "settings").labels).toEqual([]);
  });
  it("gives no other kind a Settings form of its own", () => {
    for (const kind of [
      "switch",
      "transform",
      "parallel",
      "wait",
      "fail",
      "llm",
      "decisionTable",
    ] as const)
      expect(ndvRegistry.kind(kind)?.settings, kind).toBeUndefined();
    expect(ndvRegistry.kind("signal")?.settings).toBe(signalSettings);
    expect(ndvRegistry.kind("humanTask")?.settings).toBe(humanSettings);
  });
});

describe("required marks and counts", () => {
  const required = (
    labels: { spec: { label: string; required?: boolean } }[],
  ) => labels.filter((f) => f.spec.required).map((f) => f.spec.label);
  it("marks the fields the product requires, and no others", () => {
    expect(required(shown(fresh("switch")).state.fields)).toEqual([]);
    expect(required(shown(fresh("transform")).state.fields)).toEqual([]);
    expect(required(shown(fresh("parallel")).state.fields)).toEqual([]);
    expect(required(shown(fresh("wait")).state.fields)).toEqual(["Wait for"]);
    expect(required(shown(fresh("signal")).state.fields)).toEqual([
      "Signal name",
    ]);
    expect(required(shown(fresh("fail")).state.fields)).toEqual([
      "Error code",
      "Message",
    ]);
    expect(required(shown(fresh("humanTask")).state.fields)).toEqual([
      "Assign to",
      "Title",
      "Answers",
    ]);
    expect(required(shown(fresh("llm")).state.fields)).toEqual([
      "Model",
      "Prompt",
    ]);
    expect(required(shown(fresh("decisionTable")).state.fields)).toEqual([
      "Table",
    ]);
    expect(required(workflowShown(triggerForm()).fields)).toEqual([]);
    expect(required(workflowShown(endForm()).fields)).toEqual([]);
    expect(required(workflowShown(workflowSettingsForm()).fields)).toEqual([
      "Name",
      "Version",
    ]);
  });
  it("requires a path's condition inside the Paths field", () => {
    const step = fresh("switch");
    const paths = shown(step).spec.fields[0].children!(
      step,
      context(freshWorkflow()),
    );
    const inside = paths[0].item!.children!(step, context(freshWorkflow()));
    expect(inside.filter((c) => c.required).map((c) => c.label)).toEqual([
      "Runs when",
    ]);
    expect(inside.map((c) => c.label)).toEqual(["Runs when", "Path result"]);
    expect(
      (
        paths[1].children!(step, context(freshWorkflow())) as {
          label: string;
        }[]
      ).map((c) => c.label),
    ).toEqual(["Path result"]);
  });
  it("counts the default fields of every new step", () => {
    const counts = Object.fromEntries(
      kinds
        .filter((kind) => kind !== "action")
        .map((kind) => [kind, shown(fresh(kind)).ids.length] as const),
    );
    expect(counts).toEqual({
      decisionTable: 1,
      llm: 2,
      transform: 1,
      switch: 1,
      parallel: 1,
      wait: 1,
      signal: 1,
      humanTask: 3,
      fail: 2,
    });
  });
});

describe("defaults and limits", () => {
  it("starts a wait at one minute, in whole seconds to days", () => {
    const [duration] = waitForm().fields;
    expect(duration).toMatchObject({
      id: "duration",
      path: ["durationSeconds"],
      type: "duration",
      label: "Wait for",
      required: true,
      default: 60,
      min: 1,
      units: ["seconds", "minutes", "hours", "days"],
    });
  });
  it("starts a signal's timeout at one hour, and its code field with a name rule", () => {
    expect(signalSettings().fields[0]).toMatchObject({
      path: ["timeoutSeconds"],
      type: "duration",
      required: true,
      default: 3600,
      min: 1,
    });
    expect(signalForm().fields[0]).toMatchObject({
      path: ["name"],
      identifier: true,
      required: true,
    });
    expect(failForm().fields[0]).toMatchObject({
      path: ["code"],
      default: "business-error",
      identifier: true,
    });
    expect(failForm().fields[1]).toMatchObject({
      path: ["message"],
      type: "multiline",
      required: true,
    });
  });
  it("starts a human task with reviewers who approve or reject", () => {
    const [assign, title, answers] = humanForm().fields;
    expect(assign).toMatchObject({
      path: ["assignment"],
      default: "reviewers",
      identifier: true,
    });
    expect(title).toMatchObject({
      path: ["title"],
      mapping: "both",
      templateCapable: true,
    });
    expect(answers).toMatchObject({
      path: ["decisions"],
      type: "list",
      display: "chips",
      default: ["approve", "reject"],
      minItems: 1,
      maxItems: 32,
    });
  });
  it("keeps a parallel step's branches in one list with at least one", () => {
    expect(parallelForm().fields[0]).toMatchObject({
      path: ["branches"],
      type: "list",
      minItems: 1,
    });
  });
});

describe("options", () => {
  it("offers a parallel step's limit and branch results, and shows them once set", () => {
    const step = fresh("parallel");
    expect(addableOf(shown(step).state)).toEqual([
      "Run at most",
      "Branch results",
    ]);
    const limited = shown({ ...step, concurrency: 3 }).state;
    expect(labelsOf(limited)).toEqual(["Branches", "Run at most"]);
    const branches = step["branches"] as Record<string, { output: unknown }>;
    const mapped = shown({
      ...step,
      branches: {
        ...branches,
        first: { ...branches["first"], output: { ref: "/input/total" } },
      },
    }).state;
    expect(labelsOf(mapped)).toEqual(["Branches", "Branch results"]);
  });
  it("names each branch's result", () => {
    const step = fresh("parallel");
    const group = parallelForm().options!.find(
      (o) => o.id === "branch-results",
    )!;
    expect(
      group.children!(step, context(freshWorkflow())).map((c) => [
        c.label,
        c.path,
      ]),
    ).toEqual([
      ["first result", ["branches", "first", "output"]],
      ["second result", ["branches", "second", "output"]],
    ]);
  });
  it("offers the payload, the details and form of a human task, and a signal's payload", () => {
    expect(addableOf(shown(fresh("signal")).state)).toEqual(["Payload fields"]);
    expect(addableOf(shown(fresh("humanTask")).state)).toEqual([
      "Details to show",
      "Form fields",
    ]);
    expect(addableOf(workflowShown(endForm()))).toEqual(["Result schema"]);
    expect(addableOf(workflowShown(workflowSettingsForm()))).toEqual([
      "Stop runs after",
      "Callable by other workflows",
    ]);
  });
  it("offers a model's context, action version and connection", () => {
    expect(addableOf(shown(fresh("llm")).state)).toEqual([
      "Context",
      "AI action version",
      "AI connection",
    ]);
  });
  it("shows Callable by other workflows once the workflow is callable", () => {
    const callable = (workflow: Workflow) => {
      workflow.spec["callable"] = {};
    };
    const state = workflowShown(
      workflowSettingsForm(),
      ["flow.callWorkflow"],
      callable,
    );
    expect(labelsOf(state)).toEqual([
      "Name",
      "Version",
      "Callable by other workflows",
    ]);
    expect(addableOf(state)).toEqual(["Stop runs after"]);
  });
  it("shows the timeout of a workflow that has one", () => {
    const timed = (workflow: Workflow) => {
      workflow.spec["timeoutSeconds"] = 600;
    };
    expect(labelsOf(workflowShown(workflowSettingsForm(), [], timed))).toEqual([
      "Name",
      "Version",
      "Stop runs after",
    ]);
  });
});

describe("transform", () => {
  const types = (step: Step) =>
    shown(step).state.fields.map((f) => [
      f.spec.id,
      f.spec.type,
      f.spec.mapping,
    ]);
  it("edits a set of fields as rows", () => {
    expect(types(fresh("transform"))).toEqual([["value", "keyValue", "both"]]);
    expect(
      types(
        fresh("transform", {
          value: { object: { total: { ref: "/input/total" } } },
        }),
      ),
    ).toEqual([["value", "keyValue", "both"]]);
  });
  it("keeps a whole mapping on the rows field", () => {
    expect(types(fresh("transform", { value: { ref: "/input" } }))).toEqual([
      ["value", "keyValue", "both"],
    ]);
    expect(
      types(
        fresh("transform", {
          value: { op: { name: "concat", args: [{ literal: "a" }] } },
        }),
      ),
    ).toEqual([["value", "keyValue", "both"]]);
  });
  it("edits a value that isn't a set of fields as JSON", () => {
    expect(types(fresh("transform", { value: { literal: "text" } }))).toEqual([
      ["value-any", "json", "both"],
    ]);
    expect(types(fresh("transform", { value: { literal: [1, 2] } }))).toEqual([
      ["value-any", "json", "both"],
    ]);
    expect(
      transformForm(fresh("transform", { value: { literal: 3 } })).fields[0]
        .label,
    ).toBe("Fields");
  });
});

describe("decision table", () => {
  const ndv = {
    catalog: {
      tables: async () => [
        { uses: "payment-policy@1.0.0", title: "payment-policy 1.0.0" },
        { uses: "refund-policy@2.1.0", title: "refund-policy 2.1.0" },
      ],
    },
  } as unknown as NdvContext;
  it("lists the published tables as choices", async () => {
    const step = fresh("decisionTable");
    const choices = decisionTableForm(step, context(freshWorkflow())).fields[0]
      .choices;
    expect(typeof choices).toBe("function");
    expect(
      await (choices as (ctx: NdvContext) => Promise<unknown>)(ndv),
    ).toEqual([
      { value: "payment-policy@1.0.0", label: "payment-policy 1.0.0" },
      { value: "refund-policy@2.1.0", label: "refund-policy 2.1.0" },
    ]);
  });
  it("asks only for a table while its contract is unknown", () => {
    const step = fresh("decisionTable", {
      uses: "missing@1.0.0",
      with: { literal: {} },
    });
    expect(shown(step).labels).toEqual(["Table"]);
    expect(
      shown(
        fresh("decisionTable", {
          uses: "payment-policy@1.0.0",
          with: { literal: {} },
        }),
      ).state.fields[0],
    ).toMatchObject({
      spec: { id: "table", path: ["uses"], type: "resource", required: true },
    });
  });
  it("maps each required input at its place in the table input", () => {
    const step = fresh("decisionTable", {
      uses: "payment-policy@1.0.0",
      with: { literal: {} },
    });
    const [, amount] = shown(step).state.fields;
    expect(amount.spec).toMatchObject({
      id: "input.amount",
      path: ["with", "amount"],
      required: true,
      mapping: "both",
    });
  });
  it("keeps a whole mapping as one Table input field with the rows inside", () => {
    const step = fresh("decisionTable", { uses: "payment-policy@1.0.0" });
    const { spec } = shown(step).state.fields[1];
    expect(spec).toMatchObject({
      id: "input",
      path: ["with"],
      type: "fields",
      mapping: "both",
    });
    expect(
      spec.children!(step, context(freshWorkflow())).map((c) => c.label),
    ).toEqual(["Amount"]);
  });
});

describe("form environment", () => {
  it("reads a step's expression fields and the schemas they target", () => {
    const step = fresh("decisionTable", { uses: "payment-policy@1.0.0" });
    const workflow = freshWorkflow();
    const ctx = context(workflow);
    const descriptor = ndvRegistry.kind("decisionTable")!;
    const subject = stepSubject(descriptor, step, ctx, { method: "GET" });
    expect(subject).toMatchObject({
      step,
      workflow,
      action: { method: "GET" },
      roots: [["with"]],
    });
    expect(subject.schemaOf?.(["with"])).toEqual(table.spec.inputSchema);
    expect(subject.schemaOf?.(["uses"])).toBeUndefined();
  });
  it("reads the workflow itself for workflow forms", () => {
    const workflow = freshWorkflow();
    expect(workflowSubject(workflow)).toEqual({
      step: null,
      workflow,
      action: null,
      roots: [],
    });
    expect(WORKFLOW_STAND_IN).toEqual({ id: "$workflow", kind: "transform" });
  });
  it("carries the read-only reason and the options added in this session", () => {
    const workflow = freshWorkflow();
    const ctx = context(workflow);
    const subject = workflowSubject(workflow);
    expect(formEnv(subject, WORKFLOW_STAND_IN, ctx)).toEqual({
      subject,
      step: WORKFLOW_STAND_IN,
      kind: ctx,
      readOnly: null,
      added: new Set(),
    });
    const added = new Set(["resultSchema"]);
    const env = formEnv(
      subject,
      WORKFLOW_STAND_IN,
      ctx,
      "Fix the source to edit this step.",
      added,
    );
    expect(env.readOnly).toBe("Fix the source to edit this step.");
    expect(labelsOf(formState(endForm(), env))).toEqual([
      "Result fields",
      "Result schema",
    ]);
  });
  it("reads workflow values from the workflow document", () => {
    const workflow = freshWorkflow();
    workflow.metadata.name = "order-intake";
    const state = formState(
      workflowSettingsForm(),
      formEnv(workflowSubject(workflow), WORKFLOW_STAND_IN, context(workflow)),
    );
    expect(
      state.fields.map((f) => [f.spec.id, f.spec.scope, f.spec.path]),
    ).toEqual([
      ["name", "workflow", ["metadata", "name"]],
      ["version", "workflow", ["metadata", "version"]],
    ]);
  });
});

describe("shared helpers", () => {
  it("builds a duration in whole seconds to days that starts at one", () => {
    expect(durationParam("due", ["dueSeconds"], "Due after")).toEqual({
      id: "due",
      path: ["dueSeconds"],
      type: "duration",
      label: "Due after",
      units: ["seconds", "minutes", "hours", "days"],
      min: 1,
    });
    expect(
      durationParam("due", ["dueSeconds"], "Due after", {
        min: 5,
        hint: "Soon.",
      }),
    ).toMatchObject({
      min: 5,
      hint: "Soon.",
    });
  });
  it("tells a whole mapping from fields", () => {
    expect(wholeMapping(undefined)).toBe(false);
    expect(wholeMapping({ literal: {} })).toBe(false);
    expect(wholeMapping({ object: { a: { ref: "/input/a" } } })).toBe(false);
    expect(wholeMapping({ ref: "/input" })).toBe(true);
    expect(wholeMapping({ op: { name: "concat", args: [] } })).toBe(true);
  });
  it("names the kinds that fail on their own and the kinds that always stop the run", () => {
    expect([...ON_ERROR_KINDS].sort()).toEqual(
      [
        "action",
        "agent",
        "callWorkflow",
        "decisionTable",
        "humanTask",
        "llm",
        "signal",
        "transform",
      ].sort(),
    );
    expect([...STOP_ONLY_KINDS].sort()).toEqual([
      "forEach",
      "parallel",
      "switch",
    ]);
  });
});

describe("reading and writing", () => {
  const subjectOf = (step: Step) => {
    const ctx = context(freshWorkflow());
    return stepSubject(ndvRegistry.kind(step.kind)!, step, ctx, null);
  };
  const workflowSpec = (form: FormSpec, id: string) =>
    [...form.fields, ...(form.options ?? [])].find((f) => f.id === id)!;
  it("puts a required field with a default back to that default", () => {
    const wait = fresh("wait", { durationSeconds: 5 });
    expect(resetChanges(subjectOf(wait), waitForm().fields[0])).toEqual([
      { scope: "step", path: ["durationSeconds"], value: 60 },
    ]);
    const signal = fresh("signal", { timeoutSeconds: 7200 });
    expect(resetChanges(subjectOf(signal), signalSettings().fields[0])).toEqual(
      [{ scope: "step", path: ["timeoutSeconds"], value: 3600 }],
    );
    const fail = fresh("fail", { code: "other" });
    expect(resetChanges(subjectOf(fail), failForm().fields[0])).toEqual([
      { scope: "step", path: ["code"], value: "business-error" },
    ]);
  });
  it("takes a human task's deadlines out of the step again", () => {
    const task = fresh("humanTask", { dueSeconds: 3600 });
    const [due] = humanSettings().fields;
    expect(isDefault(subjectOf(task), due)).toBe(false);
    expect(resetChanges(subjectOf(task), due)).toEqual([
      { scope: "step", path: ["dueSeconds"], value: undefined },
    ]);
  });
  it("turns Callable by other workflows on and off by presence", () => {
    const callable = workflowSpec(workflowSettingsForm(), "callable");
    const workflow = freshWorkflow();
    const subject = workflowSubject(workflow);
    expect(isDefault(subject, callable)).toBe(true);
    expect(writeParam(subject, callable, fixed({}))).toEqual([
      { scope: "workflow", path: ["spec", "callable"], value: {} },
    ]);
    workflow.spec["callable"] = {};
    expect(isDefault(subject, callable)).toBe(false);
    expect(resetChanges(subject, callable)).toEqual([
      { scope: "workflow", path: ["spec", "callable"], value: undefined },
    ]);
  });
  it("reads a path's result inside the decision, and the workflow result and input fields", () => {
    const step = fresh("switch");
    const subject = subjectOf(step);
    const result = { path: ["cases", 0, "output"] as (string | number)[] };
    expect(readParam(subject, result)).toEqual(fixed({}));
    expect(
      isDefault(subject, {
        ...result,
        id: "x",
        type: "json",
        label: "Path result",
        default: {},
      }),
    ).toBe(true);
    const workflow = freshWorkflow();
    const wf = workflowSubject(workflow);
    expect(readParam(wf, workflowSpec(triggerForm(), "inputSchema"))).toEqual(
      fixed({ type: "object" }),
    );
    expect(readEntries(wf, workflowSpec(endForm(), "result"))).toEqual({
      kind: "object",
      keys: [],
    });
  });
  it("asks for what the product requires, and accepts a human task's default answers", () => {
    const rules = (step: Step, spec: FormSpec["fields"][number]) => {
      const subject = subjectOf(step);
      return fieldRules(
        spec,
        readParam(subject, spec),
        readEntries(subject, spec),
      ).map((p) => p.message);
    };
    expect(rules(fresh("fail"), failForm().fields[1])).toEqual([
      "Enter a value.",
    ]);
    expect(rules(fresh("signal"), signalForm().fields[0])).toEqual([
      "Enter a value.",
    ]);
    // The first message is the one a field shows.
    expect(
      rules(fresh("wait", { durationSeconds: 0 }), waitForm().fields[0])[0],
    ).toBe("Enter a whole number greater than 0.");
    const task = fresh("humanTask");
    delete task["decisions"];
    expect(rules(task, humanForm().fields[2])).toEqual([]);
    expect(
      rules(fresh("humanTask", { decisions: [] }), humanForm().fields[2]),
    ).toEqual(["Add at least one."]);
    expect(
      rules(fresh("parallel", { branches: {} }), parallelForm().fields[0]),
    ).toEqual(["Add at least 1."]);
  });
});

describe("form contract", () => {
  it("keeps every form of a new step inside the contract", () => {
    const ctx = context(freshWorkflow());
    for (const kind of kinds.filter((k) => k !== "action")) {
      const step = fresh(kind);
      expect(formProblems(ndvRegistry.kind(kind)!, step, ctx), kind).toEqual(
        [],
      );
    }
  });
  it("keeps a table step with a contract and a step with more branches inside the contract", () => {
    const ctx = context(freshWorkflow());
    const withTable = fresh("decisionTable", {
      uses: "payment-policy@1.0.0",
      with: { literal: {} },
    });
    expect(
      formProblems(ndvRegistry.kind("decisionTable")!, withTable, ctx),
    ).toEqual([]);
    const many = fresh("switch");
    (many["cases"] as unknown[]).push({ steps: [], output: { literal: {} } });
    expect(formProblems(ndvRegistry.kind("switch")!, many, ctx)).toEqual([]);
    const fan = fresh("parallel");
    (fan["branches"] as Record<string, unknown>)["third"] = {
      steps: [],
      output: { literal: {} },
    };
    expect(formProblems(ndvRegistry.kind("parallel")!, fan, ctx)).toEqual([]);
  });
  it("keeps the workflow forms free of duplicate field IDs", () => {
    const ids = [triggerForm(), endForm(), workflowSettingsForm()].flatMap(
      (form) => [...form.fields, ...(form.options ?? [])].map((f) => f.id),
    );
    expect(new Set(ids).size).toBe(ids.length);
  });
});
