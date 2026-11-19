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
// The settings of a step that calls a workflow: which workflow version, its
// input from the called workflow's input, whether the step waits, and what a
// failed call does. The form is read through the same model step details
// use, so these cases see what the person sees and what each change writes.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { delimiter, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parsePointer } from "../src/app/forms/core/json";
import { formProblems } from "../src/app/editor/ndv/form-contract";
import { fieldFor } from "../src/app/editor/ndv/issues";
import {
  callFields,
  callForm,
  newCallStep,
} from "../src/app/editor/ndv/kinds/forms/call-forms";
import { fieldRules } from "../src/app/editor/ndv/params/field-rules";
import { formEnv, stepSubject } from "../src/app/editor/ndv/params/form-env";
import {
  formState,
  hiddenChanges,
  type FormState,
} from "../src/app/editor/ndv/params/form-model";
import {
  applyChange,
  fixed,
  mapped,
  readEntries,
  readParam,
  writeParam,
  type FormSubject,
} from "../src/app/editor/ndv/params/value-io";
import type {
  Json,
  KindContext,
  NdvContext,
  ParamSpec,
  StepKindDescriptor,
} from "../src/app/editor/ndv/registry";
import { freshWorkflow, type Step } from "../src/app/model";
import { python, pythonAvailable, repository } from "./python-path";

const NOTIFY = "notify-customer@1.0.0";
const notifyInput = {
  type: "object",
  properties: {
    customerId: { type: "string", title: "Customer ID" },
    channel: { type: "string", enum: ["email", "sms"] },
  },
  required: ["customerId"],
};
const contracts: Record<string, Json> = {
  [NOTIFY]: {
    inputSchema: notifyInput,
    outputSchema: { type: "object" },
    callable: {},
    source: "project",
  },
  "archive-order@0.2.0": {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    callable: {},
  },
  "customer-profile@2.0.0": {
    inputSchema: { $ref: "weave://schemas/customer@1.0.0" },
    outputSchema: { type: "object" },
    callable: {},
  },
  "customer-profile@3.0.0": {
    inputSchema: { $ref: "customer.json" },
    outputSchema: { type: "object" },
    callable: { allowedCallers: ["order-intake"] },
    source: "local",
    schemas: {
      "customer.json": {
        type: "object",
        properties: { customerId: { type: "string", title: "Customer ID" } },
        required: ["customerId"],
      },
    },
  },
  "notify-customer@1.1.0": {
    inputSchema: { type: "object" },
    outputSchema: { type: "object" },
    callable: { allowedCallers: ["billing-close"] },
    source: "project",
  },
};
/** Step details' view of the call kind: its expression fields and this form. */
const CALL = {
  kind: "callWorkflow",
  fields: callFields,
  form: callForm,
} as unknown as StepKindDescriptor;

/** What the editor says when a version isn't known, as its call sources would. */
let notes: (uses?: string) => string | null = () => null;
function context(step: Step): KindContext {
  const workflow = freshWorkflow();
  workflow.metadata.name = "order-intake";
  workflow.spec.steps.push(step);
  return {
    workflow,
    features: ["flow.callWorkflow"],
    actionContract: () => null,
    tableContract: () => null,
    workflowContract: (uses) => contracts[uses] ?? null,
    callNote: (uses) => notes(uses),
  };
}
const call = (extra: Record<string, unknown> = {}): Step => ({
  ...newCallStep("notify"),
  ...extra,
});

/** The form as step details draw it for `step`. */
function shown(step: Step) {
  const ctx = context(step);
  const subject = stepSubject(CALL, step, ctx, null);
  const env = formEnv(subject, step, ctx);
  const state = formState(callForm(step, ctx), env);
  const field = (label: string) =>
    state.fields.find((entry) => entry.spec.label === label)?.spec;
  return {
    ctx,
    subject,
    env,
    state,
    field,
    labels: state.fields.map((entry) => entry.spec.label),
    addable: state.addable.map((entry) => entry.spec.label),
  };
}
/** What step details write when the person sets `label` to `value`, hidden fields cleared in the same change. */
function choose(step: Step, label: string, value: Json): Step {
  const before = shown(step);
  let subject: FormSubject = before.subject;
  for (const change of writeParam(subject, before.field(label)!, fixed(value)))
    subject = applyChange(subject, change);
  const after = subject.step!;
  const afterCtx = context(after);
  const afterEnv = formEnv(subject, after, afterCtx);
  const afterState: FormState = formState(callForm(after, afterCtx), afterEnv);
  for (const change of hiddenChanges(
    before.state,
    afterState,
    afterEnv,
    new Set(),
  ).changes)
    subject = applyChange(subject, change);
  return subject.step!;
}
const childrenOf = (spec: ParamSpec | undefined, step: Step) =>
  spec?.children?.(step, context(step)) ?? [];

