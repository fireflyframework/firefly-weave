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
// The part of a runs.read answer the Runs tab draws, read field by field:
// a run's status, version and steps come only from typed fields, and an
// answer with any of them in another shape reads as nothing.
import { runStatuses, type RunStatus } from "../../operate/run-contracts";

type Json = unknown;

export interface RunRecord {
  id: string;
  /** The definition version the run executed. */
  versionId: string;
  status: RunStatus;
  input: Json;
  /**
   * Step ID to its binding ({output}, or {error} for a handled failure), from
   * the run's steps and every branch's steps, as expressions read them.
   */
  completed: Readonly<Record<string, Json>>;
  /** Instance keys the run is at. */
  active: readonly string[];
  /** Instance keys waiting (the keys of `state.waits`). */
  waiting: readonly string[];
  incidents: readonly { key: string; code: string }[];
  /**
   * A loop's instance key to how many items it had (`orders`, `lines[2]`):
   * the one thing the run keeps of a loop that ended, whose items and
   * frames it clears.
   */
  loops: Readonly<Record<string, number>>;
}

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isUuid = (value: unknown): value is string =>
  typeof value === "string" && uuid.test(value);
const isStatus = (value: unknown): value is RunStatus =>
  (runStatuses as readonly unknown[]).includes(value);
/** An object field: absent reads as empty; any other shape is unreadable. */
const objectField = (value: unknown): Record<string, unknown> | null =>
  value === undefined ? {} : isRecord(value) ? value : null;

/** The run's record, or null when any field it relies on has another shape. */
export function readRunRecord(value: unknown): RunRecord | null {
  if (!isRecord(value) || !isUuid(value["id"])) return null;
  const activation = value["activation"];
  const request = isRecord(activation) ? activation["request"] : null;
  const versionId = isRecord(request) ? request["version_id"] : null;
  if (!isUuid(versionId)) return null;
  const state = value["state"];
  if (!isRecord(state) || !isStatus(state["status"])) return null;
  const steps = state["steps"];
  if (!isRecord(steps)) return null;
  const active = state["active"] ?? [];
  if (!Array.isArray(active) || !active.every((key) => typeof key === "string"))
    return null;
  const waits = objectField(state["waits"]);
  const incidents = objectField(state["incidents"]);
  const branches = objectField(state["branches"]);
  const loopStates = objectField(state["loops"]);
  if (!waits || !incidents || !branches || !loopStates) return null;
  const loops: Record<string, number> = {};
  for (const [key, loop] of Object.entries(loopStates)) {
    const count = isRecord(loop) ? loop["count"] : null;
    if (typeof count !== "number" || !Number.isSafeInteger(count) || count < 0)
      return null;
    loops[key] = count;
  }
  const completed: Record<string, Json> = { ...steps };
  for (const branch of Object.values(branches)) {
    const inner = isRecord(branch) ? objectField(branch["steps"]) : null;
    if (!inner) return null;
    Object.assign(completed, inner);
  }
  const failures: { key: string; code: string }[] = [];
  for (const incident of Object.values(incidents)) {
    if (
      !isRecord(incident) ||
      typeof incident["node_id"] !== "string" ||
      typeof incident["code"] !== "string"
    )
      return null;
    failures.push({ key: incident["node_id"], code: incident["code"] });
  }
  return {
    id: value["id"],
    versionId,
    status: state["status"],
    input: state["input"] ?? null,
    completed,
    active: [...active],
    waiting: Object.keys(waits),
    incidents: failures,
    loops,
  };
}
