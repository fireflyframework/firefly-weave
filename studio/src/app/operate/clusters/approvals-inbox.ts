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
// The approvals inbox: change requests waiting for an approval or ready to
// apply, then plans made outside a change request, each recent plan with its
// approval. Plans without an approval are waiting for one. The server checks
// the approver again.
import { describeError, type PlainError } from "../../errors";
import type {
  Approval,
  ChangeRequest,
  Page,
  Plan,
} from "../../operations/deployment-contracts";
import { MAX_PAGES } from "../operate-limits";
import {
  approveBy,
  changeCountdown,
  effectiveState,
  newApproval,
} from "./change-request-model";
import { EXPIRED_KEPT_MS, inboxCandidates } from "./cluster-model";

export type InboxRequest = <T>(path: string) => Promise<T>;

/**
 * How many approvals the inbox reads at once. The Studio host serves a few
 * requests at a time; a poll that asked for every plan's approval together
 * would hold all of them, and other tabs or windows would wait or be refused.
 */
export const APPROVAL_READS_AT_ONCE = 4;

/**
 * Runs `task` for each item with at most `limit` running at once, and
 * settles like Promise.allSettled: one result per item, in the items' order.
 */
export async function settleEach<T, R>(
  items: readonly T[],
  limit: number,
  task: (item: T) => Promise<R>,
): Promise<PromiseSettledResult<R>[]> {
  const results: PromiseSettledResult<R>[] = new Array(items.length);
  let next = 0;
  const reader = async () => {
    while (next < items.length) {
      const index = next++;
      try {
        results[index] = {
          status: "fulfilled",
          value: await task(items[index]),
        };
      } catch (reason) {
        results[index] = { status: "rejected", reason };
      }
    }
  };
  const readers = Math.min(Math.max(1, limit), items.length);
  await Promise.all(Array.from({ length: readers }, reader));
  return results;
}

/** One inbox row: a change request, or a plan made outside one. */
export type InboxEntry =
  | { kind: "change"; id: string; deadline: number; change: ChangeRequest }
  | { kind: "plan"; id: string; deadline: number; plan: Plan };

/** The change request states the inbox shows. */
const SHOWN = new Set(["awaiting_approval", "approved", "expired"]);

/**
 * The inbox rows: change requests waiting for an approval or ready to apply,
 * and those that expired in the last 15 minutes, with the plans no change
 * request lists. Open rows come first, the soonest to approve first; ended
 * ones follow, the most recent first.
 */
export function inboxEntries(
  changes: readonly ChangeRequest[],
  plans: readonly Plan[],
  now = Date.now(),
): InboxEntry[] {
  const listed = new Set(changes.map((item) => item.plan_id));
  const entries: InboxEntry[] = [
    ...changes
      .filter((item) => {
        const state = effectiveState(item, now);
        if (!SHOWN.has(state)) return false;
        if (state !== "expired") return true;
        const ended = Date.parse(item.plan_expires_at ?? "");
        return ended > now - EXPIRED_KEPT_MS;
      })
      .map((item) => ({
        kind: "change" as const,
        id: item.id,
        deadline: approveBy(item) ?? 0,
        change: item,
      })),
    ...plans
      .filter((item) => !listed.has(item.id))
      .map((item) => ({
        kind: "plan" as const,
        id: item.id,
        deadline: Date.parse(item.expires_at),
        plan: item,
      })),
  ];
  const open = entries
    .filter((entry) => entry.deadline > now)
    .sort((a, b) => a.deadline - b.deadline);
  const ended = entries
    .filter((entry) => !(entry.deadline > now))
    .sort((a, b) => b.deadline - a.deadline);
  return [...open, ...ended];
}

/** Reads every page of change requests a query names, up to MAX_PAGES. */
async function readChanges(
  request: InboxRequest,
  scope: string,
  states: readonly string[],
): Promise<{ items: ChangeRequest[]; truncated: boolean }> {
  const items: ChangeRequest[] = [];
  let cursor: string | null = null;
  let pages = 0;
  do {
    const query = new URLSearchParams({ limit: "100" });
    for (const state of states) query.append("state", state);
    if (cursor) query.set("cursor", cursor);
    const page: Page<ChangeRequest> = await request<Page<ChangeRequest>>(
      `${scope}/deployment-change-requests?${query}`,
    );
    items.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor && ++pages < MAX_PAGES);
  return { items, truncated: !!cursor };
}

/**
 * The Clusters badge and its notice: the change requests waiting for an
 * approval this person can give. The first read in an environment only
 * counts; later reads announce the first change that is new since.
 */
