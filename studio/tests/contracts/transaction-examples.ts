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
// Compile-only: transaction records, in-doubt incidents and their case links
// as the platform serializes them, typed with Studio's contracts. The Vitest
// suite parses the same bodies with the platform's models
// (studio/tests/transaction-contracts.test.ts), so a field the platform
// drops, renames or never sends fails there.
import type { IncidentView } from "../../src/app/operate/incidents/incidents-model";
import type {
  ForbiddenSection,
  UnavailableSection,
} from "../../src/app/operate/overview/overview-model";
import type {
  RunSummary,
  RunSummaryQuery,
} from "../../src/app/operate/run-contracts";
import type {
  RunTransactionPage,
  TransactionsSection,
} from "../../src/app/operate/transactions/transaction-contracts";

export const ids = {
  parentRun: "4f2c1a9e-6b1d-4c2e-9f3a-1d2e3f4a5b6c",
  calledRun: "9a01c7d2-3e4f-4a5b-8c6d-7e8f9a0b1c2d",
  loopRun: "c3a85e62-0d1e-4f2a-b3c4-d5e6f7a8b9c0",
  settleTimeout: "6d18b3f0-1a2b-4c3d-8e4f-5a6b7c8d9e0f",
  undoStuck: "2b7e4c19-8d0a-4f1b-9c2d-3e4f5a6b7c8d",
  withheld: "7b3e9d10-2c4d-4e5f-a6b7-c8d9e0f1a2b3",
  decided: "5c90e1b7-4d5e-4f6a-8b7c-9d0e1f2a3b4c",
  loopInDoubt: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
  loopReconciled: "b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e",
  resolvingRun: "e8f7a6b5-c4d3-4e2f-9a1b-0c9d8e7f6a5b",
  activation: "0e1d2c3b-4a59-4687-b6c5-d4e3f2a1b0c9",
  definition: "1f2e3d4c-5b6a-4798-a7b6-c5d4e3f2a1b0",
  person: "3a4b5c6d-7e8f-4091-a2b3-c4d5e6f7a8b9",
  receipt: "8e9f0a1b-2c3d-4e4f-a5b6-c7d8e9f0a1b2",
};

/** A parent's settle timeout: it waits for the called run's undo, which is stuck. */
export const settleTimeout: IncidentView = {
  id: ids.settleTimeout,
  run_id: ids.parentRun,
  incident_key: "move:0",
  node_id: "move",
  generation: 0,
  origin_code: "WV-TXN-SETTLE-TIMEOUT",
  code: "WV-TXN-SETTLE-TIMEOUT",
  status: "active",
  revision: 1,
  actor_id: null,
  resolved_at: null,
  resolution: null,
  external_effects_may_continue: false,
  opened_at: "2026-11-22T10:31:40Z",
  updated_at: "2026-11-22T10:31:40Z",
  workflow: { name: "parent-move", version: "1.0.0" },
  run_status: "suspended",
  priority: "high",
  transaction: {
    instance_key: "move",
    state: "in_doubt",
    business_key: "T-1029",
    participants: [
      {
        node_id: "reserve",
        instance_key: "reserve",
        mode: "reserve",
        status: "done",
        effect: "none",
      },
      {
        node_id: "elsewhere",
        instance_key: "elsewhere",
        mode: "compensate",
        status: "dispatched",
        effect: "unknown",
        child_run_id: ids.calledRun,
      },
    ],
    pending: ["elsewhere"],
  },
  waits_on: [
    {
      id: ids.undoStuck,
      run_id: ids.calledRun,
      code: "WV-TXN-UNDO-STUCK",
      status: "active",
      node_id: "add",
      instance_key: "add#undo",
      workflow: { name: "child-move", version: "1.0.0" },
    },
  ],
};

