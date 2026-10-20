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
// What Studio says and allows about a change request. Pure: the wizard, the
// progress view and the approvals inbox render it, tests check it. The
// server decides every state and checks every action again.
import type { Tone } from "../../status-labels";
import type {
  ChangeIntent,
  ChangeRequest,
  ChangeState,
} from "../../operations/deployment-contracts";

export const ACTIVE_STATES: readonly ChangeState[] = [
  "observing",
  "ready_to_plan",
  "awaiting_approval",
  "approved",
  "draining",
  "applying",
];
/** States whose plan is not applied yet: past the plan's expiry they read as expired. */
const PLANNED: readonly ChangeState[] = [
  "awaiting_approval",
  "approved",
  "draining",
];
/** Seconds a drain must leave before the plan expires, so a forced apply still lands. */
export const DRAIN_HANDOFF_SECONDS = 120;
/** Seconds a draining change's plan must outlive the drain timeout. */
export const DRAIN_PLAN_MARGIN_SECONDS = 180;
export const DEFAULT_PLAN_SECONDS = 600;
export const DRAINING_PLAN_SECONDS = 900;
/** Failure codes that only mean the plan went stale: planning again fixes them. */
const REPLANNABLE = new Set([
  "WV-PLAN-STALE",
  "WV-PRECONDITION-FAILED",
  "WV-DRAIN-WINDOW",
  "WV-JOB-CANCELLED",
]);

export function isActive(state: ChangeState): boolean {
  return ACTIVE_STATES.includes(state);
}

/** The state readers see: an unapplied plan past its expiry reads as expired (the server's rule). */
export function effectiveState(
  record: ChangeRequest,
  now = Date.now(),
): ChangeState {
  if (
    PLANNED.includes(record.state) &&
    record.plan_expires_at &&
    Date.parse(record.plan_expires_at) <= now
  )
    return "expired";
  return record.state;
}

export function drains(record: Pick<ChangeRequest, "drain">): boolean {
  return record.drain.mode === "drain_first";
}

/** When approval must happen: the plan's expiry, earlier by the drain timeout and 2 minutes when it drains. */
export function approveBy(record: ChangeRequest): number | null {
  if (!record.plan_expires_at) return null;
  const expires = Date.parse(record.plan_expires_at);
  return drains(record)
    ? expires - (record.drain.timeout_seconds + DRAIN_HANDOFF_SECONDS) * 1000
    : expires;
}

