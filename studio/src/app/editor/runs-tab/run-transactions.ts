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
// A run's transactions as the Runs tab reads them (runs.transactions): the
// first page of 100, field by field, a failed read waiting longer each time,
// and the caption of a run whose transaction is in doubt. Studio keeps no
// business key and no resolution of a transaction: it shows states, statuses,
// attempts and codes only. Loaded with the Runs tab.
import { ApiError } from "../../api";
import type { RunViewsApi } from "../../operate/run-contracts";
import { failureCodeText } from "../canvas/tile-facts";
import { instanceStep } from "../state/instance-keys";
import type { Phase } from "../transactions/phase-keys";
import {
  phaseRowText,
  type ParticipantStatus,
  type TransactionState,
  type UndoMode,
} from "../transactions/transaction-words";
import { failedStep } from "./runs-list-model";

/** One step of a transaction, as a run recorded it; never a value. */
export interface TransactionParticipantView {
  node_id: string;
  instance_key: string;
  mode: UndoMode;
  /** The platform's word, typed as free text: one Studio doesn't know reads in plain words. */
  status: ParticipantStatus;
  effect: "none" | "unknown";
  /** Attempts of its latest phase, when any ran. */
  attempts?: number;
}
/** One transaction of a run (contracts/run_views.py RunTransactionView), its business key and resolution left out. */
export interface RunTransactionView {
  instance_key: string;
  node_id: string;
  /** Loop indexes of the instance key, outermost first. */
  iteration: number[];
  state: TransactionState;
  /** Why it left its normal path: the code, the step instance that caused it, and its effect. */
  cause?: { code: string; instance_key: string; effect: "none" | "unknown" };
  opened_at: string;
  decided_at?: string;
  ended_at?: string;
  participants: TransactionParticipantView[];
  /** Its in-doubt incident, which Operate resolves. */
  incident_id?: string;
}
export type RunTransactionsRead =
  | { views: RunTransactionView[]; more: boolean }
  | { refused: "forbidden" | "missing" };

/** One page: the overlay draws the first 100 transactions and says when there are more. */
export const RUN_TRANSACTIONS_LIMIT = 100;
export const MORE_TRANSACTIONS = "Showing the first 100 transactions.";
export const TRANSACTIONS_REFUSED =
  "You can't see this run's transactions here.";

const STATES: readonly TransactionState[] = [
  "open",
  "deciding",
  "committing",
  "completed",
  "compensating",
  "compensated",
  "cancelled",
  "in_doubt",
  "reconciled",
  "force_ended",
];
const MODES: readonly UndoMode[] = ["reserve", "compensate", "irreversible"];
const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown): value is string =>
  typeof value === "string" && value.length > 0;
const count = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const effectOf = (value: unknown): "none" | "unknown" =>
  value === "none" ? "none" : "unknown";

function participantOf(value: unknown): TransactionParticipantView | null {
  if (!isRecord(value)) return null;
  const { node_id, instance_key, mode, status, attempts } = value;
  if (!text(node_id) || !text(instance_key) || !text(status)) return null;
  if (!MODES.includes(mode as UndoMode)) return null;
  return {
    node_id,
    instance_key,
    mode: mode as UndoMode,
    status: status as ParticipantStatus,
    effect: effectOf(value["effect"]),
    ...(count(attempts) ? { attempts } : {}),
  };
}
/** The fields Studio shows of one transaction; null for one in another shape or a state Studio doesn't know. */
function viewOf(value: unknown): RunTransactionView | null {
  if (!isRecord(value)) return null;
  const { instance_key, node_id, state, opened_at, participants, cause } =
    value;
  if (!text(instance_key) || !text(node_id) || !text(opened_at)) return null;
  if (!STATES.includes(state as TransactionState)) return null;
  if (!Array.isArray(participants)) return null;
  const steps = participants.map(participantOf);
  if (steps.some((step) => step === null)) return null;
  const iteration = Array.isArray(value["iteration"])
    ? value["iteration"].filter(count)
    : [];
  const why =
    isRecord(cause) && text(cause["code"]) && text(cause["instance_key"])
      ? {
          code: cause["code"],
          instance_key: cause["instance_key"],
          effect: effectOf(cause["effect"]),
        }
      : null;
  const when = (key: string) =>
    text(value[key]) ? { [key]: value[key] as string } : {};
  return {
    instance_key,
    node_id,
    iteration,
    state: state as TransactionState,
    ...(why ? { cause: why } : {}),
    opened_at,
    ...when("decided_at"),
    ...when("ended_at"),
    participants: steps as TransactionParticipantView[],
    ...(text(value["incident_id"])
      ? { incident_id: value["incident_id"] }
      : {}),
  };
}

/**
 * The first page of a run's transactions, and whether more follow. Refused
 * without run.read here, missing on a platform that doesn't serve them; any
 * other failure throws, for the caller's back-off.
 */
export async function readRunTransactions(
  api: RunViewsApi,
  runId: string,
): Promise<RunTransactionsRead> {
  let page: unknown;
  try {
    page = await api.request<unknown>(
      `${api.environment}/runs/${encodeURIComponent(runId)}/transactions?limit=${RUN_TRANSACTIONS_LIMIT}`,
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 403)
      return { refused: "forbidden" };
    if (
      error instanceof ApiError &&
      (error.status === 404 || error.status === 501)
    )
      return { refused: "missing" };
    throw error;
  }
  if (!isRecord(page) || !Array.isArray(page["items"]))
    throw new Error("Studio can't read this run's transactions.");
  return {
    views: page["items"]
      .map(viewOf)
      .filter((view): view is RunTransactionView => view !== null),
    more: text(page["next_cursor"]),
  };
}

