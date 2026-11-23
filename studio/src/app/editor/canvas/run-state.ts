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
  /** A simulation played back: its whole trace, how far the playback is, and each loop's count. */
  trace?: readonly TraceStep[];
  /** Entries before it have played; the one at it is playing. */
  at?: number;
  /** A loop's instance key (`orders`, `lines[2]`) to how many items it had. */
  counts?: Readonly<Record<string, number>>;
  /** The playback ended: the whole trace has played. */
  ended?: boolean;
  /** Steps whose failure their On error handled, by step ID: the run went past them. */
  handled?: Readonly<Record<string, HandledFailure>>;
  /**
   * The loops the answer ended inside, by instance key, with how many items
   * they finished: Execute previous steps stopped in one of their items, or
   * the run is blocked there. Read once the playback ended.
   */
  paused?: Readonly<Record<string, number>>;
}
/** One step of a simulated run's trace, as the playback reads it. */
export interface TraceStep {
  nodeId: string;
  /** "" when it is the step ID. */
  instanceKey: string;
  kind: string;
  status: "running" | "waiting" | "completed" | "failed";
  /** A loop's entry: how many results its output list holds. */
  results?: number;
  /** The incident a failed entry stopped the run with, when the answer says (WV-RUNTIME-LOOP-LIMIT). */
  code?: string;
  /** A failed entry its On error handled: the item went on past it. */
  handled?: true;
}
/** One run of a loop at a point of the playback. */
export interface LoopRun {
  /** The loop step. */
  id: string;
  /** The item of each loop around it, outermost first; [] at the top level. */
  within: readonly number[];
  /** Items finished so far. */
  finished: number;
  /** How many items it has; null when the simulation doesn't say. */
  count: number | null;
  /** "paused": the run ended inside it, before its items finished. */
  status: "running" | "done" | "failed" | "paused";
  /** The failed item, one-based; null while none failed. */
  failedAt: number | null;
  /** Once done, how many results it returned (Skip empty results leaves some out); null otherwise. */
  results: number | null;
  /** It failed before its first item because its list was longer than Up to; absent otherwise. */
  overLimit?: true;
  /** Items with a failure their On error handled so far; absent when none. */
  handled?: number;
}
/** The incident a loop over more items than its Up to opens. */
export const LOOP_LIMIT = "WV-RUNTIME-LOOP-LIMIT";

