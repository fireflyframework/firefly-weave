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
  /** Step ID to how long it had been at work when the run was read (ms). */
  runningFor?: Readonly<Record<string, number>>;
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
  // plain. A step new in the draft wasn't there to reach: the run went past
  // it, so both its edges are dashed. A simulation dashes the whole path it
  // didn't take.
  if (
    run.mode === "real" &&
    edge.leaves !== null &&
    !reached(edge.leaves) &&
    !(isRecordedRun(run) && run.added.includes(edge.leaves))
  )
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
/** The zooms the Runs tab tries, largest first: 100% to 60% in 2% steps. */
const FRAME_ZOOMS = Array.from({ length: 21 }, (_, i) => (100 - 2 * i) / 100);
/** On a canvas 768 px wide or more, a whole run may go down to 55%. */
const WIDE_FRAME_ZOOMS = [0.59, 0.58, 0.57, 0.56, 0.55];

/**
 * How the Runs tab frames a run in the visible canvas area: the largest zoom
 * from 100% down to 60%, in 2% steps, at which the workflow's outline (all
 * that is drawn: tiles, badges, the text of their labels) fits with 8 px to
 * spare on each side, centered across and down; on a canvas 768 px wide or
 * more, also 59% down to 55%, so a run that misses by a little still shows
 * whole. The outline may depend on the zoom (labels grow as it shrinks).
 * When nothing fits, at 60%: each direction where the outline fits is
 * centered; in a direction where it is larger, `step` is centered and the
 * pan then stops before either edge of the outline comes inside the area,
 * so no room is left empty while the workflow goes on beyond the other edge.
 */
export function frameRun(
  workflow: FrameBox | ((zoom: number) => FrameBox),
  step: FrameBox | null,
  area: { width: number; height: number },
): { zoom: number; pan: { x: number; y: number } } {
  const outlineAt = typeof workflow === "function" ? workflow : () => workflow;
  const zooms =
    area.width >= 768 ? [...FRAME_ZOOMS, ...WIDE_FRAME_ZOOMS] : FRAME_ZOOMS;
  for (const zoom of zooms) {
    const outline = outlineAt(zoom);
    // A hair of tolerance: 1,400 × 0.56 is 784.0000000000001 in floating point.
    if (
      outline.width * zoom + 16 <= area.width + 1e-6 &&
      outline.height * zoom + 16 <= area.height + 1e-6
    )
      return {
        zoom,
        pan: {
          x: (area.width - outline.width * zoom) / 2 - outline.x * zoom,
          y: (area.height - outline.height * zoom) / 2 - outline.y * zoom,
        },
      };
  }
  const zoom = 0.6;
  const outline = outlineAt(zoom);
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
  const focus = step ?? outline;
  return {
    zoom,
    pan: {
      x: axis(outline.x, outline.width, focus.x, focus.width, area.width),
      y: axis(outline.y, outline.height, focus.y, focus.height, area.height),
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
