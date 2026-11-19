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
// the error. With reduced motion only the last frame is shown. DOM-free.
import type { ExecuteResponse } from "../state/execution";
import { loopProgress, type CanvasRun, type TraceStep } from "./run-state";

export const PLAYBACK_STEP_MS = 120;

/** The steps of the trace a playback reads: this workflow's, not a called one's. */
export const stepsOf = (response: ExecuteResponse): TraceStep[] =>
  response.trace
    .filter((entry) => !entry.frame)
    .map(({ nodeId, instanceKey, kind, status, output }) => ({
      nodeId,
      instanceKey,
      kind,
      status,
      ...(kind === "forEach" && Array.isArray(output)
        ? { results: output.length }
        : {}),
    }));
/** Each loop instance's count, as the simulator reports it (`orders`, `lines[2]`). */
export const countsOf = (response: ExecuteResponse): Record<string, number> =>
  Object.fromEntries(
    (response.loops ?? []).map((loop) => [
      loop.instanceKey || loop.nodeId,
      loop.count,
    ]),
  );

/** The run as it stands after the whole trace. */
export function finalRun(response: ExecuteResponse): CanvasRun {
  const top = response.trace.filter((entry) => !entry.frame);
  const done = top.filter((e) => e.status === "completed").map((e) => e.nodeId);
  const failed = top.filter((e) => e.status === "failed").map((e) => e.nodeId);
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
  };
}

/**
 * The frames of the playback: one per trace entry (that step live, the ones
 * before it finished), then the final run. Reduced motion: the final run only.
 */
export function playbackFrames(
  response: ExecuteResponse,
  reducedMotion: boolean,
): CanvasRun[] {
  const last = finalRun(response);
  if (reducedMotion) return [last];
  const top = response.trace.filter((entry) => !entry.frame);
  // Every frame shares the trace; each says how far the playback is.
  const trace = last.trace!;
  const counts = last.counts!;
  const frames: CanvasRun[] = top.map((entry, index) => ({
    mode: "simulated",
    status: "running",
    current: [entry.nodeId],
    active: [],
    done: [
      ...new Set(
        top
          .slice(0, index)
          .filter((e) => e.status === "completed")
          .map((e) => e.nodeId),
      ),
    ],
    trace,
    at: index,
    counts,
  }));
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

/** What the live region says when the playback ends: a loop that failed names its item. */
export function finalAnnouncement(
  response: ExecuteResponse,
  loopsAround?: (stepId: string) => readonly string[],
): string {
  const trace = stepsOf(response);
  const failed = loopProgress(
    trace,
    trace.length,
    countsOf(response),
    loopsAround ?? loopsInTrace(trace),
    true,
  ).find((loop) => loop.status === "failed");
  if (failed) return `${failed.id} failed at item ${failed.failedAt}.`;
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
