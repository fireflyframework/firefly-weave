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
import type { Workflow } from "../../model";
import {
  terminalRunStatuses,
  type StepFact,
  type StepStatus,
} from "../../operate/run-contracts";
import type { RecordedRun } from "../canvas/run-state";
import { allSteps, withChildIds } from "../ndv/step-walk";
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
): Map<string, RunStepState> {
  const states = new Map<string, RunStepState>();
  const over = terminalRunStatuses.includes(run.status);
  const mark = (id: string, state: RunStepState) => {
    if (over && (state === "running" || state === "waiting")) return;
    const was = states.get(id);
    if (!was || RANK[state] > RANK[was]) states.set(id, state);
  };
  for (const key of Object.keys(run.completed))
    mark(instanceStep(key), "completed");
  for (const key of run.waiting) mark(instanceStep(key), "waiting");
  // A run that waits is waiting at the steps it is at (a person, a signal).
  const at = run.status === "waiting" ? "waiting" : "running";
  for (const key of run.active) mark(instanceStep(key), at);
  for (const { key } of run.incidents) mark(instanceStep(key), "failed");
  for (const fact of facts ?? []) {
    const state = FACT_STATES[fact.status];
    if (state) mark(fact.node_id, state);
  }
  return states;
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
): RunOverlay {
  const reached = stepStates(run, facts);
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
    failures[instanceStep(key)] ??= code;
  for (const fact of facts ?? [])
    if (fact.error_code && FACT_STATES[fact.status] === "failed")
      failures[fact.node_id] ??= fact.error_code;
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
