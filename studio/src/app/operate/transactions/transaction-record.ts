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
// What Operate says about a transaction: its pill, each step's words with
// what stays applied when it is left in doubt, what stays done in other
// systems, and the run detail's summary, folded line, cause, timeline and
// resolution. Pure: the section and the dialogs render it, tests check it.
import {
  TRANSACTION_CODE_TEXT,
  participantText,
  stateBadge,
  stateWord,
  wentAhead,
  type ParticipantStatus,
  type TransactionState,
  type Tone as WordTone,
} from "../../editor/transactions/transaction-words";
import { shortId } from "../../format";
import type { Tone } from "../../status-labels";
import {
  stepName,
  type RunTransactionView,
  type TransactionParticipantView,
} from "./transaction-contracts";

const STATE_TONES: Readonly<Record<TransactionState, Tone>> = {
  open: "neutral",
  deciding: "info",
  committing: "info",
  completed: "success",
  compensating: "info",
  compensated: "warning",
  cancelled: "warning",
  in_doubt: "danger",
  reconciled: "neutral",
  force_ended: "danger",
};
/** A transaction pill's tone: the running states read as Operate's Running. */
export function stateTone(state: TransactionState): Tone {
  return STATE_TONES[state];
}
/** The shared words' tones in Operate's: running reads as info, muted as neutral. */
const operateTone = (tone: WordTone): Tone =>
  tone === "live" ? "info" : tone === "muted" ? "neutral" : tone;

/** The icons a step's word carries; the tone colors the icon only. */
export type StepIcon =
  | "ok"
  | "failCircle"
  | "help"
  | "spinner"
  | "lock"
  | "warning";
export interface StepWord {
  text: string;
  icon: StepIcon | null;
  tone: Tone | null;
}
export interface RecordRow {
  stepId: string;
  instanceKey: string;
  how: "Reserved" | "Reversible" | "Can't be undone";
  howIcon: "lock" | "undo" | "revoke";
  forward: StepWord;
  undo: StepWord | null;
  /** "3 tries" when its latest phase took more than one. */
  attempts: string | null;
  operationKey: string;
  childRunId: string | null;
  pending: boolean;
  stuck: boolean;
  /** Done and never released or undone in a transaction left in doubt or force ended. */
  stillApplied: boolean;
}
/** A run's transaction or an incident's: both carry their state and steps. */
export interface RecordInput {
  state: TransactionState;
  participants: readonly TransactionParticipantView[];
  pending?: readonly string[];
}

const HOW: Readonly<
  Record<TransactionParticipantView["mode"], Pick<RecordRow, "how" | "howIcon">>
> = {
  reserve: { how: "Reserved", howIcon: "lock" },
  compensate: { how: "Reversible", howIcon: "undo" },
  irreversible: { how: "Can't be undone", howIcon: "revoke" },
};
/** The phase each later status belongs to, as its operation key names it. */
const PHASES: Readonly<
  Partial<Record<ParticipantStatus, "cancel" | "undo" | "confirm">>
> = {
  cancelling: "cancel",
  cancelled: "cancel",
  cancel_stuck: "cancel",
  undoing: "undo",
  undone: "undo",
  undo_stuck: "undo",
  confirming: "confirm",
  confirmed: "confirm",
  confirm_stuck: "confirm",
};
const RUNNING = new Set<ParticipantStatus>([
  "cancelling",
  "undoing",
  "confirming",
]);
const STUCK = new Set<ParticipantStatus>([
  "cancel_stuck",
  "undo_stuck",
  "confirm_stuck",
]);
/** Statuses that say nothing of the step stays applied. */
const NOT_APPLIED = new Set<ParticipantStatus>([
  "pending",
  "dispatched",
  "failed",
  "unknown",
  "skipped",
  "cancelled",
  "undone",
]);
const FORWARD: Readonly<Partial<Record<ParticipantStatus, StepWord>>> = {
  pending: { text: "Not started", icon: null, tone: null },
  dispatched: { text: "Running", icon: "spinner", tone: "info" },
  done: { text: "Done", icon: "ok", tone: "success" },
  failed: { text: "Failed", icon: "failCircle", tone: "danger" },
  unknown: { text: "Unknown", icon: "help", tone: "warning" },
  skipped: { text: "Skipped", icon: null, tone: null },
};
const DONE: StepWord = { text: "Done", icon: "ok", tone: "success" };
const CHANGED: StepWord = {
  text: "May have changed data",
  icon: "help",
  tone: "warning",
};

