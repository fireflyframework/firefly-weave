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
// The run log's rows for transactions: one per transaction, after the step it
// followed, with its undo steps under it in the order they ran. In a loop, one
// per item that undid anything. Pure.
import { failureCodeText } from "../canvas/tile-facts";
import { stateIcon } from "../canvas/transaction-frame";
import type { ExecuteResponse, TraceEntry } from "../state/execution";
import { splitPhase, type Phase } from "../transactions/phase-keys";
import {
  containedFailures,
  itemOf,
  type OneTransaction,
  type PhaseRow,
  type TransactionRun,
} from "../transactions/rollback";
import {
  phaseRowText,
  stateBadge,
  type Tone,
} from "../transactions/transaction-words";
import type { RunLogEntry } from "./run-log-panel";

/** A row: "Transaction move · Undone", or "Undo of add · Undone" under one. */
export interface TransactionRow {
  /** The instance its click opens (`move[2]`, `add[2]`). */
  key: string;
  title: string;
  text: string;
  tone: Tone;
  icon: string;
  /** Its accessible name: "Undo of add, undone". */
  label: string;
}
export interface TransactionLog extends TransactionRow {
  steps: TransactionRow[];
}

const DONE: Readonly<Record<Phase, string>> = {
  undo: "Undone",
  cancel: "Released",
  confirm: "Confirmed",
};
function phaseText({ phase, status, attempts }: PhaseRow): string {
  switch (status) {
    case "completed":
      return DONE[phase];
    case "failed":
      return attempts
        ? `Failed after ${attempts.toLocaleString("en-US")} ${attempts === 1 ? "try" : "tries"}`
        : "Failed";
    case "running":
      return "Running";
    case "waiting":
      return "Waiting";
  }
}
const row = (
  key: string,
  title: string,
  text: string,
  tone: Tone,
  icon: string,
): TransactionRow => ({
  key,
  title,
  text,
  tone,
  icon,
  label: `${title}, ${text.toLowerCase().replaceAll(" · ", ", ")}`,
});
function phaseRow(phase: PhaseRow, transaction: string): TransactionRow {
  const key = splitPhase(phase.instanceKey)?.key ?? phase.stepId;
  // A loop inside the transaction ran it more than once.
  const item = itemOf(key) === itemOf(transaction) ? "" : itemOf(key);
  const confirms = phase.phase === "confirm";
  const [tone, icon]: [Tone, string] =
    phase.status === "completed"
      ? confirms
        ? ["success", "check"]
        : ["warning", "undo"]
      : phase.status === "failed"
        ? ["danger", "critical"]
        : ["live", "wait"];
  return row(
    key,
    phaseRowText(phase.phase, phase.stepId) + (item && `, ${item}`),
    phaseText(phase),
    tone,
    icon,
  );
}

/** States a transaction passes through on its way: no row until its undo steps run. */
const ON_ITS_WAY = new Set(["open", "deciding", "committing"]);

/**
 * The run log's rows with those of the transactions: each after the row of
 * the step it followed. A transaction's own row goes, unless it failed or its
 * On error went on: then it follows what was undone.
 */
export function withTransactions(
  rows: readonly RunLogEntry[],
  answer: ExecuteResponse,
  run: TransactionRun,
  live: boolean,
  loopsAround: (stepId: string) => readonly string[],
): RunLogEntry[] {
  const top = answer.trace.filter((entry) => !entry.frame);
  const undoSteps = new Map<string | null, PhaseRow[]>();
  for (const phase of run.phases) {
    const own = undoSteps.get(phase.transaction);
    if (own) own.push(phase);
    else undoSteps.set(phase.transaction, [phase]);
  }
  const logs = new Map<string, TransactionLog[]>();
  const followed = new Map<string, string>();
  for (const one of run.each) {
    const phases = undoSteps.get(one.key) ?? [];
    if (!shown(one, phases, live)) continue;
    const after = entryBefore(one, phases, top);
    if (!after) continue;
    const at = loopsAround(after.nodeId)[0] ?? after.nodeId;
    logs.set(at, [...(logs.get(at) ?? []), logOf(one, phases)]);
    followed.set(one.id, at);
  }
  const own = new Set(run.each.map(({ id }) => id));
  const listed: RunLogEntry[] = [];
  for (const entry of rows) {
    if (own.has(entry.id)) {
      const ended = entry.status === "failed" || entry.status === "handled";
      if (!ended || followed.has(entry.id)) continue;
    }
    const transactions = logs.get(entry.id);
    listed.push(transactions ? { ...entry, transactions } : entry);
    for (const other of rows)
      if (
        followed.get(other.id) === entry.id &&
        (other.status === "failed" || other.status === "handled")
      )
        listed.push(other);
  }
  return listed;
}
/** A transaction that is over, or whose undo steps ran; in a loop, only one that left its path. */
function shown(
  { key, facts }: OneTransaction,
  phases: readonly PhaseRow[],
  live: boolean,
): boolean {
  const { tone } = stateBadge(facts);
  if (tone === "live" && !live) return false;
  if (itemOf(key)) return !ON_ITS_WAY.has(facts.state) && tone !== "success";
  return !ON_ITS_WAY.has(facts.state) || phases.length > 0;
}
function logOf(
  { key, id, facts }: OneTransaction,
  phases: readonly PhaseRow[],
): TransactionLog {
  const { text, tone } = stateBadge(facts);
  const item = itemOf(key);
  return {
    ...row(
      key,
      `Transaction ${id}${item && `, ${item}`}`,
      text,
      tone,
      stateIcon(facts.state) ?? "wait",
    ),
    steps: phases.map((phase) => phaseRow(phase, key)),
  };
}
/** The step a transaction's row follows: the one before its first undo step, else the one that failed or decided. */
function entryBefore(
  { id, participants, cause }: OneTransaction,
  [first]: readonly PhaseRow[],
  trace: readonly TraceEntry[],
): TraceEntry | undefined {
  // Its own row is this one.
  const top = trace.filter(({ nodeId }) => nodeId !== id);
  if (first) return top.filter(({ index }) => index <= first.afterIndex).at(-1);
  const keyOf = ({ instanceKey, nodeId }: TraceEntry) => instanceKey || nodeId;
  const steps = new Set(participants.map(({ key }) => key));
  return (
    top.find((entry) => keyOf(entry) === cause) ??
    top.filter((entry) => steps.has(keyOf(entry))).at(-1) ??
    top.at(-1)
  );
}

/**
 * The steps a transaction that went on was undone for, each with the code it
 * failed with, for their rows: the run went past them, so no row says where
 * it stopped.
 */
export function undoneFor(
  response: Pick<ExecuteResponse, "trace" | "transactions">,
): ReadonlyMap<string, string> {
  const contained = containedFailures(response);
  const codes = new Map<string, string>();
  for (const entry of response.trace)
    if (
      !entry.frame &&
      entry.status === "failed" &&
      !entry.handled &&
      entry.error &&
      contained.has(entry.instanceKey || entry.nodeId)
    )
      codes.set(entry.nodeId, failureCodeText(entry.error.code));
  return codes;
}
