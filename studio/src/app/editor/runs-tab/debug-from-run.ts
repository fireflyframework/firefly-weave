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
// A run's recorded data as test data, for Debug in editor and Copy to
// editor: the run's input, the outputs of the steps a simulation can't work
// out (actions, AI steps and calls that wait) as pins, people's answers and
// signals as scripts. Every entry is marked as run data (its run and that
// run's environment), only typed values are copied (never people's or
// workers' IDs), and the step that failed gets nothing, so the simulation
// stops there. Pure.
import type { Step, Workflow } from "../../model";
import {
  OMISSION_WORDS,
  type RunStatus,
  type StepFact,
} from "../../operate/run-contracts";
import type { Json } from "../ndv/registry";
import { allSteps } from "../ndv/step-walk";
import type { CheckCode } from "../state/execution";
import type { StepFingerprints } from "../state/fingerprints";
import { instanceStep } from "../state/instance-keys";
import type {
  HumanScript,
  PinEntry,
  Provenance,
  SignalScript,
  StudioTestData,
} from "../state/test-data";
import { emptyTestData, fitsTestData } from "../state/test-data-ops";
import { OVERLAY_FACTS_LIMIT } from "./run-overlay";
import type { RunRecord } from "./run-record";

/** Everything about one run that test data is made from. */
export interface RunSource {
  runId: string;
  /** The environment the run is in: kept with every entry. */
  environmentId: string;
  status: RunStatus;
  record: RunRecord;
  /** Its step instances, read with their outputs. */
  facts: readonly StepFact[];
  /** The version the run used. */
  definition: Workflow;
  failedStep: { key: string; code: string | null } | null;
}
export type LeftOutReason =
  | "not-in-draft"
  | "omitted"
  | "not-recorded"
  | "refused";
export interface LeftOut {
  key: string;
  reason: LeftOutReason;
  detail: string;
}
export interface RunTestData {
  data: StudioTestData;
  /** The pointer of every entry, for the host's check. */
  pointers: string[];
  leftOut: LeftOut[];
  /** Where the simulation will stop: the step that failed in the run. */
  stopAt: { key: string; code: string | null } | null;
  /** Steps of the draft that finished in the run and that Execute works out again from the copied data. */
  recomputed: string[];
  /** Every instance key and step ID of the draft that the run reached, the failed one included. */
  reached: string[];
}

/** Steps whose output a simulation can't work out: the run's output stands for them. */
export const PINNED_KINDS: ReadonlySet<string> = new Set([
  "action",
  "llm",
  "agent",
]);
const pinned = (kind: string, step: Step | undefined) =>
  PINNED_KINDS.has(kind) ||
  (kind === "callWorkflow" && step?.["mode"] !== "detach");
const SECTION_LIMIT = 1000;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const TOO_LARGE_FOR_TEST_DATA =
  "This run's data is larger than test data holds (1 MB, 1,000 entries a section), so Studio kept nothing.";
export const UNKNOWN_ENVIRONMENT =
  "Studio can't tell which environment this run is from, so it can't keep its data.";
export const NO_STEPS_RECORDED =
  "Studio can't copy this run: the platform didn't record its steps.";
