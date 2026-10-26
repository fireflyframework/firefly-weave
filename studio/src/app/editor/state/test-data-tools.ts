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
import { TestDataService, type TestDataHost } from "./test-data-service";
export { checkInputsOf } from "./test-data-inputs";
export * as testDataOps from "./test-data-ops";
export { readTestData } from "./test-data-read";
export { CHECK_WORDS } from "./test-data-check";

/** The open workflow's test data service, made here so it loads with the tools, after the first page. */
export function createTestDataService(host: TestDataHost): TestDataService {
  return new TestDataService(host);
}

/** Every step's fingerprints for this workflow and test input. */
export function printsOf(
  model: StructuredCanvasAdapter,
  testInput: Json | undefined,
): Map<string, StepFingerprints> {
  return fingerprints({
    workflow: model.definition,
    testInput,
    contractOf: (step) =>
      contractOf(step, {
        owned: (uses) => ownedContract(model.canvas, uses),
        profiles:
          (model.definition.spec["llmProfiles"] as
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
