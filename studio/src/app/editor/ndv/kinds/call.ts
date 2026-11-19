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
// The Call a workflow step: a stacked tile named after the workflow version
// it calls, the called workflow's result as its output (or nothing when it
// doesn't wait), and the call form in step details.
import { callOutputSchema } from "../../calls/callee-interface";
import { registerKind, type StepKindDescriptor } from "../registry";
import { callFields, callForm, newCallStep } from "./forms/call-forms";
import { isRecord, schemaOf } from "./shared";

export const callKind: StepKindDescriptor = {
  kind: "callWorkflow",
  feature: "flow.callWorkflow",
  label: "Call a workflow",
  description: "Run another workflow and use its result",
  keywords: "sub-workflow subworkflow call execute run workflow",
  icon: "callWorkflow",
  role: "sub-workflow",
  category: "flow",
  idPrefix: "call-workflow",
  create: newCallStep,
  fields: callFields,
  /** "notify-customer 1.0.0"; the tile says on a line of its own when the call doesn't wait. */
  summary: (step) => {
    const uses = typeof step["uses"] === "string" ? step["uses"] : "";
    const at = uses.indexOf("@");
    return at > 0 ? `${uses.slice(0, at)} ${uses.slice(at + 1)}` : "";
  },
  outputSchema: (step, ctx) => {
    const uses = typeof step["uses"] === "string" ? step["uses"] : "";
    const contract = uses ? ctx.workflowContract(uses) : null;
    return callOutputSchema(
      step["mode"],
      step["onFailure"],
      schemaOf(isRecord(contract) ? contract["outputSchema"] : null),
    );
  },
  // A call that doesn't wait has no result to pin.
  pinnable: (step) => step["mode"] !== "detach",
  form: callForm,
};

export function register(): void {
  registerKind(callKind);
}
