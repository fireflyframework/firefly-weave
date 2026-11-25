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
// The decisions an incident in doubt offers, by its code and what the person
// may do; what each asks for before it can be sent, the request it builds and
// how a refusal reads; and how the drawer names the decision recorded. The
// incident's code says which phase a retry runs again, and whether it is Wait
// again: a transaction's record alone can't. Pure.
import type { ParticipantStatus } from "../../editor/transactions/transaction-words";
import type { PlainError } from "../../errors";
import { REASON_LIMIT, tooLongMessage } from "../operate-limits";
import {
  HELD_RUN,
  stepName,
  type TransactionParticipantView,
} from "../transactions/transaction-contracts";
import {
  RESOLUTION_LABELS,
  RETRY_LABELS,
  recordRows,
  typedConfirmation,
} from "../transactions/transaction-record";
import {
  inDoubt,
  resolutionLabel,
  transactionStep,
  type IncidentTransaction,
  type IncidentView,
  type ResolutionKind,
  type ResolutionRequest,
} from "./incidents-model";
import { typedJson } from "./typed-json";

const RETRIES: Readonly<Record<string, string>> = {
  "WV-TXN-UNDO-STUCK": RETRY_LABELS.undo,
  "WV-TXN-CANCEL-STUCK": RETRY_LABELS.cancel,
  "WV-TXN-CONFIRM-STUCK": RETRY_LABELS.confirm,
  "WV-TXN-AFTER-PIVOT-STUCK": RETRY_LABELS.step,
  "WV-TXN-SETTLE-TIMEOUT": RESOLUTION_LABELS.wait_again,
};
/** "Retry the undo", "Wait again"…, by the incident's code; null when no retry fits it. */
export function retryLabel(code: string): string | null {
  return RETRIES[code] ?? null;
}

export type TransactionAction =
  | "retry"
  | "mark_reconciled"
  | "it_happened"
  | "it_didnt_happen"
  | "force_end";
type Capability = "incident.resolve" | "transaction.reconcile";
export interface ActionOffer {
  action: TransactionAction;
  kind: ResolutionKind;
  label: string;
  help: string;
  needs: readonly Capability[];
  /** What the person lacks to choose it; null when nothing. */
  missing: string | null;
}

/** A decision of any kind but a retry says how a transaction ended, or ends it. */
const RECONCILER: readonly Capability[] = [
  "incident.resolve",
  "transaction.reconcile",
];
const MISSING: Readonly<Record<Capability, string>> = {
  "incident.resolve": "Needs permission to resolve incidents.",
  "transaction.reconcile": "Needs the transaction reconciler role.",
};
const DECISIONS: Readonly<
  Record<
    Exclude<TransactionAction, "retry">,
    Pick<ActionOffer, "kind" | "help">
  >
> = {
  mark_reconciled: {
    kind: "accept_reconciled_result",
    help: "You checked every step in the other systems. Say how the transaction ended.",
  },
  it_happened: {
    kind: "accept_reconciled_result",
    help: "The step that can't be undone did happen. The transaction finishes and its later steps run.",
  },
  it_didnt_happen: {
    kind: "accept_reconciled_failure",
    help: "The step that can't be undone didn't happen. Weave undoes what was done.",
  },
  force_end: {
    kind: "terminate",
    help: "Ends the transaction and the run as they are. Nothing more is done or undone.",
  },
};
/** Force end on a run held whole ends every transaction it has. */
const HELD_RUN_HELP =
  "Ends the run and its transactions as they are. Weave starts and undoes nothing more, but a step that is already running may still finish. Open the run to see what stays done in other systems.";
/** "Runs the release again…": a retry's help names what its label does. */
function retryHelp(label: string): string {
  if (label === RESOLUTION_LABELS.wait_again)
    return "Waits again for the steps still running, up to the same limit.";
  const what = label.replace(/^Retry /, "");
  return `Runs ${what} again with the same operation key. Only if the other system can take it again.`;
}

