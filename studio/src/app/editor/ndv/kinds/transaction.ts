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
import { branchTitle } from "../../../model";
import { newTransactionStep, stepCount } from "../../transactions/group";
import { registerKind, type StepKindDescriptor } from "../registry";
import {
  transactionFields,
  transactionForm,
  transactionSettings,
} from "./forms/transaction-forms";
import { inferredOutput } from "./shared";

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
  fields: transactionFields,
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
  form: transactionForm,
  settings: transactionSettings,
};

export function register(): void {
  registerKind(transactionKind);
}
