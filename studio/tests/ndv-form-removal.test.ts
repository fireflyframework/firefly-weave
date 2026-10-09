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
// What removing an option or resetting a field leaves behind: a definition the
// platform's own loader still accepts. Each reset of every form field runs
// against the language models in Python; the few fields with no value to write
// back are named, so a new one that deletes a required key fails here.
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import "@angular/compiler";
import { beforeAll, describe, expect, it } from "vitest";
import { allParams } from "../src/app/editor/ndv/form-contract";
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
import { resetParam } from "../src/app/editor/ndv/params/form-model";
import {
  applyChange,
  type FormSubject,
} from "../src/app/editor/ndv/params/value-io";
import {
  ndvRegistry,
  type FormSpec,
  type Json,
  type KindContext,
  type ParamSpec,
} from "../src/app/editor/ndv/registry";
import {
  createStep,
  freshWorkflow,
  kinds,
  type Kind,
  type Step,
  type Workflow,
} from "../src/app/model";
import { python, pythonAvailable, repository } from "./python-path";

beforeAll(() => loadKindRegistrations());

const table = {
  kind: "DecisionTable",
  metadata: { name: "payment-policy", version: "1.0.0" },
  spec: {
    inputSchema: {
      type: "object",
      required: ["amount"],
      properties: { amount: { type: "number" } },
    },
  },
};
const context = (workflow: Workflow): KindContext => ({
  workflow,
  features: ["flow.callWorkflow"],
  actionContract: () => null,
  tableContract: (uses) =>
    uses === "payment-policy@1.0.0" ? (table as Json) : null,
  workflowContract: () => null,
});

/** A step the loader accepts as it stands, per kind. */
const complete = (kind: Kind): Step => {
  const step = createStep(kind, `${kind}-1`);
  switch (kind) {
    case "switch":
      (step["cases"] as Record<string, unknown>[])[0]["when"] = {
        literal: true,
      };
      return step;
    case "signal":
      return { ...step, name: "payment-received" };
    case "fail":
      return { ...step, message: "Customer not found" };
    case "decisionTable":
      return { ...step, uses: "payment-policy@1.0.0" };
    case "action":
      return { ...step, uses: "get-pet@1.0.0" };
    default:
      return step;
  }
};
const documentOf = (subject: FormSubject): Workflow => {
  const workflow = structuredClone(subject.workflow);
  if (subject.step) workflow.spec.steps = [subject.step];
  return workflow;
};
interface Case {
  id: string;
  document: Workflow;
}

/** Every field of the forms, reset one at a time on its own copy of the document. */
function resets(
  label: string,
  subject: FormSubject,
  entries: { spec: ParamSpec; relative: boolean }[],
  env: ReturnType<typeof formEnv>,
): Case[] {
  return entries
    .filter(({ relative }) => !relative)
    .map(({ spec }) => {
      const changes = resetParam(spec, env);
      return {
        id: `${label}:${spec.id}`,
        document: documentOf(changes.reduce(applyChange, subject)),
      };
    });
}
function stepCases(kind: Kind): Case[] {
  const workflow = freshWorkflow();
  const step = complete(kind);
  workflow.spec.steps.push(step);
  const ctx = context(workflow);
  const descriptor = ndvRegistry.kind(kind)!;
  const roots = descriptor.fields(step).map((field) => field.path);
  const subject = stepSubject(descriptor, step, ctx, null);
  const env = formEnv(subject, step, ctx);
  const forms = [
    descriptor.form?.(step, ctx),
    descriptor.settings?.(step, ctx),
  ];
  return [
    { id: `${kind}:untouched`, document: documentOf(subject) },
    ...forms.flatMap((form) =>
      resets(kind, subject, allParams(form as FormSpec, step, ctx, roots), env),
    ),
  ];
}
function workflowCases(): Case[] {
  const workflow = freshWorkflow();
  const ctx = context(workflow);
  const subject = workflowSubject(workflow);
  const env = formEnv(subject, WORKFLOW_STAND_IN, ctx);
  return [
    { id: "workflow:untouched", document: documentOf(subject) },
    ...[triggerForm(), endForm(), workflowSettingsForm()].flatMap((form) =>
      resets("workflow", subject, allParams(form, WORKFLOW_STAND_IN, ctx), env),
    ),
  ];
}
/** The result of a decision path, which sits inside a list item: reset at its place in the step. */
function pathResultCases(): Case[] {
  const workflow = freshWorkflow();
  const step = complete("switch");
  workflow.spec.steps.push(step);
  const ctx = context(workflow);
  const descriptor = ndvRegistry.kind("switch")!;
  const subject = stepSubject(descriptor, step, ctx, null);
  const env = formEnv(subject, step, ctx);
  const paths = descriptor.form!(step, ctx).fields[0].children!(step, ctx);
  const result = paths[0].item!.children!(step, ctx)[1];
  const spec = { ...result, path: ["cases", 0, "output"] };
  return [
    {
      id: "switch:case.output",
      document: documentOf(resetParam(spec, env).reduce(applyChange, subject)),
    },
  ];
}

/**
 * Required by the language, with no value that would be valid to write back: a
 * name, a message, a table and a path's condition are the person's to give.
 * Resetting them leaves what a new step has.
 */
const NO_VALID_DEFAULT = [
  "decisionTable:table",
  "fail:message",
  "signal:name",
  "switch:cases",
  "switch:paths",
];

const available = pythonAvailable();
describe.skipIf(!available)("what removing and resetting leave behind", () => {
  const validated = (): Map<string, string | null> => {
    const cases = [
      ...kinds.filter((kind) => kind !== "action").flatMap(stepCases),
      ...workflowCases(),
      ...pathResultCases(),
    ];
    const output = execFileSync(
      python,
      [
        "-c",
        `
import json, sys
from firefly_weave.contracts.definitions import load_definition
verdicts = {}
for case in json.load(sys.stdin):
    try:
        load_definition(case["document"])
        verdicts[case["id"]] = None
    except Exception as error:
        verdicts[case["id"]] = str(error).splitlines()[0]
print(json.dumps(verdicts))
`,
      ],
      {
        cwd: repository,
        encoding: "utf8",
        input: JSON.stringify(cases),
        timeout: 180_000,
        env: { ...process.env, PYTHONPATH: resolve(repository, "src") },
      },
    );
    return new Map(
      Object.entries(JSON.parse(output) as Record<string, string | null>),
    );
  };

  it("accepts every document the tests start from", () => {
    const verdicts = validated();
    expect(
      [...verdicts].filter(([id, error]) => id.endsWith(":untouched") && error),
    ).toEqual([]);
    expect(verdicts.size).toBeGreaterThan(40);
  });
  it("keeps every required key when a field is reset or an option is removed", () => {
    const failing = [...validated()]
      .filter(([, error]) => error)
      .map(([id]) => id);
    expect(failing.sort()).toEqual(NO_VALID_DEFAULT);
  });
});