const corpus = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/callable-corpus.json"),
    "utf8",
  ),
) as { lists: { lock: unknown }[] };
/** Each workflow's compiler findings, compiled by the platform against the corpus's project. */
function compile(workflows: unknown[]): { code: string; path: string }[][] {
  const script = `
import json, sys
from firefly_weave.compiler.api import compile_source
from firefly_weave.compiler.catalog import CatalogSnapshot
request = json.load(sys.stdin)
catalog = CatalogSnapshot.from_lock(request["lock"])
print(json.dumps([
    [{"code": d.code, "path": d.path} for d in compile_source(w, format="object", catalog=catalog).diagnostics]
    for w in request["workflows"]
]))
`;
  return JSON.parse(
    execFileSync(python, ["-c", script], {
      cwd: repository,
      encoding: "utf8",
      timeout: 180_000,
      input: JSON.stringify({ lock: corpus.lists[0].lock, workflows }),
      env: {
        ...process.env,
        PYTHONPATH: [resolve(repository, "src"), process.env["PYTHONPATH"]]
          .filter(Boolean)
          .join(delimiter),
      },
    }),
  );
}

/** The fields step details show for `step`, with the rows inside groups: where its issues can land. */
function placedOf(step: Step) {
  const { state, ctx } = shown(step);
  const out: { spec: ParamSpec; tab: "parameters" }[] = [];
  const visit = (spec: ParamSpec) => {
    out.push({ spec, tab: "parameters" });
    if (spec.type === "fields") spec.children?.(step, ctx).forEach(visit);
  };
  state.fields.forEach((entry) => visit(entry.spec));
  return out;
}

describe("a new call step", () => {
  it("waits for the called workflow's result, with an empty input", () => {
    expect(newCallStep("notify")).toEqual({
      id: "notify",
      kind: "callWorkflow",
      mode: "wait",
      with: { literal: {} },
    });
  });

  it("asks for a workflow to call before anything else", () => {
    const { labels, addable, field, subject } = shown(call());
    expect(labels).toEqual(["Workflow", "When it runs", "If it fails"]);
    expect(addable).toEqual(["Business key"]);
    const workflow = field("Workflow")!;
    expect(workflow).toMatchObject({ path: ["uses"], required: true });
    expect(
      fieldRules(
        workflow,
        readParam(subject, workflow),
        readEntries(subject, workflow),
      ).map((problem) => problem.message),
    ).toEqual(["Choose a workflow to call."]);
  });
});

describe("the Workflow field", () => {
  it("lists the versions this workflow may call, in their groups and with why one can't be chosen", async () => {
    const asked: unknown[] = [];
    const ndv = {
      workflow: { metadata: { name: "order-intake" } },
      catalog: {
        workflows: async (filter?: { callableBy?: string }) => {
          asked.push(filter);
          return [
            {
              uses: "notify-customer@1.1.0",
              title: "notify-customer 1.1.0",
              group: "In this project",
              disabled:
                "This workflow version does not allow calls from order-intake; add order-intake to its callable.allowedCallers in a new version.",
            },
            {
              uses: "customer-profile@2.0.0",
              title: "customer-profile 2.0.0",
              group: "In this project",
              description:
                "Studio can't tell whether customer-profile 2.0.0 allows this workflow.",
            },
            {
              uses: "lookup-customer@0.1.0",
              title: "lookup-customer 0.1.0",
              group: "On this computer",
              aside: "Draft",
            },
          ];
        },
      },
    } as unknown as NdvContext;
    const choices = shown(call()).field("Workflow")!.choices as (
      ctx: NdvContext,
    ) => Promise<unknown[]>;
    expect(await choices(ndv)).toEqual([
      {
        value: "notify-customer@1.1.0",
        label: "notify-customer 1.1.0",
        group: "In this project",
        disabled:
          "This workflow version does not allow calls from order-intake; add order-intake to its callable.allowedCallers in a new version.",
      },
      {
        value: "customer-profile@2.0.0",
        label: "customer-profile 2.0.0",
        group: "In this project",
        description:
          "Studio can't tell whether customer-profile 2.0.0 allows this workflow.",
      },
      {
        value: "lookup-customer@0.1.0",
        label: "lookup-customer 0.1.0",
        group: "On this computer",
        aside: "Draft",
      },
    ]);
    expect(asked).toEqual([{ callableBy: "order-intake" }]);
  });

  it("keeps one list provider however often the form is drawn, so the list stays loaded", () => {
    const first = shown(call()).field("Workflow")?.choices;
    expect(typeof first).toBe("function");
    expect(shown(call({ uses: NOTIFY })).field("Workflow")?.choices).toBe(
      first,
    );
  });
});

