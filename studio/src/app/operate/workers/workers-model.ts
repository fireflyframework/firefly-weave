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
// What the Workers page says about each worker: presence, claims, load and
// the filters that narrow the loaded list. Pure: the page renders it, tests
// check it. Presence is the platform's observation, not container health.
import type { Tone } from "../../status-labels";

/** A worker as the platform reports it (`workers.list`, `workers.read`). */
export interface WorkerStatus {
  id: string;
  principal_id: string;
  release_id: string;
  task_types: string[];
  capacity: number;
  revoked: boolean;
  revision: number;
  draining: boolean;
  presence: "unknown" | "recent" | "stale";
  last_seen_at: string | null;
  presence_expires_at: string | null;
  presence_ttl_seconds?: number;
  observed_at: string;
  active_leases: number;
  available_capacity: number | null;
}
/** A worker the platform can't show. */
export interface UnavailableWorker {
  id: string;
  unavailable: true;
  omissions?: unknown[];
}
export type WorkerRecord = WorkerStatus | UnavailableWorker;
export const isUnavailableWorker = (
  worker: WorkerRecord,
): worker is UnavailableWorker =>
  "unavailable" in worker && !!worker.unavailable;

/** Online while contact is recent, offline once it lapses, or not seen yet. */
export type Presence = "online" | "offline" | "unknown";
export function workerPresence(
  worker: WorkerStatus,
  now = Date.now(),
): Presence {
  if (!worker.presence || worker.presence === "unknown") return "unknown";
  return worker.presence === "recent" &&
    Date.parse(worker.presence_expires_at ?? "") > now
    ? "online"
    : "offline";
}
const presenceWords: Record<Presence, [string, Tone]> = {
  online: ["Online", "success"],
  offline: ["Offline", "danger"],
  unknown: ["Not seen yet", "neutral"],
};
export function presenceLabel(presence: Presence) {
  return presenceWords[presence][0];
}
export function presenceTone(presence: Presence): Tone {
  return presenceWords[presence][1];
}

/** Whether the worker takes new tasks. */
export function claimsLabel(worker: WorkerStatus) {
  return worker.revoked
    ? "Revoked"
    : worker.draining
      ? "Draining"
      : "Accepting new tasks";
}

/** Active tasks against the registered task slots, for the load meter. */
export interface Load {
  active: number | null;
  capacity: number | null;
  /** 0 to 1 for the meter's fill. */
  fill: number;
  over: boolean;
  text: string;
}
export function workerLoad(worker: WorkerStatus): Load {
  const active = Number.isInteger(worker.active_leases)
    ? worker.active_leases
    : null;
  const capacity = Number.isInteger(worker.capacity) ? worker.capacity : null;
  if (active === null || !capacity)
    return { active, capacity, fill: 0, over: false, text: "Unknown" };
  return {
    active,
    capacity,
    fill: Math.min(1, active / capacity),
    over: active > capacity,
    text: `${active} of ${capacity}`,
  };
}

/** Filters over the loaded workers; they live in the address. */
export interface WorkerFilters {
  presence: Presence | "";
  draining: boolean;
  release: string;
  taskType: string;
  revoked: boolean;
}
export const noWorkerFilters: WorkerFilters = {
  presence: "",
  draining: false,
  release: "",
  taskType: "",
  revoked: false,
};

export function filterWorkers(
  workers: readonly WorkerRecord[],
  filters: WorkerFilters,
  now = Date.now(),
): WorkerRecord[] {
  return workers.filter((worker) => {
    if (isUnavailableWorker(worker)) return filterCount(filters) === 0;
    if (worker.revoked && !filters.revoked) return false;
    if (filters.presence && workerPresence(worker, now) !== filters.presence)
      return false;
    if (filters.draining && !worker.draining) return false;
    if (filters.release && worker.release_id !== filters.release) return false;
    if (filters.taskType && !worker.task_types.includes(filters.taskType))
      return false;
    return true;
  });
}

/** How many filters narrow the list ("Clear filters" shows from one). */
export function filterCount(filters: WorkerFilters) {
  return [
    filters.presence,
    filters.draining,
    filters.release,
    filters.taskType,
    filters.revoked,
  ].filter(Boolean).length;
}

const presences: readonly string[] = ["online", "offline", "unknown"];
/** Reads the filters from "?presence=online&draining=true&task_type=…". */
export function filtersFromQuery(search: string): WorkerFilters {
  const query = new URLSearchParams(search);
  const presence = query.get("presence") ?? "";
  return {
    presence: presences.includes(presence) ? (presence as Presence) : "",
    draining: query.get("draining") === "true",
    release: query.get("release") ?? "",
    taskType: query.get("task_type") ?? "",
    revoked: query.get("revoked") === "true",
  };
}
/** The query for the filters, "" when none is set. */
export function filtersToQuery(filters: WorkerFilters): string {
  const query = new URLSearchParams();
  if (filters.presence) query.set("presence", filters.presence);
  if (filters.draining) query.set("draining", "true");
  if (filters.release) query.set("release", filters.release);
  if (filters.taskType) query.set("task_type", filters.taskType);
  if (filters.revoked) query.set("revoked", "true");
  const text = query.toString();
  return text ? `?${text}` : "";
}

/** The releases and task types the loaded workers report, sorted. */
export function filterChoices(workers: readonly WorkerRecord[]) {
  const releases = new Set<string>();
  const taskTypes = new Set<string>();
  for (const worker of workers) {
    if (isUnavailableWorker(worker)) continue;
    releases.add(worker.release_id);
    for (const type of worker.task_types) taskTypes.add(type);
  }
  return { releases: [...releases].sort(), taskTypes: [...taskTypes].sort() };
}
