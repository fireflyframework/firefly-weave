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
/**
 * A run that happened, drawn on the Runs tab over the open draft: the steps
 * that differ from the version it used, and why steps failed.
 */
export interface RecordedRun extends CanvasRun {
  mode: "real";
  /** Steps changed since the run's version. */
  changed: readonly string[];
  /** Steps the run's version didn't have. */
  added: readonly string[];
  /** Step ID to the error code it failed with. */
  failures: Readonly<Record<string, string>>;
}
export const isRecordedRun = (run: CanvasRun | null): run is RecordedRun =>
  !!run && "failures" in run;
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
  const finished = run.done.includes(id);
  if (run.current.includes(id) || run.active.includes(id)) {
    if (run.status === "failed" || run.status === "timed_out") return "failed";
    // An ended run is nowhere any more; what it finished keeps its check.
    if (runEnded(run)) return finished ? "done" : null;
    return run.status === "waiting" ? "waiting" : "live";
  }
  return finished ? "done" : null;
}

const NO_GROUPS: ReadonlySet<string> = new Set();

/**
 * The decisions and parallel steps the run is inside: a group is reached when
 * any step on one of its paths is, however deep, though a run records the
 * group itself only when it finishes. `parentOf` names the group whose path
 * holds a step.
 */
export function groupsEntered(
  run: CanvasRun | null,
  parentOf: (id: string) => string | null,
): ReadonlySet<string> {
  const groups = new Set<string>();
  if (!run) return groups;
  for (const id of [...run.current, ...run.active, ...run.done]) {
    let group = parentOf(id);
    // Stopping at a group already added: the ones above it are in too.
    while (group && !groups.has(group)) {
      groups.add(group);
      group = parentOf(group);
    }
  }
  return groups;
}

/**
 * Taken when the run finished the step the edge leaves and reached the one
 * it enters (either may be absent; `inside` adds the groups the run is in,
 * which count as reached); live while the run goes on, taken once it ended,
 * skipped when it ended elsewhere. An edge naming no step stays idle.
 */
export function edgeRun(
  edge: { leaves: string | null; enters: string | null },
  run: CanvasRun | null,
  inside: ReadonlySet<string> = NO_GROUPS,
): EdgeRun {
  if (!run || (edge.leaves === null && edge.enters === null)) return "idle";
  const reached = (id: string) =>
    run.done.includes(id) || (id === "$end" && run.status === "succeeded");
  const here = (id: string) =>
    reached(id) ||
    run.current.includes(id) ||
    run.active.includes(id) ||
    inside.has(id);
  const taken =
    (edge.leaves === null || reached(edge.leaves)) &&
    (edge.enters === null || here(edge.enters));
  const ended = runEnded(run);
  if (taken) return ended ? "taken" : "live";
  if (!ended) return "idle";
  // A run that happened marks only the ways a finished step didn't take:
  // edges out of a failed step, or on from a step it never reached, stay
  // plain. A simulation dashes the whole path it didn't take.
  if (run.mode === "real" && edge.leaves !== null && !reached(edge.leaves))
    return "idle";
  return "skipped";
}

/** The live pulse plays only while a real run is followed. */
export function pulses(state: TileRun, run: CanvasRun | null): boolean {
  return run?.mode === "real" && (state === "live" || state === "waiting");
}

export interface FrameBox {
  x: number;
  y: number;
  width: number;
  height: number;
}
/**
 * How the Runs tab frames a run in the visible canvas area: the largest zoom
 * from 100% down to 60%, in 2% steps, at which the workflow's outline (all
 * that is drawn: tiles, badges, labels) fits with 8 px to spare on each side,
 * centered across and down. When it doesn't fit at 60%, each direction
 * where the outline fits is centered; in a direction where it is larger,
 * `step` is centered and the pan then stops before either edge of the
 * outline comes inside the area, so no room is left empty while the
 * workflow goes on beyond the other edge. Below 60% tiles are too small to read.
 */
export function frameRun(
  workflow: FrameBox,
  step: FrameBox | null,
  area: { width: number; height: number },
): { zoom: number; pan: { x: number; y: number } } {
  for (let percent = 100; percent >= 60; percent -= 2) {
    const zoom = percent / 100;
    if (
      workflow.width * zoom + 16 <= area.width &&
      workflow.height * zoom + 16 <= area.height
    )
      return {
        zoom,
        pan: {
          x: (area.width - workflow.width * zoom) / 2 - workflow.x * zoom,
          y: (area.height - workflow.height * zoom) / 2 - workflow.y * zoom,
        },
      };
  }
  const zoom = 0.6;
  const axis = (
    start: number,
    size: number,
    focusStart: number,
    focusSize: number,
    room: number,
  ) => {
    if (size * zoom <= room) return (room - size * zoom) / 2 - start * zoom;
    const centered = (room - focusSize * zoom) / 2 - focusStart * zoom;
    // The outline's near edge stays at or before the area's, its far edge at or after.
    return Math.min(
      -start * zoom,
      Math.max(room - (start + size) * zoom, centered),
    );
  };
  const focus = step ?? workflow;
  return {
    zoom,
    pan: {
      x: axis(workflow.x, workflow.width, focus.x, focus.width, area.width),
      y: axis(workflow.y, workflow.height, focus.y, focus.height, area.height),
    },
  };
}

/**
 * The step a run is framed on: where it failed, else where it is, else the
 * last step it finished, else the trigger. `tiles` are in the canvas's order.
 */
export function framedStep(
  run: RecordedRun,
  tiles: readonly string[],
): string | null {
  const states = tiles.map((id) => tileRun(id, run));
  const at = (wanted: (state: TileRun) => boolean) => {
    const index = states.findIndex(wanted);
    return index < 0 ? null : tiles[index];
  };
  const lastDone = states.lastIndexOf("done");
  return (
    at((state) => state === "failed") ??
    at((state) => state === "live" || state === "waiting") ??
    (lastDone >= 0 ? tiles[lastDone] : null) ??
    tiles.find((id) => id.startsWith("$trigger")) ??
    null
  );
}