const FAILED = new Set<RunStatus>(["failed", "timed_out"]);
const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const escape = (key: string) => key.replace(/~/g, "~0").replace(/\//g, "~1");
const unescape = (key: string) => key.replace(/~1/g, "/").replace(/~0/g, "~");

export function testDataFromRun(
  source: RunSource,
  draft: Workflow,
  runPrints: ReadonlyMap<string, StepFingerprints>,
  now: string,
): RunTestData | { refused: string } {
  if (!UUID.test(source.environmentId)) return { refused: UNKNOWN_ENVIRONMENT };
  if (source.facts.length > OVERLAY_FACTS_LIMIT)
    return { refused: TOO_LARGE_FOR_TEST_DATA };
  const reached =
    Object.keys(source.record.completed).length > 0 ||
    source.record.active.length > 0 ||
    source.record.incidents.length > 0;
  if (!source.facts.length && reached) return { refused: NO_STEPS_RECORDED };
  const provenance: Required<Provenance> = {
    runId: source.runId,
    runEnvironmentId: source.environmentId,
  };
  const inDraft = new Map(
    allSteps(draft.spec.steps).map((step) => [step.id, step]),
  );
  const pins: Record<string, PinEntry> = {};
  const humans: Record<string, HumanScript> = {};
  const signals: Record<string, SignalScript> = {};
  const leftOut: LeftOut[] = [];
  const recomputed = new Set<string>();
  const visited = new Set<string>();
  const stopAt = FAILED.has(source.status) ? source.failedStep : null;
  for (const fact of source.facts) {
    const key = fact.instance_key || fact.node_id;
    const step = inDraft.get(fact.node_id);
    const kind = fact.kind as string;
    const copied =
      pinned(kind, step) || kind === "humanTask" || kind === "signal";
    if (step) visited.add(key).add(fact.node_id);
    if (!copied && step && fact.status === "succeeded")
      recomputed.add(fact.node_id);
    // Only what finished is copied; the failed step and steps at work get nothing.
    if (!copied || fact.status !== "succeeded" || key === stopAt?.key) continue;
    if (!step) {
      leftOut.push({
        key,
        reason: "not-in-draft",
        detail: "not in this draft",
      });
      continue;
    }
    const omitted = fact.omissions?.find((item) => item.path === "/output");
    if (omitted) {
      leftOut.push({
        key,
        reason: "omitted",
        detail: OMISSION_WORDS[omitted.reason] ?? "The platform left it out.",
      });
      continue;
    }
    if (!("output" in fact)) {
      leftOut.push({
        key,
        reason: "not-recorded",
        detail: "the platform didn't record it",
      });
      continue;
    }
    const output = fact.output as Json;
    if (kind === "humanTask") {
      // Only the typed answer: never who answered or the task's ID.
      if (!isRecord(output) || typeof output["decision"] !== "string") {
        leftOut.push({
          key,
          reason: "not-recorded",
          detail: "the platform didn't record it",
        });
        continue;
      }
      humans[key] = {
        decision: output["decision"],
        data: (output["data"] ?? {}) as Json,
        source: "run",
        ...provenance,
      };
    } else if (kind === "signal") {
      signals[key] = {
        payload: (isRecord(output) && "payload" in output
          ? output["payload"]
          : output) as Json,
        source: "run",
        ...provenance,
      };
    } else {
      const prints = runPrints.get(fact.node_id);
      if (!prints) {
        leftOut.push({
          key,
          reason: "not-in-draft",
          detail: "not in this draft",
        });
        continue;
      }
      // The run's version's hashes: a step changed since reads as stale.
      pins[key] = {
        output,
        source: "run",
        pinnedAt: now,
        configHash: prints.config,
        contractHash: prints.contract,
        upstreamHash: prints.upstream,
        ...provenance,
      };
    }
  }
  if (
    [pins, humans, signals].some(
      (section) => Object.keys(section).length > SECTION_LIMIT,
    )
  )
    return { refused: TOO_LARGE_FOR_TEST_DATA };
  const data: StudioTestData = {
    ...emptyTestData(now),
    input: { value: source.record.input as Json, source: "run", ...provenance },
    ...(Object.keys(pins).length ? { pins } : {}),
    ...(Object.keys(humans).length ? { humans } : {}),
    ...(Object.keys(signals).length ? { signals } : {}),
  };
  if (!fitsTestData(data)) return { refused: TOO_LARGE_FOR_TEST_DATA };
  return {
    data,
    pointers: [
      "/input",
      ...Object.keys(pins).map((key) => `/pins/${escape(key)}`),
      ...Object.keys(humans).map((key) => `/humans/${escape(key)}`),
      ...Object.keys(signals).map((key) => `/signals/${escape(key)}`),
    ],
    leftOut,
    stopAt,
    recomputed: [...recomputed],
    reached: [...visited],
  };
}

/**
 * Merge: the run's entries replace the test data of every step the run
 * reached, the failed one included (Execute then stops where the run
 * failed); entries of steps the run didn't reach stay.
 */
export function mergedTestData(
  current: StudioTestData,
  fromRun: Pick<RunTestData, "data" | "reached">,
  now: string,
): StudioTestData {
  const reached = new Set(fromRun.reached);
  const merged: StudioTestData = { ...current, updatedAt: now };
  if (fromRun.data.input) merged.input = fromRun.data.input;
  for (const section of ["pins", "humans", "signals"] as const) {
    const kept = Object.fromEntries(
      Object.entries(current[section] ?? {}).filter(
        ([key]) => !reached.has(key),
      ),
    );
    const entries = { ...kept, ...(fromRun.data[section] ?? {}) };
    if (Object.keys(entries).length)
      Object.assign(merged, { [section]: entries });
    else delete merged[section];
  }
  return merged;
}

/** "check", "check and log", "check, log and send" */
export function listText(items: readonly string[]): string {
  return items.length > 1
    ? `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`
    : (items[0] ?? "");
}
/** "the run's input, fetch's output, approve's answer and wait-for-reply's signal" */
function copiedText(data: StudioTestData): string {
  const named = (
    section: Record<string, unknown> | undefined,
    one: string,
    many: string,
  ) => {
    const counts = new Map<string, number>();
    for (const key of Object.keys(section ?? {}))
      counts.set(instanceStep(key), (counts.get(instanceStep(key)) ?? 0) + 1);
    return [...counts].map(([step, n]) => `${step}'s ${n === 1 ? one : many}`);
  };
  const parts = [
    ...(data.input ? ["the run's input"] : []),
    ...named(data.pins, "output", "outputs"),
    ...named(data.humans, "answer", "answers"),
    ...named(data.signals, "signal", "signals"),
  ];
  // A long run: the first few by name, then how many more.
  const shown =
    parts.length > 6
      ? [...parts.slice(0, 5), `the data of ${parts.length - 5} more steps`]
      : parts;
  return listText(shown) || "nothing";
}
/**
 * The Merge or Replace question when test data is already there, in three
 * paragraphs: what each answer does; what Studio copies and where Execute
 * stops; who can open the copied entries. Sentences start with a word, never
 * a step's name.
 */
export function mergeQuestion(
  built: RunTestData,
  existing: number,
  environment: string,
): string {
  const stop = built.stopAt
    ? ` Execute stops at ${instanceStep(built.stopAt.key)}, where the run failed.`
    : "";
  const again = built.recomputed.length
    ? ` Studio works out ${listText(built.recomputed)} again from the copied data.`
    : "";
  return [
    `Your test data already has ${existing} ${existing === 1 ? "entry" : "entries"}. Merge keeps the ones for steps this run didn't reach. Replace removes them.`,
    `Studio copies ${copiedText(built.data)}.${stop}${again}`,
    `Each entry is marked as data from a run in ${environment}, so only people who can read those runs can open it.`,
  ].join("\n\n");
}

const CHECK_PHRASES: Readonly<Record<CheckCode, string>> = {
  "WV-SCHEMA-SECRET_VALUE": "marked secret",
  "WV-SCHEMA-CLASSIFICATION": "Weave can't tell whether it's secret",
  "WV-STUDIO-TESTDATA-SCHEMA": "doesn't fit the step's fields",
  "WV-STUDIO-TESTDATA-UNRESOLVED": "its fields aren't known yet",
};
const phraseOf = (item: LeftOut) =>
  item.reason === "omitted"
    ? item.detail === OMISSION_WORDS.classified_secret
      ? "marked secret"
      : "left out by the platform"
    : item.detail;

/** "Left out 2 values: send (marked secret), log (not in this draft)."; "" when nothing was. */
export function leftOutText(
  leftOut: readonly LeftOut[],
  refused: readonly { pointer: string; code: CheckCode }[],
): string {
  const items = [
    ...leftOut.map((item) => `${item.key} (${phraseOf(item)})`),
    ...refused.map(
      ({ pointer, code }) =>
        `${unescape(pointer.split("/").slice(2).join("/") || pointer)} (${CHECK_PHRASES[code] ?? "refused"})`,
    ),
  ];
  if (!items.length) return "";
  return `Left out ${items.length} ${items.length === 1 ? "value" : "values"}: ${items.join(", ")}.`;
}
