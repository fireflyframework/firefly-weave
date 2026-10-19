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
import { REASON_LIMIT, tooLongMessage } from "../operate-limits";

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
  /** The full step instance; left out for an incident not tied to one. */
  instance_key?: string;
  /** When it opened; left out while unknown. */
  opened_at?: string;
  /**
   * The opening time was inferred from the run's last recorded event; left
   * out (false) when it is exact. Never true without `opened_at`.
   */
  opened_at_estimated?: boolean;
  updated_at?: string;
  workflow?: { name: string; version: string };
  run_status?: string;
  test?: boolean;
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
    problems.reason = tooLongMessage("reason");
  if (evidence.length > REASON_LIMIT)
    problems.evidence = tooLongMessage("evidence reference");
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

/**
 * Inbox filters, which the platform applies; they live in the address.
 * Active incidents, newest opened first, without test runs by default.
 */
export interface IncidentFilters {
  status: IncidentStatus | "all";
  code: string;
  workflow: string;
  nodeId: string;
  /** RFC 3339 instants, or "" for no bound. */
  openedAfter: string;
  openedBefore: string;
  includeTest: boolean;
  order: IncidentOrder;
}
export type IncidentOrder = "opened_desc" | "opened_asc" | "id";
export const defaultIncidentFilters: IncidentFilters = {
  status: "active",
  code: "",
  workflow: "",
  nodeId: "",
  openedAfter: "",
  openedBefore: "",
  includeTest: false,
  order: "opened_desc",
};
/** The `incidents.list` query for the filters, from the start or after `cursor`. */
export function incidentQuery(
  filters: IncidentFilters,
  cursor: string | null,
): string {
  const query = new URLSearchParams({
    limit: "50",
    order: filters.order,
    include_test: String(filters.includeTest),
  });
  if (filters.status !== "all") query.append("status", filters.status);
  for (const [key, value] of [
    ["code", filters.code],
    ["workflow", filters.workflow],
    ["node_id", filters.nodeId],
    ["opened_after", filters.openedAfter],
    ["opened_before", filters.openedBefore],
  ])
    if (value) query.set(key, value);
  if (cursor) query.set("cursor", cursor);
  return query.toString();
}
/** How many filters differ from the defaults ("Clear filters" shows from one). */
export function incidentFilterCount(filters: IncidentFilters): number {
  return [
    filters.status !== "active",
    filters.code,
    filters.workflow,
    filters.nodeId,
    filters.openedAfter,
    filters.openedBefore,
    filters.includeTest,
  ].filter(Boolean).length;
}
const statusValues: readonly string[] = ["all", "active", "resolved", "closed"];
const orderValues: readonly string[] = ["opened_desc", "opened_asc", "id"];
const instant = (value: string | null) =>
  value && Number.isFinite(Date.parse(value))
    ? new Date(value).toISOString()
    : "";
export function incidentFiltersFromQuery(search: string): IncidentFilters {
  const query = new URLSearchParams(search);
  const status = query.get("status") ?? "active";
  const order = query.get("order") ?? "";
  return {
    status: statusValues.includes(status)
      ? (status as IncidentFilters["status"])
      : "active",
    code: query.get("code") ?? "",
    workflow: query.get("workflow") ?? "",
    nodeId: query.get("node_id") ?? "",
    openedAfter: instant(query.get("opened_after")),
    openedBefore: instant(query.get("opened_before")),
    includeTest: query.get("include_test") === "true",
    order: orderValues.includes(order)
      ? (order as IncidentOrder)
      : "opened_desc",
  };
}
/** The address query for the filters (and an open incident), "" for the defaults. */
export function incidentFiltersToQuery(
  filters: IncidentFilters,
  incident = "",
): string {
  const query = new URLSearchParams();
  if (filters.status !== "active") query.set("status", filters.status);
  if (filters.code) query.set("code", filters.code);
  if (filters.workflow) query.set("workflow", filters.workflow);
  if (filters.nodeId) query.set("node_id", filters.nodeId);
  if (filters.openedAfter) query.set("opened_after", filters.openedAfter);
  if (filters.openedBefore) query.set("opened_before", filters.openedBefore);
  if (filters.includeTest) query.set("include_test", "true");
  if (filters.order !== "opened_desc") query.set("order", filters.order);
  if (incident) query.set("incident", incident);
  const text = query.toString();
  return text ? `?${text}` : "";
}
/** The incident an address opens ("?incident=ID"), or "". */
export function incidentFromQuery(search: string): string {
  const id = new URLSearchParams(search).get("incident") ?? "";
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
    id,
  )
    ? id.toLowerCase()
    : "";
}
/** The codes and workflows the loaded incidents carry, sorted, for suggestions. */
export function incidentSuggestions(incidents: readonly IncidentRecord[]) {
  const codes = new Set<string>();
  const workflows = new Set<string>();
  const steps = new Set<string>();
  for (const item of incidents) {
    if (isUnavailableIncident(item)) continue;
    codes.add(item.code);
    if (item.workflow?.name) workflows.add(item.workflow.name);
    if (item.node_id) steps.add(item.node_id);
  }
  return {
    codes: [...codes].sort(),
    workflows: [...workflows].sort(),
    steps: [...steps].sort(),
  };
}
/**
 * When an incident opened, for the drawer: "Unknown" without a time, and the
 * estimate marked as one.
 */
export function openedText(
  incident: Pick<IncidentView, "opened_at" | "opened_at_estimated">,
  format: (value: string) => string,
): string {
  if (!incident.opened_at) return "Unknown";
  const text = format(incident.opened_at);
  if (!text) return "Unknown";
  return (incident.opened_at_estimated ?? false) ? `${text} (estimated)` : text;
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
