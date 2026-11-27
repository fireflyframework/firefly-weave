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
// A simulated run played back on the canvas, one step every 120 ms: the
// current tile gets the live border, finished ones the check, a failed one
// the error. A transaction's undo steps play after the step they followed.
// With reduced motion only the last frame is shown. DOM-free.
import type { Workflow } from "../../model";
import { getAt, isJsonObject, parsePointer } from "../../forms/core/json";
import { loopLimitText } from "./loop-frame";
import { stepPointerOf } from "../formula/loops";
import type { ExecuteResponse, TraceEntry } from "../state/execution";
import { transactionSummary } from "../state/run-summary";
import {
  beatsOf,
  containedFailures,
  playedBack,
  simulatedRollback,
  type TransactionRun,
} from "../transactions/rollback";
import { TRANSACTION_CODE_TEXT } from "../transactions/transaction-words";
import {
  type HandledFailure,
  LOOP_LIMIT,
  loopProgress,
  type CanvasRun,
  type TraceStep,
} from "./run-state";

export const PLAYBACK_STEP_MS = 120;

/**
 * Whether the run went on past a step's failure: its On error handled it, or
 * the transaction it undid did.
 */
export function wentOn(
  response: ExecuteResponse,
): (entry: TraceEntry) => boolean {
  const contained = containedFailures(response);
  return (entry) =>
    entry.status === "failed" &&
    (!!entry.handled || contained.has(entry.instanceKey || entry.nodeId));
}
/** The step of this workflow the run stopped at, not a called one's. */
export function stoppedAt(response: ExecuteResponse): TraceEntry | null {
  const past = wentOn(response);
  return (
    response.trace.find(
      (entry) => entry.status === "failed" && !entry.frame && !past(entry),
    ) ?? null
  );
}
/** The steps of the trace a playback reads: this workflow's, not a called one's. */
export function stepsOf(response: ExecuteResponse): TraceStep[] {
  const top = response.trace.filter((entry) => !entry.frame);
  const past = wentOn(response);
  // The loop the run stopped at failed itself on its Up to: the answer's diagnostic.
  const overLimit = (response.diagnostics ?? []).some(
    (problem) => problem.code === LOOP_LIMIT,
  );
  return top.map((entry, index) => {
    const { nodeId, instanceKey, kind, status, output } = entry;
    return {
      nodeId,
      instanceKey,
      kind,
      status,
      ...(past(entry) ? { handled: true as const } : {}),
      ...(kind === "forEach" && Array.isArray(output)
        ? { results: output.length }
        : {}),
      ...(overLimit &&
      kind === "forEach" &&
      status === "failed" &&
      index === top.length - 1
        ? { code: LOOP_LIMIT }
        : {}),
    };
  });
}
/** Each loop instance's count, as the simulator reports it (`orders`, `lines[2]`). */
export const countsOf = (response: ExecuteResponse): Record<string, number> =>
  Object.fromEntries(
    (response.loops ?? []).map((loop) => [
      loop.instanceKey || loop.nodeId,
      loop.count,
    ]),
  );

/** The loops the answer ended inside, with the items each finished (`orders`, `lines[2]`). */
export const pausedOf = (response: ExecuteResponse): Record<string, number> =>
  Object.fromEntries(
    (response.loops ?? [])
      .filter((loop) => loop.status === "running")
      .map((loop) => [loop.instanceKey || loop.nodeId, loop.completed]),
  );

/** The run as it stands after the whole trace; with the workflow, its transactions too. */
export function finalRun(
  response: ExecuteResponse,
  workflow?: Workflow,
): CanvasRun {
  const top = response.trace.filter((entry) => !entry.frame);
  const past = wentOn(response);
  // A failure its On error handled: the run went on past the step.
  const went = (e: TraceEntry) =>
    e.status === "completed" || (e.status === "failed" && !!e.handled);
  const done = top.filter(went).map((e) => e.nodeId);
  const failed = top
    .filter((e) => e.status === "failed" && !past(e))
    .map((e) => e.nodeId);
  const waiting = top
    .filter((e) => e.status === "waiting")
    .map((e) => e.nodeId);
  const blocked = response.blocked ? [response.blocked.nodeId] : [];
  const status =
    response.status === "completed"
      ? "succeeded"
      : response.status === "blocked"
        ? "waiting"
        : "failed";
  const trace = stepsOf(response);
  return {
    mode: "simulated",
    status,
    current: [...new Set([...failed, ...blocked])],
    active: waiting.filter((id) => !blocked.includes(id)),
    done: [...new Set(done)],
    trace,
    at: trace.length,
    counts: countsOf(response),
    ended: true,
    ...handledOf(top),
    ...failedOf(top.filter(past)),
    paused: pausedOf(response),
    ...(workflow && response.transactions?.length
      ? { transactions: simulatedRollback(response, workflow) }
      : {}),
  };
}

