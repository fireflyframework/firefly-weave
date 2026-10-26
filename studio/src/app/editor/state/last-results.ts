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
// The last execution's results: what each step returned in the most recent
// simulated run, kept only on this computer under the account's prefix
// (`<prefix>weave.lastExecution.<draftId>`), and out of date once what the
// step's result depends on changes. DOM-free.
import type { Json } from "../ndv/registry";
import type { ExecuteResponse, ExecuteStatus, TraceStatus } from "./execution";
import type { StepFingerprints } from "./fingerprints";
import { instanceStep } from "./instance-keys";
import type { Fingerprint } from "./test-data";

export const LAST_RESULTS_MAX_BYTES = 1024 * 1024;

export interface LastResult {
  status: TraceStatus;
  input?: Json;
  output?: Json;
  /** The step's execution fingerprint when it ran. */
  execution: Fingerprint;
}
export interface LastResults {
  schemaVersion: 1;
  at: string;
  status: ExecuteStatus;
  /** By instance key ("" keys are the step ID). */
  steps: Record<string, LastResult>;
}

export const lastResultsKey = (prefix: string, draftId: string): string =>
  `${prefix}weave.lastExecution.${draftId}`;

/**
 * The results after an execution: every step it reached replaces its own
 * result; steps it didn't reach keep theirs, so executing one step never
 * forgets what earlier executions found. A step without fingerprints (it was
 * removed meanwhile) is left out.
 */
export function withExecution(
  previous: LastResults | null,
  response: ExecuteResponse,
  prints: ReadonlyMap<string, StepFingerprints>,
  at: string,
): LastResults {
  const steps: Record<string, LastResult> = { ...(previous?.steps ?? {}) };
  for (const entry of response.trace) {
    if (entry.frame) continue;
    const print = prints.get(entry.nodeId);
    if (!print) continue;
    const key = entry.instanceKey || entry.nodeId;
    steps[key] = {
      status: entry.status,
      execution: print.execution,
      ...(entry.input !== undefined ? { input: entry.input } : {}),
      ...(entry.output !== undefined ? { output: entry.output } : {}),
    };
  }
  return { schemaVersion: 1, at, status: response.status, steps };
}

/** Results whose step changed, or something it reads changed, since it ran. */
export function staleResults(
  results: LastResults | null,
  prints: ReadonlyMap<string, StepFingerprints>,
): Set<string> {
  const stale = new Set<string>();
  for (const [key, result] of Object.entries(results?.steps ?? {}))
    if (prints.get(instanceStep(key))?.execution !== result.execution)
      stale.add(key);
  return stale;
}

/** Results of steps that still exist, keyed as before. */
export function pruneResults(
  results: LastResults,
  exists: (stepId: string) => boolean,
): LastResults {
  const steps = Object.fromEntries(
    Object.entries(results.steps).filter(([key]) => exists(instanceStep(key))),
  );
  return { ...results, steps };
}

/** The text kept, or null when it is larger than 1 MiB (then nothing is kept). */
export function keptText(results: LastResults): string | null {
  const text = JSON.stringify(results);
  return new TextEncoder().encode(text).length > LAST_RESULTS_MAX_BYTES
    ? null
    : text;
}

const STATUSES = new Set(["running", "waiting", "completed", "failed"]);
const ENDS = new Set(["completed", "blocked", "failed", "limit"]);
const isObject = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** What was kept, or null when it isn't results this version wrote. */
export function readResults(text: string | null): LastResults | null {
  if (text === null) return null;
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return null;
  }
  if (
    !isObject(raw) ||
    raw["schemaVersion"] !== 1 ||
    typeof raw["at"] !== "string" ||
    !ENDS.has(raw["status"] as string) ||
    !isObject(raw["steps"])
  )
    return null;
  for (const result of Object.values(raw["steps"]))
    if (
      !isObject(result) ||
      !STATUSES.has(result["status"] as string) ||
      typeof result["execution"] !== "string"
    )
      return null;
  return raw as unknown as LastResults;
}
