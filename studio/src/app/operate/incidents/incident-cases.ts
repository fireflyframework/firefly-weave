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
// The inbox's cases: a caller's settle timeout and the in-doubt incidents of
// the called runs it waits for are one row, which names the one to resolve
// first. The platform links them (`waits_on`, `held_by`), so grouping never
// guesses from the runs on a page. Pure: the page renders it, tests check it.
import { shortId } from "../../format";
import {
  incidentStep,
  isUnavailableIncident,
  type IncidentLink,
  type IncidentRecord,
  type IncidentView,
} from "./incidents-model";

export interface IncidentCase {
  /** The lead incident's ID. */
  key: string;
  lead: IncidentView;
  /** The lead first, then its loaded partners in link order. */
  members: IncidentView[];
  /** The incident to resolve before the others; null when its links are all decided. */
  resolveFirst: IncidentLink | null;
  /** Links whose incident isn't loaded, or already sits in another case. */
  missing: IncidentLink[];
}
export type InboxRow =
  | { kind: "case"; value: IncidentCase }
  | { kind: "incident"; value: IncidentRecord };

const linkTo = (incident: IncidentView): IncidentLink => ({
  id: incident.id,
  run_id: incident.run_id,
  code: incident.code,
  status: incident.status,
  node_id: incident.node_id,
  ...(incident.instance_key ? { instance_key: incident.instance_key } : {}),
  ...(incident.workflow ? { workflow: incident.workflow } : {}),
});
/** The platform leaves an empty list out: no links reads as none. */
const waitsOn = (incident: IncidentView) => incident.waits_on ?? [];

/**
 * The first active incident the lead waits for, followed down while that one
 * is loaded and waits for another; a called run's incident that leads its own
 * case is itself the first.
 */
function firstToResolve(
  lead: IncidentView,
  loaded: ReadonlyMap<string, IncidentView>,
): IncidentLink | null {
  const seen = new Set([lead.id]);
  let first: IncidentLink | null = null;
  let waiting: IncidentView | undefined = lead;
  while (waiting) {
    const next: IncidentLink | undefined = waitsOn(waiting).find(
      (link) => link.status === "active" && !seen.has(link.id),
    );
    if (!next) break;
    seen.add(next.id);
    first = next;
    waiting = loaded.get(next.id);
  }
  return first ?? (lead.held_by ? linkTo(lead) : null);
}

/**
 * Both bands' rows: plain incidents and cases, each loaded incident in one
 * row only. A settle timeout leads its case, with every loaded incident it
 * waits for, whichever band that sits in; a called run's incident whose
 * caller's isn't loaded leads its own. A case sits where its first member sat.
 */
export function inboxRows(
  high: readonly IncidentRecord[],
  normal: readonly IncidentRecord[],
): { high: InboxRow[]; normal: InboxRow[] } {
  const loaded = new Map<string, IncidentView>();
  for (const item of [...high, ...normal])
    if (!isUnavailableIncident(item) && !loaded.has(item.id))
      loaded.set(item.id, item);
  const caseOf = new Map<string, IncidentCase>();

  const open = (lead: IncidentView) => {
    const value: IncidentCase = {
      key: lead.id,
      lead,
      members: [],
      resolveFirst: firstToResolve(lead, loaded),
      missing: [],
    };
    const isMissing = (link: IncidentLink) => {
      if (!value.missing.some((item) => item.id === link.id))
        value.missing.push(link);
    };
    const add = (incident: IncidentView) => {
      value.members.push(incident);
      caseOf.set(incident.id, value);
      for (const link of waitsOn(incident)) {
        const partner = loaded.get(link.id);
        if (!partner) isMissing(link);
        else if (!caseOf.has(partner.id)) add(partner);
        else if (caseOf.get(partner.id) !== value) isMissing(link);
      }
    };
    add(lead);
    if (lead.held_by && !loaded.has(lead.held_by.id)) isMissing(lead.held_by);
  };

  const waiting = [...loaded.values()].filter((item) => waitsOn(item).length);
  const waitedFor = new Set(
    waiting.flatMap((item) => waitsOn(item).map((link) => link.id)),
  );
  // Nobody loaded waits for these: each leads its case.
  for (const item of waiting) if (!waitedFor.has(item.id)) open(item);
  // Incidents that wait for each other: the first one loaded leads.
  for (const item of waiting) if (!caseOf.has(item.id)) open(item);
  for (const item of loaded.values())
    if (!caseOf.has(item.id) && item.held_by && !loaded.has(item.held_by.id))
      open(item);

  const shown = new Set<string>();
  const rows = (items: readonly IncidentRecord[]): InboxRow[] =>
    items.flatMap((item): InboxRow[] => {
      const value = isUnavailableIncident(item)
        ? undefined
        : caseOf.get(item.id);
      const key = value ? value.key : item.id;
      if (shown.has(key)) return [];
      shown.add(key);
      return [
        value ? { kind: "case", value } : { kind: "incident", value: item },
      ];
    });
  return { high: rows(high), normal: rows(normal) };
}

/** Rows drawn together: a case, or the incidents between two cases. */
export interface RowGroup {
  key: string;
  case: IncidentCase | null;
  items: IncidentRecord[];
}
export function rowGroups(rows: readonly InboxRow[]): RowGroup[] {
  const groups: RowGroup[] = [];
  for (const row of rows) {
    const last = groups.at(-1);
    if (row.kind === "case")
      groups.push({
        key: `case-${row.value.key}`,
        case: row.value,
        items: [],
      });
    else if (last && !last.case) last.items.push(row.value);
    else groups.push({ key: row.value.id, case: null, items: [row.value] });
  }
  return groups;
}

/** "Case: move is waiting for a called run in doubt": the step that waits, whichever incident leads. */
export function caseLabel({ lead }: IncidentCase): string {
  const step = waitsOn(lead).length ? lead.node_id : lead.held_by?.node_id;
  return `Case: ${incidentStep(step ?? null) ?? "the run"} is waiting for a called run in doubt`;
}

/** "add in run 9a01c7d2": where a linked incident is. */
export function linkPlace(
  link: Pick<IncidentLink, "node_id" | "run_id">,
): string {
  const run = `run ${shortId(link.run_id)}`;
  const step = incidentStep(link.node_id);
  return step ? `${step} in ${run}` : run;
}

/** "Not in this list · move in run 6d18b3f0". */
export function missingText(link: IncidentLink): string {
  return `Not in this list · ${linkPlace(link)}`;
}
