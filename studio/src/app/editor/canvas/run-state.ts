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
// A run as the canvas draws it: where it is, what it finished, and whether
// it is a simulation played back or a real run followed live. Only a real
// run pulses; a simulated one shows its live border without the pulse.
export interface CanvasRun {
  mode: "simulated" | "real";
  status: string;
  /** Steps the run is at. */
  current: readonly string[];
  /** Steps waiting (a signal, a person, a timer). */
  active: readonly string[];
  /** Steps it finished. */
  done: readonly string[];
}
export type TileRun = "live" | "waiting" | "done" | "failed" | null;
export type EdgeRun = "idle" | "live" | "taken" | "skipped";
export const ENDED_STATUSES: readonly string[] = [
  "succeeded",
  "failed",
  "cancelled",
  "timed_out",
];
export const runEnded = (run: CanvasRun): boolean =>
  ENDED_STATUSES.includes(run.status);

export function tileRun(id: string, run: CanvasRun | null): TileRun {
  if (!run) return null;
  if (run.current.includes(id) || run.active.includes(id)) {
    if (run.status === "failed" || run.status === "timed_out") return "failed";
    if (runEnded(run)) return null;
    return run.status === "waiting" ? "waiting" : "live";
  }
  return run.done.includes(id) ? "done" : null;
}

/**
 * Taken when the run finished the step the edge leaves and reached the one
 * it enters (either may be absent); live while the run goes on, taken once
 * it ended, skipped when it ended elsewhere. An edge naming no step stays idle.
 */
export function edgeRun(
  edge: { leaves: string | null; enters: string | null },
  run: CanvasRun | null,
): EdgeRun {
  if (!run || (edge.leaves === null && edge.enters === null)) return "idle";
  const reached = (id: string) =>
    run.done.includes(id) || (id === "$end" && run.status === "succeeded");
  const here = (id: string) =>
    reached(id) || run.current.includes(id) || run.active.includes(id);
  const taken =
    (edge.leaves === null || reached(edge.leaves)) &&
    (edge.enters === null || here(edge.enters));
  const ended = runEnded(run);
  return taken ? (ended ? "taken" : "live") : ended ? "skipped" : "idle";
}

/** The live pulse plays only while a real run is followed. */
export function pulses(state: TileRun, run: CanvasRun | null): boolean {
  return run?.mode === "real" && (state === "live" || state === "waiting");
}