describe("the Input field", () => {
  it("draws a row for each required field of the called workflow's input, and offers the optional ones", () => {
    const step = call({ uses: NOTIFY });
    const { field, labels } = shown(step);
    expect(labels).toEqual([
      "Workflow",
      "Input",
      "When it runs",
      "If it fails",
    ]);
    const input = field("Input")!;
    expect(input).toMatchObject({
      type: "fields",
      path: ["with"],
      hint: "From notify-customer's input",
    });
    expect(
      childrenOf(input, step).map((row) => [
        row.label,
        row.path,
        row.type,
        !!row.required,
      ]),
    ).toEqual([
      ["Customer ID", ["with", "customerId"], "text", true],
      ["Channel", ["with", "channel"], "select", false],
    ]);
  });

  it("writes a typed row into the step's input", () => {
    const step = call({ uses: NOTIFY });
    const { field, subject } = shown(step);
    const row = childrenOf(field("Input"), step)[0];
    let next = subject;
    for (const change of writeParam(next, row, mapped({ ref: "/input/id" })))
      next = applyChange(next, change);
    expect(next.step!["with"]).toEqual({
      object: { customerId: { ref: "/input/id" } },
    });
  });

  it("reads rows through the project's shared schemas", () => {
    const step = call({ uses: "customer-profile@3.0.0" });
    const input = shown(step).field("Input");
    expect(input).toMatchObject({ type: "fields" });
    expect(childrenOf(input, step).map((row) => [row.label, row.path])).toEqual(
      [["Customer ID", ["with", "customerId"]]],
    );
  });

  it("takes free-form input when Studio can't read the called workflow's input", () => {
    for (const uses of ["notify-customer@9.0.0", "customer-profile@2.0.0"]) {
      const { field } = shown(call({ uses }));
      const name = uses.split("@")[0];
      expect(field("Input"), uses).toMatchObject({
        type: "json",
        path: ["with"],
        mapping: "both",
        hint: `Studio can't read ${name}'s input.`,
      });
    }
  });

  it("takes the input as named values when the called workflow's input lists no fields, and an empty one is fine", () => {
    const { field, subject } = shown(call({ uses: "archive-order@0.2.0" }));
    const input = field("Input")!;
    expect(input).toMatchObject({
      path: ["with"],
      type: "keyValue",
      mapping: "both",
    });
    // An object with no listed fields takes {}: nothing is missing.
    expect(
      fieldRules(input, readParam(subject, input), readEntries(subject, input)),
    ).toEqual([]);
  });

  it("keeps an input mapped as a whole and offers its rows behind Fixed", () => {
    const step = call({ uses: NOTIFY, with: { ref: "/input" } });
    const input = shown(step).field("Input")!;
    expect(input).toMatchObject({
      type: "fields",
      mapping: "both",
      hint: "The whole input is mapped. Switch to Fixed to fill it field by field.",
    });
    expect(childrenOf(input, step).map((row) => row.label)).toEqual([
      "Customer ID",
    ]);
  });
});

describe("the Workflow field's hint", () => {
  it("says where the version lives and that this workflow may call it", () => {
    expect(shown(call({ uses: NOTIFY })).field("Workflow")?.hint).toBe(
      "In this project · callable by order-intake",
    );
    expect(
      shown(call({ uses: "customer-profile@3.0.0" })).field("Workflow")?.hint,
    ).toBe("On this computer · callable by order-intake");
  });

  it("says nothing while the version can't be called or isn't known", () => {
    for (const uses of ["notify-customer@1.1.0", "unknown@1.0.0", ""])
      expect(
        shown(call({ uses })).field("Workflow")?.hint,
        uses,
      ).toBeUndefined();
  });

  it("says why the project's versions are missing, or that the chosen one can't be found", () => {
    notes = (uses) =>
      uses
        ? `Studio can't find ${uses.replace("@", " ")} in this project.`
        : "Connect to a platform to call published workflows.";
    try {
      expect(shown(call()).field("Workflow")?.hint).toBe(
        "Connect to a platform to call published workflows.",
      );
      expect(
        shown(call({ uses: "unknown@1.0.0" })).field("Workflow")?.hint,
      ).toBe("Studio can't find unknown 1.0.0 in this project.");
      expect(shown(call({ uses: NOTIFY })).field("Workflow")?.hint).toBe(
        "In this project · callable by order-intake",
      );
    } finally {
      notes = () => null;
    }
  });
});