/** What happened forward: a later phase means the step went forward first. */
function forwardWord(participant: TransactionParticipantView): StepWord {
  const word = FORWARD[participant.status];
  if (word) return word;
  if (!PHASES[participant.status])
    return { text: participantWord(participant).text, icon: null, tone: null };
  return participant.effect === "unknown" ? CHANGED : DONE;
}
function participantWord(participant: TransactionParticipantView) {
  return participantText(participant.status, participant.mode)!;
}

/** Each step of a record: how it is undone, forward and in its phase, and its keys. */
export function recordRows(record: RecordInput, runId: string): RecordRow[] {
  const frame = {
    state: record.state,
    wentAhead: wentAhead(record.participants),
  };
  const leftInDoubt =
    (record.state === "in_doubt" || record.state === "force_ended") &&
    !frame.wentAhead;
  const pending = new Set(record.pending ?? []);
  return record.participants.map((participant) => {
    const phase = PHASES[participant.status];
    let undo: StepWord | null = null;
    if (phase) {
      const { text, tone } = participantWord(participant);
      undo = {
        text,
        icon: RUNNING.has(participant.status)
          ? "spinner"
          : STUCK.has(participant.status)
            ? "failCircle"
            : "ok",
        tone: operateTone(tone),
      };
    }
    let forward = forwardWord(participant);
    const stillApplied = leftInDoubt && !NOT_APPLIED.has(participant.status);
    if (stillApplied) {
      forward = { text: forward.text, icon: null, tone: null };
      // Only a step done says so: a status Studio doesn't know isn't one.
      if (participant.status === "done") {
        const reading = participantText("done", participant.mode, frame)!;
        undo = {
          text: reading.text,
          icon: participant.mode === "reserve" ? "lock" : "warning",
          tone: operateTone(reading.tone),
        };
      }
    }
    const attempts = participant.attempts ?? 0;
    return {
      stepId: participant.node_id,
      instanceKey: participant.instance_key,
      ...HOW[participant.mode],
      forward,
      undo,
      attempts: attempts > 1 ? `${attempts} tries` : null,
      operationKey: `${runId}:${participant.instance_key}${phase ? `#${phase}` : ""}`,
      childRunId: participant.child_run_id ?? null,
      pending: pending.has(participant.instance_key),
      stuck: STUCK.has(participant.status),
      stillApplied,
    };
  });
}

const IN_DOUBT_BY_STUCK: Readonly<Record<string, string>> = {
  undo_stuck: "WV-TXN-UNDO-STUCK",
  cancel_stuck: "WV-TXN-CANCEL-STUCK",
  confirm_stuck: "WV-TXN-CONFIRM-STUCK",
};
/**
 * Why a record is in doubt, read from its steps: the first stuck step, the
 * step that can't be undone with an unknown outcome, a step stuck after the
 * transaction went ahead, or steps still running at the deadline. The record
 * keeps the failure that started an undo as its cause, not this.
 */
function inDoubtCode(
  participants: readonly TransactionParticipantView[],
): string | null {
  const stuck = participants.find((item) => STUCK.has(item.status));
  if (stuck) return IN_DOUBT_BY_STUCK[stuck.status];
  if (
    participants.some(
      (item) => item.mode === "irreversible" && item.status === "unknown",
    )
  )
    return "WV-TXN-PIVOT-UNKNOWN";
  if (wentAhead(participants)) return "WV-TXN-AFTER-PIVOT-STUCK";
  if (participants.some((item) => item.status === "dispatched"))
    return "WV-TXN-SETTLE-TIMEOUT";
  return null;
}
const UNDO_PHASES = new Set<ParticipantStatus>([
  "cancelling",
  "cancelled",
  "cancel_stuck",
  "undoing",
  "undone",
  "undo_stuck",
]);

/** Of an undo, only what its state says: it goes on, it ended, or it started. */
const UNDO_WORDS: Partial<Record<TransactionState, string>> = {
  compensating: "Undoing",
  compensated: "Undone",
};
/**
 * "Undoing after add[2] failed: UNAVAILABLE", or for a transaction in doubt
 * "In doubt: " and the platform's sentence for what holds it. Null when
 * nothing was undone and nothing is in doubt.
 */
