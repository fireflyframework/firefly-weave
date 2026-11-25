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
// A run's transactions as the platform reports them (runs.transactions) and
// the environment summary's transactions section. Mirrors
// src/firefly_weave/contracts/run_views.py and operations_summary.py; every
// optional key is left out by the platform while it is empty. Pure.
import type { StudioApi } from "../../api";
import type {
  ParticipantStatus,
  TransactionState,
  UndoMode,
} from "../../editor/transactions/transaction-words";
import type { ResolutionKind } from "../incidents/incidents-model";

/** One step of a transaction: its keys, how it is undone and where it stands; never a value. */
export interface TransactionParticipantView {
  node_id: string;
  instance_key: string;
  mode: UndoMode;
  status: ParticipantStatus;
  effect: "none" | "unknown";
  /** Attempts of its latest phase, when any ran. */
  attempts?: number;
  /** A called step: the run of its latest call, its undo, release or confirm call first. */
  child_run_id?: string;
}
export interface RunTransactionView {
  instance_key: string;
  node_id: string;
  /** Loop indexes of the instance key, outermost first. */
  iteration: number[];
  state: TransactionState;
  business_key?: string;
  /** Why it left its normal path: the code, the step that caused it, and whether it may have changed data. */
  cause?: { code: string; instance_key: string; effect: "none" | "unknown" };
  opened_at: string;
  decided_at?: string;
  ended_at?: string;
  participants: TransactionParticipantView[];
  /** Its in-doubt incident. */
  incident_id?: string;
  /** How a person, or a workflow run's resolve step, resolved that incident; never the values reconciled. */
  resolution?: {
    kind: ResolutionKind;
    reason: string;
    evidence_reference?: string;
    actor_id?: string;
    resolved_at?: string;
    reconciled_as?: "completed" | "compensated";
    /** The triggered workflow's run whose resolve step applied it. */
    workflow_run_id?: string;
  };
}
export interface RunTransactionPage {
  items: RunTransactionView[];
  next_cursor: string | null;
}
/** Transactions waiting for a person and being undone in the environment; test runs left out. */
export interface TransactionsSection {
  in_doubt: number;
  undoing: number;
  /** Seconds since the oldest transaction still undoing opened; left out while none is undoing. */
  oldest_undoing_seconds?: number;
}

/** The code of a run held whole: one incident stands for all its transactions. */
export const HELD_RUN = "WV-TXN-RUN-OVERSIZED";
/** The states of a transaction that hasn't settled: canceling its run undoes it first. */
export const UNSETTLED: readonly TransactionState[] = [
  "open",
  "deciding",
  "committing",
  "compensating",
  "in_doubt",
];

/** One page of a run's transactions, oldest first: 50 from the start or after `cursor`. */
export async function readRunTransactions(
  api: Pick<StudioApi, "request">,
  base: string,
  runId: string,
  cursor: string | null,
): Promise<{ views: RunTransactionView[]; next: string | null }> {
  const query = new URLSearchParams({ limit: "50" });
  if (cursor) query.set("cursor", cursor);
  const page = await api.request<RunTransactionPage>(
    `${base}/runs/${encodeURIComponent(runId)}/transactions?${query}`,
  );
  return { views: page.items, next: page.next_cursor };
}

/** A step as a sentence names it: "add#undo" reads "add", "hold[3]#cancel" reads "hold[3]". */
export function stepName(key: string): string {
  return key.replace(/#(?:undo|cancel|confirm)$/, "");
}
