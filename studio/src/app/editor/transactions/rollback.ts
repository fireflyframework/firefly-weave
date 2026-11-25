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
// What a run did to its transactions, for tiles, frames and the run log:
// where each one stands, each of its steps and the undo steps that ran. From
// the answer of Execute, as it ended and at each moment of its playback.
// Pure; loaded with Execute and the canvas.
import type { Step, Workflow } from "../../model";
import { allSteps } from "../ndv/step-walk";
import type {
  ExecuteResponse,
  ParticipantTrace,
  PhaseEntry,
  TraceEntry,
  TransactionTrace,
} from "../state/execution";
import { instanceStep } from "../state/instance-keys";
import { splitPhase, type Phase } from "./phase-keys";
import { stepsInside } from "./phases";
import { triesOf } from "./policy";
import {
  participantText,
  phaseRowText,
  stateBadge,
  wentAhead,
  type ParticipantStatus,
  type Tone,
  type TransactionFacts,
  type UndoMode,
} from "./transaction-words";

/** One step of a transaction: how it is undone and where it stands. */
export interface ParticipantRun {
  stepId: string;
  /** Its instance (`add[2]`). */
  key: string;
  mode: UndoMode;
  status: ParticipantStatus;
}
/** One run of a transaction: once, or once per item of the loops around it. */
export interface OneTransaction {
  id: string;
  /** Its instance (`move[2]`). */
  key: string;
  facts: TransactionFacts;
  participants: readonly ParticipantRun[];
  /** The step instance whose failure it left its path for, once it failed. */
  cause: string | null;
}
/** An undo step that ran, in the order they ran. */
export interface PhaseRow {
  /** The trace entry before it. */
  afterIndex: number;
  stepId: string;
  /** `add[2]#undo`. */
  instanceKey: string;
  phase: Phase;
  status: "running" | "completed" | "failed" | "waiting";
  /** How often it ran, when the answer says. */
  attempts?: number;
  /** "Undo of add (try 10 of 10)". */
  text: string;
  /** The transaction it belongs to, by its instance; null when the answer doesn't say. */
  transaction: string | null;
}
export interface TransactionRun {
  /** A transaction's instance to where it stands. */
  byFrame: ReadonlyMap<string, TransactionFacts>;
  /** A step's instance to where it stands. */
  byStep: ReadonlyMap<string, ParticipantStatus>;
  phases: readonly PhaseRow[];
  each: readonly OneTransaction[];
}
type Answer = Pick<ExecuteResponse, "transactions" | "phases">;