/** The called run's undo stuck, held by its caller's settle timeout. */
export const undoStuck: IncidentView = {
  id: ids.undoStuck,
  run_id: ids.calledRun,
  incident_key: "add#undo:0",
  node_id: "add",
  generation: 0,
  origin_code: "WV-TXN-UNDO-STUCK",
  code: "WV-TXN-UNDO-STUCK",
  status: "active",
  revision: 2,
  actor_id: null,
  resolved_at: null,
  resolution: null,
  external_effects_may_continue: false,
  instance_key: "add#undo",
  opened_at: "2026-11-22T10:31:12Z",
  updated_at: "2026-11-22T10:31:12Z",
  workflow: { name: "child-move", version: "1.0.0" },
  run_status: "suspended",
  priority: "high",
  transaction: {
    instance_key: "move",
    state: "in_doubt",
    participants: [
      {
        node_id: "hold",
        instance_key: "hold",
        mode: "reserve",
        status: "done",
        effect: "none",
      },
      {
        node_id: "add",
        instance_key: "add",
        mode: "compensate",
        status: "undo_stuck",
        effect: "none",
        attempts: 3,
      },
    ],
    pending: [],
  },
  held_by: {
    id: ids.settleTimeout,
    run_id: ids.parentRun,
    code: "WV-TXN-SETTLE-TIMEOUT",
    status: "active",
    node_id: "move",
    workflow: { name: "parent-move", version: "1.0.0" },
  },
};

/** A settle timeout whose links were withheld: the platform sends no waits_on, only that more wait. */
const { waits_on: _links, ...lead } = settleTimeout;
export const withheldLinks: IncidentView = {
  ...lead,
  id: ids.withheld,
  waits_on_more: true,
};

/** The step that can't be undone didn't happen, as a triggered workflow recorded it. */
export const decided: IncidentView = {
  id: ids.decided,
  run_id: ids.loopRun,
  incident_key: "submit[5]:0",
  node_id: "submit",
  generation: 0,
  origin_code: "WV-TXN-PIVOT-UNKNOWN",
  code: "WV-TXN-PIVOT-UNKNOWN",
  status: "resolved",
  revision: 3,
  actor_id: ids.person,
  resolved_at: "2026-11-22T10:48:02Z",
  resolution: {
    receipt_id: ids.receipt,
    kind: "accept_reconciled_failure",
    reason: "The other system has no record of the submission.",
    output: null,
    evidence_reference: "ticket 4411",
    workflow_run_id: ids.resolvingRun,
  },
  external_effects_may_continue: false,
  instance_key: "submit[5]",
  opened_at: "2026-11-22T10:40:00Z",
  updated_at: "2026-11-22T10:48:02Z",
  workflow: { name: "move-between-systems", version: "1.2.0" },
  run_status: "running",
  priority: "high",
  transaction: {
    instance_key: "move[5]",
    state: "compensating",
    business_key: "T-1034",
    participants: [
      {
        node_id: "hold",
        instance_key: "hold[5]",
        mode: "reserve",
        status: "cancelling",
        effect: "none",
      },
      {
        node_id: "add",
        instance_key: "add[5]",
        mode: "compensate",
        status: "undoing",
        effect: "none",
      },
      {
        node_id: "submit",
        instance_key: "submit[5]",
        mode: "irreversible",
        status: "failed",
        effect: "none",
      },
    ],
    pending: [],
  },
};