/** The decisions that fit an incident in doubt, Force end last; none for any other incident. */
export function transactionActions(
  incident: IncidentView,
  can: (capability: string) => boolean,
): ActionOffer[] {
  if (!inDoubt(incident)) return [];
  const offer = (
    action: TransactionAction,
    parts: Pick<ActionOffer, "kind" | "label" | "help">,
    needs: readonly Capability[],
  ): ActionOffer => {
    const lacks = needs.find((capability) => !can(capability));
    return { action, ...parts, needs, missing: lacks ? MISSING[lacks] : null };
  };
  const decision = (action: Exclude<TransactionAction, "retry">) =>
    offer(
      action,
      { label: RESOLUTION_LABELS[action], ...DECISIONS[action] },
      RECONCILER,
    );
  const retry = retryLabel(incident.code);
  const offers: ActionOffer[] = [];
  if (retry) {
    offers.push(
      offer(
        "retry",
        { kind: "retry_safe", label: retry, help: retryHelp(retry) },
        ["incident.resolve"],
      ),
    );
    // Its checklist is built from the record.
    if (incident.transaction) offers.push(decision("mark_reconciled"));
  } else if (incident.code === "WV-TXN-PIVOT-UNKNOWN")
    offers.push(decision("it_happened"), decision("it_didnt_happen"));
  // A run held whole, or a code Studio doesn't know, can only be ended.
  const end = decision("force_end");
  offers.push(
    incident.code === HELD_RUN ? { ...end, help: HELD_RUN_HELP } : end,
  );
  return offers;
}

/** A step Weave doesn't know ended as the person says, which is checked in its own system. */
export interface ChecklistRow {
  instanceKey: string;
  stepId: string;
  status: ParticipantStatus;
  sentence: string;
  /** "Undo failed after 3 tries · operation key " and the whole key. */
  caption: string;
  operationKey: string;
  /** What the record may say of it; the first is sent. */
  outcomes: readonly ("undone" | "confirmed" | "done")[];
}
/** Of the steps Undone asks about, the statuses whose calls all ended. */
const ENDED = new Set<ParticipantStatus>([
  "done",
  "unknown",
  "cancel_stuck",
  "undo_stuck",
  "confirmed",
  "confirm_stuck",
]);
/** Those a call may still change the other system in. */
const RUNS = new Set<ParticipantStatus>([
  "dispatched",
  "cancelling",
  "undoing",
  "confirming",
]);
/**
 * What a person confirms of a step of a transaction said to be undone. A
 * reservation is released, or reversed once confirmed. Nothing may have
 * happened at a step of unknown outcome, at a confirm that isn't over, or in a
 * status Studio doesn't know: there, nothing is left to undo. While a call may
 * still change the other system, the step is stopped too.
 */
function undoneWords(
  status: ParticipantStatus,
  mode: TransactionParticipantView["mode"],
): string {
  const reserved = mode === "reserve";
  const stopped = !ENDED.has(status);
  if (
    status === "unknown" ||
    (stopped && !RUNS.has(status)) ||
    (reserved && (status === "confirming" || status === "confirm_stuck"))
  )
    return `${stopped ? "is stopped and " : ""}has nothing left to undo`;
  if (reserved && status !== "confirmed")
    return stopped
      ? "is stopped and its reservation is released"
      : "was released";
  return stopped ? "is stopped and its change is reversed" : "was reversed";
}
/**
 * Whether Weave knows that a step ended as the transaction is said to have: it
 * stops there, so a step it doesn't know is left as it is for good. A failed
 * step is asked about only when finished, and only when the incident waits for
 * it (`own`): any other one's On error setting may have taken care of it. A
 * status Studio doesn't know is asked about.
 */
function endedSo(
  { status, mode, instance_key }: TransactionParticipantView,
  as: "compensated" | "completed",
  own: string | null,
): boolean {
  switch (status) {
    case "pending":
    case "skipped":
      return true;
    case "failed":
      return as === "compensated" || instance_key !== own;
    case "undone":
    case "cancelled":
      return as === "compensated";
    case "confirmed":
      return as === "completed";
    case "done":
      return as === "completed" && mode !== "reserve";
    default:
      return false;
  }
}
/** The steps to check before saying how a transaction ended; each is sent as ticked. */
export function checklistRows(
  incident: Pick<
    IncidentView,
    "run_id" | "node_id" | "instance_key" | "transaction"
  >,
  as: "compensated" | "completed",
): ChecklistRow[] {
  const { transaction, run_id } = incident;
  if (!transaction) return [];
  const own = transactionStep(incident);
  return recordRows(transaction, run_id).flatMap((row, index) => {
    const participant = transaction.participants[index];
    if (endedSo(participant, as, own)) return [];
    const { status, mode } = participant;
    const outcome =
      as === "compensated"
        ? "undone"
        : mode === "reserve"
          ? "confirmed"
          : "done";
    const did = {
      // While the transaction is in doubt, a step's own answer isn't shown;
      // what its confirm, its release or its undo answers is.
      undone: undoneWords(status, mode),
      confirmed: "was confirmed",
      done: "finished",
    }[outcome];
    const over = row.stuck || status === "failed" ? " after " : " · ";
    const tries = row.attempts ? over + row.attempts : "";
    return [
      {
        instanceKey: row.instanceKey,
        stepId: row.stepId,
        status,
        sentence: `I confirmed ${stepName(row.instanceKey)} ${did} in the other system.`,
        caption: `${(row.undo ?? row.forward).text}${tries} · operation key ${row.operationKey}`,
        operationKey: row.operationKey,
        outcomes: [outcome],
      },
    ];
  });
}

