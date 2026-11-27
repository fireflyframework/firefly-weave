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
// What the shell computes from test data — fingerprints, pin states for the
// canvas, the simulation's starting values and the check's schemas — and the
// test data service itself, loaded after the first page, so the shell's first
// chunk stays small.
import type { StructuredCanvasAdapter } from "../../model";
import type { Json } from "../ndv/registry";
import { fingerprints, type StepFingerprints } from "./fingerprints";
import { contractOf, ownedContract, pinsView, staleText } from "./pins-stale";
import type { StudioTestData } from "./test-data";
import { playbackFrames } from "../canvas/trace-playback";
import { TestDataService, type TestDataHost } from "./test-data-service";
import type { ExecuteResponse } from "./execution";
import { ExecutionService, type ExecutionHost } from "./execution-service";
import { checkInputsOf } from "./test-data-inputs";
import type { CheckInputs } from "./test-data-check";
import {
  studioDefinitions as definitionsOf,
  type StudioDefinitionsHost,
} from "./host-definitions";
import {
  calleeDefinitions,
  undoDefinitions,
  type CalleesHost,
} from "../calls/callee-drafts";
import type { StudioApi } from "../../api";
import { eventFields } from "../triggers/platform-event";
import { loadFileTestData } from "./test-data-file";
import type { Workflow } from "../../model";
export { checkInputsOf } from "./test-data-inputs";
export { studioCallees } from "../calls/callee-drafts";

/**
 * Studio's definitions for its host: the drafts and copies the workflow
 * uses, then the workflows it calls and the actions that undo its steps, so
 * a call's output and an undo step's have fields.
 */
export async function studioDefinitions(
  host: StudioDefinitionsHost & CalleesHost,
) {
  const own = await definitionsOf(host);
  // Reading the workflows it calls reads the project's actions too.
  const called = await calleeDefinitions(host);
  return [...own, ...called, ...undoDefinitions(host, own)];
}
export * as testDataOps from "./test-data-ops";
export { readTestData } from "./test-data-read";
export { CHECK_WORDS } from "./test-data-check";
export {
  finalAnnouncement,
  PLAYBACK_STEP_MS,
  runFailureOf,
} from "../canvas/trace-playback";
/** The answer's playback on this page: only how it ended with reduced motion. */
export const playbackOf = (response: ExecuteResponse, workflow: Workflow) =>
  playbackFrames(
    response,
    typeof matchMedia === "function" &&
      matchMedia("(prefers-reduced-motion: reduce)").matches,
    workflow,
  );
export { summaryLine } from "./run-summary";
export { findStep } from "../ndv/step-walk";
export { branchVariables, staleResults } from "./last-results";

/** Executes on the Studio host and keeps the last results; it loads with the tools too. */
export function createExecutionService(host: ExecutionHost): ExecutionService {
  return new ExecutionService(host);
}

/** The open workflow's test data service, made here so it loads with the tools, after the first page. */
export function createTestDataService(host: TestDataHost): TestDataService {
  return new TestDataService(host);
}

/** Keeps the test data saved in a file with the open workflow, or says why not. */
export function keepFile(
  service: TestDataService,
  data: StudioTestData,
  workflow: Workflow,
  api: StudioApi,
) {
  return loadFileTestData(service, data, workflow.spec["inputSchema"], () =>
    eventFields(api),
  );
}

/** Every step's fingerprints for this workflow and test input. */
export function printsOf(
  model: StructuredCanvasAdapter,
  testInput: Json | undefined,
): Map<string, StepFingerprints> {
  return runPrints(model.definition, testInput, model);
}

/**
 * The fingerprints of `workflow` (a run's version) with `input` (its input),
 * reading contracts the way the draft open in `model` does: pins copied from
 * a run carry these, so a step changed since the run reads as out of date.
 */
export function runPrints(
  workflow: Workflow,
  input: Json | undefined,
  model: StructuredCanvasAdapter,
): Map<string, StepFingerprints> {
  return fingerprints({
    workflow,
    testInput: input,
    contractOf: (step) =>
      contractOf(step, {
        owned: (uses) => ownedContract(model.canvas, uses),
        profiles:
          (workflow.spec["llmProfiles"] as
            | Record<string, unknown>
            | undefined) ?? {},
      }),
  });
}

/** The plain words for a pin stamped in another workflow. */
export const FOREIGN_PIN = "pinned in another workflow; check it still fits";

/**
 * Each pinned step's stale words (null when current). Pins in `foreign`
 * came from another workflow; one that is current again leaves the set.
 */
export function pinStatesOf(
  data: StudioTestData,
  prints: ReadonlyMap<string, StepFingerprints>,
  foreign: Set<string>,
): Map<string, { stale: string | null }> {
  const states = new Map<string, { stale: string | null }>();
  for (const [id, view] of pinsView(data, prints)) {
    // Kept, or pinned again here: it is this workflow's pin now.
    if (!view.stale) foreign.delete(id);
    states.set(id, {
      stale: !view.stale
        ? null
        : foreign.has(id)
          ? FOREIGN_PIN
          : staleText(view.stale),
    });
  }
  return states;
}

/** What the simulation's editors start from, or null without test data. */
export function simulationDataOf(
  data: StudioTestData,
): { input?: Json; pins: Record<string, Json> } | null {
  const pins: Record<string, Json> = {};
  for (const [id, view] of pinsView(data, new Map()))
    pins[id] = view.entry.output as Json;
  const input = data.input?.value as Json | undefined;
  return input === undefined && !Object.keys(pins).length
    ? null
    : { ...(input === undefined ? {} : { input }), pins };
}

/** What the Studio host checks test data against, with the drafts and copies the workflow uses. */
export async function checkInputsFor(
  host: StudioDefinitionsHost & CalleesHost,
): Promise<CheckInputs> {
  return {
    ...checkInputsOf(host.model.definition as Workflow, host.catalogContracts),
    definitions: (await studioDefinitions(host)) as Json[],
  };
}
