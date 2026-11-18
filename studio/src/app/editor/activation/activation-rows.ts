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
// The activation dialog's first two steps, as plain data: whether the
// platform runs the language features the workflow uses, and one connection
// row per slot the activation needs (the workflow's slots, each AI step's
// connection, each agent tool's slots), prefilled from the dev binding this
// computer tested while it can still be bound, else from the only
// connection the person can bind.
import { relativeTime } from "../../format";

/** Language features in words, as people know them from the steps that use them. */
const FEATURE_WORDS: Record<string, string> = {
  "ai.agent": "AI agents",
  "ai.memory": "agent memory",
  "flow.callWorkflow": "calls to other workflows",
  "flow.eventTrigger": "platform event triggers",
  "flow.forEach": "loops",
  "flow.onError": "error paths",
  "flow.transaction": "transactions",
  "text.concat": "text templates",
  "text.join": "joined text",
};

/** The stop message for the first feature the workflow uses that the platform doesn't run, or null. */
export function missingFeatures(
  workflowFeatures: readonly string[],
  platformFeatures: readonly string[],
): string | null {
  const supported = new Set(platformFeatures);
  const missing = workflowFeatures.find((feature) => !supported.has(feature));
  if (missing === undefined) return null;
  const words = FEATURE_WORDS[missing];
  return `This platform doesn't run ${words ? `${words} yet (${missing})` : `${missing} yet`}. Update the platform first.`;
}

/** A connection slot the activation needs. */
export interface SlotNeed {
  /** The slot name the activation binds (`connection_revision_ids` key). */
  name: string;
  /** `name@version` of the connector the slot uses. */
  connector: string;
  required: boolean;
  /** The step that needs it, for AI steps and agent tools. */
  step?: string;
  /** The agent tool that needs it. */
  tool?: string;
}
/** A connection revision the person can bind (`bindable_connections.list`). */
export interface BindableChoice {
  connection_id: string;
  revision_id: string;
  name: string;
  connector: string;
  label: string;
  enabled: boolean;
}
/** A connection this computer bound to a slot for local runs. */
export interface DevBinding {
  slot: string;
  revisionId: string;
  /** When a step test last used it successfully. */
  testedAt?: string;
}
export interface ConnectionNeeds {
  /** The environment's name, as messages say it. */
  environment: string;
  slots: SlotNeed[];
  aiSteps: SlotNeed[];
  agentToolSlots: SlotNeed[];
  /** What the person can bind here, or "forbidden" when they may not bind connections. */
  bindable: BindableChoice[] | "forbidden";
  devBindings: DevBinding[];
  now?: Date;
}

export type SlotSource = "workflow" | "ai-step" | "agent-tool";
export interface ConnectionRow {
  slot: string;
  connector: string;
  required: boolean;
  source: SlotSource;
  step?: string;
  tool?: string;
  candidates: BindableChoice[];
  /** The revision Studio chose for the person. */
  chosen?: string;
  chosenBy?: "dev-binding" | "only-candidate";
  /** "Tested 2 min ago", for a dev binding a step test used. */
  tested?: string;
  /** Why the row can't be filled here. */
  message?: string;
}

const connectorWords = (connector: string) => connector.replace("@", " ");

/** One row per slot, the workflow's first; a slot several steps need appears once. */
export function connectionRows(needs: ConnectionNeeds): ConnectionRow[] {
  const rows: ConnectionRow[] = [];
  const bySlot = new Map<string, ConnectionRow>();
  const sources: [SlotSource, SlotNeed[]][] = [
    ["workflow", needs.slots],
    ["ai-step", needs.aiSteps],
    ["agent-tool", needs.agentToolSlots],
  ];
  for (const [source, slots] of sources)
    for (const slot of slots) {
      const known = bySlot.get(slot.name);
      if (known) {
        known.required ||= slot.required;
        continue;
      }
      const row: ConnectionRow = {
        slot: slot.name,
        connector: slot.connector,
        required: slot.required,
        source,
        ...(slot.step ? { step: slot.step } : {}),
        ...(slot.tool ? { tool: slot.tool } : {}),
        candidates: [],
      };
      bySlot.set(slot.name, row);
      rows.push(row);
    }

  for (const row of rows) {
    if (needs.bindable === "forbidden") {
      row.message = `You can't bind connections in ${needs.environment}. Ask an administrator for the connection binder role.`;
      continue;
    }
    row.candidates = needs.bindable.filter(
      (choice) => choice.enabled && choice.connector === row.connector,
    );
    if (!row.candidates.length) {
      row.message = `No connection for ${connectorWords(row.connector)} in ${needs.environment}. Create one in Connections.`;
      continue;
    }
    const dev = needs.devBindings.find((binding) => binding.slot === row.slot);
    const bound =
      dev &&
      row.candidates.some((choice) => choice.revision_id === dev.revisionId);
    if (dev && bound) {
      row.chosen = dev.revisionId;
      row.chosenBy = "dev-binding";
      if (dev.testedAt)
        row.tested = `Tested ${relativeTime(dev.testedAt, needs.now)}`;
    } else if (row.candidates.length === 1) {
      row.chosen = row.candidates[0].revision_id;
      row.chosenBy = "only-candidate";
    }
  }
  return rows;
}

/** "3 chosen for you · Review", or null when Studio chose none. */
export function chosenSummary(rows: ConnectionRow[]): string | null {
  const chosen = rows.filter((row) => row.chosenBy).length;
  return chosen ? `${chosen} chosen for you · Review` : null;
}