describe("the Workflow field's buttons", () => {
  const actions = (step: Step) => shown(step).field("Workflow")?.actions ?? [];
  it("opens the called workflow once one is chosen", () => {
    const opened: string[] = [];
    const ndv = {
      openWorkflow: (uses: string) => opened.push(uses),
    } as unknown as NdvContext;
    const step = call({ uses: NOTIFY });
    const open = actions(step).find(
      (action) => action.label === "Open workflow",
    )!;
    open.run(ndv);
    expect(opened).toEqual([NOTIFY]);
    // Nothing to open before a workflow is chosen.
    expect(actions(call())).toEqual([]);
  });

  it("offers Choose another only for a version Studio can't find, and clears it in one step", () => {
    expect(
      actions(call({ uses: NOTIFY })).map((action) => action.label),
    ).toEqual(["Open workflow"]);
    const missing = call({ uses: "unknown@1.0.0" });
    const choose = actions(missing).find(
      (action) => action.label === "Choose another",
    )!;
    const edits: unknown[] = [];
    choose.run({
      edit: (changes: unknown, label: string) => edits.push([changes, label]),
    } as unknown as NdvContext);
    expect(edits).toEqual([
      [[{ path: ["uses"], value: undefined }], "Choose another workflow"],
    ]);
  });
});

describe("When it runs and If it fails", () => {
  it("says what each choice does with the called workflow", () => {
    expect(shown(call({ uses: NOTIFY })).field("When it runs")).toMatchObject({
      path: ["mode"],
      default: "wait",
      hint: "This workflow waits until notify-customer finishes, then reads its result.",
      choices: [
        { value: "wait", label: "Wait for its result" },
        { value: "detach", label: "Start it and continue" },
      ],
    });
    expect(
      shown(call({ uses: NOTIFY, mode: "detach" })).field("When it runs")?.hint,
    ).toBe(
      "notify-customer runs on its own; this workflow doesn't wait and reads no result.",
    );
    expect(shown(call()).field("When it runs")?.hint).toBeUndefined();
    expect(shown(call()).field("If it fails")).toMatchObject({
      path: ["onFailure"],
      choices: [
        { value: "stop", label: "Stop this workflow" },
        { value: "continue", label: "Continue with the result status" },
      ],
    });
  });

  it("hides If it fails for a call that doesn't wait, and removes its choice in the same change", () => {
    for (const onFailure of ["continue", "stop"]) {
      const detached = choose(
        call({ uses: NOTIFY, onFailure }),
        "When it runs",
        "detach",
      );
      expect(detached, onFailure).toEqual({
        id: "notify",
        kind: "callWorkflow",
        uses: NOTIFY,
        mode: "detach",
        with: { literal: {} },
      });
      expect(shown(detached).labels).toEqual([
        "Workflow",
        "Input",
        "When it runs",
      ]);
    }
  });

  it("shows If it fails unset again when the call waits once more", () => {
    const detached = choose(
      call({ uses: NOTIFY, onFailure: "continue" }),
      "When it runs",
      "detach",
    );
    const waiting = choose(detached, "When it runs", "wait");
    const { field, subject, labels } = shown(waiting);
    expect(labels).toContain("If it fails");
    expect(readParam(subject, field("If it fails")!)).toEqual({
      mode: "absent",
    });
    expect(field("If it fails")?.hint).toBe(
      "Without a choice, a failed call stops this workflow.",
    );
    expect(
      shown(call({ uses: NOTIFY, onFailure: "stop" })).field("If it fails")
        ?.hint,
    ).toBeUndefined();
  });
});