/** A page of a run's transactions inside a loop: undone, in doubt, and reconciled by a person. */
export const transactionPage: RunTransactionPage = {
  items: [
    {
      instance_key: "move[2]",
      node_id: "move",
      iteration: [2],
      state: "compensated",
      business_key: "T-1051",
      cause: { code: "UNAVAILABLE", instance_key: "add[2]", effect: "unknown" },
      opened_at: "2026-11-22T10:31:00Z",
      ended_at: "2026-11-22T10:31:09Z",
      participants: [
        {
          node_id: "hold",
          instance_key: "hold[2]",
          mode: "reserve",
          status: "cancelled",
          effect: "none",
        },
        {
          node_id: "add",
          instance_key: "add[2]",
          mode: "compensate",
          status: "undone",
          effect: "unknown",
          attempts: 2,
        },
      ],
    },
    {
      instance_key: "move[3]",
      node_id: "move",
      iteration: [3],
      state: "in_doubt",
      // Outside ASCII: the platform reads it as UTF-8 whatever its locale.
      business_key: "T-1052-Müller",
      cause: {
        code: "UNAVAILABLE",
        instance_key: "elsewhere[3]",
        effect: "none",
      },
      opened_at: "2026-11-22T10:31:02Z",
      participants: [
        {
          node_id: "hold",
          instance_key: "hold[3]",
          mode: "reserve",
          status: "done",
          effect: "none",
        },
        {
          node_id: "elsewhere",
          instance_key: "elsewhere[3]",
          mode: "compensate",
          status: "undo_stuck",
          effect: "none",
          attempts: 3,
          child_run_id: ids.calledRun,
        },
      ],
      incident_id: ids.loopInDoubt,
    },
    {
      instance_key: "move[4]",
      node_id: "move",
      iteration: [4],
      state: "reconciled",
      opened_at: "2026-11-22T10:31:03Z",
      decided_at: "2026-11-22T10:31:04Z",
      ended_at: "2026-11-22T10:52:30Z",
      participants: [
        {
          node_id: "hold",
          instance_key: "hold[4]",
          mode: "reserve",
          status: "cancel_stuck",
          effect: "none",
          attempts: 10,
        },
      ],
      incident_id: ids.loopReconciled,
      resolution: {
        kind: "accept_reconciled_result",
        reason: "Released by hand in the source system.",
        evidence_reference: "ticket 4409",
        actor_id: ids.person,
        resolved_at: "2026-11-22T10:52:30Z",
        reconciled_as: "compensated",
      },
    },
    {
      instance_key: "move[5]",
      node_id: "move",
      iteration: [5],
      state: "completed",
      opened_at: "2026-11-22T10:31:04Z",
      decided_at: "2026-11-22T10:31:05Z",
      ended_at: "2026-11-22T10:40:03Z",
      participants: [
        {
          node_id: "hold",
          instance_key: "hold[5]",
          mode: "reserve",
          status: "confirmed",
          effect: "none",
          attempts: 2,
        },
      ],
      incident_id: ids.decided,
      resolution: {
        kind: "retry_safe",
        reason: "Confirm calls that fail with UNAVAILABLE are retried once.",
        resolved_at: "2026-11-22T10:40:00Z",
        workflow_run_id: ids.resolvingRun,
      },
    },
  ],
  next_cursor: "djI6bW92ZVs0XQ",
};

/** The environment summary's transactions section, and what a reader without run.read gets. */
export const transactionsSections: (
  | TransactionsSection
  | ForbiddenSection
  | UnavailableSection
)[] = [
  { in_doubt: 4, undoing: 1, oldest_undoing_seconds: 720 },
  { in_doubt: 0, undoing: 0 },
  { state: "forbidden" },
  { state: "unavailable" },
];

/** A run whose worst transaction is in doubt, and the Runs filter that finds it. */
export const runInDoubt: RunSummary = {
  id: ids.parentRun,
  workflow: {
    name: "parent-move",
    version: "1.0.0",
    definition_version_id: ids.definition,
  },
  activation_id: ids.activation,
  activation: { name: "parent-move", revision: 1 },
  status: "suspended",
  paused: false,
  test: false,
  origin: "manual",
  caller: null,
  retried_from_run_id: null,
  started_at: "2026-11-22T10:30:58Z",
  updated_at: "2026-11-22T10:31:40Z",
  ended_at: null,
  duration_ms: null,
  business_key: "T-1029",
  correlation_key: null,
  failed_step: null,
  active_incidents: 1,
  archived: false,
  transaction_state: "in_doubt",
};
export const byTransactionState: RunSummaryQuery = {
  transaction_state: ["in_doubt", "compensating"],
};
