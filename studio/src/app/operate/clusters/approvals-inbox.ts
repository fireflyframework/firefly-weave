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
// The approvals inbox, built from what the platform already serves: every
// plan the person can read, then each recent plan's approval. Plans without
// an approval are waiting for one. The server checks the approver again.
import { describeError, type PlainError } from "../../errors";
import type {
  Approval,
  Page,
  Plan,
} from "../../operations/deployment-contracts";
import { inboxCandidates } from "./cluster-model";

/** Plans are capped at 1,000 per environment: ten pages of 100. */
const MAX_PAGES = 10;

export type InboxRequest = <T>(path: string) => Promise<T>;

export class ApprovalsInbox {
  /** Plans waiting for an approval, open ones first. */
  rows: Plan[] = [];
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
    this.loaded = false;
    this.error = null;
    this.unchecked = 0;
    this.truncated = false;
  }

  /** Reads the plans and their approvals; throws when the plans can't be read. */
  async load(scope: string, now = Date.now()): Promise<void> {
    const generation = ++this.generation;
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
      const candidates = inboxCandidates(plans, now);
      const approvals = await Promise.allSettled(
        candidates.map((plan) =>
          this.request<Approval | null>(
            `${scope}/deployment-plans/${encodeURIComponent(plan.id)}/approval`,
          ),
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
      this.truncated = !!cursor;
      this.error = null;
      this.loaded = true;
    } catch (error) {
      if (generation !== this.generation) return;
      this.error = describeError(error);
      throw error;
    }
  }
}
