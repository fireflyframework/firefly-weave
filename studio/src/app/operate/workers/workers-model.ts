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
// What the Workers page says about each worker: presence, claims, load, where
// it runs, and the filters the platform applies to the list. Pure: the page
// renders it, tests check it. Presence is the platform's observation, not
// container health; identity is what the worker reports, for display only.
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
  /** What the worker reports about where it runs; left out when unknown. */
  identity?: WorkerIdentity;
  /** When the platform recorded the registration; left out when unknown. */
  registered_at?: string;
  /** Where an observation of a target the reader may see places it. */
  placement?: WorkerPlacement;
  /** When it started draining; left out when unknown or not draining. */
  drained_at?: string;
}
/** Where a worker says it runs. Display only: never authority or placement. */
export interface WorkerIdentity {
  host?: string;
  runtime: "embedded" | "container" | "process";
  sdk_version?: string;
  language_version?: string;
  os?: string;
  arch?: string;
  platform_hint?: "kubernetes" | "docker" | "none";
  namespace?: string;
  node?: string;
  labels?: Record<string, string>;
}
/** The target, deployment and component the platform matched the worker to. */
export interface WorkerPlacement {
  target_id: string;
  target_name: string;
  deployment_id: string;
  component: string;
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

/** The status pills: Revoked alone, or the presence and then Draining. */
export function workerBadges(
  worker: WorkerStatus,
  presence = workerPresence(worker),
): { label: string; tone: Tone }[] {
  if (worker.revoked) return [{ label: "Revoked", tone: "neutral" }];
  const badges = [
    { label: presenceLabel(presence), tone: presenceTone(presence) },
  ];
  if (worker.draining) badges.push({ label: "Draining", tone: "warning" });
  return badges;
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

/**
 * Where the worker runs: "Target › component" when an observation places it,
 * "In API process" for an executor inside the API, "Unmanaged" for a worker
 * that reports where it runs but no target the reader may see holds, and
 * "Unknown" for one that reports nothing.
 */
export function workerWhere(worker: WorkerStatus): string {
  if (worker.placement)
    return `${worker.placement.target_name} › ${worker.placement.component}`;
  if (!worker.identity) return "Unknown";
  return worker.identity.runtime === "embedded"
    ? "In API process"
    : "Unmanaged";
}
/** "SDK 0.1.0a15 · Python 3.12.4", or "" when the worker reports neither. */
export function workerVersion(worker: WorkerStatus): string {
  const identity = worker.identity;
  return [
    identity?.sdk_version ? `SDK ${identity.sdk_version}` : "",
    identity?.language_version ? `Runtime ${identity.language_version}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
}

/**
 * Filters the platform applies to the list. Every one but the host prefix
 * lives in the address; a host prefix may name a machine, so it stays on the
 * page and goes when the platform, environment or person changes.
 */
export interface WorkerFilters {
  presence: Presence | "";
  /** Draining (true), not draining (false), or either (null). */
  draining: boolean | null;
  release: string;
  taskType: string;
  target: string;
  hostPrefix: string;
  /** Show retired workers: revoked and long offline ones too. */
  revoked: boolean;
  order: WorkerOrder;
}
export type WorkerOrder = "last_seen_desc" | "registered_desc";
export const noWorkerFilters: WorkerFilters = {
  presence: "",
  draining: null,
  release: "",
  taskType: "",
  target: "",
  hostPrefix: "",
  revoked: false,
  order: "last_seen_desc",
};

/** The `workers.list` query for the filters, from the start or after `cursor`. */
export function workerQuery(
  filters: WorkerFilters,
  cursor: string | null,
): string {
  const query = new URLSearchParams({
    limit: "50",
    state: filters.revoked ? "all" : "current",
    order: filters.order,
  });
  const presence = {
    online: "recent",
    offline: "stale",
    unknown: "unknown",
  } as const;
  if (filters.presence) query.set("presence", presence[filters.presence]);
  if (filters.draining !== null)
    query.set("draining", String(filters.draining));
  for (const [key, value] of [
    ["release_id", filters.release],
    ["task_type", filters.taskType],
    ["target_id", filters.target],
    ["host_prefix", filters.hostPrefix],
  ])
    if (value) query.set(key, value);
  if (cursor) query.set("cursor", cursor);
  return query.toString();
}

/** How many filters narrow the list ("Clear filters" shows from one). */
export function filterCount(filters: WorkerFilters) {
  return [
    filters.presence,
    filters.draining !== null,
    filters.release,
    filters.taskType,
    filters.target,
    filters.hostPrefix,
    filters.revoked,
  ].filter(Boolean).length;
}

const presences: readonly string[] = ["online", "offline", "unknown"];
const orders: readonly string[] = ["last_seen_desc", "registered_desc"];
/** Reads the filters from "?presence=online&draining=true&task_type=…". */
export function filtersFromQuery(search: string): WorkerFilters {
  const query = new URLSearchParams(search);
  const presence = query.get("presence") ?? "";
  const draining = query.get("draining");
  const order = query.get("order") ?? "";
  return {
    presence: presences.includes(presence) ? (presence as Presence) : "",
    draining: draining === "true" ? true : draining === "false" ? false : null,
    release: query.get("release") ?? "",
    taskType: query.get("task_type") ?? "",
    target: query.get("target") ?? "",
    hostPrefix: "",
    revoked: query.get("revoked") === "true",
    order: orders.includes(order) ? (order as WorkerOrder) : "last_seen_desc",
  };
}
/** The address query for the filters, "" when none is set; never the host prefix. */
export function filtersToQuery(filters: WorkerFilters): string {
  const query = new URLSearchParams();
  if (filters.presence) query.set("presence", filters.presence);
  if (filters.draining !== null)
    query.set("draining", String(filters.draining));
  if (filters.release) query.set("release", filters.release);
  if (filters.taskType) query.set("task_type", filters.taskType);
  if (filters.target) query.set("target", filters.target);
  if (filters.revoked) query.set("revoked", "true");
  if (filters.order !== "last_seen_desc") query.set("order", filters.order);
  const text = query.toString();
  return text ? `?${text}` : "";
}

/**
 * The releases and task types to offer: those the loaded workers report,
 * those of the releases read for the environment, and the ones chosen.
 */
export function filterChoices(
  workers: readonly WorkerRecord[],
  releases: readonly { id: string; task_types: readonly string[] }[] = [],
  chosen: Pick<WorkerFilters, "release" | "taskType"> = noWorkerFilters,
) {
  const ids = new Set<string>();
  const taskTypes = new Set<string>();
  for (const worker of workers) {
    if (isUnavailableWorker(worker)) continue;
    ids.add(worker.release_id);
    for (const type of worker.task_types) taskTypes.add(type);
  }
  for (const release of releases) {
    ids.add(release.id);
    for (const type of release.task_types) taskTypes.add(type);
  }
  if (chosen.release) ids.add(chosen.release);
  if (chosen.taskType) taskTypes.add(chosen.taskType);
  return { releases: [...ids].sort(), taskTypes: [...taskTypes].sort() };
}

/** A worker release as the platform reports it (`releases.list`, `releases.read`). */
export interface WorkerRelease {
  id: string;
  image_digest: string;
  capabilities: { taskType: string; taskVersion: string }[];
  credential_capabilities?: string[];
  connector_bindings?: unknown[];
}
/** What runs a release (`releases.usage`): only targets the reader may see. */
export interface ReleaseUsage {
  workers: {
    online: number;
    offline: number;
    draining: number;
    revoked: number;
  };
  deployments: {
    target_id: string;
    target_name: string;
    deployment_id: string;
    deployment_name: string;
    component: string;
    replicas: number;
  }[];
  activations: number;
  next_cursor?: string | null;
}
/** The task types a release serves, as workers register them ("echo@1.2.0"). */
export function releaseTaskTypes(release: WorkerRelease): string[] {
  return (release.capabilities ?? []).map(
    (capability) => `${capability.taskType}@${capability.taskVersion}`,
  );
}
/** "sha256:3f2a9b1c…": the start of the image digest a release admits. */
export function shortDigest(digest: string): string {
  const [algorithm, value] = digest.includes(":")
    ? digest.split(":", 2)
    : ["", digest];
  return `${algorithm ? algorithm + ":" : ""}${value.slice(0, 12)}`;
}
/**
 * Admitting a release stays a CLI step: the exact command, which reads the
 * release request (image digest and capabilities) from a file.
 */
export const releaseAdmissionCommand =
  "weave workers releases create --request release.json";

/** The release usage query, from the start or after `cursor`. */
export function releaseUsageQuery(cursor: string | null): string {
  const query = new URLSearchParams({ limit: "100" });
  if (cursor) query.set("cursor", cursor);
  return query.toString();
}

/** The Retire offline workers thresholds, in seconds; 24 hours is the default. */
export const retireThresholds: { seconds: number; label: string }[] = [
  { seconds: 86_400, label: "24 hours" },
  { seconds: 7 * 86_400, label: "7 days" },
  { seconds: 30 * 86_400, label: "30 days" },
];
/** One retirement command revokes at most this many workers. */
export const RETIRE_BATCH = 100;
/** What a retirement preview or result says. */
export function retireSummary(result: {
  matched: number;
  revoked: number;
  dry_run: boolean;
}): string {
  const workers = (count: number) =>
    `${count.toLocaleString("en-US")} ${count === 1 ? "worker" : "workers"}`;
  if (result.dry_run) {
    if (!result.matched)
      return "No worker matches: every offline worker holds work or was seen more recently.";
    return result.matched > RETIRE_BATCH
      ? `${workers(result.matched)} match. One retirement revokes ${RETIRE_BATCH}, oldest first; retire again for the rest.`
      : `${workers(result.matched)} ${result.matched === 1 ? "matches" : "match"}. Retiring revokes ${result.matched === 1 ? "it" : "them"}.`;
  }
  const left = Math.max(0, result.matched - result.revoked);
  return left
    ? `Revoked ${workers(result.revoked)}. ${workers(left)} still ${left === 1 ? "matches" : "match"}.`
    : `Revoked ${workers(result.revoked)}.`;
}