const FIRST_WAIT_MS = 5_000;
const LONGEST_WAIT_MS = 60_000;
/**
 * When a failed read may be tried again: 5 s after the first failure,
 * doubling up to a minute; at once after a read that worked.
 */
export class TransactionReadBackoff {
  private failedAt: number | null = null;
  private wait = 0;
  ready(now: number): boolean {
    return this.failedAt === null || now - this.failedAt >= this.wait;
  }
  failed(now: number): void {
    this.wait = this.wait
      ? Math.min(this.wait * 2, LONGEST_WAIT_MS)
      : FIRST_WAIT_MS;
    this.failedAt = now;
  }
  succeeded(): void {
    this.failedAt = null;
    this.wait = 0;
  }
}

/** The undo step of a stuck status, and what stuck means for the steps done. */
const STUCK_PHASES: Readonly<Partial<Record<string, Phase>>> = {
  undo_stuck: "undo",
  cancel_stuck: "cancel",
  confirm_stuck: "confirm",
};
/** "hold", "hold and lock", "hold, lock and add" */
const names = (ids: readonly string[]) =>
  ids.length > 1
    ? `${ids.slice(0, -1).join(", ")} and ${ids.at(-1)}`
    : (ids[0] ?? "");

/** What step details say of a transaction in doubt, in a recorded run. */
export interface InDoubtLines {
  title: string;
  /** Which undo step failed, how often, with its code, and what stayed applied; null when no step says. */
  detail: string | null;
  /** Where its result would be. */
  result: string;
  /** Its incident in Operate; null when the run names none. */
  incident: string | null;
}
/**
 * The banner of a transaction in doubt: "Undo of add failed after 10 tries:
 * UNAVAILABLE. Weave stopped undoing, so hold is still reserved." The code
 * is the step fact's of that undo step (`add#undo`), when the run has one.
 * Null for a transaction that isn't in doubt.
 */
export function inDoubtLines(
  view: RunTransactionView,
  codeOf: (instanceKey: string) => string | null,
): InDoubtLines | null {
  if (view.state !== "in_doubt") return null;
  const stuck = view.participants.find(
    ({ status }) => STUCK_PHASES[status] !== undefined,
  );
  const phase = stuck && STUCK_PHASES[stuck.status]!;
  let detail: string | null = null;
  if (stuck && phase) {
    const code = codeOf(`${stuck.instance_key}#${phase}`);
    const tries =
      (stuck.attempts ?? 0) > 1 ? ` after ${stuck.attempts} tries` : "";
    detail = `${phaseRowText(phase, stuck.node_id)} failed${tries}${code ? `: ${failureCodeText(code)}` : ""}.`;
    // Undoing stopped: the steps done before stay applied.
    if (phase !== "confirm") {
      const done = (mode: UndoMode) =>
        view.participants
          .filter((one) => one.mode === mode && one.status === "done")
          .map((one) => one.node_id);
      const reserved = done("reserve");
      const reversed = done("compensate");
      const left = [
        ...(reserved.length
          ? [
              `${names(reserved)} ${reserved.length > 1 ? "are" : "is"} still reserved`,
            ]
          : []),
        ...(reversed.length
          ? [
              `${names(reversed)} ${reversed.length > 1 ? "aren't" : "isn't"} undone`,
            ]
          : []),
      ];
      if (left.length)
        detail += ` Weave stopped undoing, so ${left.join(" and ")}.`;
    }
  }
  return {
    title: "This transaction is in doubt. Someone must resolve it in Operate.",
    detail,
    result: `No result: the run stopped while ${view.node_id} was in doubt.`,
    incident: view.incident_id ?? null,
  };
}

/**
 * What a step of a recorded transaction says once the transaction undid it:
 * "This step was undone after the transaction failed (try 2 of 10).", the
 * try only after the first; null for a step it didn't undo or release.
 */
export function undoneLine(
  step: TransactionParticipantView,
  tries: number,
): string | null {
  const attempts = step.attempts ?? 0;
  const tried =
    attempts > 1 ? ` (try ${attempts} of ${Math.max(attempts, tries)})` : "";
  if (step.status === "undone")
    return `This step was undone after the transaction failed${tried}.`;
  if (step.status === "cancelled")
    return `This step's reservation was released after the transaction failed${tried}.`;
  return null;
}

/**
 * The Runs list's caption for a run whose transaction is in doubt, once its
 * transactions are read: "Failed at submit · move in doubt"; null when none
 * is in doubt.
 */
export function inDoubtCaption(
  views: readonly RunTransactionView[],
): string | null {
  const doubt = views.find((view) => view.state === "in_doubt");
  if (!doubt) return null;
  const cause = doubt.cause;
  return cause
    ? `Failed at ${failedStep({ node_id: instanceStep(cause.instance_key), instance_key: cause.instance_key })} · ${doubt.node_id} in doubt`
    : `Transaction ${doubt.node_id} in doubt`;
}
