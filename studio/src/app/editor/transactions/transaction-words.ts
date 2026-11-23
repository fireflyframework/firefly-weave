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
// Studio's words for a transaction: its state, each step's status and the
// platform's sentence for each code. The editor and Operate share them, and a
// corpus the platform's tests also read keeps them equal to the platform's
// values (studio/tests/fixtures/transaction-corpus.json). Pure.

export type TransactionState =
  | "open"
  | "deciding"
  | "committing"
  | "completed"
  | "compensating"
  | "compensated"
  | "cancelled"
  | "in_doubt"
  | "reconciled"
  | "force_ended";
export type ParticipantStatus =
  | "pending"
  | "dispatched"
  | "done"
  | "failed"
  | "unknown"
  | "skipped"
  | "cancelling"
  | "cancelled"
  | "cancel_stuck"
  | "undoing"
  | "undone"
  | "undo_stuck"
  | "confirming"
  | "confirmed"
  | "confirm_stuck";
/** How a step is undone: reserved then confirmed or released, reversed by another action, or not at all. */
export type UndoMode = "reserve" | "compensate" | "irreversible";
/** What a frame's badge and its steps' words read from a transaction. */
export interface TransactionFacts {
  state: TransactionState;
  /** The step that can't be undone, which a deciding transaction waits for. */
  pivot?: string;
  /** How many steps are undone of those to undo, while it undoes them. */
  undoing?: { done: number; of: number };
  incidentKey?: string;
  code?: string;
  /** Set from `wentAhead(participants)`; left out means it didn't. */
  wentAhead?: boolean;
}
export type Tone = "live" | "success" | "warning" | "danger" | "muted";

const STATE_WORDS: Readonly<Record<TransactionState, string>> = {
  open: "Open",
  deciding: "Deciding",
  committing: "Finishing",
  completed: "Completed",
  compensating: "Undoing",
  compensated: "Undone",
  cancelled: "Canceled",
  in_doubt: "In doubt",
  reconciled: "Reconciled",
  force_ended: "Force ended",
};
/** A state in one word, for Operate's columns, filters and summaries. */
export function stateWord(state: TransactionState): string {
  return STATE_WORDS[state];
}

/** The frame's badge. An open transaction has no words: its live border shows it runs. */
export function stateBadge(facts: TransactionFacts): {
  text: string;
  tone: Tone;
} {
  switch (facts.state) {
    case "open":
      return { text: "", tone: "live" };
    case "deciding":
      return {
        text: facts.pivot
          ? `Waiting for ${facts.pivot}, which can't be undone`
          : "Waiting for the step that can't be undone",
        tone: "live",
      };
    case "committing":
      return { text: "Finishing", tone: "live" };
    case "completed":
      return { text: "Completed", tone: "success" };
    case "compensating":
      return {
        text: facts.undoing
          ? `Undoing · ${facts.undoing.done} of ${facts.undoing.of}`
          : "Undoing",
        tone: "live",
      };
    case "compensated":
      return { text: "Undone", tone: "warning" };
    case "cancelled":
      return { text: "Canceled", tone: "warning" };
    case "in_doubt":
      return { text: "In doubt · action needed", tone: "danger" };
    case "reconciled":
      return { text: "Reconciled", tone: "muted" };
    case "force_ended":
      return { text: "Force ended", tone: "danger" };
  }
}

/**
 * Whether the transaction went ahead: its step that can't be undone is done,
 * or a reserved step is being or was confirmed. Steps done before that stay
 * applied on purpose.
 */
export function wentAhead(
  participants: readonly { mode: UndoMode; status: ParticipantStatus }[],
): boolean {
  return participants.some(
    ({ mode, status }) =>
      (mode === "irreversible" && status === "done") ||
      (mode === "reserve" &&
        (status === "confirming" ||
          status === "confirmed" ||
          status === "confirm_stuck")),
  );
}

const STATUS_WORDS: Readonly<
  Record<Exclude<ParticipantStatus, "pending">, [string, Tone]>
