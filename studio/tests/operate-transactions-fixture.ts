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
// The moment of tests/transaction-incidents-fixture.ts (10:54) as the Runs
// list and each run's detail report it: the four runs in doubt beside runs
// that are open, undoing, undone, completed, force ended and reconciled.
// transaction-contracts.test.ts parses these records with the platform's
// models.
import type { RunSummary } from "../src/app/operate/run-contracts";
import type {
  RunTransactionView,
  TransactionParticipantView,
} from "../src/app/operate/transactions/transaction-contracts";
import {
  completed,
  reconciled,
  retried,
  undone,
} from "./run-transactions-fixture";
import {
  called,
  caller,
  loop,
  runs,
  unknown,
} from "./transaction-incidents-fixture";

/** The runs of this moment that aren't in doubt. */
export const otherRuns = {
  /** Still going, its transaction open. */
  open: "4f2c1a9e-5d6e-4f70-8a91-b2c3d4e5f607",
  /** Going back for 12 minutes. */
  undoing: "8e3f5a20-6e7f-4081-9ba2-c3d4e5f60718",
  undone: "e2b4f6a8-7f80-4192-acb3-d4e5f6071829",
  completed: "1f9c3d57-8091-42a3-bdc4-e5f60718293a",
  plain: "8a2d5e61-91a2-43b4-8ed5-f60718293a4b",
  forceEnded: "3c7e1b09-a2b3-44c5-9fe6-0718293a4b5c",
  reconciled: "5b1e9c44-b3c4-45d6-a0f7-18293a4b5c6d",
} as const;

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
/** An incident's transaction as its run's detail lists it. */
const recordOf = (
  incident: Pick<typeof caller, "id" | "transaction">,
  facts: Pick<RunTransactionView, "opened_at"> & Partial<RunTransactionView>,
): RunTransactionView => {
  const { instance_key, state, business_key, participants } =
    incident.transaction!;
  return {
    instance_key,
    node_id: instance_key.replace(/\[\d+\]$/, ""),
    iteration: [],
    state,
    ...(business_key ? { business_key } : {}),
    participants,
    incident_id: incident.id,
    ...facts,
  };
};

/** The step that can't be undone may or may not have happened. */
export const unknownRecord = recordOf(unknown, {
  opened_at: "2026-11-22T10:45:02Z",
  cause: { code: "UNAVAILABLE", instance_key: "submit", effect: "unknown" },
});
/**
 * The caller was going back because its reservation was about to expire, and
 * the called step was still running at the deadline.
 */
export const callerRecord = recordOf(caller, {
  opened_at: "2026-11-22T10:33:00Z",
  cause: { code: "WV-TXN-HOLD-EXPIRED", instance_key: "move", effect: "none" },
});
export const calledRecord = recordOf(called, {
  opened_at: "2026-11-22T10:33:01Z",
  cause: { code: "UNAVAILABLE", instance_key: "check", effect: "none" },
});
/** The loop's third transaction, whose reservation couldn't be released. */
export const loopRecord = recordOf(loop, {
  iteration: [3],
  opened_at: "2026-11-22T10:31:02Z",
  cause: { code: "UNAVAILABLE", instance_key: "check[3]", effect: "none" },
});
export const openRecord: RunTransactionView = {
  instance_key: "move",
  node_id: "move",
  iteration: [],
  state: "open",
  business_key: "T-1035",
  opened_at: "2026-11-22T10:52:11Z",
  participants: [
    step("hold", "reserve", "done"),
    step("add", "compensate", "dispatched"),
    step("submit", "irreversible", "pending"),
  ],
};
export const undoingRecord: RunTransactionView = {
  instance_key: "move",
  node_id: "move",
  iteration: [],
  state: "compensating",
  business_key: "T-1036",
  cause: { code: "REJECTED", instance_key: "submit", effect: "none" },
  opened_at: "2026-11-22T10:42:00Z",
  participants: [
    step("hold", "reserve", "done"),
    step("add", "compensate", "undoing", { attempts: 4 }),
    step("submit", "irreversible", "failed"),
  ],
};

/** Each run's transactions, as runs.transactions answers them. */
export const transactions: Record<string, RunTransactionView[]> = {
  [runs.unknown]: [unknownRecord],
  [runs.caller]: [callerRecord],
  [runs.called]: [calledRecord],
  [runs.loop]: [completed, undone, loopRecord, reconciled, retried],
  [otherRuns.open]: [openRecord],
  [otherRuns.undoing]: [undoingRecord],
};