export function causeText(
  view: Pick<RunTransactionView, "cause" | "state" | "participants">,
): string | null {
  if (view.state === "in_doubt") {
    const code = inDoubtCode(view.participants);
    return code ? `In doubt: ${TRANSACTION_CODE_TEXT[code]}` : null;
  }
  if (!view.cause) return null;
  const undoing =
    ["compensating", "compensated", "cancelled"].includes(view.state) ||
    view.participants.some((item) => UNDO_PHASES.has(item.status));
  if (!undoing) return null;
  return (
    TRANSACTION_CODE_TEXT[view.cause.code] ??
    `${UNDO_WORDS[view.state] ?? "Undo started"} after ${view.cause.instance_key} failed: ${view.cause.code}`
  );
}

/** Each resolution's label, by the action a person chose. */
export const RESOLUTION_LABELS = {
  retry: "Retry the undo",
  wait_again: "Wait again",
  mark_reconciled: "Mark as reconciled",
  it_happened: "It happened",
  it_didnt_happen: "It didn't happen",
  force_end: "Force end",
} as const;
/** A retry's label, by what runs again: a step's phase, or the step itself. */
export const RETRY_LABELS = {
  undo: "Retry the undo",
  cancel: "Retry the release",
  confirm: "Retry the confirm",
  step: "Retry the step",
} as const;
/**
 * The phase a retry ran again: a step still stuck, else the one that took more
 * than one try. A record that shows neither can't tell a retry from Wait again.
 */
function retryLabel(
  participants: readonly TransactionParticipantView[],
): string {
  const retried =
    participants.find((item) => STUCK.has(item.status)) ??
    participants.find(
      (item) => PHASES[item.status] && (item.attempts ?? 0) > 1,
    );
  const phase = retried && PHASES[retried.status];
  return phase ? RETRY_LABELS[phase] : "Retry or Wait again";
}
/** "Mark as reconciled as undone", "Force end"…: what the person chose. */
export function resolutionText(
  resolution: NonNullable<RunTransactionView["resolution"]>,
  participants: readonly TransactionParticipantView[] = [],
): string {
  switch (resolution.kind) {
    case "retry_safe":
      return retryLabel(participants);
    case "terminate":
      return RESOLUTION_LABELS.force_end;
    case "accept_reconciled_failure":
      return RESOLUTION_LABELS.it_didnt_happen;
    case "accept_reconciled_result":
      return resolution.reconciled_as
        ? `${RESOLUTION_LABELS.mark_reconciled} as ${
            resolution.reconciled_as === "compensated" ? "undone" : "finished"
          }`
        : RESOLUTION_LABELS.it_happened;
  }
}

/** The record's moments, oldest first; an end and a resolution at once keep that order. */
export function recordTimeline(
  view: RunTransactionView,
): { at: string; text: string }[] {
  const moments: { at: string; text: string }[] = [
    { at: view.opened_at, text: "Opened" },
  ];
  if (view.decided_at)
    moments.push({ at: view.decided_at, text: "Decided to finish" });
  if (view.ended_at)
    moments.push({ at: view.ended_at, text: stateWord(view.state) });
  if (view.resolution?.resolved_at)
    moments.push({
      at: view.resolution.resolved_at,
      text: `Resolved: ${resolutionText(view.resolution, view.participants)}`,
    });
  return moments
    .map((moment, index) => ({ moment, index, time: Date.parse(moment.at) }))
    .sort((a, b) => a.time - b.time || a.index - b.index)
    .map(({ moment }) => moment);
}

/** The summary's order: what needs a person first, what ended well last. */
const SUMMARY_ORDER: readonly TransactionState[] = [
  "in_doubt",
  "force_ended",
  "compensating",
  "deciding",
  "committing",
  "open",
  "compensated",
  "cancelled",
  "reconciled",
  "completed",
];
/**
 * "5 transactions: 1 in doubt, 1 undone, …", or while more follow "The first 50
 * of this run's transactions: …", and the first one in doubt.
 */
