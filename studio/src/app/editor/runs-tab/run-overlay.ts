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
// A run drawn over the draft that is open: each step's state in the run, by
// step ID, the canvas's run, and what changed in the draft since the version
// the run used. Pure.
import type { Step, Workflow } from "../../model";
import {
  terminalRunStatuses,
  type StepFact,
  type StepStatus,
} from "../../operate/run-contracts";
import type {
  RecordedItem,
  RecordedLoop,
  RecordedRun,
} from "../canvas/run-state";
import { allSteps, childLists, withChildIds } from "../ndv/step-walk";
import { fingerprint } from "../state/fingerprints";
import { instanceStep } from "../state/instance-keys";
import type { RunRecord } from "./run-record";

export type RunStepState =
  | "completed"
  | "running"
  | "waiting"
  | "failed"
  | "not-reached";

/** At most this many step instances are read for the overlay. */
export const OVERLAY_FACTS_LIMIT = 2000;

/** When instances of one step disagree, the higher rank wins. */
const RANK: Readonly<Record<RunStepState, number>> = {
  "not-reached": 0,
  completed: 1,
  waiting: 2,
  running: 3,
  failed: 4,
};
const FACT_STATES: Readonly<Partial<Record<StepStatus, RunStepState>>> = {
  scheduled: "running",
  running: "running",
  waiting: "waiting",
  succeeded: "completed",
  failed: "failed",
  timed_out: "failed",
};

/**
 * Each step the run reached, by static step ID: failed when any instance
 * failed, else running when any runs, else waiting when any waits, else
 * completed. Step facts add instances the run's state doesn't name. In a
 * run that ended nothing runs or waits any more, whatever step facts read
 * a moment earlier say.
 */
export function stepStates(
  run: RunRecord,
  facts: readonly StepFact[] | null,
  /** Only these instances count, for a step inside a loop: the item picked. */
  counts: (key: string) => boolean = () => true,
): Map<string, RunStepState> {
  const states = new Map<string, RunStepState>();
  const over = terminalRunStatuses.includes(run.status);
  const mark = (key: string, state: RunStepState) => {
    if (over && (state === "running" || state === "waiting")) return;
    if (!counts(key)) return;
    const id = instanceStep(key);
    const was = states.get(id);
    if (!was || RANK[state] > RANK[was]) states.set(id, state);
  };
  for (const key of Object.keys(run.completed)) mark(key, "completed");
  for (const key of run.waiting) mark(key, "waiting");
  // A run that waits is waiting at the steps it is at (a person, a signal).
  const at = run.status === "waiting" ? "waiting" : "running";
  for (const key of run.active) mark(key, at);
  for (const { key } of run.incidents) mark(key, "failed");
  for (const fact of facts ?? []) {
    const state = FACT_STATES[fact.status];
    if (state) mark(factKey(fact), state);
  }
  return states;
}

