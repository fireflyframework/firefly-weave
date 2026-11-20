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
// Descriptors for the built-in step kinds other than the AI task (llm.ts).
// Field lists match the classic property grid, so both editors map the same
// expressions while the new one is behind its flag.
import { conditionSummary } from "../../../designer/conditions";
import { branchTitle, type Step } from "../../../model";
import type { Json } from "../../../forms/core/json";
import {
  registerKind,
  type KindContext,
  type NdvContext,
  type RealExecutionSupport,
  type StepKindDescriptor,
} from "../registry";
import { actionForm, actionSettings } from "./forms/action-forms";
import { decisionTableForm, transformForm } from "./forms/data-forms";
import {
  failForm,
  loopForm,
  loopSummary,
  parallelForm,
  signalForm,
  signalSettings,
  switchForm,
  waitForm,
} from "./forms/flow-forms";
import { humanForm, humanSettings } from "./forms/human-form";
import { common, field, isRecord, schemaOf, specOf } from "./shared";

const uses = (step: Step) => String(step["uses"] ?? "");
/** Each case with its index; a malformed entry keeps its index so paths stay true. */
const cases = (step: Step): [number, Record<string, unknown>][] =>
  (Array.isArray(step["cases"]) ? step["cases"] : []).map(
    (branch: unknown, index): [number, Record<string, unknown>] => [
      index,
      isRecord(branch) ? branch : {},
    ],
  );
const branchNames = (step: Step): string[] =>
  isRecord(step["branches"]) ? Object.keys(step["branches"]) : [];

/** The platform runs a step test for at most five minutes. */
const STEP_TEST_TIMEOUT_SECONDS = 300;

/** An action step's step test: its resolved input and the connection bound to its slot. */
function actionRequest(
  ctx: NdvContext,
  kind: KindContext,
): ReturnType<RealExecutionSupport["request"]> {
  const step = ctx.step;
  const action = uses(step);
  if (!action) return { blocked: "Choose an action first." };
  const slot = typeof step["connection"] === "string" ? step["connection"] : "";
  const connections: Record<string, string> = {};
  if (slot) {
    const bound = ctx.connections.devBinding(slot);
    if (!bound)
      return { blocked: `Choose the connection for slot ${slot} first.` };
    connections[slot] = bound.revisionId;
  }
  let input: Json = {};
  if (step["with"] !== undefined) {
    const resolved = ctx.sample.resolvedInput();
    if (resolved.state !== "available")
      return {
        blocked: `${step.id} needs data from earlier steps. Execute previous steps first.`,
      };
    input = resolved.value;
  }
  const timeout = specOf(kind.actionContract(action))["timeoutSeconds"];
  return {
    body: {
      kind: "action",
      step_id: step.id,
      uses: action,
      input,
      connections,
      releases: {},
      timeout_seconds:
        typeof timeout === "number" && timeout > 0
          ? Math.min(Math.ceil(timeout), STEP_TEST_TIMEOUT_SECONDS)
          : STEP_TEST_TIMEOUT_SECONDS,
    },
  };
}

export const actionKind: StepKindDescriptor = {
  ...common("action", "app", "app"),
  fields: () => [
    {
      path: ["with"],
      label: "Input",
      expectedSchema: (step, ctx) =>
        schemaOf(specOf(ctx.actionContract(uses(step)))["inputSchema"]),
    },
  ],
  pinnable: true,
  form: actionForm,
  settings: actionSettings,
  real: { request: actionRequest },
};

export const decisionTableKind: StepKindDescriptor = {
  ...common("decisionTable", "rules", "data"),
  fields: () => [
    {
      path: ["with"],
      label: "Table input",
      expectedSchema: (step, ctx) =>
        schemaOf(specOf(ctx.tableContract(uses(step)))["inputSchema"]),
    },
  ],
  pinnable: false,
  form: decisionTableForm,
};

export const transformKind: StepKindDescriptor = {
  ...common("transform", "transform", "data"),
  fields: () => [field("Value", "value")],
  pinnable: false,
  form: transformForm,
};

export const switchKind: StepKindDescriptor = {
  ...common("switch", "decision", "flow"),
  fields: (step) => [
    ...cases(step).flatMap(([i]) => [
      field(`Case ${i + 1} condition`, "cases", i, "when"),
      field(`Case ${i + 1} output`, "cases", i, "output"),
    ]),
    field("Otherwise output", "default", "output"),
  ],
  containers: (step) => [
    ...cases(step).map(([i]) => ({
      path: ["cases", i, "steps"],
      layout: "lanes" as const,
      label: (owner: Step) => branchTitle(owner, `case ${i + 1}`),
    })),
    {
      path: ["default", "steps"],
      layout: "lanes" as const,
      label: (owner: Step) => branchTitle(owner, "default"),
    },
  ],
  outputs: (step, ctx) => [
    ...cases(step).map(([i, branch]) => ({
      id: `case:${i}`,
      label: conditionSummary(branch["when"], ctx.workflow),
      containerPath: ["cases", i, "steps"],
    })),
    { id: "default", label: "Otherwise", containerPath: ["default", "steps"] },
  ],
  pinnable: false,
  form: switchForm,
};

export const parallelKind: StepKindDescriptor = {
  ...common("parallel", "parallel", "flow"),
  fields: (step) =>
    branchNames(step).map((name) =>
      field(`${name} output`, "branches", name, "output"),
    ),
  containers: (step) =>
    branchNames(step).map((name) => ({
      path: ["branches", name, "steps"],
      layout: "lanes" as const,
      label: () => name,
    })),
  outputs: (step) =>
    branchNames(step).map((name) => ({
      id: `branch:${name}`,
      label: name,
      containerPath: ["branches", name, "steps"],
    })),
  pinnable: false,
  form: parallelForm,
};

/** A loop: its steps run once per item of a list, inside its frame. */
export const forEachKind: StepKindDescriptor = {
  ...common("forEach", "loop", "flow"),
  feature: "flow.forEach",
  summary: (step) => loopSummary(step),
  fields: () => [
    {
      path: ["items"],
      label: "Items",
      expectedSchema: () => ({ type: "array" }),
    },
    field("Result per item", "body", "output"),
  ],
  containers: () => [
    {
      path: ["body", "steps"],
      layout: "frame" as const,
      label: (owner: Step) => branchTitle(owner, "body"),
    },
  ],
  pinnable: false,
  form: loopForm,
};

export const waitKind: StepKindDescriptor = {
  ...common("wait", "wait", "wait"),
  fields: () => [],
  pinnable: false,
  form: waitForm,
};

export const signalKind: StepKindDescriptor = {
  ...common("signal", "signal", "wait"),
  fields: () => [],
  pinnable: false,
  form: signalForm,
  settings: signalSettings,
  script: "signal",
};

export const humanTaskKind: StepKindDescriptor = {
  ...common("humanTask", "human", "human"),
  fields: () => [
    { path: ["title"], label: "Title", templateCapable: true },
    field("Context", "context"),
  ],
  pinnable: false,
  form: humanForm,
  settings: humanSettings,
  script: "human",
};

export const failKind: StepKindDescriptor = {
  ...common("fail", "fail", "flow"),
  fields: () => [],
  outputs: () => [],
  outputSchema: () => null,
  pinnable: false,
  form: failForm,
};

export const builtInKinds: readonly StepKindDescriptor[] = [
  actionKind,
  decisionTableKind,
  transformKind,
  switchKind,
  parallelKind,
  waitKind,
  signalKind,
  humanTaskKind,
  failKind,
  forEachKind,
];

export function register(): void {
  for (const descriptor of builtInKinds) registerKind(descriptor);
}