/** The steps that failed without their On error handling it, of the entries the run went on past. */
function failedOf(entries: readonly TraceEntry[]): { failed?: string[] } {
  const failed = entries.flatMap((entry) =>
    entry.status === "failed" && !entry.handled ? [entry.nodeId] : [],
  );
  return failed.length ? { failed: [...new Set(failed)] } : {};
}

/**
 * The steps whose failure their On error handled, with what the trace says
 * of it, and those of them that completed in another item of a loop.
 */
function handledOf(top: readonly TraceEntry[]): {
  handled?: Record<string, HandledFailure>;
  alsoCompleted?: string[];
} {
  const handled: Record<string, HandledFailure> = {};
  for (const entry of top)
    if (entry.status === "failed" && entry.handled && entry.error)
      handled[entry.nodeId] = {
        code: entry.error.code,
        message: entry.error.message,
        policy: entry.handled,
      };
  if (!Object.keys(handled).length) return {};
  const alsoCompleted = [
    ...new Set(
      top
        .filter(
          (entry) => entry.status === "completed" && handled[entry.nodeId],
        )
        .map((entry) => entry.nodeId),
    ),
  ];
  return { handled, ...(alsoCompleted.length ? { alsoCompleted } : {}) };
}

/**
 * The frames of the playback: one per trace entry (that step live, the ones
 * before it finished), and with the workflow one per undo step of its
 * transactions, after the entry it followed; then the final run. Reduced
 * motion: the final run only.
 */
export function playbackFrames(
  response: ExecuteResponse,
  reducedMotion: boolean,
  workflow?: Workflow,
): CanvasRun[] {
  const last = finalRun(response, workflow);
  if (reducedMotion) return [last];
  const top = response.trace.filter((entry) => !entry.frame);
  // Every frame shares the trace; each says how far the playback is.
  const trace = last.trace!;
  const counts = last.counts!;
  const phases = (last.transactions && response.phases) || [];
  const beats = beatsOf(top, phases);
  const transactions =
    last.transactions && workflow
      ? playedBack(response, workflow, top, beats)
      : null;
  let index = 0;
  const frames = beats.map((beat, moment): CanvasRun => {
    const phase = "phase" in beat ? phases[beat.phase] : null;
    const played = phase ? index : index++;
    let kept: TransactionRun | undefined;
    return {
      mode: "simulated",
      status: "running",
      current: [phase ? phase.nodeId : top[played].nodeId],
      active: [],
      done: [
        ...new Set(
          top
            .slice(0, played)
            .filter(
              (e) =>
                e.status === "completed" ||
                (e.status === "failed" && !!e.handled),
            )
            .map((e) => e.nodeId),
        ),
      ],
      trace,
      at: played,
      counts,
      ...handledOf(top.slice(0, played)),
      ...failedOf(top.slice(0, played)),
      ...(phase && phase.phase !== "confirm" ? { undoing: phase.nodeId } : {}),
      // Worked out when the frame shows.
      get transactions() {
        return (kept ??= transactions?.(moment));
      },
    };
  });
  return [...frames, last];
}

/**
 * The loops around each step, read from the trace when the workflow isn't at
 * hand: a step in an item belongs to the loop that started last at the depth
 * above it, as loops in one sequence run one after another.
 */
export function loopsInTrace(
  trace: readonly TraceStep[],
): (stepId: string) => readonly string[] {
  const around = new Map<string, readonly string[]>();
  const open: string[] = [];
  for (const entry of trace) {
    const depth = (entry.instanceKey.match(/\[\d+\]/g) ?? []).length;
    if (entry.kind === "forEach") {
      open.length = depth;
      open[depth] = entry.nodeId;
    }
    if (depth && !around.has(entry.nodeId))
      around.set(entry.nodeId, open.slice(0, depth));
  }
  return (stepId) => around.get(stepId) ?? [];
}

/** Up to when a loop leaves it out. */
const DEFAULT_MAX_ITEMS = 1000;

/**
 * A loop that failed over its Up to: Up to, and how many items its list
 * held when the answer says, or when Items reads a step's output the run
 * recorded (`steps.fetch.output.rows`); null otherwise.
 */
