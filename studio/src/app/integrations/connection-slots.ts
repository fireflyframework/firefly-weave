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
// Slot edits and every nested action reference form one reversible document edit.
import { StructuredCanvasAdapter, type Step } from "../model";
import { validVersion } from "../forms/core/identifiers";
import type { WorkflowSlot } from "./slot-binding";

const namePattern = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

export function slotValidation(
  slot: WorkflowSlot,
  existing: readonly WorkflowSlot[],
  previous = "",
): string {
  if (!namePattern.test(slot.name))
    return "Use letters, numbers, dots, underscores or hyphens for the slot name.";
  if (
    existing.some((item) => item.name === slot.name && item.name !== previous)
  )
    return `A slot named ${slot.name} already exists. Choose another name.`;
  const [connector, version, extra] = slot.connector.split("@");
  if (
    !namePattern.test(connector ?? "") ||
    !validVersion(version ?? "") ||
    extra !== undefined
  )
    return "Enter a connector and version, such as weave-postgresql@1.0.0.";
  return "";
}

export function changeSlot(
  model: StructuredCanvasAdapter,
  previous: string,
  next: WorkflowSlot | null,
): void {
  const connections = (model.definition.spec["connections"] ?? {}) as Record<
    string,
    { connector: string; required?: boolean }
  >;
  if (previous && !(previous in connections))
    throw Error(
      "This connection slot no longer exists. Reopen the connection list.",
    );
  if (next) {
    const error = slotValidation(
      next,
      Object.entries(connections).map(([name, slot]) => ({
        name,
        connector: slot.connector,
        required: slot.required !== false,
      })),
      previous,
    );
    if (error) throw Error(error);
  }
  model.batch(() => {
    const document = structuredClone(model.definition);
    const updated: Record<string, unknown> = {};
    for (const [name, value] of Object.entries(connections)) {
      if (name !== previous) updated[name] = value;
      else if (next)
        updated[next.name] = {
          ...value,
          connector: next.connector,
          required: next.required,
        };
    }
    if (!previous && next)
      updated[next.name] = {
        connector: next.connector,
        required: next.required,
      };
    if (Object.keys(updated).length) document.spec["connections"] = updated;
    else delete document.spec["connections"];
    model.updateWorkflow(document);
    if (previous && previous !== next?.name) {
      for (const node of model.nodes()) {
        if (
          !["action", "llm"].includes(node.step.kind) ||
          node.step["connection"] !== previous
        )
          continue;
        const step: Step = structuredClone(node.step);
        if (next) step["connection"] = next.name;
        else delete step["connection"];
        model.update(step.id, JSON.stringify(step));
      }
    }
  });
}