/** What an ending says of every step; Weave reversed none that can't be undone. */
export function endingHelp(
  as: "compensated" | "completed",
  transaction: IncidentTransaction | undefined,
): string {
  if (as === "completed") return "Every step went through.";
  const happened = (transaction?.participants ?? []).filter(
    ({ mode, status }) => mode === "irreversible" && status === "done",
  ).length;
  const byHand =
    happened === 1
      ? " The step that can't be undone was reversed by hand."
      : happened
        ? " The steps that can't be undone were reversed by hand."
        : "";
  return `Every step was reversed or released.${byHand}`;
}

/** What the person entered in Mark as reconciled. */
export interface ReconcileDraft {
  as: "compensated" | "completed" | "";
  /** Instance keys of the checklist's ticked lines. */
  ticked: ReadonlySet<string>;
  blockOutput: string;
  reason: string;
  evidence: string;
}
/** What a confirm still lacks, in the form's order. */
export type MissingPart =
  | { kind: "as" }
  | { kind: "tick"; steps: readonly string[] }
  | {
      kind: "field";
      label:
        | "Result of the transaction"
        | "Result of the step"
        | "Reason"
        | "Evidence reference";
    }
  | { kind: "typed" }
  | { kind: "refresh" };
/** The request a draft makes, or what it lacks and the problem under each field. */
export type Checked<Field extends string> =
  | { request: ResolutionRequest }
  | {
      missing: MissingPart[];
      problems: Partial<Record<Field | "reason" | "evidence", string>>;
    };

const field = (
  label: Extract<MissingPart, { kind: "field" }>["label"],
): MissingPart => ({ kind: "field", label });
const NOT_JSON = 'Enter the result as JSON, for example {"quantity": 40}.';
/** The reason and the evidence every decision but a retry and Force end asks for. */
function checkSaid(
  draft: { reason: string; evidence?: string },
  missing: MissingPart[],
  problems: Partial<Record<"reason" | "evidence", string>>,
) {
  if (!draft.reason.trim()) missing.push(field("Reason"));
  else if (draft.reason.length > REASON_LIMIT)
    problems.reason = tooLongMessage("reason");
  if (draft.evidence === undefined) return;
  const evidence = draft.evidence.trim();
  if (!evidence) missing.push(field("Evidence reference"));
  else if (evidence.length > REASON_LIMIT)
    problems.evidence = tooLongMessage("evidence reference");
}
const lacks = (missing: MissingPart[], problems: object) =>
  missing.length > 0 || Object.keys(problems).length > 0;

/**
 * The record of how a transaction ended: its checklist's steps as ticked, and
 * the result the steps after it read, only when it finished.
 */
export function checkReconcile(
  draft: ReconcileDraft,
  rows: readonly ChecklistRow[],
): Checked<"blockOutput"> {
  const missing: MissingPart[] = [];
  const problems: Partial<
    Record<"blockOutput" | "reason" | "evidence", string>
  > = {};
  if (!draft.as) missing.push({ kind: "as" });
  const unticked = rows.filter((row) => !draft.ticked.has(row.instanceKey));
  if (unticked.length)
    missing.push({
      kind: "tick",
      steps: unticked.map((row) => stepName(row.instanceKey)),
    });
  const finished = draft.as === "completed";
  const typed = draft.blockOutput.trim();
  const result = finished && typed ? typedJson(typed, NOT_JSON) : null;
  if (finished && !typed) missing.push(field("Result of the transaction"));
  else if (result && "problem" in result) problems.blockOutput = result.problem;
  else if (draft.as === "compensated" && typed)
    problems.blockOutput = "A transaction that was undone has no result.";
  checkSaid(draft, missing, problems);
  if (lacks(missing, problems)) return { missing, problems };
  return {
    request: {
      kind: "accept_reconciled_result",
      reason: draft.reason,
      evidence_reference: draft.evidence.trim(),
      output: {
        as: draft.as,
        participants: rows.map((row) => ({
          instance_key: row.instanceKey,
          outcome: row.outcomes[0],
        })),
        ...(result && "value" in result ? { blockOutput: result.value } : {}),
      },
    },
  };
}
/**
 * Whether an incident read again asks about other steps, or says otherwise how
 * Weave left one: its revision doesn't move with its transaction's record. A
 * step tried again is left the same, so its tries alone change nothing.
 */
