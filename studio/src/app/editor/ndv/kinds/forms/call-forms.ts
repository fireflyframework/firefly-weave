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
// A step that calls another workflow: which workflow version, its input in
// rows from the called workflow's input, whether this workflow waits for the
// result, and what a failed call does. If it fails exists only for a call
// that waits: choosing Start it and continue hides it, and step details clear
// its choice in the same change, so the compiler's CALL_MODE never fires.
import {
  describeField,
  fieldsOf,
  resolveSchema,
  type ResolveOptions,
} from "../../../../forms/core/resolve";
import { allowsCaller } from "../../../calls/callable-catalog";
import type { Step } from "../../../../model";
import {
  isShownField,
  paramFromField,
  paramsFromSchema,
} from "../../params/schema-params";
import type {
  Choice,
  FieldAction,
  FieldSpec,
  FormSpec,
  KindContext,
  NdvContext,
  ParamSpec,
} from "../../registry";
import { isRecord, schemaOf } from "../shared";
import { wholeMapping } from "./shared";

/** The language's limits on a call's business key (compiler/calls.py BUSINESS_KEY_SCHEMA). */
const BUSINESS_KEY_SCHEMA = { type: "string", minLength: 1, maxLength: 200 };

/** A new call step: it waits for the result and sends an empty input until a workflow is chosen. */
export const newCallStep = (id: string): Step => ({
  id,
  // The canvas model's Kind lists the kinds it creates by itself; a call
  // step comes from its own descriptor.
  kind: "callWorkflow" as Step["kind"],
  mode: "wait",
  with: { literal: {} },
});

const calleeOf = (step: Step) => {
  const uses = typeof step["uses"] === "string" ? step["uses"] : "";
  const at = uses.indexOf("@");
  return { uses, name: at > 0 ? uses.slice(0, at) : "" };
};
/** What step details know of the called version: its contract as `workflowContract` gives it. */
function contractOf(
  step: Step,
  ctx: KindContext,
): Record<string, unknown> | null {
  const { uses } = calleeOf(step);
  const contract = uses ? ctx.workflowContract(uses) : null;
  return isRecord(contract) ? contract : null;
}
/**
 * The called workflow's input schema and where its references resolve, or
 * null when Studio can't read it (unknown, or a reference it can't follow).
 */
function calleeInput(
  step: Step,
  ctx: KindContext,
): { schema: Record<string, unknown>; resolve: ResolveOptions } | null {
  const contract = contractOf(step, ctx);
  const schema = schemaOf(contract?.["inputSchema"]);
  if (!schema) return null;
  const bundle = isRecord(contract?.["schemas"]) ? contract["schemas"] : {};
  const resolve: ResolveOptions = { root: schema, bundle };
  const unresolved =
    resolveSchema(schema, resolve).unresolved.length > 0 ||
    fieldsOf(schema, resolve).some((field) => field.unresolved.length > 0);
  return unresolved ? null : { schema, resolve };
}

/** The step's expression fields: its input and its business key. */
export const callFields = (): FieldSpec[] => [
  {
    path: ["with"],
    label: "Input",
    expectedSchema: (step, ctx) => calleeInput(step, ctx)?.schema ?? null,
  },
  {
    path: ["businessKey"],
    label: "Business key",
    templateCapable: true,
    expectedSchema: () => BUSINESS_KEY_SCHEMA,
  },
];

/**
 * The versions this workflow may call, from step details' catalog port. One
 * function for every form, so the field keeps its list while the form is
 * drawn again.
 */
const workflowChoices = (ndv: NdvContext) =>
  ndv.catalog
    .workflows({ callableBy: ndv.workflow.metadata.name })
    .then((rows) =>
      rows.map(
        (row): Choice => ({
          value: row.uses,
          label: row.title,
          ...(row.group ? { group: row.group } : {}),
          ...(row.heading ? { heading: row.heading } : {}),
          ...(row.description ? { description: row.description } : {}),
          ...(row.note ? { note: row.note } : {}),
          ...(row.aside ? { aside: row.aside } : {}),
          ...(row.disabled ? { disabled: row.disabled } : {}),
        }),
      ),
    );