/** The item of each loop around an instance, outermost first: `add[2][0]#undo` gives [2, 0]. */
const indexes = (key: string): number[] =>
  [...key.replace(/[#~].*$/, "").matchAll(/\[(\d+)\]/g)].map((match) =>
    Number(match[1]),
  );
const keyOf = (entry: Pick<TraceEntry, "nodeId" | "instanceKey">) =>
  entry.instanceKey || entry.nodeId;

const runOf = (
  each: readonly OneTransaction[],
  phases: readonly PhaseRow[],
): TransactionRun => ({
  byFrame: new Map(each.map(({ key, facts }) => [key, facts])),
  byStep: new Map(
    each.flatMap(({ participants }) =>
      participants.map(({ key, status }) => [key, status] as const),
    ),
  ),
  phases,
  each,
});

/** A transaction at a state, its steps at theirs. */
function at(
  { nodeId, instanceKey, participants, incidentKey }: TransactionTrace,
  facts: TransactionFacts,
  statusOf: (participant: ParticipantTrace) => ParticipantStatus,
  cause: string | null,
): OneTransaction {
  const steps = participants.map((participant) => ({
    stepId: participant.nodeId,
    key: participant.instanceKey,
    mode: participant.mode,
    status: statusOf(participant),
  }));
  return {
    id: nodeId,
    key: instanceKey,
    facts: {
      ...facts,
      ...(facts.state === "deciding" ? pivotOf(steps) : {}),
      ...(facts.state === "in_doubt" && incidentKey ? { incidentKey } : {}),
      wentAhead: wentAhead(steps),
    },
    participants: steps,
    cause,
  };
}
/** The step that can't be undone a deciding transaction waits for. */
function pivotOf(
  steps: readonly ParticipantRun[],
): Pick<TransactionFacts, "pivot"> {
  const pivot = steps.find(({ mode }) => mode === "irreversible");
  return pivot ? { pivot: pivot.stepId } : {};
}

/** The rows of an answer's undo steps. */
function rowsOf(
  { transactions = [], phases = [] }: Answer,
  workflow: Workflow,
) {
  const tries = new Map(
    transactionsOf(workflow).map((step) => [step.id, triesOf(step)]),
  );
  const steps = new Map(
    transactions.flatMap((transaction) =>
      transaction.participants.map(
        ({ instanceKey, attempts }) =>
          [instanceKey, { transaction, attempts }] as const,
      ),
    ),
  );
  return phases.map(
    ({ afterIndex, nodeId, instanceKey, phase, status }): PhaseRow => {
      const { transaction, attempts } =
        steps.get(splitPhase(instanceKey)?.key ?? "") ?? {};
      const of = transaction && tries.get(transaction.nodeId);
      return {
        afterIndex,
        stepId: nodeId,
        instanceKey,
        phase,
        status,
        ...(attempts === undefined ? {} : { attempts }),
        text: phaseRowText(
          phase,
          nodeId,
          attempts !== undefined && of !== undefined
            ? { attempt: attempts, of: Math.max(attempts, of) }
            : undefined,
        ),
        transaction: transaction?.instanceKey ?? null,
      };
    },
  );
}
const transactionsOf = (workflow: Workflow): Step[] =>
  allSteps(workflow.spec.steps ?? []).filter(
    (step) => (step.kind as string) === "transaction",
  );

/** What a simulated run did to its transactions, as it ended. */
export function simulatedRollback(
  answer: Answer,
  workflow: Workflow,
): TransactionRun {
  return runOf(
    (answer.transactions ?? []).map((each) =>
      at(
        each,
        { state: each.state },
        ({ status }) => status,
        each.cause?.instanceKey ?? null,
      ),
    ),
    rowsOf(answer, workflow),
  );
}

/** A moment of the playback: a step of the trace, or an undo step after it. */
export type Beat = { entry: number } | { phase: number };
/** The playback's moments: each undo step after the entry it followed, in the order they ran. */
export function beatsOf(
  top: readonly Pick<TraceEntry, "index">[],
  phases: readonly Pick<PhaseEntry, "afterIndex">[] = [],
): Beat[] {
  const beats: Beat[] = [];
  let phase = 0;
  const upTo = (index: number) => {
    for (; phase < phases.length && phases[phase].afterIndex < index; phase++)
      beats.push({ phase });
  };
  top.forEach((entry, position) => {
    upTo(entry.index);
    beats.push({ entry: position });
  });
  upTo(Infinity);
  return beats;
}

const RUNNING: Readonly<Record<Phase, ParticipantStatus>> = {
  confirm: "confirming",
  cancel: "cancelling",
  undo: "undoing",
};
const FAILED: readonly ParticipantStatus[] = ["failed", "unknown", "skipped"];

/**
 * The transactions at each moment of a playback: one is open from its first
 * step, waits for the step that can't be undone while that runs, undoes or
 * finishes while its undo steps play, and shows how it ended once they all
 * played.
 */
export function playedBack(
  answer: ExecuteResponse,
  workflow: Workflow,
  top: readonly TraceEntry[],
  beats: readonly Beat[],
): (moment: number) => TransactionRun {
  const { transactions = [] } = answer;
  const rows = rowsOf(answer, workflow);
  const end = simulatedRollback(answer, workflow);
  const undoSteps = new Map<string, number[]>();
  rows.forEach(({ transaction }, at) => {
    if (transaction !== null)
      undoSteps.set(transaction, [...(undoSteps.get(transaction) ?? []), at]);
  });
  // Where each step of a transaction stands in the trace, and its first and
  // last one: in a loop, those of the item the transaction runs for.
  const inside = new Map<string, string>();
  for (const step of transactionsOf(workflow)) {
    inside.set(step.id, step.id);
    for (const { id } of stepsInside(step)) inside.set(id, step.id);
  }
  const depths = new Map(
    transactions.map(({ nodeId, instanceKey }) => [
      nodeId,
      indexes(instanceKey).length,
    ]),
  );
  const places = new Map<string, number>();
  const first = new Map<string, number>();
  const last = new Map<string, number>();
  top.forEach((entry, position) => {
    const key = keyOf(entry);
    places.set(key, position);
    const id = inside.get(entry.nodeId);
    if (id === undefined) return;
    const owner =
      id +
      indexes(key)
        .slice(0, depths.get(id) ?? 0)
        .map((index) => `[${index}]`)
        .join("");
    if (!first.has(owner)) first.set(owner, position);
    last.set(owner, position);
  });
  return (moment) => {
    const beat = beats[moment];
    // The entry playing, or null between entries; the undo step playing.
    const current = beat && "entry" in beat ? beat.entry : null;
    const undo = beat && "phase" in beat ? beat.phase : null;
    const before = beats.slice(0, moment);
    const played = before.filter((each) => "entry" in each).length;
    const settled = before.length - played;
    const each = transactions.flatMap((one, index): OneTransaction[] => {
      const start = first.get(one.instanceKey);
      if (start === undefined || start > (current ?? played - 1)) return [];
      const mine = undoSteps.get(one.instanceKey) ?? [];
      const left = mine.some((at) => at >= settled);
      const over = played > last.get(one.instanceKey)!;
      if (over && !left) return [end.each[index]];
      const statusOf = ({
        instanceKey,
        status,
      }: ParticipantTrace): ParticipantStatus => {
        const own = mine.filter(
          (at) => splitPhase(rows[at].instanceKey)?.key === instanceKey,
        );
        const now = own.find((at) => at === undo);
        if (now !== undefined) return RUNNING[rows[now].phase];
        if (own.length && own.every((at) => at < settled)) return status;
        const place = places.get(instanceKey);
        if (place === undefined || place > (current ?? played - 1))
          return "pending";
        if (place === current) return "dispatched";
        const entry = top[place];
        if (entry.status === "completed") return "done";
        if (entry.status !== "failed") return "dispatched";
        return entry.error?.outcome === "unknown" ? "unknown" : "failed";
      };
      const cause = one.cause?.instanceKey ?? null;
      const failed = cause !== null && (places.get(cause) ?? Infinity) < played;
      return [
        at(
          one,
          stateAt(
            one,
            rows,
            mine,
            settled,
            undo,
            current === null ? null : keyOf(top[current]),
          ),
          statusOf,
          failed ? cause : null,
        ),
      ];
    });
    return runOf(
      each,
      rows
        .slice(0, undo === null ? settled : undo + 1)
        .map((row, at) => (at === undo ? { ...row, status: "running" } : row)),
    );
  };
}
/** Where a transaction that isn't over stands while its steps or its undo steps play. */
function stateAt(
  { participants }: TransactionTrace,
  rows: readonly PhaseRow[],
  mine: readonly number[],
  settled: number,
  undo: number | null,
  playing: string | null,
): TransactionFacts {
  const now = mine.filter((at) => at < settled || at === undo);
  const latest = now.at(-1);
  if (latest !== undefined && rows[latest].phase === "confirm")
    return { state: "committing" };
  if (latest !== undefined)
    return {
      state: "compensating",
      undoing: { done: mine.indexOf(latest) + 1, of: mine.length },
    };
  const decides = participants.find(({ mode }) => mode === "irreversible");
  return {
    state: decides?.instanceKey === playing ? "deciding" : "open",
  };
}

/**
 * The instance a transaction's frame and steps show: the one that runs while
 * the run does, else the one that needs attention most, the first of them.
 */
function shownOf(
  run: TransactionRun,
  id: string,
  live: boolean,
): OneTransaction | null {
  const weight = ({ facts }: OneTransaction): number => {
    switch (stateBadge(facts).tone) {
      case "live":
        return live ? 5 : 0;
      case "danger":
        return 4;
      case "warning":
        return 3;
      case "muted":
        return 2;
      case "success":
        return 1;
    }
  };
  return run.each.reduce<OneTransaction | null>(
    (best, one) =>
      one.id === id && (!best || weight(one) > weight(best)) ? one : best,
    null,
  );
}
/** Whether a transaction's steps were undone or released. */
const wasUndone = ({ facts }: OneTransaction) =>
  facts.state === "compensated" || facts.state === "cancelled";

/** "item 2", or "item 2 › 1" inside the second item of the loop around it; "" outside loops. */
export function itemOf(key: string): string {
  const items = indexes(key).map((index) => index + 1);
  return items.length ? `item ${items.join(" › ")}` : "";
}

/** A frame's state, as its header says it. */
export interface FrameState {
  facts: TransactionFacts;
  text: string;
  tone: Tone;
  /** ", item 2": the item it is of, when other items ended another way. */
  item: string;
}
/** Where a transaction stands; null before its first step, and for one the run left open. */
export function frameState(
  run: TransactionRun,
  id: string,
  live: boolean,
): FrameState | null {
  const one = shownOf(run, id, live);
  if (!one) return null;
  const { text, tone } = stateBadge(one.facts);
  if (tone === "live" && !live) return null;
  const others = run.each.some(
    (other) => other.id === id && other.facts.state !== one.facts.state,
  );
  const item = others && tone !== "live" ? itemOf(one.key) : "";
  return { facts: one.facts, text, tone, item: item && `, ${item}` };
}

/** What a step's tile says of its transaction. */
export interface StepState {
  /** Null for a step with nothing to undo, whose failure undid the transaction. */
  status: ParticipantStatus | null;
  text: string;
  tone: Tone;
  /** Its transaction was undone. */
  undone: boolean;
}
/** Where a step of a transaction stands; null for a step it says nothing of. */
export function stepState(
  run: TransactionRun,
  id: string,
  live: boolean,
): StepState | null {
  const holds = run.each.find(
    ({ participants, cause }) =>
      participants.some(({ stepId }) => stepId === id) ||
      (cause !== null && instanceStep(cause) === id),
  );
  const one = holds && shownOf(run, holds.id, live);
  if (!one) return null;
  const undone = wasUndone(one);
  const own = one.participants.find(({ stepId }) => stepId === id);
  if (own) {
    const words = participantText(own.status, own.mode, one.facts);
    return words && (live || words.tone !== "live")
      ? { status: own.status, ...words, undone }
      : null;
  }
  return one.cause !== null && instanceStep(one.cause) === id
    ? { status: null, text: "", tone: "danger", undone }
    : null;
}

/**
 * The failures a transaction's On error handled: the steps whose failure it
 * undid, and any of its other steps that failed with it. The run went on.
 */
export function containedFailures(
  answer: Pick<ExecuteResponse, "trace" | "transactions">,
): ReadonlySet<string> {
  const contained = new Set<string>();
  if (!answer.transactions?.length) return contained;
  const handled = new Set(
    answer.trace.flatMap((entry) =>
      !entry.frame && entry.handled ? [keyOf(entry)] : [],
    ),
  );
  for (const { instanceKey, cause, participants } of answer.transactions) {
    if (!handled.has(instanceKey)) continue;
    if (cause) contained.add(cause.instanceKey);
    for (const participant of participants)
      if (FAILED.includes(participant.status))
        contained.add(participant.instanceKey);
  }
  return contained;
}
