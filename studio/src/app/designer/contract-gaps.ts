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
// Derive visible setup gaps from the same bound-field rules as the input form.
import type { Step, Workflow } from "../model";
import { missingBound, formFields } from "../forms/core/form-model";
import { fieldsOf, type FieldInfo } from "../forms/core/resolve";
export interface ContractGap {
  label: string;
  field: "with" | "connection";
  dataPath: string[];
}
const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
export function contractGaps(
  step: Step,
  contract: unknown,
  workflow: Workflow,
): ContractGap[] {
  if (!["action", "decisionTable", "llm"].includes(step.kind)) return [];
  const spec = record(record(contract)["spec"]),
    gaps: ContractGap[] = [];
  if (step.kind !== "llm" && spec["inputSchema"]) {
    const schema = spec["inputSchema"],
      paths = new Map<string, string[]>();
    const visit = (
      fields: FieldInfo[],
      parent: string[],
      prefix: string,
      depth = 0,
    ) => {
      if (depth > 16) return;
      for (const field of fields) {
        const path = [...parent, field.key],
          label = prefix + field.label;
        paths.set(label, path);
        if (field.kind === "object")
          visit(
            fieldsOf(field.schema, field.context),
            path,
            label + " › ",
            depth + 1,
          );
      }
    };
    visit(formFields(schema), [], "");
    for (const label of missingBound(schema, step["with"]))
      gaps.push({
        label: "Needs: " + label,
        field: "with",
        dataPath: paths.get(label) ?? [],
      });
  }
  const requirement = record(spec["connection"]),
    connection = String(step["connection"] ?? "");
  const slot = record(record(workflow.spec["connections"])[connection]);
  if (
    (requirement["connector"] &&
      requirement["required"] !== false &&
      !connection) ||
    (connection && !slot["connector"])
  )
    gaps.push({
      label: "Needs a connection",
      field: "connection",
      dataPath: [],
    });
  else if (
    connection &&
    requirement["connector"] &&
    slot["connector"] !== requirement["connector"]
  )
    gaps.push({
      label: "Needs a compatible connection",
      field: "connection",
      dataPath: [],
    });
  return gaps;
}