export function sectionSummary(
  views: readonly RunTransactionView[],
  more = false,
): {
  text: string;
  firstInDoubt: string | null;
} {
  const counts = SUMMARY_ORDER.map(
    (state) =>
      [state, views.filter((view) => view.state === state).length] as const,
  )
    .filter(([, count]) => count > 0)
    .map(([state, count]) => `${count} ${stateWord(state).toLowerCase()}`);
  const noun = views.length === 1 ? "transaction" : "transactions";
  const lead = more
    ? `The first ${views.length} of this run's transactions`
    : `${views.length} ${noun}`;
  return {
    text: `${lead}: ${counts.join(", ")}`,
    firstInDoubt:
      views.find((view) => view.state === "in_doubt")?.instance_key ?? null,
  };
}

/** "10:31:05" on a 24-hour clock, in the viewer's time zone or `timeZone`. */
export function clockTime(value: string, timeZone?: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
    ...(timeZone ? { timeZone } : {}),
  }).format(date);
}

/**
 * The one line a completed transaction nobody had to resolve folds to; null
 * for every other transaction, which stays open.
 */
export function foldedLine(
  view: RunTransactionView,
  timeZone?: string,
): string[] | null {
  if (view.state !== "completed" || view.incident_id || view.resolution)
    return null;
  const steps = view.participants.length;
  return [
    ...(view.business_key ? [`Business key ${view.business_key}`] : []),
    `${steps} ${steps === 1 ? "step" : "steps"}`,
    ...(view.ended_at
      ? [`Completed at ${clockTime(view.ended_at, timeZone)}`]
      : []),
  ];
}

/** The block's pill: the frame's badge, counting undone steps and naming the step it waits for. */
export function recordBadge(
  view: Pick<RunTransactionView, "state" | "participants">,
): {
  text: string;
  tone: Tone;
} {
  const participants = view.participants;
  const toUndo = participants.filter(
    (item) => item.status === "done" || UNDO_PHASES.has(item.status),
  );
  const undone = toUndo.filter(
    (item) => item.status === "cancelled" || item.status === "undone",
  ).length;
  const pivot = participants.find((item) => item.mode === "irreversible");
  const badge = stateBadge({
    state: view.state,
    ...(pivot ? { pivot: stepName(pivot.instance_key) } : {}),
    ...(toUndo.length ? { undoing: { done: undone, of: toUndo.length } } : {}),
  });
  return {
    text: badge.text || stateWord(view.state),
    tone: stateTone(view.state),
  };
}

/** One line of Stays done in other systems: "hold · Reserved: Weave won't release it". */
export interface StaysDoneLine {
  step: string;
  how: RecordRow["how"];
  howIcon: RecordRow["howIcon"];
  what: string;
}
const RUNNING_STAYS: Readonly<
  Record<TransactionParticipantView["mode"], string>
> = {
  reserve: "still running and may finish; Weave won't release it",
  compensate: "still running and may finish; Weave won't undo it",
  irreversible: "still running and may finish",
};
/**
 * What stays applied in other systems if Weave stops now: every step running,
 * done, unknown or in a phase other than released or undone, in order.
 */
export function staysDone(records: readonly RecordInput[]): StaysDoneLine[] {
  return records.flatMap((record) =>
    record.participants.flatMap((participant): StaysDoneLine[] => {
      const { status, mode } = participant;
      if (
        status === "pending" ||
        status === "failed" ||
        status === "skipped" ||
        status === "cancelled" ||
        status === "undone"
      )
        return [];
      // A status Studio doesn't know says nothing of what the step did.
      const what =
        status === "unknown" || !(FORWARD[status] || PHASES[status])
          ? "it may or may not have happened"
          : status === "dispatched"
            ? RUNNING_STAYS[mode]
            : mode === "reserve"
              ? "Weave won't release it"
              : mode === "compensate"
                ? status === "undo_stuck"
                  ? "its undo failed, and Weave won't try again"
                  : "Weave won't undo it"
                : "it stays done";
      return [{ step: stepName(participant.instance_key), ...HOW[mode], what }];
    }),
  );
}

/**
 * Whether a person typed the run's short ID to confirm, whatever its case and
 * spaces: a guard against mistakes, never a permission.
 */
export function typedConfirmation(runId: string, typed: string): boolean {
  const wanted = shortId(runId).toLowerCase();
  return !!wanted && typed.replace(/\s+/g, "").toLowerCase() === wanted;
}

/** The wait before reading again after `failures` failed reads: 5 s, doubling, at most 60 s. */
export function retryDelay(failures: number): number {
  return Math.min(5_000 * 2 ** Math.max(0, failures - 1), 60_000);
}
