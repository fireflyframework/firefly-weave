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
// One vocabulary for statuses: the words and tones that pills, filters, Home
// and the admin tables use for the API's raw values.

export type StatusKind =
  | "run"
  | "task"
  | "email"
  | "connection"
  | "worker"
  | "account";
/** A pill's `data-tone`; neutral pills carry no tone attribute. */
export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

const statuses: Record<StatusKind, Record<string, [string, Tone]>> = {
  run: {
    queued: ["Queued", "neutral"],
    running: ["Running", "info"],
    waiting: ["Waiting", "warning"],
    suspended: ["On hold", "warning"],
    paused: ["Paused", "warning"],
    succeeded: ["Succeeded", "success"],
    failed: ["Failed", "danger"],
    cancelled: ["Canceled", "neutral"],
    timed_out: ["Timed out", "danger"],
    unavailable: ["Unavailable", "neutral"],
  },
  task: {
    ready: ["Ready to claim", "warning"],
    claimed: ["Claimed", "info"],
    completed: ["Completed", "success"],
    expired: ["Expired", "neutral"],
    cancelled: ["Canceled", "neutral"],
  },
  email: {
    received: ["Received", "neutral"],
    queued: ["Waiting to send", "warning"],
    accepted: ["Sent to mail server", "success"],
    unknown: ["Send status unknown", "warning"],
  },
  connection: {
    available: ["Available", "success"],
    unavailable: ["Unavailable", "danger"],
  },
  worker: {
    active: ["Active", "success"],
    revoked: ["Revoked", "danger"],
    unavailable: ["Unavailable", "danger"],
  },
  account: {
    human: ["Person", "neutral"],
    application: ["App", "neutral"],
    worker: ["Worker", "neutral"],
  },
};

/** "timed_out" reads "Timed out": a value the table doesn't know yet. */
const sentence = (value: string) => {
  const words = value.replaceAll("_", " ").trim().toLowerCase();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "";
};

/**
 * The label for a raw status value. A claimed task reads "Claimed by you"
 * when it is the viewer's own (`mine`).
 */
export function statusLabel(
  kind: StatusKind,
  value: unknown,
  options: { mine?: boolean } = {},
): string {
  const raw = typeof value === "string" ? value : "";
  if (kind === "task" && raw === "claimed" && options.mine)
    return "Claimed by you";
  return statuses[kind][raw]?.[0] ?? (sentence(raw) || "Status unavailable");
}

/** The tone for a raw status value; unknown values are neutral. */
export function statusTone(kind: StatusKind, value: unknown): Tone {
  const raw = typeof value === "string" ? value : "";
  return statuses[kind][raw]?.[1] ?? "neutral";
}

/** The `data-tone` attribute value: none for neutral pills. */
export function toneAttribute(tone: Tone): string | null {
  return tone === "neutral" ? null : tone;
}

/** Filter choices for a kind, in the table's order: [value, label]. */
export function statusOptions(kind: StatusKind): [string, string][] {
  return Object.entries(statuses[kind])
    .filter(([value]) => value !== "unavailable" && value !== "paused")
    .map(([value, [label]]) => [value, label]);
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/**
 * A run's status value: "paused" while an operator holds it, "unavailable"
 * for a run the platform can't show, otherwise `state.status`.
 */
export function runStatus(run: Record<string, unknown>): string {
  if (run["unavailable"]) return "unavailable";
  const state = isRecord(run["state"]) ? run["state"] : {};
  if (state["manual_paused"]) return "paused";
  const status = state["status"] ?? run["status"];
  return typeof status === "string" ? status : "";
}