/** The item of each loop around an instance, outermost first: `send[2][0]` gives [2, 0]. */
const iterationOf = (key: string): number[] =>
  [...key.replace(/[#~].*$/, "").matchAll(/\[(\d+)\]/g)].map((match) =>
    Number(match[1]),
  );

/**
 * Each run of each loop at a point of the playback: the entries before `at`
 * have played (all of them once it `ended`). An item is finished once its
 * last entry in the whole trace has played and completed, and failed once a
 * step failed in it; a loop is done when every item it has finished.
 * `loopsAround` names the loops around a step, outermost first. A loop in
 * `paused` is one the run ended inside: once ended, it is paused with the
 * items the answer says it finished, never done.
 */
export function loopProgress(
  trace: readonly TraceStep[],
  at: number,
  counts: Readonly<Record<string, number>>,
  loopsAround: (stepId: string) => readonly string[],
  ended: boolean,
  paused: Readonly<Record<string, number>> = {},
): LoopRun[] {
  const played = ended ? trace.length : Math.min(at, trace.length);
  // Each loop's runs, in the order they started; each item's last entry,
  // and the items a step failed in once that failure played.
  const runs = new Map<
    string,
    { id: string; within: number[]; results: number | null }
  >();
  const last = new Map<string, Map<number, number>>();
  const failures = new Map<string, Set<number>>();
  // The items a step failed in and went on, its On error handling it.
  const handledIn = new Map<string, Set<number>>();
  // A loop the run stopped at, by its own failed entry, and the incident it opened.
  const stopped = new Map<string, string | null>();
  const keyOf = (id: string, within: readonly number[]) =>
    id + within.map((index) => `[${index}]`).join("");
  trace.forEach((entry, position) => {
    if (entry.kind === "forEach" && position <= played) {
      const within = iterationOf(entry.instanceKey);
      const key = keyOf(entry.nodeId, within);
      if (!runs.has(key))
        runs.set(key, {
          id: entry.nodeId,
          within,
          results: entry.results ?? null,
        });
      if (
        entry.status === "failed" &&
        position < played &&
        position === trace.length - 1
      )
        stopped.set(key, entry.code ?? null);
    }
    const iteration = iterationOf(entry.instanceKey);
    loopsAround(entry.nodeId).forEach((loop, depth) => {
      if (iteration.length <= depth) return;
      const key = keyOf(loop, iteration.slice(0, depth));
      let items = last.get(key);
      if (!items) last.set(key, (items = new Map()));
      items.set(iteration[depth], position);
      // A failure inside an item, nested loops included, fails it, unless
      // its On error handled it: then the item went on.
      if (entry.status === "failed" && position < played) {
        const kept = entry.handled ? handledIn : failures;
        let items = kept.get(key);
        if (!items) kept.set(key, (items = new Set()));
        items.add(iteration[depth]);
      }
    });
  });
  return [...runs].map(([key, run]) => {
    let finished = 0;
    for (const position of last.get(key)?.values() ?? [])
      if (
        position < played &&
        (trace[position].status === "completed" || trace[position].handled)
      )
        finished++;
    const failed = [...(failures.get(key) ?? [])];
    const handled = [...(handledIn.get(key) ?? [])].filter(
      (index) => !failed.includes(index),
    ).length;
    const failedAt = failed.length ? Math.min(...failed) + 1 : null;
    const count = counts[key] ?? null;
    // The run stopped at the loop itself, before any of its items ran.
    const failedItself = stopped.has(key) && !last.get(key)?.size;
    const stoppedInside = ended && paused[key] !== undefined;
    const status =
      failedAt !== null || failedItself
        ? "failed"
        : stoppedInside
          ? "paused"
          : ended || (count !== null && finished >= count)
            ? "done"
            : "running";
    return {
      id: run.id,
      within: run.within,
      finished: status === "paused" ? paused[key] : finished,
      count,
      status,
      failedAt,
      results: status === "done" ? run.results : null,
      ...(failedItself && stopped.get(key) === LOOP_LIMIT
        ? { overLimit: true as const }
        : {}),
      ...(handled ? { handled } : {}),
    };
  });
}
/**
 * A run that happened, drawn on the Runs tab over the open draft: the steps
 * that differ from the version it used, and why steps failed.
 */
/** One item of a loop in a recorded run, and what it did. */
export interface RecordedItem {
  index: number;
  /**
   * "stopped": a cancelled run cut it off; "timed-out": the run timed out
   * while it ran; "handled": a step failed in it and its On error went on.
   */
  status:
    | "finished"
    | "failed"
    | "handled"
    | "running"
    | "not-run"
    | "stopped"
    | "timed-out";
  /** A failed or handled item's error code. */
  code: string | null;
}
/** One run of a loop in a recorded run: once, or once per item of the loops around it. */
export interface RecordedLoop {
  /** The loop step. */
  id: string;
  /** The item of each loop around it, outermost first; [] at the top level. */
  within: readonly number[];
  /** How many items it had: the run's count, else the items the run reached. */
  count: number;
  /** The count is the run's own. */
  known: boolean;
  items: readonly RecordedItem[];
  /** How many results it returned, when the run kept its output; null otherwise. */
  results: number | null;
}
export interface RecordedRun extends CanvasRun {
  mode: "real";
  /** Each run of a loop, for the item chip. */
  loops?: readonly RecordedLoop[];
  /** Loop step ID to the item its steps show (zero-based); absent: every item at once. */
  picks?: Readonly<Record<string, number>>;
  /** Shows another item of a loop (zero-based). */
  pick?: (loop: string, index: number) => void;
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
export type TileRun = "live" | "waiting" | "done" | "failed" | "handled" | null;
/** A failure a step's On error handled: its code and message, and the setting. */
export interface HandledFailure {
  code: string;
  message: string;
  policy: "continue" | "errorOutput";
}
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
  if (finished && run.handled?.[id]) return "handled";
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
  edge: { leaves: string | null; enters: string | null; success?: string },
  run: CanvasRun | null,
  inside: ReadonlySet<string> = NO_GROUPS,
): EdgeRun {
  if (!run) return "idle";
  // A step's Success output: taken when it finished without a failure its
  // error path handled; when its error path ran, the way it didn't take.
  if (edge.success !== undefined) {
    const handled = run.handled?.[edge.success]?.policy === "errorOutput";
    if (!handled && run.done.includes(edge.success))
      return runEnded(run) ? "taken" : "live";
    return runEnded(run) && (handled || run.mode === "simulated")
      ? "skipped"
      : "idle";
  }
  if (edge.leaves === null && edge.enters === null) return "idle";
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

/** The zooms Execute workflow frames at: 100% down to 55%, in 2% steps, then 55%. */
const EXECUTE_ZOOMS = [
  ...Array.from({ length: 23 }, (_, i) => (100 - 2 * i) / 100),
  0.55,
];

/** The largest zoom of `zooms` at which a box fits the area with 8 px to spare, centered; null when none does. */
function fitBox(
  boxAt: (zoom: number) => FrameBox,
  area: { width: number; height: number },
  zooms: readonly number[],
): { zoom: number; pan: { x: number; y: number } } | null {
  for (const zoom of zooms) {
    const box = boxAt(zoom);
    if (
      box.width * zoom + 16 <= area.width + 1e-6 &&
      box.height * zoom + 16 <= area.height + 1e-6
    )
      return {
        zoom,
        pan: {
          x: (area.width - box.width * zoom) / 2 - box.x * zoom,
          y: (area.height - box.height * zoom) / 2 - box.y * zoom,
        },
      };
  }
  return null;
}

/**
 * How Execute workflow frames its run in the canvas area the run log
 * leaves: the whole workflow's outline at 55% or more; else the loop that
 * holds the step the run failed or is at, at 55% or more; else that step
 * at 60%, as the Runs tab frames one.
 */
export function frameExecuted(
  workflow: (zoom: number) => FrameBox,
  loop: FrameBox | null,
  step: FrameBox | null,
  area: { width: number; height: number },
): { zoom: number; pan: { x: number; y: number } } {
  return (
    fitBox(workflow, area, EXECUTE_ZOOMS) ??
    (loop ? fitBox(() => loop, area, EXECUTE_ZOOMS) : null) ??
    frameRun(workflow, step ?? loop, area)
  );
}

/**
 * The step a run is framed on: where it failed, else where it is, else the
 * first step that failed and continued, else the last step it finished,
 * else the trigger. `tiles` are in the canvas's order.
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
    // A step that failed and continued: the run went on, but that's the why.
    at((state) => state === "handled") ??
    (lastDone >= 0 ? tiles[lastDone] : null) ??
    tiles.find((id) => id.startsWith("$trigger")) ??
    null
  );
}