/** Input: rows from the called workflow's input, free-form JSON when Studio can't read it. */
function inputParams(step: Step, ctx: KindContext, name: string): FormSpec {
  const called = calleeInput(step, ctx);
  if (!called)
    return {
      fields: [
        {
          id: "input",
          path: ["with"],
          type: "json",
          label: "Input",
          mapping: "both",
          hint: `Studio can't read ${name}'s input.`,
        },
      ],
    };
  const { schema: input, resolve } = called;
  const info = describeField("input", input, { ...resolve, required: true });
  if (!isShownField(info)) return { fields: [] };
  if (!fieldsOf(input, resolve).length)
    return {
      fields: [{ ...paramFromField(info, ["with"], "", 0), label: "Input" }],
    };
  const rows = paramsFromSchema(input, ["with"], {
    idPrefix: "input.",
    resolve,
  });
  if (wholeMapping(step["with"]))
    return {
      fields: [
        {
          id: "input",
          path: ["with"],
          type: "fields",
          label: "Input",
          mapping: "both",
          hint: "The whole input is mapped. Switch to Fixed to fill it field by field.",
          children: () => rows.fields,
        },
      ],
    };
  return {
    fields: [
      {
        id: "input",
        path: ["with"],
        type: "fields",
        label: "Input",
        hint: `From ${name}'s input`,
        children: () => [...rows.fields, ...(rows.options ?? [])],
      },
    ],
  };
}

/**
 * "In this project · callable by order-intake" once the version is known and
 * allows this workflow; why it can't be found, or why the project's versions
 * are missing, before.
 */
function workflowHint(step: Step, ctx: KindContext): Pick<ParamSpec, "hint"> {
  const { uses } = calleeOf(step);
  const contract = contractOf(step, ctx);
  if (!contract) {
    const note = ctx.callNote?.(uses || undefined);
    return note ? { hint: note } : {};
  }
  const caller = ctx.workflow.metadata.name;
  if (!caller || !allowsCaller(contract["callable"], caller)) return {};
  const where =
    contract["source"] === "project"
      ? "In this project"
      : contract["source"] === "local"
        ? "On this computer"
        : null;
  return where ? { hint: `${where} · callable by ${caller}` } : {};
}

/** Open workflow, and Choose another while the chosen version can't be found here. */
function workflowActions(step: Step, ctx: KindContext): FieldAction[] {
  const { uses } = calleeOf(step);
  if (!uses) return [];
  const open: FieldAction = {
    id: "open-workflow",
    label: "Open workflow",
    icon: "external",
    run: (ndv) => ndv.openWorkflow?.(uses),
  };
  if (contractOf(step, ctx)) return [open];
  return [
    open,
    {
      id: "choose-another",
      label: "Choose another",
      icon: "refresh",
      run: (ndv) =>
        ndv.edit(
          [{ path: ["uses"], value: undefined }],
          "Choose another workflow",
        ),
    },
  ];
}

/** The Parameters tab of a step that calls a workflow. */
export function callForm(step: Step, ctx: KindContext): FormSpec {
  const { uses, name } = calleeOf(step);
  const detached = step["mode"] === "detach";
  const workflow: ParamSpec = {
    id: "workflow",
    path: ["uses"],
    type: "resource",
    label: "Workflow",
    required: true,
    requiredText: "Choose a workflow to call.",
    placeholder: "notify-customer@1.0.0",
    choices: workflowChoices,
    actions: workflowActions(step, ctx),
    actionsBeside: true,
    ...workflowHint(step, ctx),
  };
  const mode: ParamSpec = {
    id: "mode",
    path: ["mode"],
    type: "select",
    label: "When it runs",
    display: "segments",
    required: true,
    default: "wait",
    whenRemoved: "default",
    choices: [
      { value: "wait", label: "Wait for its result" },
      { value: "detach", label: "Start it and continue" },
    ],
    ...(name
      ? {
          hint: detached
            ? `${name} runs on its own; this workflow doesn't wait and reads no result.`
            : `This workflow waits until ${name} finishes, then reads its result.`,
        }
      : {}),
  };
  // No default: a stored "stop" must be cleared too when the call stops
  // waiting, and clear-on-hide keeps a value equal to its default.
  const onFailure: ParamSpec = {
    id: "onFailure",
    path: ["onFailure"],
    type: "select",
    label: "If it fails",
    display: "segments",
    choices: [
      { value: "stop", label: "Stop this workflow" },
      { value: "continue", label: "Continue with the result status" },
    ],
    showWhen: (current) => current["mode"] !== "detach",
    ...(step["onFailure"] === undefined
      ? { hint: "Without a choice, a failed call stops this workflow." }
      : {}),
  };
  return {
    fields: [
      workflow,
      ...(uses && name ? inputParams(step, ctx, name).fields : []),
      mode,
      onFailure,
    ],
    options: [
      {
        id: "businessKey",
        path: ["businessKey"],
        type: "text",
        label: "Business key",
        mapping: "both",
        templateCapable: true,
        maxLength: 200,
        hint: "Your own reference for the run it starts, such as an order number.",
      },
    ],
  };
}
