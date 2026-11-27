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
// A run whose loop opened five transactions, as runs.transactions reports
// them: one completed plainly, one undone, one in doubt with a reservation
// still held, one a person marked as reconciled, and one that completed after
// its confirm was retried. Unit and browser tests read the same records.
import type { RunStatus, RunSummary } from "../src/app/operate/run-contracts";
import type {
  RunTransactionView,
  TransactionParticipantView,
} from "../src/app/operate/transactions/transaction-contracts";

export const loopRunId = "c3a85e62-0d1e-4f2a-b3c4-d5e6f7a8b9c0";
export const inDoubtIncident = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d";
export const reconciledIncident = "b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e";
export const retriedIncident = "d4e5f6a7-b8c9-4d0e-8f1a-2b3c4d5e6f7a";
export const person = "3a4b5c6d-7e8f-4091-a2b3-c4d5e6f7a8b9";
export const calledRun = "9a01c7d2-3e4f-4a5b-8c6d-7e8f9a0b1c2d";
/** The run of a triggered workflow whose resolve step retried a confirm. */
export const resolvingRun = "5c90e1b7-4d5e-4f6a-8b7c-9d0e1f2a3b4c";

const step = (
  node: string,
  index: number,
  mode: TransactionParticipantView["mode"],
  status: TransactionParticipantView["status"],
  extra: Partial<TransactionParticipantView> = {},
): TransactionParticipantView => ({
  node_id: node,
  instance_key: `${node}[${index}]`,
  mode,
  status,
  effect: "none",
  ...extra,
});

export const completed: RunTransactionView = {
  instance_key: "move[1]",
  node_id: "move",
  iteration: [1],
  state: "completed",
  business_key: "T-1050",
  opened_at: "2026-11-22T10:31:00Z",
  decided_at: "2026-11-22T10:31:01Z",
  ended_at: "2026-11-22T10:31:05Z",
  participants: [
    step("hold", 1, "reserve", "confirmed"),
    step("add", 1, "compensate", "done"),
    step("submit", 1, "irreversible", "done"),
  ],
};

export const undone: RunTransactionView = {
  instance_key: "move[2]",
  node_id: "move",
  iteration: [2],
  state: "compensated",
  business_key: "T-1051",
  cause: { code: "UNAVAILABLE", instance_key: "add[2]", effect: "none" },
  opened_at: "2026-11-22T10:31:02Z",
  ended_at: "2026-11-22T10:31:08Z",
  participants: [
    step("hold", 2, "reserve", "cancelled"),
    step("add", 2, "compensate", "failed"),
    step("submit", 2, "irreversible", "pending"),
  ],
};

/** The undo of add failed for good while hold was still reserved. */
export const inDoubt: RunTransactionView = {
  instance_key: "move[3]",
  node_id: "move",
  iteration: [3],
  state: "in_doubt",
  business_key: "T-1052",
  cause: { code: "REJECTED", instance_key: "submit[3]", effect: "none" },
  opened_at: "2026-11-22T10:31:02Z",
  participants: [
    step("hold", 3, "reserve", "done"),
    step("add", 3, "compensate", "undo_stuck", {
      attempts: 3,
      child_run_id: calledRun,
    }),
    step("submit", 3, "irreversible", "failed"),
  ],
  incident_id: inDoubtIncident,
};

export const reconciled: RunTransactionView = {
  instance_key: "move[4]",
  node_id: "move",
  iteration: [4],
  state: "reconciled",
  business_key: "T-1053",
  cause: { code: "REJECTED", instance_key: "submit[4]", effect: "none" },
  opened_at: "2026-11-22T10:31:02Z",
  ended_at: "2026-11-22T10:48:17Z",
  participants: [
    step("hold", 4, "reserve", "cancelled"),
    step("add", 4, "compensate", "undo_stuck", { attempts: 3 }),
    step("submit", 4, "irreversible", "failed"),
  ],
  incident_id: reconciledIncident,
  resolution: {
    kind: "accept_reconciled_result",
    reason:
      "Checked the target system: the quantity was removed by hand at 10:41.",
    evidence_reference: "Ticket OPS-2291 <b>line 88123</b>",
    actor_id: person,
    resolved_at: "2026-11-22T10:48:17Z",
    reconciled_as: "compensated",
  },
};

/** Its confirm failed until a person retried it. */
export const retried: RunTransactionView = {
  instance_key: "move[5]",
  node_id: "move",
  iteration: [5],
  state: "completed",
  business_key: "T-1054",
  opened_at: "2026-11-22T10:31:02Z",
  decided_at: "2026-11-22T10:31:04Z",
  ended_at: "2026-11-22T10:40:03Z",
  participants: [
    step("hold", 5, "reserve", "confirmed", { attempts: 2 }),
    step("add", 5, "compensate", "done"),
    step("submit", 5, "irreversible", "done"),
  ],
  incident_id: retriedIncident,
  resolution: {
    kind: "retry_safe",
    reason: "Confirm calls that fail with UNAVAILABLE are retried once.",
    resolved_at: "2026-11-22T10:40:00Z",
    workflow_run_id: resolvingRun,
  },
};

/** The loop's transactions in the platform's order. */
export const loop: RunTransactionView[] = [
  completed,
  undone,
  inDoubt,
  reconciled,
  retried,
];

/**
 * The open run as the Runs list shows it beside its detail, with its worst
 * transaction's state: the list names the run the detail shows.
 */
export function loopSummary(
  status: RunStatus,
  transactions: readonly RunTransactionView[],
): RunSummary {
  const worst =
    (["in_doubt", "open", "compensating", "completed"] as const).find((state) =>
      transactions.some((view) => view.state === state),
    ) ?? transactions[0]?.state;
  return {
    id: loopRunId,
    workflow: {
      name: "move-each",
      version: "1.0.0",
      definition_version_id: "6d7e8f90-a1b2-4c3d-8e4f-5a6b7c8d9e0f",
    },
    activation_id: "act",
    activation: { name: "move-each", revision: 1 },
    status,
    paused: false,
    test: false,
    origin: "manual",
    caller: null,
    retried_from_run_id: null,
    started_at: "2026-11-22T10:31:00Z",
    updated_at: "2026-11-22T10:48:17Z",
    ended_at: null,
    duration_ms: null,
    business_key: null,
    correlation_key: null,
    failed_step: null,
    active_incidents: worst === "in_doubt" ? 1 : 0,
    archived: false,
    ...(worst ? { transaction_state: worst } : {}),
  };
}