const factKey = (fact: StepFact) => fact.instance_key || fact.node_id;
/** The item of each loop around an instance, outermost first: `send[2][0]` gives [2, 0]. */
const iterationOf = (key: string): number[] =>
  [...key.replace(/[#~].*$/, "").matchAll(/\[(\d+)\]/g)].map((match) =>
    Number(match[1]),
  );
const ITEM_RANK: Readonly<Record<RecordedItem["status"], number>> = {
  "not-run": 0,
  finished: 1,
  running: 2,
  // Cut off by the run's end: above finished, below a failure.
  stopped: 3,
  "timed-out": 3,
  failed: 4,
};

/** Each step inside a loop, and the loops around it, outermost first. */
function loopsAround(
  steps: readonly Step[],
  around: readonly string[] = [],
  found = new Map<string, readonly string[]>(),
): Map<string, readonly string[]> {
  for (const step of steps) {
    if (around.length) found.set(step.id, around);
    const inner = step.kind === "forEach" ? [...around, step.id] : around;
    for (const list of childLists(step)) loopsAround(list, inner, found);
  }
  return found;
}

/**
 * What one step instance says of its item. A run that ended closes the
 * instances still open as cancelled, or timed out when the run timed out:
 * the item was cut off, never finished.
 */
function itemStatus(
  fact: StepStatus,
  run: RunRecord["status"],
): RecordedItem["status"] | null {
  const cutOff = run === "timed_out" ? "timed-out" : "stopped";
  if (fact === "cancelled") return "stopped";
  if (fact === "timed_out") return run === "timed_out" ? "timed-out" : "failed";
  const state = FACT_STATES[fact];
  if (state === "failed") return "failed";
  if (state === "completed") return "finished";
  if (!state) return null;
  return terminalRunStatuses.includes(run) ? cutOff : "running";
}

/** Each loop of a workflow, the loops around it and the steps inside it. */
function loopsOf(
  steps: readonly Step[],
  around: readonly string[] = [],
): { id: string; around: readonly string[]; inside: Set<string> }[] {
  return steps.flatMap((step) => {
    const lists = childLists(step);
    const inner = step.kind === "forEach" ? [...around, step.id] : around;
    return [
      ...(step.kind === "forEach"
        ? [
            {
              id: step.id,
              around,
              inside: new Set(lists.flatMap(allSteps).map((s) => s.id)),
            },
          ]
        : []),
      ...lists.flatMap((list) => loopsOf(list, inner)),
    ];
  });
}

/**
 * Each run of each loop in a recorded run, with what each of its items did:
 * failed when a step inside failed in it, running while one is at work,
 * finished once one ran, else not run. The run's state keeps how many items
 * a loop had; the step facts say what each did, as its frames are gone.
 */
export function recordedLoops(
  definition: Workflow,
  run: RunRecord,
  facts: readonly StepFact[] | null,
): RecordedLoop[] {
  const over = terminalRunStatuses.includes(run.status);
  const marks: [string, RecordedItem["status"], string | null][] = [
    ...(facts ?? []).flatMap(
      (fact): [string, RecordedItem["status"], string | null][] => {
        const status = itemStatus(fact.status, run.status);
        if (!status) return [];
        return [
          [
            factKey(fact),
            status,
            status === "failed" ? (fact.error_code ?? fact.status) : null,
          ],
        ];
      },
    ),
    ...run.incidents.map(
      ({ key, code }): [string, RecordedItem["status"], string | null] => [
        key,
        "failed",
        code,
      ],
    ),
    ...(over ? [] : [...run.active, ...run.waiting]).map(
      (key): [string, RecordedItem["status"], string | null] => [
        key,
        "running",
        null,
      ],
    ),
  ];
  return loopsOf(definition.spec.steps).flatMap((loop) => {
    const depth = loop.around.length;
    // Within each run of the loop: item index to what it did.
    const runs = new Map<string, Map<number, RecordedItem>>();
    const within = (prefix: readonly number[]) => {
      const name = prefix.join(",");
      let items = runs.get(name);
      if (!items) runs.set(name, (items = new Map()));
      return items;
    };
    const instance = (prefix: readonly number[]) =>
      loop.id + prefix.map((index) => `[${index}]`).join("");
    for (const key of Object.keys(run.loops)) {
      if (instanceStep(key) !== loop.id) continue;
      const prefix = iterationOf(key);
      if (prefix.length === depth) within(prefix);
    }
    for (const [key, status, code] of marks) {
      if (!loop.inside.has(instanceStep(key))) continue;
      const iteration = iterationOf(key);
      if (iteration.length <= depth) continue;
      const items = within(iteration.slice(0, depth));
      const index = iteration[depth];
      const was = items.get(index);
      if (!was || ITEM_RANK[status] > ITEM_RANK[was.status])
        items.set(index, { index, status, code });
    }
    return [...runs]
      .map(([name, items]) => {
        const prefix = name ? name.split(",").map(Number) : [];
        const recorded = run.loops[instance(prefix)];
        const binding = run.completed[instance(prefix)];
        const output =
          binding && typeof binding === "object"
            ? (binding as { output?: unknown }).output
            : undefined;
        const count =
          recorded ?? Math.max(0, ...[...items.keys()].map((i) => i + 1));
        return {
          id: loop.id,
          within: prefix,
          count,
          known: recorded !== undefined,
          items: Array.from(
            { length: count },
            (_, index): RecordedItem =>
              items.get(index) ?? { index, status: "not-run", code: null },
          ),
          results: Array.isArray(output) ? output.length : null,
        };
      })
      .sort((a, b) => compareIndexes(a.within, b.within));
  });
}
const compareIndexes = (a: readonly number[], b: readonly number[]) => {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const delta = (a[i] ?? -1) - (b[i] ?? -1);
    if (delta) return delta;
  }
  return 0;
};

/** A loop's items for its menu: the failed ones first, then the others, each by number. */
export function failedFirst(loop: RecordedLoop): RecordedItem[] {
  return [
    ...loop.items.filter((item) => item.status === "failed"),
    ...loop.items.filter((item) => item.status !== "failed"),
  ];
}

/** The item a recorded loop opens on: its first failed one, else its first. */
export function defaultPick(loop: RecordedLoop): number {
  return loop.items.find((item) => item.status === "failed")?.index ?? 0;
}

/**
 * The item each loop shows: the one picked while it is one of its items,
 * else its first failed one. A nested loop's items are those of its run in
 * the items picked around it, so outer loops are settled first.
 */
export function picksFor(
  definition: Workflow,
  loops: readonly RecordedLoop[],
  picked: Readonly<Record<string, number>>,
): Record<string, number> {
  const picks: Record<string, number> = {};
  const around = loopsAround(definition.spec.steps);
  for (const { id } of loopsOf(definition.spec.steps)) {
    const within = (around.get(id) ?? []).map((owner) => picks[owner] ?? 0);
    const loop = loops.find(
      (candidate) =>
        candidate.id === id && compareIndexes(candidate.within, within) === 0,
    );
    if (!loop) continue;
    const wanted = picked[id];
    picks[id] =
      wanted !== undefined && wanted >= 0 && wanted < loop.count
        ? wanted
        : defaultPick(loop);
  }
  return picks;
}

/** Steps compared by ID between the run's version and the draft. */
export interface VersionDiff {
  /** In both, with different settings (the steps they hold aside). */
  changed: string[];
  /** In the run's version, not in the draft. */
  missing: string[];
  /** In the draft, not in the run's version. */
  added: string[];
}
const stepPrints = (workflow: Workflow) =>
  new Map(
    allSteps(workflow.spec.steps).map((step) => [
      step.id,
      fingerprint(withChildIds(step)),
    ]),
  );
export function versionDiff(run: Workflow, draft: Workflow): VersionDiff {
  const before = stepPrints(run);
  const after = stepPrints(draft);
  return {
    changed: [...after]
      .filter(([id, print]) => before.has(id) && before.get(id) !== print)
      .map(([id]) => id)
      .sort(),
    missing: [...before.keys()].filter((id) => !after.has(id)).sort(),
    added: [...after.keys()].filter((id) => !before.has(id)).sort(),
  };
}

/** Which version the run used, when it isn't the draft's; null when it is. */
export function versionBanner(
  runVersion: string,
  draftVersion: string,
  test: boolean,
): string | null {
  // A test run's version is the draft's with "+test…" after it.
  if (test) return `This test run used a draft of ${runVersion.split("+")[0]}.`;
  return runVersion === draftVersion
    ? null
    : `This run used ${runVersion}; you're editing the draft of ${draftVersion}.`;
}

/** The banner in one line on a phone; null when no step differs. */
export function bannerSummary(
  runVersion: string,
  diff: VersionDiff,
): string | null {
  const differ = diff.changed.length + diff.added.length + diff.missing.length;
  if (!differ) return null;
  return `This run used ${runVersion}. ${differ} ${differ === 1 ? "step differs" : "steps differ"}.`;
}

export interface RunOverlay {
  canvas: RecordedRun;
  /** Every step of the draft, and every step the run reached. */
  states: ReadonlyMap<string, RunStepState>;
  /** Null when the run's version couldn't be read. */
  diff: VersionDiff | null;
  banner: string | null;
}

export function overlayOf(
  run: RunRecord,
  facts: readonly StepFact[] | null,
  versions: {
    run: Workflow | null;
    draft: Workflow;
    runVersion: string;
    test: boolean;
  },
  /** When the run was read (epoch ms), for how long its steps have been at work. */
  now?: number,
  /** Loop step ID to the item its steps show; without it, every item at once. */
  picks?: Readonly<Record<string, number>>,
): RunOverlay {
  // A step inside a loop shows its instance in the items picked.
  const wanted = new Map<string, string>();
  if (picks)
    for (const [id, around] of loopsAround(versions.draft.spec.steps))
      wanted.set(
        id,
        id + around.map((owner) => `[${picks[owner] ?? 0}]`).join(""),
      );
  // A repeated activation (#) or a yield (~) of that instance counts too.
  const counts = (key: string) => {
    const want = wanted.get(instanceStep(key));
    return !want || key.replace(/[#~].*$/, "") === want;
  };
  const reached = stepStates(run, facts, counts);
  const states = new Map(reached);
  for (const step of allSteps(versions.draft.spec.steps))
    if (!states.has(step.id)) states.set(step.id, "not-reached");
  const inState = (wanted: RunStepState) =>
    [...reached]
      .filter(([, state]) => state === wanted)
      .map(([id]) => id)
      .sort();
  const failures: Record<string, string> = {};
  for (const { key, code } of run.incidents)
    if (counts(key)) failures[instanceStep(key)] ??= code;
  for (const fact of facts ?? [])
    if (
      fact.error_code &&
      FACT_STATES[fact.status] === "failed" &&
      counts(factKey(fact))
    )
      failures[fact.node_id] ??= fact.error_code;
  const runningFor: Record<string, number> = {};
  if (now !== undefined && !terminalRunStatuses.includes(run.status))
    for (const fact of facts ?? []) {
      const started = fact.started_at ? Date.parse(fact.started_at) : NaN;
      if (fact.status !== "running" || !Number.isFinite(started)) continue;
      if (!counts(factKey(fact))) continue;
      runningFor[fact.node_id] = Math.max(
        runningFor[fact.node_id] ?? 0,
        now - started,
      );
    }
  const diff = versions.run ? versionDiff(versions.run, versions.draft) : null;
  return {
    canvas: {
      mode: "real",
      status: run.status,
      current: [...inState("failed"), ...inState("running")].sort(),
      active: inState("waiting"),
      done: inState("completed"),
      changed: diff?.changed ?? [],
      added: diff?.added ?? [],
      failures,
      ...(Object.keys(runningFor).length ? { runningFor } : {}),
      loops: recordedLoops(versions.draft, run, facts),
      ...(picks ? { picks: { ...picks } } : {}),
    },
    states,
    diff,
    banner: versionBanner(
      versions.runVersion,
      versions.draft.metadata.version,
      versions.test,
    ),
  };
}
