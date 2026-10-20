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
// Pinned output and whether it is out of date, for the canvas badges and the
// Output pane: a pin stores the fingerprints of its step when it was pinned,
// and is out of date when they differ now (its settings first, then what it
// calls, then what it reads). Keep pinned stamps today's fingerprints.
import type { Step } from "../../model";
import { currentVersion, ownedUses } from "../ndv/owned/owned-actions";
import { ownedRecipes, type CanvasSidecar } from "./canvas-sidecar";
import type { Json, StaleReason } from "../ndv/registry";
import {
  STALE_WORDS,
  fingerprint,
  staleReason,
  type StepFingerprints,
} from "./fingerprints";
import type {
  DataSource,
  PinEntry,
  Provenance,
  StudioTestData,
} from "./test-data";

export interface PinView {
  key: string;
  entry: PinEntry;
  stale: StaleReason | null;
}

/** Each step's own pin (its step ID, every iteration) and whether it is out of date. */
export function pinsView(
  data: StudioTestData,
  prints: ReadonlyMap<string, StepFingerprints>,
): Map<string, PinView> {
  const view = new Map<string, PinView>();
  for (const [key, entry] of Object.entries(data.pins ?? {}))
    if (/^[^[#~]+$/.test(key))
      view.set(key, { key, entry, stale: staleReason(entry, prints.get(key)) });
  return view;
}

/** A new pin of this output with the step's fingerprints of now. */
export function stampedPin(
  output: Json,
  source: DataSource,
  now: StepFingerprints,
  at: string,
  provenance: Provenance = {},
): PinEntry {
  return {
    output,
    source,
    pinnedAt: at,
    configHash: now.config,
    contractHash: now.contract,
    upstreamHash: now.upstream,
    ...provenance,
  };
}
/** Keep pinned: the same output and source, current again. */
export function keepPinned(entry: PinEntry, now: StepFingerprints): PinEntry {
  return {
    ...entry,
    configHash: now.config,
    contractHash: now.contract,
    upstreamHash: now.upstream,
  };
}
/** The tile tooltip after "Pinned test data, out of date: ". */
export const staleText = (reason: StaleReason): string => STALE_WORDS[reason];

export interface ContractSources {
  /** The recipe of an action this workflow owns, by its uses; null for a published one. */
  owned(uses: string): unknown;
  /** The workflow's AI profiles by name. */
  profiles: Readonly<Record<string, unknown>>;
}
/** The recipe of the action this workflow owns that `uses` calls, or null. */
export function ownedContract(canvas: CanvasSidecar, uses: string): unknown {
  for (const [name, recipe] of Object.entries(ownedRecipes(canvas)))
    if (
      uses === ownedUses(name, currentVersion(recipe as { version?: unknown }))
    )
      return recipe;
  return null;
}
/** What a step calls, as text for its contract fingerprint. */
export function contractOf(step: Step, sources: ContractSources): string {
  const uses = typeof step["uses"] === "string" ? step["uses"] : "";
  if (step.kind === "action") {
    const recipe = uses ? sources.owned(uses) : null;
    return recipe ? fingerprint(recipe) : uses;
  }
  if (step.kind === "llm") {
    const name = typeof step["profile"] === "string" ? step["profile"] : "";
    return fingerprint(sources.profiles[name] ?? null);
  }
  return uses;
}
