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
// The incidents of one moment, as incidents.list reports them: a step that
// can't be undone with an unknown outcome, a caller's settle timeout and the
// called run's stuck undo it waits for, a reservation that couldn't be
// released, a run held whole, and incidents with no transaction. Unit and
// browser tests read the same records; transaction-contracts.test.ts parses
// them with the platform's models.
import type { IncidentView } from "../src/app/operate/incidents/incidents-model";
import type { TransactionParticipantView } from "../src/app/operate/transactions/transaction-contracts";

/** The moment every time below is read at. */
export const NOW = "2026-11-22T10:54:00Z";

export const runs = {
  unknown: "7b3e9d10-2c4d-4e5f-a6b7-c8d9e0f1a2b3",
  caller: "6d18b3f0-1a2b-4c3d-8e4f-5a6b7c8d9e0f",
  called: "9a01c7d2-3e4f-4a5b-8c6d-7e8f9a0b1c2d",
  loop: "c3a85e62-0d1e-4f2a-b3c4-d5e6f7a8b9c0",
  held: "0e5a7c13-9b8a-4c7d-a6e5-f4d3c2b1a0f9",
  resolving: "5c90e1b7-4d5e-4f6a-8b7c-9d0e1f2a3b4c",
};
export const ids = {
  unknown: "a1000000-0000-4000-8000-000000000001",
  caller: "a1000000-0000-4000-8000-000000000002",
  called: "a1000000-0000-4000-8000-000000000003",
  loop: "a1000000-0000-4000-8000-000000000004",
  held: "a1000000-0000-4000-8000-000000000005",
};

const step = (
  node: string,
  mode: TransactionParticipantView["mode"],
  status: TransactionParticipantView["status"],
  extra: Partial<TransactionParticipantView> = {},
): TransactionParticipantView => ({
  node_id: node,
  instance_key: node,
  mode,
  status,
  effect: "none",
  ...extra,
});

const incident = (
  view: Pick<IncidentView, "id" | "run_id" | "code" | "node_id" | "opened_at"> &
    Partial<IncidentView>,
): IncidentView => ({
  incident_key: `${view.node_id ?? "run"}:0`,
  generation: 0,
  origin_code: view.code,
  status: "active",
  revision: 1,
  actor_id: null,
  resolved_at: null,
  resolution: null,
  external_effects_may_continue: false,
  updated_at: view.opened_at,
  run_status: "suspended",
  ...view,
});

/** The step that can't be undone may or may not have happened. */
export const unknown = incident({
  id: ids.unknown,
  run_id: runs.unknown,
  code: "WV-TXN-PIVOT-UNKNOWN",
  node_id: "submit",
  opened_at: "2026-11-22T10:48:00Z",
  workflow: { name: "move-between-systems", version: "3.0.0" },
  priority: "high",
  transaction: {
    instance_key: "move",
    state: "in_doubt",
    business_key: "T-1034",
    participants: [
      step("hold", "reserve", "done"),
      step("add", "compensate", "done"),
      step("submit", "irreversible", "unknown", { effect: "unknown" }),
    ],
    pending: [],
  },
});

/** A caller's settle timeout: it waits for the called run's undo, which is stuck. */
export const caller = incident({
  id: ids.caller,
  run_id: runs.caller,
  code: "WV-TXN-SETTLE-TIMEOUT",
  node_id: "move",
  opened_at: "2026-11-22T10:36:00Z",
  workflow: { name: "move-between-systems", version: "3.0.0" },
  priority: "high",
  transaction: {
    instance_key: "move",
    state: "in_doubt",
    business_key: "T-1029",
    participants: [
      step("hold", "reserve", "done"),
      step("add", "compensate", "dispatched", {
        effect: "unknown",
        child_run_id: runs.called,
      }),
      step("submit", "irreversible", "pending"),
    ],
    pending: ["add"],
  },
  waits_on: [
    {
      id: ids.called,
      run_id: runs.called,
      code: "WV-TXN-UNDO-STUCK",
      status: "active",
      node_id: "add",
      instance_key: "add#undo",
      workflow: { name: "add-quantity", version: "2.0.0" },
    },
  ],
});

/** The called run's undo stuck, held by its caller's settle timeout. */
export const called = incident({
  id: ids.called,
  run_id: runs.called,
  code: "WV-TXN-UNDO-STUCK",
  // The failure that put it in doubt: the step's own code.
  origin_code: "UNAVAILABLE",
  node_id: "add",
  instance_key: "add#undo",
  incident_key: "add#undo:0",
  opened_at: "2026-11-22T10:33:00Z",
  workflow: { name: "add-quantity", version: "2.0.0" },
  priority: "high",
  transaction: {
    instance_key: "apply",
    state: "in_doubt",
    business_key: "T-1029",
    participants: [
      step("hold", "reserve", "done"),
      step("add", "compensate", "undo_stuck", { attempts: 3 }),
    ],
    pending: [],
  },
  held_by: {
    id: ids.caller,
    run_id: runs.caller,
    code: "WV-TXN-SETTLE-TIMEOUT",
    status: "active",
    node_id: "move",
    workflow: { name: "move-between-systems", version: "3.0.0" },
  },
});

/** A reservation in a loop that couldn't be released. */
export const loop = incident({
  id: ids.loop,
  run_id: runs.loop,
  code: "WV-TXN-CANCEL-STUCK",
  node_id: "hold",
  instance_key: "hold[3]#cancel",
  incident_key: "hold[3]#cancel:0",
  opened_at: "2026-11-22T10:31:00Z",
  workflow: { name: "move-each", version: "1.0.0" },
  priority: "high",
  transaction: {
    instance_key: "move[3]",
    state: "in_doubt",
    business_key: "T-1052",
    participants: [
      step("hold", "reserve", "cancel_stuck", {
        instance_key: "hold[3]",
        attempts: 10,
      }),
    ],
    pending: [],
  },
});

/**
 * A run that grew too large: all its transactions are held behind one
 * incident, which names no transaction and no step of the workflow.
 */
export const held = incident({
  id: ids.held,
  run_id: runs.held,
  code: "WV-TXN-RUN-OVERSIZED",
  node_id: "@transactions",
  incident_key: "@transactions:0",
  origin_code: "WV-RUNTIME-CAPACITY",
  opened_at: "2026-11-22T10:50:00Z",
  workflow: { name: "move-each", version: "1.0.0" },
  priority: "high",
});

/** The High priority band, newest opened first. */
export const highPriority: IncidentView[] = [unknown, caller, called, loop];

/** Incidents with no transaction, newest opened first. */
export const others: IncidentView[] = (
  [
    ["WV-TASK-AMBIGUOUS", "send", "10:51", "notify-on-change"],
    ["WV-TASK-FAILED", "fetch", "10:43", "reference-sync"],
    ["WV-TASK-LOST", "send", "10:20", "notify-on-change"],
    ["WV-TASK-AMBIGUOUS", "write", "09:54", "reference-sync"],
  ] as const
).map(([code, node, opened, workflow], index) =>
  incident({
    id: `b1000000-0000-4000-8000-00000000000${index + 1}`,
    run_id: `8a2d5e61-0000-4000-8000-00000000000${index + 1}`,
    code,
    node_id: node,
    opened_at: `2026-11-22T${opened}:00Z`,
    workflow: { name: workflow, version: "4.0.0" },
    run_status: "waiting",
  }),
);