describe("the business key", () => {
  it("waits behind Add option and keeps a mapped key as it is", () => {
    const key = { ref: "/input/orderId" };
    const step = call({ uses: NOTIFY, businessKey: key });
    const { field, subject, addable } = shown(step);
    const business = field("Business key")!;
    expect(addable).not.toContain("Business key");
    expect(business).toMatchObject({
      path: ["businessKey"],
      mapping: "both",
      maxLength: 200,
    });
    expect(readParam(subject, business)).toEqual({
      mode: "mapped",
      expression: key,
    });
    const changed = choose(step, "When it runs", "detach");
    expect(changed["businessKey"]).toEqual(key);
    expect(changed["with"]).toEqual({ literal: {} });
  });

  it("writes typed text as the key", () => {
    const step = call({ uses: NOTIFY });
    const { subject } = shown(step);
    const business = shown(step).state.addable.find(
      (entry) => entry.spec.label === "Business key",
    )!.spec;
    let next = subject;
    for (const change of writeParam(next, business, fixed("A-1042")))
      next = applyChange(next, change);
    expect(next.step!["businessKey"]).toEqual({ literal: "A-1042" });
  });
});

describe("the compiler's findings", () => {
  /** order-intake calling as `steps` say, compiled by the platform against the corpus's project. */
  const caller = (steps: Step[]) => ({
    ...freshWorkflow(),
    metadata: { name: "order-intake", version: "1.2.0" },
    spec: { ...freshWorkflow().spec, steps },
  });
  const cases: [string, Step, string, string | null][] = [
    [
      "a version the project doesn't have",
      call({ uses: "notify-customer@9.0.0" }),
      "WV-COMP-UNKNOWN_WORKFLOW",
      "Workflow",
    ],
    [
      "a version that doesn't allow order-intake",
      call({
        uses: "notify-customer@1.9.0",
        with: { literal: { customerId: "c-1042" } },
      }),
      "WV-COMP-CALL_NOT_CALLABLE",
      "Workflow",
    ],
    [
      "an input of the wrong type",
      call({ uses: NOTIFY, with: { literal: { customerId: 5 } } }),
      "WV-COMP-TYPE_MISMATCH",
      "Input",
    ],
    // Studio never writes this one (If it fails is cleared with Start it and
    // continue); from a file, it shows on the step as a whole.
    [
      "If it fails on a call that doesn't wait",
      call({
        uses: NOTIFY,
        with: { literal: { customerId: "c-1042" } },
        mode: "detach",
        onFailure: "stop",
      }),
      "WV-COMP-CALL_MODE",
      null,
    ],
  ];

  it.skipIf(!pythonAvailable())(
    "show on the field they point at, with their codes",
    () => {
      const found = compile(cases.map(([, step]) => caller([step])));
      cases.forEach(([name, step, code, label], index) => {
        const [finding] = found[index];
        // The compiler reports at /spec/steps/0/…; the field paths are the step's.
        const at = parsePointer(finding.path)!.slice(3);
        expect(
          [finding.code, fieldFor(at, placedOf(step))?.spec.label ?? null],
          name,
        ).toEqual([code, label]);
      });
    },
  );
});

describe("the form contract", () => {
  it("holds for every state of the step", () => {
    for (const step of [
      call(),
      call({ uses: NOTIFY }),
      call({ uses: NOTIFY, with: { ref: "/input" } }),
      call({ uses: "customer-profile@2.0.0", mode: "detach" }),
      call({ uses: "archive-order@0.2.0", businessKey: { literal: "x" } }),
    ])
      expect(formProblems(CALL, step, context(step))).toEqual([]);
  });
});

describe("a call step read from a run", () => {
  it("names the version and only opens it: no input it can't read, no catalog note, no Choose another", () => {
    const step = call({ uses: "gone-workflow@1.0.0" });
    const ctx = { ...context(step), viewingRun: true };
    const form = callForm(step, ctx);
    const labels = form.fields.map((field) => field.label);
    expect(labels).not.toContain("Input");
    const workflow = form.fields.find((field) => field.label === "Workflow")!;
    expect(workflow.actions?.map((action) => action.label)).toEqual([
      "Open workflow",
    ]);
    // Editing, the same step offers Choose another and free-form input.
    const editing = callForm(step, context(step));
    expect(editing.fields.map((field) => field.label)).toContain("Input");
    expect(
      editing.fields
        .find((field) => field.label === "Workflow")!
        .actions?.map((action) => action.label),
    ).toEqual(["Open workflow", "Choose another"]);
  });
});
