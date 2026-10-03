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
// Which workflow connection slot an inserted action uses (WP-19/WP-20). Pure:
// the editor applies the plan, declaring the slot and inserting the step as
// one undo step. A slot only names a connection; environments bind it to a
// real connection revision when a version is activated.

/** A slot declared in the workflow's spec.connections. */
export interface WorkflowSlot {
  name: string;
  connector: string;
  required: boolean;
}
/** An Action's `spec.connection`: the connector it needs. */
export interface SlotRequirement {
  connector: string;
  required?: boolean;
}
/** What the editor does: declare `add` first, then bind `connection`. */
export interface SlotPlan {
  /** The slot the step uses; absent when the person must choose. */
  connection?: string;
  add?: { name: string; connector: string; required: boolean };
}

/**
 * Slots that satisfy a requirement, as the compiler checks them: the same
 * connector, and a required connection never uses an optional slot.
 */
export function compatibleSlots(
  requirement: SlotRequirement,
  slots: readonly WorkflowSlot[],
): WorkflowSlot[] {
  return slots.filter(
    (slot) =>
      slot.connector === requirement.connector &&
      (requirement.required === false || slot.required),
  );
}

/** A valid slot name from `base` that isn't in `used` (crm, crm-2, …). */
export function slotName(base: string, used: Iterable<string>): string {
  const clean =
    base.replace(/[^A-Za-z0-9_.-]/g, "-").replace(/^[^A-Za-z0-9]+/, "") ||
    "connection";
  const taken = new Set(used);
  let name = clean;
  for (let n = 2; taken.has(name); n++) name = `${clean}-${n}`;
  return name;
}

/**
 * The slot for an inserted action. With `preferred` (the API action
 * builder's name for the API), that slot is reused when it fits and declared
 * otherwise. Without it, the only compatible slot is bound; with none, a
 * slot named after the connector is declared; with several, the person
 * chooses in the inspector.
 */
export function planSlot(
  requirement: SlotRequirement | null,
  slots: readonly WorkflowSlot[],
  preferred?: string,
): SlotPlan {
  if (!requirement) return {};
  const fitting = compatibleSlots(requirement, slots);
  const declare = (base: string): SlotPlan => {
    const name = slotName(
      base,
      slots.map((slot) => slot.name),
    );
    return {
      connection: name,
      add: {
        name,
        connector: requirement.connector,
        required: requirement.required !== false,
      },
    };
  };
  if (preferred) {
    if (fitting.some((slot) => slot.name === preferred))
      return { connection: preferred };
    return declare(preferred);
  }
  if (fitting.length === 1) return { connection: fitting[0].name };
  if (fitting.length) return {};
  return declare(requirement.connector.split("@")[0]);
}

/** spec.connections with one more slot; existing slots are kept as they are. */
export function declareSlot(
  connections: Record<string, unknown> | undefined,
  add: NonNullable<SlotPlan["add"]>,
): Record<string, unknown> {
  return {
    ...(connections ?? {}),
    [add.name]: { connector: add.connector, required: add.required },
  };
}
