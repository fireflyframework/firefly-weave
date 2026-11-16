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
// A workflow's runs in its Runs tab: the query that reads them, one row per
// run, how a refresh of the first page merges with the runs loaded below
// it, and what an empty list says. Pure: the caller passes the time.
import { absoluteTime, relativeTime, shortId } from "../../format";
import {
  isUnavailableRun,
  type RunOrigin,
  type RunStatus,
  type RunSummaryItem,
  type RunSummaryQuery,
} from "../../operate/run-contracts";
import {
  liveDuration,
  originLabel,
  runOutcome,
} from "../../operate/runs/runs-model";
import type { Tone } from "../../status-labels";

/** The list's filters. They live in memory only, never in the address. */
export interface RunsTabFilters {
  status: RunStatus[];
  version: string | null;
  origin: RunOrigin | null;
  includeTest: boolean;
}
export const defaultRunsTabFilters = (): RunsTabFilters => ({
  status: [],
  version: null,
  origin: null,
  includeTest: true,
});
/** How many filters differ from the defaults. */
export const filterCount = (filters: RunsTabFilters): number =>
  [
    filters.status.length > 0,
    filters.version !== null,
    filters.origin !== null,
    !filters.includeTest,
  ].filter(Boolean).length;

/** Why the Runs tab can't list runs yet, or "ready". */
export type RunsTabMode =
  | "offline"
  | "no-environment"
  | "signed-out"
  | "pending"
  | "unnamed"
  | "ready";
/**
 * What the Runs tab can list: nothing without a platform, an environment, a
 * sign-in or the account's identity; nothing for a workflow without a name
 * either, whose query would name no workflow and list every run in the
 * environment.
 */
export function runsTabMode(facts: {
  profile: { environmentId?: string | null } | null;
  identity: unknown;
  /** Nobody is signed in to the platform, or the sign-in ended. */
  signedOut: boolean;
  name: string;
}): RunsTabMode {
  if (!facts.profile) return "offline";
  if (!facts.profile.environmentId) return "no-environment";
  if (facts.signedOut) return "signed-out";
  if (!facts.identity) return "pending";
  return facts.name.trim() ? "ready" : "unnamed";
}

export const RUNS_TAB_PAGE = 50;
/** The list stops loading older runs here and says so. */
export const MAX_LISTED_RUNS = 500;
export const MORE_THAN_LISTED =
  "Showing the newest 500 runs. Use the filters, or Operate › Runs, to find older ones.";

/** One page of the workflow's runs, newest first. */
export function runsTabQuery(
  workflow: string,
  filters: RunsTabFilters,
  cursor?: string,
): RunSummaryQuery {
  return {
    workflow,
    include_test: filters.includeTest,
    order: "started_desc",
    limit: RUNS_TAB_PAGE,
    ...(filters.status.length ? { status: [...filters.status] } : {}),
    // The platform reads a version only within one workflow.
    ...(filters.version !== null && workflow
      ? { version: filters.version }
      : {}),
    ...(filters.origin !== null ? { origin: filters.origin } : {}),
    ...(cursor ? { cursor } : {}),
  };
}

/** How a run started, as an icon of icon-data.ts. */
export const ORIGIN_ICONS: Readonly<Record<RunOrigin, string>> = {
  manual: "manualTrigger",
  webhook: "webhook",
  schedule: "schedule",
  broker: "kafka",
  email: "email",
  provider: "appEvent",
  retry: "refresh",
  call: "calledTrigger",
  test: "runHistory",
};

export interface RunRow {
  id: string;
  /** "1c0d7f8a" */
  short: string;
  /** "Failed", "Succeeded with errors", or "Unavailable". */
  label: string;
  tone: Tone;
  /** "2 hours ago" */
  started: string;
  /** "Wed 7 Oct, 10:00", for the title. */
  startedTitle: string;
  duration: string;
  version: string;
  test: boolean;
  origin: { label: string; icon: string } | null;
  /** "Called by run 1c0d7f8a" or "Retry of 1c0d7f8a". */
  link: string | null;
  /** "Failed at send" */
  failedAt: string | null;
  /** The step the run stopped at ("send"); null when it didn't stop at one. */
  at: string | null;
  /** The platform can't show this run: only its ID is known. */
  unavailable: boolean;
}

export function runRow(item: RunSummaryItem, now: Date): RunRow {
  if (isUnavailableRun(item))
    return {
      id: item.id,
      short: shortId(item.id),
      label: "Unavailable",
      tone: "neutral",
      started: "",
      startedTitle: "",
      duration: "",
      version: "",
      test: false,
      origin: null,
      link: null,
      failedAt: null,
      at: null,
      unavailable: true,
    };
  const outcome = runOutcome(item);
  return {
    id: item.id,
    short: shortId(item.id),
    label: outcome.label,
    tone: outcome.tone,
    started: relativeTime(item.started_at, now),
    startedTitle: absoluteTime(item.started_at),
    duration: liveDuration(item, now),
    version: item.workflow.version,
    test: item.test,
    origin: item.origin
      ? { label: originLabel(item.origin), icon: ORIGIN_ICONS[item.origin] }
      : null,
    link: item.caller
      ? `Called by run ${shortId(item.caller.run_id)}`
      : item.retried_from_run_id
        ? `Retry of ${shortId(item.retried_from_run_id)}`
        : null,
    failedAt: item.failed_step ? `Failed at ${item.failed_step.node_id}` : null,
    at: item.failed_step?.node_id ?? null,
    unavailable: false,
  };
}

/**
 * The list after its first page is read again: that page leads, its runs
 * replacing their earlier copies, and the older runs loaded below it keep
 * their order. At most MAX_LISTED_RUNS.
 */
export function refreshed(
  current: readonly RunSummaryItem[],
  firstPage: readonly RunSummaryItem[],
): RunSummaryItem[] {
  const seen = new Set<string>();
  const merged: RunSummaryItem[] = [];
  for (const item of [...firstPage, ...current]) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    merged.push(item);
    if (merged.length === MAX_LISTED_RUNS) break;
  }
  return merged;
}

/** What the list says with no runs; `clear` offers Clear filters. */
export function emptyText(
  workflow: string,
  filters: RunsTabFilters,
): { title: string; detail: string; clear: boolean } {
  return filterCount(filters)
    ? { title: "No runs match these filters.", detail: "", clear: true }
    : {
        title: `No runs of ${workflow} yet.`,
        detail: "Runs appear here once it's activated and started.",
        clear: false,
      };
}