export function checklistChanged(
  before: IncidentView,
  after: IncidentView,
): boolean {
  const lines = (incident: IncidentView) =>
    JSON.stringify(
      (["compensated", "completed"] as const).map((as) =>
        checklistRows(incident, as).map(({ instanceKey, status, outcomes }) => [
          instanceKey,
          status,
          outcomes,
        ]),
      ),
    );
  return before.revision !== after.revision || lines(before) !== lines(after);
}
/** The draft once the incident is read again: its record may list other steps, so each is ticked again. */
export function afterRevisionRefresh(draft: ReconcileDraft): ReconcileDraft {
  return { ...draft, ticked: new Set() };
}

/** Whether the step that can't be undone happened: its output is the step's own. */
export function checkStepOutcome(draft: {
  happened: boolean;
  output: string;
  reason: string;
  evidence: string;
}): Checked<"output"> {
  const missing: MissingPart[] = [];
  const problems: Partial<Record<"output" | "reason" | "evidence", string>> =
    {};
  const typed = draft.output.trim();
  const result = draft.happened && typed ? typedJson(typed, NOT_JSON) : null;
  if (draft.happened && !typed) missing.push(field("Result of the step"));
  else if (result && "problem" in result) problems.output = result.problem;
  checkSaid(draft, missing, problems);
  if (lacks(missing, problems)) return { missing, problems };
  return {
    request: {
      kind: draft.happened
        ? "accept_reconciled_result"
        : "accept_reconciled_failure",
      reason: draft.reason,
      evidence_reference: draft.evidence.trim(),
      ...(result && "value" in result ? { output: result.value } : {}),
    },
  };
}
export function checkRetry(draft: { reason: string }): Checked<never> {
  const missing: MissingPart[] = [];
  const problems = {};
  checkSaid({ reason: draft.reason }, missing, problems);
  if (lacks(missing, problems)) return { missing, problems };
  return { request: { kind: "retry_safe", reason: draft.reason } };
}
export function checkForceEnd(
  draft: { reason: string; typed: string },
  runId: string,
): Checked<never> {
  const missing: MissingPart[] = [];
  const problems = {};
  checkSaid({ reason: draft.reason }, missing, problems);
  if (!typedConfirmation(runId, draft.typed)) missing.push({ kind: "typed" });
  if (lacks(missing, problems)) return { missing, problems };
  return { request: { kind: "terminate", reason: draft.reason } };
}

const LEADS: Readonly<
  Record<Exclude<TransactionAction, "retry"> | "wait_again", string>
> = {
  wait_again: "To wait again",
  mark_reconciled: "To mark it as reconciled",
  it_happened: "To record that it happened",
  it_didnt_happen: "To record that it didn't happen",
  force_end: "To force end it",
};
/** "one", "one and two", "one, two and three". */
const listed = (items: readonly string[]) =>
  items.length > 1
    ? `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`
    : items.join("");
function partText(part: Exclude<MissingPart, { kind: "field" }>): string {
  switch (part.kind) {
    case "as":
      return "choose how it ended";
    case "tick":
      return part.steps.length === 1
        ? `tick the line for \`${part.steps[0]}\``
        : `tick ${part.steps.length} more lines`;
    case "typed":
      return "type the run ID";
    case "refresh":
      return "refresh the incident";
  }
}
/**
 * Why a confirm waits, naming only what is missing; a step's name is between
 * backticks, and the fields, which follow each other, share one "fill in".
 * Null when nothing is.
 */
export function confirmReason(
  action: TransactionAction | "wait_again",
  missing: readonly MissingPart[],
  retry: string = RETRY_LABELS.undo,
): string | null {
  if (!missing.length) return null;
  const lead = action === "retry" ? `To ${retry.toLowerCase()}` : LEADS[action];
  const fields = missing.flatMap((part) =>
    part.kind === "field" ? [part.label] : [],
  );
  const parts = missing.flatMap((part) =>
    part.kind !== "field"
      ? [partText(part)]
      : part.label === fields[0]
        ? [`fill in ${listed(fields)}`]
        : [],
  );
  return `${lead}, ${listed(parts)}.`;
}

