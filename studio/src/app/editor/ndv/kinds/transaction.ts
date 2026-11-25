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
// The Transaction step: steps that all succeed, or whose effects are undone.
// Its own steps sit in its frame; later steps read its result, never theirs.
import { branchTitle, type Step } from "../../../model";
import { blocksOf } from "../../step-blocks";
import { newTransactionStep } from "../../transactions/group";
import { registerKind, type StepKindDescriptor } from "../registry";
import { field, inferredOutput } from "./shared";

/** "3 steps": the steps it holds itself; nothing while it is empty. */
function stepCount(step: Step): string {
  const own = blocksOf(step).find((block) => block.kind === "body")?.branch;
  const count = Array.isArray(own?.steps) ? own.steps.length : 0;
  return count ? `${count} ${count === 1 ? "step" : "steps"}` : "";
}

export const transactionKind: StepKindDescriptor = {
  kind: "transaction",
  feature: "flow.transaction",
  label: "Transaction",
  description: "Run steps together; if one fails, undo what's done",
  keywords: "transaction undo all or nothing together reserve confirm",
  icon: "transaction",
  role: "group",
  category: "flow",
  idPrefix: "transaction",
  create: newTransactionStep,
  fields: () => [
    field("Result", "output"),
    field("Business key", "businessKey"),
  ],
  containers: () => [
    {
      path: ["steps"],
      layout: "frame",
      label: (owner) => branchTitle(owner, "steps"),
    },
  ],
  summary: stepCount,
  outputSchema: inferredOutput,
  pinnable: false,
};

export function register(): void {
  registerKind(transactionKind);
}