export class ApprovalWatch {
  count = 0;
  private seen: Set<string> | null = null;
  private scope = "";
  constructor(private readonly request: InboxRequest) {}
  clear() {
    this.count = 0;
    this.seen = null;
    this.scope = "";
  }
  async load(
    scope: string,
    approvable: (row: ChangeRequest) => boolean,
    now = Date.now(),
  ): Promise<{ id: string; text: string } | null> {
    if (scope !== this.scope) this.clear();
    this.scope = scope;
    const { items } = await readChanges(this.request, scope, [
      "awaiting_approval",
    ]);
    if (scope !== this.scope) return null;
    const mine = items.filter(approvable);
    this.count = mine.length;
    const fresh = this.seen ? newApproval(this.seen, mine, now) : null;
    this.seen = new Set([...(this.seen ?? []), ...mine.map((row) => row.id)]);
    return fresh
      ? {
          id: fresh.id,
          text: `Plan ready for approval: ${fresh.title} — expires in ${changeCountdown(fresh, now).label}`,
        }
      : null;
  }
}

export class ApprovalsInbox {
  /** Plans made outside a change request that wait for an approval. */
  rows: Plan[] = [];
  /** Change requests the inbox shows. */
  changes: ChangeRequest[] = [];
  /** Every row, in the order the inbox shows them. */
  entries: InboxEntry[] = [];
  /** Why the change requests could not be read, while the plans could. */
  changeError: PlainError | null = null;
  private plans = new Map<string, Plan>();
  /** A plan the last read returned, for a change request's risks. */
  plan(id: string | null): Plan | undefined {
    return id ? this.plans.get(id) : undefined;
  }
  loaded = false;
  error: PlainError | null = null;
  /** Plans whose approval could not be read this time. */
  unchecked = 0;
  /** More plans exist than the inbox reads. */
  truncated = false;
  private generation = 0;

  constructor(private readonly request: InboxRequest) {}

  /** Forgets the rows, for example when the workspace changes. */
  clear() {
    this.generation++;
    this.rows = [];
    this.changes = [];
    this.entries = [];
    this.changeError = null;
    this.loaded = false;
    this.error = null;
    this.unchecked = 0;
    this.truncated = false;
  }

  /** Reads the plans and their approvals; throws when the plans can't be read. */
  async load(scope: string, now = Date.now()): Promise<void> {
    const generation = ++this.generation;
    // Every change request, so the plans of declined or replaced ones are
    // never shown again as plans of their own.
    const changes = readChanges(this.request, scope, []).then(
      (value) => ({ value, error: null }),
      (error: unknown) => ({ value: null, error: describeError(error) }),
    );
    try {
      const plans: Plan[] = [];
      let cursor: string | null = null;
      let pages = 0;
      do {
        const query = new URLSearchParams({ limit: "100" });
        if (cursor) query.set("cursor", cursor);
        const page: Page<Plan> = await this.request<Page<Plan>>(
          `${scope}/deployment-plans?${query}`,
        );
        plans.push(...page.items);
        cursor = page.next_cursor;
      } while (cursor && ++pages < MAX_PAGES);
      this.plans = new Map(plans.map((plan) => [plan.id, plan]));
      const read = await changes;
      if (generation !== this.generation) return;
      const listed = new Set((read.value?.items ?? []).map((c) => c.plan_id));
      const candidates = inboxCandidates(
        plans.filter((plan) => !listed.has(plan.id)),
        now,
      );
      const approvals = await settleEach(
        candidates,
        APPROVAL_READS_AT_ONCE,
        (plan) =>
          this.request<Approval | null>(
            `${scope}/deployment-plans/${encodeURIComponent(plan.id)}/approval`,
          ),
      );
      if (generation !== this.generation) return;
      this.rows = candidates.filter(
        (_, index) =>
          approvals[index].status === "fulfilled" &&
          (approvals[index] as PromiseFulfilledResult<Approval | null>)
            .value === null,
      );
      this.unchecked = approvals.filter(
        (result) => result.status === "rejected",
      ).length;
      this.changeError = read.error;
      this.changes = read.value?.items ?? [];
      this.entries = inboxEntries(this.changes, this.rows, now);
      this.truncated = !!cursor || !!read.value?.truncated;
      this.error = null;
      this.loaded = true;
    } catch (error) {
      if (generation !== this.generation) return;
      this.error = describeError(error);
      throw error;
    }
  }
  /** Re-sorts the rows as time passes, without reading again. */
  resort(now = Date.now()) {
    this.entries = inboxEntries(this.changes, this.rows, now);
  }
}