/** How a refused decision reads; a refusal Studio has no words for keeps the platform's. */
export function explainDecision(
  refused: PlainError,
  offer: Pick<ActionOffer, "needs">,
): PlainError {
  if (refused.status === 403)
    return {
      ...refused,
      message: `You need ${offer.needs.join(" and ")} in this environment. Ask an administrator in Settings › People and access.`,
    };
  // The same code says an incident is no longer active.
  if (refused.code === "WV-RUNTIME-STATE") {
    const message = /still running|has not ended/.test(refused.message)
      ? "The step is running again. Wait for it to finish, then decide."
      : /still being handled/.test(refused.message)
        ? "An attempt failed and Weave is still handling it. Wait a moment, then decide."
        : null;
    if (message) return { ...refused, message };
  }
  if (
    refused.code === "WV-RUNTIME-OUTPUT" &&
    NEVER_COMPLETES.test(refused.message)
  )
    return { ...refused, message: NEVER_FINISHES };
  return refused;
}
/** What the platform says of Finished for a block that every way through ends at a Fail step. */
const NEVER_COMPLETES = /can never complete/;
/** Studio's words for it: what to choose instead. */
const NEVER_FINISHES =
  "Finished doesn't fit: every way through this transaction reaches a Fail step, so the run can't go on after it. Choose Undone if everything was undone, or use Force end.";
/** What the platform says of a record it has nothing more to say of. */
const DOESNT_FIT = "The reconciliation record doesn't fit the transaction";
/** How a refused record or result reads where it was typed. */
export function refusedRecordText(
  refused: PlainError,
  as: ReconcileDraft["as"],
): string {
  if (refused.message !== DOESNT_FIT)
    return `${refused.message} (${refused.code})`;
  const result = as === "completed" ? "the result and " : "";
  return `The platform refused this record. Check ${result}the lines you ticked, then try again. Support code: ${refused.code}.`;
}
/** What the platform says of a whole record: a step named like one of their words isn't meant. */
const ABOUT_NO_STEP: ReadonlySet<string> = new Set([
  DOESNT_FIT,
  "Resolution output exceeds runtime limits",
]);
/** Whether a sentence names one of these steps: its whole key, with or without its phase. */
function namesOneOf(steps: ReadonlySet<string>, sentence: string): boolean {
  return sentence.split(/[\s,;:"`()]+/).some((word) => {
    const name = word
      .replace(/[.']+$/, "")
      .replace(/'s$/, "")
      .replace(/^'+/, "");
    return steps.has(stepName(name));
  });
}
/**
 * Where a refused record shows: under the checklist when it names one of its
 * steps or there is no result to blame, under the result otherwise. A Finished
 * that the transaction can never have is about neither: null, and the
 * dialog's notice says it.
 */
export function outputRefusalAt(
  message: string,
  as: ReconcileDraft["as"],
  rows: readonly ChecklistRow[],
): "blockOutput" | "checklist" | null {
  if (message === NEVER_FINISHES || NEVER_COMPLETES.test(message)) return null;
  const steps = new Set(rows.map((row) => stepName(row.instanceKey)));
  const named = !ABOUT_NO_STEP.has(message) && namesOneOf(steps, message);
  return as === "completed" && !named ? "blockOutput" : "checklist";
}

/** How a reconciliation's record says the transaction ended. */
function endedAs(output: unknown): string {
  const as =
    typeof output === "object" && output !== null && "as" in output
      ? output.as
      : null;
  return as === "compensated"
    ? " as undone"
    : as === "completed"
      ? " as finished"
      : "";
}

/**
 * The decision recorded on an incident, in the words it was offered with: a
 * transaction in doubt has its own, every other incident the dialog's.
 */
export function decisionText(
  incident: Pick<IncidentView, "code" | "priority" | "resolution">,
): string {
  const resolution = incident.resolution;
  if (!resolution) return "";
  if (incident.priority !== "high") return resolutionLabel(resolution.kind);
  switch (resolution.kind) {
    case "retry_safe":
      return retryLabel(incident.code) ?? resolutionLabel(resolution.kind);
    case "terminate":
      return RESOLUTION_LABELS.force_end;
    case "accept_reconciled_failure":
      return RESOLUTION_LABELS.it_didnt_happen;
    case "accept_reconciled_result":
      return incident.code === "WV-TXN-PIVOT-UNKNOWN"
        ? RESOLUTION_LABELS.it_happened
        : RESOLUTION_LABELS.mark_reconciled + endedAs(resolution.output);
  }
}