export function overLimitOf(
  definition: unknown,
  response: ExecuteResponse,
  loopId: string,
): { count: number | null; limit: number } {
  const pointer = stepPointerOf(definition, loopId);
  const loop =
    pointer === null ? null : getAt(definition, parsePointer(pointer)!);
  const maxItems = isJsonObject(loop) ? loop["maxItems"] : undefined;
  const limit =
    typeof maxItems === "number" && maxItems > 0 ? maxItems : DEFAULT_MAX_ITEMS;
  const counted = countsOf(response)[loopId];
  if (counted !== undefined) return { count: counted, limit };
  const items = isJsonObject(loop) ? loop["items"] : undefined;
  const ref =
    isJsonObject(items) && Object.keys(items).length === 1
      ? items["ref"]
      : null;
  const path = typeof ref === "string" ? parsePointer(ref) : null;
  if (!path || path[0] !== "steps" || path[2] !== "output")
    return { count: null, limit };
  const ran = response.trace.find(
    (entry) =>
      !entry.frame &&
      entry.nodeId === path[1] &&
      !entry.instanceKey &&
      entry.status === "completed",
  );
  const list =
    ran?.output === undefined ? undefined : getAt(ran.output, path.slice(3));
  return { count: Array.isArray(list) ? list.length : null, limit };
}

/**
 * What the live region says when the playback ends: a loop that failed
 * names its item, or for a list longer than Up to, with the workflow,
 * how many items and Up to.
 */
export function finalAnnouncement(
  response: ExecuteResponse,
  loopsAround?: (stepId: string) => readonly string[],
  definition?: unknown,
): string {
  const transaction = transactionSummary(response);
  if (transaction)
    return [transaction.text, transaction.detail].filter(Boolean).join(" ");
  const trace = stepsOf(response);
  const failed = loopProgress(
    trace,
    trace.length,
    countsOf(response),
    loopsAround ?? loopsInTrace(trace),
    true,
    pausedOf(response),
  ).find((loop) => loop.status === "failed");
  if (failed)
    return failed.failedAt !== null
      ? `${failed.id} failed at item ${failed.failedAt}.`
      : failed.overLimit
        ? overLimitSaid(failed.id, response, definition)
        : `${failed.id} failed.`;
  if (response.status === "completed") {
    const count = new Set(
      response.trace
        .filter((e) => !e.frame && e.status === "completed")
        .map((e) => e.nodeId),
    ).size;
    return `Executed ${count} step${count === 1 ? "" : "s"}.`;
  }
  if (response.status === "blocked" && response.blocked)
    return response.blocked.message;
  if (response.status === "limit")
    return "The simulation stopped at its limit.";
  return "The test run failed.";
}

/** "orders failed: 3 items, more than Up to allows (2)." */
function overLimitSaid(
  loopId: string,
  response: ExecuteResponse,
  definition: unknown,
): string {
  if (definition === undefined)
    return `${loopId} failed: more items than Up to allows.`;
  const { count, limit } = overLimitOf(definition, response, loopId);
  const allows = `Up to allows (${limit.toLocaleString("en-US")})`;
  return count === null
    ? `${loopId} failed: more items than ${allows}.`
    : `${loopId} failed: ${count.toLocaleString("en-US")} items, more than ${allows}.`;
}

/**
 * Why the run failed at a step, for its tile and step details: the run's
 * first error, a list over Up to in Studio's own sentence; a step whose
 * failure undid its transaction, its own. Null when it didn't fail there.
 */
export function runFailureOf(
  response: ExecuteResponse,
  stepId: string,
  definition: unknown,
): { code?: string; message?: string } | null {
  const contained = containedFailures(response);
  const own = response.trace.find(
    (entry) =>
      entry.nodeId === stepId &&
      !entry.frame &&
      contained.has(entry.instanceKey || entry.nodeId),
  );
  // The run went on past its transaction.
  if (own?.error) return { code: own.error.code, message: own.error.message };
  if (response.status !== "failed") return null;
  const failed = stoppedAt(response);
  if (failed?.nodeId !== stepId) return null;
  const diagnostic = response.diagnostics?.[0];
  if (!diagnostic) return {};
  // The run's error is its transaction's: the step's is why that happened.
  if (failed.error && TRANSACTION_CODE_TEXT[diagnostic.code ?? ""])
    return { code: failed.error.code, message: failed.error.message };
  if (diagnostic.code !== LOOP_LIMIT)
    return { code: diagnostic.code, message: diagnostic.message };
  const { count, limit } = overLimitOf(definition, response, stepId);
  return { code: diagnostic.code, message: loopLimitText(count, limit) };
}
