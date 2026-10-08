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
// What the Incidents inbox says about each incident, its filters, the three
// decisions the Resolve incident dialog offers and how it checks them before
// sending. Pure: the page renders it, tests check it.
import type { Tone } from "../../status-labels";

export type IncidentStatus = "active" | "closed" | "resolved";
export type ResolutionKind =
  | "retry_safe"
  | "accept_reconciled_result"
  | "terminate";

/** An incident as the platform reports it (`incidents.list`). */
export interface IncidentView {
  id: string;
  run_id: string;
  incident_key: string;
  node_id: string | null;
  generation: number | null;
  origin_code: string;
  code: string;
  status: IncidentStatus;
  revision: number;
  actor_id?: string | null;
  resolved_at?: string | null;
  resolution?: {
    receipt_id: string;
    kind: ResolutionKind;
    reason: string;
    output?: unknown;
    evidence_reference?: string | null;
  } | null;
  external_effects_may_continue: boolean;
}
export interface UnavailableIncident {
  id: string;
  unavailable: true;
}
export type IncidentRecord = IncidentView | UnavailableIncident;
export const isUnavailableIncident = (
  incident: IncidentRecord,
): incident is UnavailableIncident =>
  "unavailable" in incident && !!incident.unavailable;

const statuses: Record<IncidentStatus, [string, Tone]> = {
  active: ["Active", "danger"],
  resolved: ["Resolved", "success"],
  closed: ["Closed", "neutral"],
};
export function incidentStatusLabel(status: IncidentStatus) {
  return statuses[status]?.[0] ?? status;
}
export function incidentTone(status: IncidentStatus): Tone {
  return statuses[status]?.[1] ?? "neutral";
}

/** The decisions, in the order the dialog offers them. */
export const resolutions: {
  kind: ResolutionKind;
  label: string;
  help: string;
}[] = [
  {
    kind: "retry_safe",
    label: "Retry the step",
    help: "Only if repeating the step is safe.",
  },
  {
    kind: "accept_reconciled_result",
    label: "Accept a verified result",
    help: "Use the result you verified in the other system. The platform checks it against the step's output schema.",
  },
  {
    kind: "terminate",
    label: "End the run",
    help: "Ends the run. Work already sent to other systems may continue.",
  },
];
export function resolutionLabel(kind: ResolutionKind) {
  return resolutions.find((item) => item.kind === kind)?.label ?? kind;
}

/** What the person entered in the Resolve incident dialog. */
export interface ResolutionDraft {
  kind: ResolutionKind | "";
  reason: string;
  evidence: string;
  output: string;
}
/** The body of `POST …/incidents/{id}/resolve`, without its receipt. */
export interface ResolutionRequest {
  kind: ResolutionKind;
  reason: string;
  evidence_reference?: string;
  output?: unknown;
}
export type DraftProblems = Partial<
  Record<"kind" | "reason" | "evidence" | "output", string>
>;
export const REASON_LIMIT = 2000;

/** Checks a draft; returns the request, or the problem under each field. */
export function checkResolution(
  draft: ResolutionDraft,
): { request: ResolutionRequest } | { problems: DraftProblems } {
  const problems: DraftProblems = {};
  const reason = draft.reason.trim();
  const evidence = draft.evidence.trim();
  if (!draft.kind) problems.kind = "Choose a decision.";
  if (!reason) problems.reason = "Enter a reason for the audit log.";
  else if (draft.reason.length > REASON_LIMIT)
    problems.reason = `Keep the reason to ${REASON_LIMIT.toLocaleString("en-US")} characters or fewer.`;
  if (evidence.length > REASON_LIMIT)
    problems.evidence = `Keep the evidence reference to ${REASON_LIMIT.toLocaleString("en-US")} characters or fewer.`;
  let output: unknown;
  if (draft.kind === "accept_reconciled_result") {
    if (!evidence)
      problems.evidence = "Enter where the verified result can be checked.";
    try {
      output = JSON.parse(draft.output);
    } catch {
      problems.output =
        'Enter the verified result as JSON, for example {"approved": true}.';
    }
  }
  if (Object.keys(problems).length) return { problems };
  return {
    request: {
      kind: draft.kind as ResolutionKind,
      reason: draft.reason,
      ...(evidence ? { evidence_reference: evidence } : {}),
      ...(draft.kind === "accept_reconciled_result" ? { output } : {}),
    },
  };
}

/** Inbox filters; they live in the address. Active is the default. */
export interface IncidentFilters {
  status: IncidentStatus | "all";
  code: string;
}
export const defaultIncidentFilters: IncidentFilters = {
  status: "active",
  code: "",
};
export function filterIncidents(
  incidents: readonly IncidentRecord[],
  filters: IncidentFilters,
): IncidentRecord[] {
  return incidents.filter((incident) => {
    if (isUnavailableIncident(incident))
      return filters.status === "all" && !filters.code;
    if (filters.status !== "all" && incident.status !== filters.status)
      return false;
    return !filters.code || incident.code === filters.code;
  });
}
const statusValues: readonly string[] = ["all", "active", "resolved", "closed"];
export function incidentFiltersFromQuery(search: string): IncidentFilters {
  const query = new URLSearchParams(search);
  const status = query.get("status") ?? "active";
  return {
    status: statusValues.includes(status)
      ? (status as IncidentFilters["status"])
      : "active",
    code: query.get("code") ?? "",
  };
}
export function incidentFiltersToQuery(filters: IncidentFilters): string {
  const query = new URLSearchParams();
  if (filters.status !== "active") query.set("status", filters.status);
  if (filters.code) query.set("code", filters.code);
  const text = query.toString();
  return text ? `?${text}` : "";
}
/** The codes the loaded incidents carry, sorted. */
export function incidentCodes(incidents: readonly IncidentRecord[]) {
  return [
    ...new Set(
      incidents
        .filter((item) => !isUnavailableIncident(item))
        .map((item) => (item as IncidentView).code),
    ),
  ].sort();
}

/** The last engine events of a run: their types and sequence numbers only. */
export interface EventLine {
  sequence: number;
  type: string;
}
export function lastEvents(
  events: readonly { sequence: number; type: string }[],
  count = 20,
): EventLine[] {
  return events.slice(-count).map(({ sequence, type }) => ({ sequence, type }));
}