> = {
  dispatched: ["Running", "live"],
  done: ["Done", "success"],
  failed: ["Failed", "danger"],
  unknown: ["Unknown", "warning"],
  skipped: ["Skipped", "muted"],
  cancelling: ["Releasing", "live"],
  cancelled: ["Released", "warning"],
  cancel_stuck: ["Release failed", "danger"],
  undoing: ["Undoing", "live"],
  undone: ["Undone", "warning"],
  undo_stuck: ["Undo failed", "danger"],
  confirming: ["Confirming", "live"],
  confirmed: ["Confirmed", "success"],
  confirm_stuck: ["Confirm failed", "danger"],
};

/** "held_open" reads "Held open": a status the platform added after this table. */
const plainWords = (status: string) => {
  const words = status.replaceAll("_", " ").trim().toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
};

/**
 * A step's word, or null while it hasn't started. In a transaction left in
 * doubt or force ended that didn't go ahead, a step still done was never
 * released or undone: it reads "Still reserved" or "Not undone", never the
 * success of "Done". Whether it went ahead comes from `frame.wentAhead` only,
 * never from an incident's code. A status this table doesn't know reads in
 * plain words, muted: the platform types it as free text.
 */
export function participantText(
  status: ParticipantStatus,
  mode: UndoMode,
  frame?: Pick<TransactionFacts, "state" | "code" | "wentAhead">,
): { text: string; tone: Tone } | null {
  if (status === "pending") return null;
  const leftInDoubt =
    frame !== undefined &&
    (frame.state === "in_doubt" || frame.state === "force_ended") &&
    frame.wentAhead !== true;
  if (status === "done" && leftInDoubt && mode !== "irreversible")
    return {
      text: mode === "reserve" ? "Still reserved" : "Not undone",
      tone: "warning",
    };
  const words = STATUS_WORDS[status] as [string, Tone] | undefined;
  if (!words) return { text: plainWords(status), tone: "muted" };
  const [text, tone] = words;
  return { text, tone };
}

const PHASE_WORDS: Readonly<Record<"confirm" | "cancel" | "undo", string>> = {
  confirm: "Confirm",
  cancel: "Release",
  undo: "Undo",
};
/** A row for a step's undo, release or confirm: "Undo of add (try 3 of 10)", the try only after the first. */
export function phaseRowText(
  phase: "confirm" | "cancel" | "undo",
  step: string,
  attempts?: { attempt: number; of: number },
): string {
  const row = `${PHASE_WORDS[phase]} of ${step}`;
  return attempts && attempts.attempt > 1
    ? `${row} (try ${attempts.attempt} of ${attempts.of})`
    : row;
}

/** The platform's sentence for each transaction code. */
export const TRANSACTION_CODE_TEXT: Readonly<Record<string, string>> = {
  "WV-TXN-UNDO-STUCK":
    "A step's undo couldn't finish, so the transaction is in doubt.",
  "WV-TXN-CANCEL-STUCK":
    "A reservation couldn't be released, so the transaction is in doubt.",
  "WV-TXN-CONFIRM-STUCK":
    "A reservation couldn't be confirmed after the transaction went ahead.",
  "WV-TXN-AFTER-PIVOT-STUCK":
    "A step after the point of no return couldn't finish.",
  "WV-TXN-PIVOT-UNKNOWN":
    "The step that can't be undone may or may not have happened.",
  "WV-TXN-SETTLE-TIMEOUT":
    "Steps still running didn't finish in time, so nothing can be undone safely yet.",
  "WV-TXN-HOLD-INSUFFICIENT":
    "Reservations wouldn't outlast the step that can't be undone, so the transaction was undone first.",
  "WV-TXN-HOLD-EXPIRED":
    "Reservations were about to expire before the transaction could finish.",
  "WV-TXN-ROLLED-BACK": "The transaction was undone.",
  "WV-TXN-RUN-CANCELLED":
    "The run was canceled, so the transaction was undone.",
  "WV-TXN-RUN-TIMED-OUT": "The run timed out, so the transaction was undone.",
  "WV-TXN-IN-DOUBT": "Resolve the transaction first.",
  "WV-TXN-RUN-OVERSIZED":
    "The run grew too large to undo its transactions, so they're held until someone force ends the run.",
  "WV-TXN-UNDO-PINNED":
    "A transaction that is still running may need this activation to undo a step.",
  "WV-TXN-RECONCILE-DENIED":
    "This run isn't allowed to resolve the transaction.",
};