export interface ChangeCountdown {
  seconds: number;
  tone: Extract<Tone, "neutral" | "warning" | "danger"> | "expired";
  /** "9:42", or "Expired". */
  label: string;
  /** "Expires soon", "Approve within 4:10", or "". */
  note: string;
}
function clock(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
/**
 * How long a change can still be approved: more than 2 minutes is neutral, 2
 * minutes or less warns "Expires soon", 30 seconds or less is urgent, then
 * "Expired". A draining change counts down to when the drain must start.
 */
export function changeCountdown(
  record: ChangeRequest,
  now = Date.now(),
): ChangeCountdown {
  const deadline = approveBy(record);
  const seconds =
    deadline === null ? 0 : Math.max(0, Math.ceil((deadline - now) / 1000));
  if (deadline === null || seconds <= 0)
    return { seconds: 0, tone: "expired", label: "Expired", note: "" };
  const label = clock(seconds);
  const note = drains(record)
    ? `Approve within ${label}`
    : seconds <= 120
      ? "Expires soon"
      : "";
  const tone =
    seconds <= 30 ? "danger" : seconds <= 120 ? "warning" : "neutral";
  return { seconds, tone, label, note };
}

/** Why a plan lifetime is too short for a change that drains, or null. */
export function planLifetimeError(
  drainsWorkers: boolean,
  timeout: number,
  ttl: number,
): string | null {
  const needed = timeout + DRAIN_PLAN_MARGIN_SECONDS;
  if (!drainsWorkers || ttl >= needed) return null;
  return `The plan must live at least ${Math.ceil(needed / 60)} minutes to let the workers finish and apply.`;
}

export function defaultPlanSeconds(drainsWorkers: boolean): number {
  return drainsWorkers ? DRAINING_PLAN_SECONDS : DEFAULT_PLAN_SECONDS;
}

export const wizardSteps = ["Intent", "Details", "Review", "Progress"] as const;

const intents: Record<ChangeIntent, string> = {
  scale_workers: "Scale workers",
  update: "Update image or resources",
  deploy: "Deploy",
  restart_workers: "Restart workers",
};
export function changeIntentLabel(intent: ChangeIntent): string {
  return intents[intent] ?? intent;
}

const states: Record<ChangeState, [string, Tone]> = {
  observing: ["Checking the target", "info"],
  ready_to_plan: ["Making the plan", "info"],
  awaiting_approval: ["Waiting for approval", "warning"],
  approved: ["Approved", "success"],
  draining: ["Letting workers finish", "info"],
  applying: ["Applying", "info"],
  succeeded: ["Done", "success"],
  failed: ["Failed", "danger"],
  reconciliation_required: ["Needs a check", "danger"],
  expired: ["Expired", "neutral"],
  declined: ["Declined", "neutral"],
  cancelled: ["Cancelled", "neutral"],
  superseded: ["Replaced", "neutral"],
};
export function changeStateLabel(state: ChangeState): string {
  return states[state]?.[0] ?? state;
}
export function changeStateTone(state: ChangeState): Tone {
  return states[state]?.[1] ?? "neutral";
}

export interface ProgressStep {
  label: string;
  state: "done" | "current" | "todo" | "failed" | "skipped";
  detail?: string;
}
/** The vertical stepper of a change: where it is, and what is still to come. */
export function progressSteps(
  record: ChangeRequest,
  now = Date.now(),
): ProgressStep[] {
  const state = effectiveState(record, now);
  const reached = new Set(record.history.map((entry) => entry.state));
  const order: [ChangeState, string][] = [
    ["observing", "Checking the target"],
    ["awaiting_approval", "Plan ready"],
    ["approved", "Approved"],
    ["draining", "Letting workers finish"],
    ["applying", "Applying and checking"],
  ];
  const position: Record<string, number> = {
    observing: 0,
    ready_to_plan: 0,
    awaiting_approval: 1,
    approved: 2,
    draining: 3,
    applying: 4,
  };
  const steps: ProgressStep[] = order.map(([key, label], index) => {
    if (key === "draining" && !drains(record) && !reached.has("draining"))
      return { label, state: "skipped" };
    const current = position[state];
    if (current === undefined)
      return {
        label,
        state:
          reached.has(key) || (key === "observing" && reached.size)
            ? "done"
            : "todo",
      };
    return {
      label,
      state: index < current ? "done" : index === current ? "current" : "todo",
    };
  });
  const end: Record<string, ProgressStep> = {
    succeeded: { label: "Done", state: "done" },
    failed: {
      label: "Failed",
      state: "failed",
      detail: record.failure?.message,
    },
    reconciliation_required: {
      label: "Needs a check",
      state: "failed",
      detail:
        "The runner may still be changing the target. Check it, then record what you found.",
    },
    expired: {
      label: "Expired",
      state: "failed",
      detail: "The plan expired before it was applied.",
    },
    declined: { label: "Declined", state: "failed" },
    cancelled: { label: "Cancelled", state: "failed" },
    superseded: {
      label: "Replaced",
      state: "failed",
      detail: "The target or deployment changed after this change was made.",
    },
  };
  return [...steps, end[state] ?? { label: "Done", state: "todo" }];
}

/** Why the viewer may not approve: they requested or planned it and a second person is needed. */
export const FOUR_EYES_COPY =
  "You requested or planned this change. Another approver must review it.";
export function blockedByFourEyes(
  record: ChangeRequest,
  viewer: string | null | undefined,
  targetPolicyStrict = true,
): boolean {
  const strict = record.approval_policy.distinct_approver || targetPolicyStrict;
  return (
    !!viewer &&
    strict &&
    (viewer === record.requested_by || viewer === record.planned_by)
  );
}

export function replannable(record: ChangeRequest, now = Date.now()): boolean {
  const state = effectiveState(record, now);
  if (state === "expired" || state === "superseded") return true;
  if (state === "approved" && drains(record) && !drainWindowOpen(record, now))
    return true;
  return (
    state === "failed" &&
    !!record.failure &&
    REPLANNABLE.has(record.failure.code)
  );
}

/** Whether a drain may still start: the plan outlives the drain timeout plus 2 minutes. */
export function drainWindowOpen(
  record: ChangeRequest,
  now = Date.now(),
): boolean {
  if (!record.plan_expires_at) return false;
  const left = (Date.parse(record.plan_expires_at) - now) / 1000;
  return left >= record.drain.timeout_seconds + DRAIN_HANDOFF_SECONDS;
}

/** When the drain started, from the change's history. */
export function drainStartedAt(record: ChangeRequest): number | null {
  const entry = [...record.history]
    .reverse()
    .find((item) => item.state === "draining");
  return entry ? Date.parse(entry.at) : null;
}

export interface ChangeActions {
  cancel: boolean;
  replan: boolean;
  approve: "hidden" | "enabled" | "blocked";
  decline: boolean;
  drain: boolean;
  apply: boolean;
  /** Apply may be forced: the drain timeout has passed. */
  force: boolean;
}
/** What the viewer may do with a change now; the server checks each one again. */
export function changeActions(
  record: ChangeRequest,
  can: (capability: string) => boolean,
  viewer: string | null | undefined,
  now = Date.now(),
  targetPolicyStrict = true,
): ChangeActions {
  const state = effectiveState(record, now);
  const beforeApply = [
    "observing",
    "ready_to_plan",
    "awaiting_approval",
    "approved",
    "draining",
  ].includes(state);
  const ready = state === "approved" || state === "draining";
  const started = drainStartedAt(record);
  return {
    cancel: beforeApply && can("deployment.plan"),
    replan: replannable(record, now) && can("deployment.plan"),
    approve:
      state !== "awaiting_approval" || !can("deployment.approve")
        ? "hidden"
        : blockedByFourEyes(record, viewer, targetPolicyStrict)
          ? "blocked"
          : "enabled",
    decline:
      ["awaiting_approval", "approved", "draining"].includes(state) &&
      can("deployment.approve"),
    drain:
      ready &&
      record.drain_set.length > 0 &&
      can("worker.drain") &&
      (state === "draining" || drainWindowOpen(record, now)),
    apply: ready && can("deployment.apply"),
    force:
      state === "draining" &&
      started !== null &&
      now - started >= record.drain.timeout_seconds * 1000 &&
      can("worker.drain"),
  };
}

/** "Plan ready for approval: Scale fixture-worker to 3 — expires in 9:42" for the first new row, once. */
export function approvalToast(
  previous: ReadonlySet<string>,
  rows: readonly ChangeRequest[],
  now = Date.now(),
): string | null {
  const fresh = rows.find(
    (row) =>
      !previous.has(row.id) && effectiveState(row, now) === "awaiting_approval",
  );
  if (!fresh) return null;
  return `Plan ready for approval: ${fresh.title} — expires in ${changeCountdown(fresh, now).label}`;
}