const action = (id: string, uses: string, undo: object = {}) => ({
  id,
  kind: "action",
  uses: `${uses}@1.0.0`,
  with: { object: { reference: { ref: "/input/moveId" } } },
  ...undo,
});
const move = {
  id: "move",
  kind: "transaction",
  businessKey: { ref: "/input/moveId" },
  steps: [
    action("hold", "source-system.hold-quantity", {
      confirm: { kind: "action", uses: "source-system.confirm-hold@1.0.0" },
      cancel: { kind: "action", uses: "source-system.release-hold@1.0.0" },
      holdSeconds: 600,
    }),
    action("add", "target-system.add-quantity", {
      compensate: {
        kind: "action",
        uses: "target-system.remove-quantity@1.0.0",
      },
    }),
    action("submit", "orders.submit-order", { irreversible: true }),
  ],
  output: { ref: "/steps/submit/output" },
};
const notify = action("notify", "notify.send-message");
const workflow = (name: string, version: string, steps: object[]) => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name, version },
  spec: {
    inputSchema: { type: "object" },
    outputSchema: {},
    steps,
    output: { literal: {} },
  },
});
/** The workflow each run runs, by its published version. */
export const workflows: Record<string, ReturnType<typeof workflow>> = {
  "6d7e8f90-a1b2-4c3d-8e4f-5a6b7c8d9e01": workflow(
    "move-between-systems",
    "3.0.0",
    [move, notify],
  ),
  "6d7e8f90-a1b2-4c3d-8e4f-5a6b7c8d9e02": workflow("add-quantity", "2.0.0", [
    { ...move, id: "apply", steps: move.steps.slice(0, 2) },
  ]),
  "6d7e8f90-a1b2-4c3d-8e4f-5a6b7c8d9e03": workflow("move-each", "1.0.0", [
    {
      id: "each",
      kind: "forEach",
      items: { ref: "/input/items" },
      concurrency: 1,
      body: { steps: [move] },
    },
  ]),
  "6d7e8f90-a1b2-4c3d-8e4f-5a6b7c8d9e04": workflow(
    "notify-on-change",
    "4.0.0",
    [notify],
  ),
};
const versionOf = (name: string) =>
  Object.keys(workflows).find((id) => workflows[id].metadata.name === name)!;

const run = (
  id: string,
  name: string,
  version: string,
  facts: Partial<RunSummary> & Pick<RunSummary, "status" | "started_at">,
): RunSummary => ({
  id,
  workflow: { name, version, definition_version_id: versionOf(name) },
  activation_id: "7e8f90a1-b2c3-4d4e-9f50-6b7c8d9e0f12",
  activation: { name, revision: 1 },
  paused: false,
  test: false,
  origin: "webhook",
  caller: null,
  retried_from_run_id: null,
  updated_at: "2026-11-22T10:53:00Z",
  ended_at: null,
  duration_ms: null,
  business_key: null,
  correlation_key: null,
  failed_step: null,
  active_incidents: 0,
  archived: false,
  ...facts,
});
const at = (time: string) => `2026-11-22T${time}Z`;
/** Started and over, with how long it took. */
const over = (from: string, to: string) => ({
  started_at: at(from),
  updated_at: at(to),
  ended_at: at(to),
  duration_ms: Date.parse(at(to)) - Date.parse(at(from)),
});

/** The Runs list of this moment, newest started first. */
export const runList: RunSummary[] = [
  run(otherRuns.open, "move-between-systems", "3.0.0", {
    status: "running",
    started_at: at("10:52:10"),
    business_key: "T-1035",
    transaction_state: "open",
  }),
  run(runs.unknown, "move-between-systems", "3.0.0", {
    status: "suspended",
    started_at: at("10:45:00"),
    origin: "schedule",
    business_key: "T-1034",
    active_incidents: 1,
    transaction_state: "in_doubt",
  }),
  run(otherRuns.undoing, "move-between-systems", "3.0.0", {
    status: "running",
    started_at: at("10:41:58"),
    business_key: "T-1036",
    transaction_state: "compensating",
  }),
  run(runs.called, "add-quantity", "2.0.0", {
    status: "suspended",
    started_at: at("10:33:01"),
    origin: "call",
    caller: { run_id: runs.caller, node_id: "add", instance_key: "" },
    business_key: "T-1029",
    active_incidents: 1,
    transaction_state: "in_doubt",
  }),
  run(runs.caller, "move-between-systems", "3.0.0", {
    status: "suspended",
    started_at: at("10:32:58"),
    business_key: "T-1029",
    active_incidents: 1,
    transaction_state: "in_doubt",
  }),
  run(runs.loop, "move-each", "1.0.0", {
    status: "suspended",
    started_at: at("10:31:00"),
    origin: "manual",
    active_incidents: 1,
    transaction_state: "in_doubt",
  }),
  run(otherRuns.undone, "move-between-systems", "3.0.0", {
    ...over("10:28:00", "10:28:06"),
    status: "failed",
    failed_step: {
      node_id: "move",
      instance_key: "",
      error_code: "WV-TXN-ROLLED-BACK",
    },
    business_key: "T-1033",
    transaction_state: "compensated",
  }),
  run(otherRuns.completed, "move-between-systems", "3.0.0", {
    ...over("10:22:00", "10:22:04"),
    status: "succeeded",
    business_key: "T-1032",
    transaction_state: "completed",
  }),
  run(otherRuns.plain, "notify-on-change", "4.0.0", {
    ...over("10:13:00", "10:13:01"),
    status: "succeeded",
    origin: "schedule",
  }),
  run(otherRuns.forceEnded, "move-between-systems", "3.0.0", {
    ...over("08:54:00", "08:55:00"),
    status: "cancelled",
    origin: "manual",
    business_key: "T-1027",
    transaction_state: "force_ended",
  }),
  run(otherRuns.reconciled, "move-between-systems", "3.0.0", {
    ...over("07:54:00", "08:12:00"),
    status: "succeeded",
    business_key: "T-1024",
    transaction_state: "reconciled",
  }),
];
