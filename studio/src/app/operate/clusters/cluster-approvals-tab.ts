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
// The Approvals tab: change requests and plans waiting for an approval, the
// soonest to approve first, each with a countdown that ticks in the browser.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  inject,
} from "@angular/core";
import { toneAttribute, type Tone } from "../../status-labels";
import type {
  ChangeRequest,
  Plan,
} from "../../operations/deployment-contracts";
import { OperateState } from "../operate-state";
import { timeAbsolute, timeIso, timeRelative } from "../operate-time";
import type { InboxEntry } from "./approvals-inbox";
import {
  FOUR_EYES_COPY,
  blockedByFourEyes,
  changeCountdown,
  changeIntentLabel,
  changeStateLabel,
  effectiveState,
  replannable,
} from "./change-request-model";
import { ClusterSection } from "./cluster-section";
import { countdown, intentLabel, riskName } from "./cluster-model";

/** What a row's countdown cell shows. */
interface RowClock {
  text: string;
  note: string;
  tone: Tone;
  expired: boolean;
  label: string;
}

@Component({
  selector: "weave-cluster-approvals-tab",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [OperateState],
  styleUrls: ["./clusters.css", "../operate.css"],
  template: `
    <section class="operations-panel">
      <h2>Approvals</h2>
      <p class="hint">
        Changes waiting for an approval, the soonest to approve first. Approving
        signs the exact plan; applying checks the approval and the target again.
      </p>
      @let inbox = page.inbox;
      @if (inbox.error?.status === 403) {
        <weave-operate-state
          kind="access"
          heading="You don't have access to plans"
          capability="deployment.read"
        />
      } @else if (inbox.error) {
        <weave-operate-state
          kind="error"
          [message]="inbox.error.message"
          [code]="inbox.error.code"
          (retry)="page.poller.refresh()"
        />
      } @else if (!inbox.loaded) {
        <weave-operate-state kind="loading" noun="approvals" />
      } @else {
        @if (store.errors.targets || store.errors.deployments) {
          <p role="alert">
            Target and deployment names could not be loaded.
            {{ store.errors.targets || store.errors.deployments }}
          </p>
        }
        @if (inbox.changeError; as failed) {
          <weave-operate-state
            kind="partial"
            [message]="'Change requests could not be read. ' + failed.message"
            [code]="failed.code"
            (retry)="page.poller.refresh()"
          />
        }
        @if (inbox.unchecked) {
          <weave-operate-state
            kind="partial"
            [message]="uncheckedText(inbox.unchecked)"
            (retry)="page.poller.refresh()"
          />
        }
        @if (!inbox.entries.length) {
          <weave-operate-state
            kind="empty"
            heading="Nothing to approve"
            text="Changes appear here while they wait for an approval. Start one with New change request on a target."
          />
        } @else {
          <div
            class="resource-table approval-table"
            role="table"
            aria-label="Plans waiting for an approval"
          >
            <div role="rowgroup">
              <div class="table-head" role="row">
                <span role="columnheader">Change</span
                ><span role="columnheader" data-wide>Requested by</span
                ><span role="columnheader" class="end">Expires in</span
                ><span role="columnheader" data-wide>Risks</span
                ><span role="columnheader" data-wide>Plan</span
                ><span role="columnheader" data-wide>Checked</span
                ><span role="columnheader"
                  ><span class="sr-only">Actions</span></span
                >
              </div>
            </div>
            <div role="rowgroup">
              @for (entry of inbox.entries; track entry.id) {
                @let time = clock(entry);
                <div
                  class="resource-row"
                  role="row"
                  [class.expired]="time.expired"
                >
                  <span
                    role="cell"
                    class="change-cell"
                    [attr.title]="subtitle(entry)"
                    ><strong>{{ title(entry) }}</strong
                    ><small>{{ where(entry) }}</small></span
                  ><span role="cell" data-wide>{{ requester(entry) }}</span
                  ><span role="cell" class="countdown end">
                    @if (time.note) {
                      <small>{{ time.note }}</small>
                    }
                    <span
                      class="status-pill numeric"
                      [attr.data-tone]="tone(time.tone)"
                      [attr.aria-label]="time.label"
                      >{{ time.text }}</span
                    ></span
                  ><span role="cell" data-wide class="risks">{{
                    risks(entry)
                  }}</span
                  ><span role="cell" data-wide
                    ><code [attr.title]="digest(entry)">{{
                      digest(entry).slice(0, 12)
                    }}</code></span
                  ><span role="cell" data-wide class="numeric">
                    @if (observedAt(entry); as observed) {
                      <time
                        [attr.datetime]="iso(observed)"
                        [attr.title]="absolute(observed)"
                        >{{ relative(observed) }}</time
                      >
                    } @else {
                      Unknown
                    }</span
                  ><span role="cell" class="approval-actions">
                    <button
                      type="button"
                      class="tertiary sm"
                      [attr.aria-label]="'Review ' + title(entry)"
                      (click)="review(entry)"
                    >
                      Review
                    </button>
                    @if (entry.kind === "change") {
                      @let record = entry.change;
                      @let state = shown(record);
                      @if (
                        state === "awaiting_approval" &&
                        canApprove(record.target_id)
                      ) {
                        @let blocked = fourEyes(record);
                        <button
                          type="button"
                          class="primary sm"
                          [attr.aria-label]="'Approve plan ' + record.title"
                          [attr.title]="blocked ? fourEyesCopy : null"
                          [attr.aria-description]="
                            blocked ? fourEyesCopy : null
                          "
                          [disabled]="store.mutating || blocked"
                          (click)="page.approveChange(record)"
                        >
                          Approve plan
                        </button>
                      } @else if (canReplan(record)) {
                        <button
                          type="button"
                          class="sm"
                          [disabled]="store.mutating"
                          (click)="page.replanChange(record)"
                        >
                          Re-observe and re-plan
                        </button>
                      }
                    } @else if (time.expired) {
                      <button
                        type="button"
                        class="sm"
                        (click)="
                          page.openDeploymentById(entry.plan.deployment_id)
                        "
                      >
                        Open deployment
                      </button>
                    } @else if (canApprove(entry.plan.target_id)) {
                      <button
                        type="button"
                        class="primary sm"
                        [attr.aria-label]="'Approve plan ' + title(entry)"
                        [disabled]="store.mutating"
                        (click)="page.approveFromInbox(entry.plan)"
                      >
                        Approve plan
                      </button>
                    }
                  </span>
                </div>
              }
            </div>
          </div>
          @if (inbox.truncated) {
            <p class="hint">
              Studio read the first 1,000 plans and change requests. Older ones
              are not checked.
            </p>
          }
        }
      }
    </section>
  `,
})
export class ClusterApprovalsTab extends ClusterSection implements OnDestroy {
  private cdr = inject(ChangeDetectorRef);
  readonly fourEyesCopy = FOUR_EYES_COPY;
  // The countdowns tick between loads, on the very second each row's end
  // falls on, so "Expired" shows the moment a plan ends.
  private timer = setTimeout(() => this.tick(), 0);
  private tick() {
    const now = Date.now();
    this.page?.inbox.resort(now);
    this.cdr.markForCheck();
    const waits = (this.page?.inbox.entries ?? []).map((entry) => {
      const left = entry.deadline - now;
      return left > 0 ? left % 1000 || 1000 : 1000;
    });
    this.timer = setTimeout(() => this.tick(), Math.min(1000, ...waits));
  }
  clock(entry: InboxEntry): RowClock {
    if (entry.kind === "plan") {
      const time = countdown(entry.plan.expires_at);
      return {
        ...time,
        label: time.expired ? "Expired" : "Expires in " + time.text,
      };
    }
    const record = entry.change;
    const state = effectiveState(record);
    if (state === "approved")
      return {
        text: "Approved",
        note: "",
        tone: "success",
        expired: false,
        label: "Approved, ready to apply",
      };
    const time = changeCountdown(record);
    const expired = time.tone === "expired";
    return {
      text: time.label,
      // Draining changes count down to when the workers must start.
      note: time.note.startsWith("Approve within")
        ? "Approve within"
        : time.note,
      tone: time.tone === "expired" ? "neutral" : time.tone,
      expired,
      label: expired
        ? "Expired"
        : time.note.startsWith("Approve within")
          ? time.note
          : "Expires in " + time.label,
    };
  }
  tone(tone: Tone) {
    return toneAttribute(tone);
  }
  shown(record: ChangeRequest) {
    return effectiveState(record);
  }
  title(entry: InboxEntry) {
    if (entry.kind === "change") return entry.change.title;
    const plan = entry.plan;
    const deployment = this.store.deployments.find(
      (item) => item.id === plan.deployment_id,
    );
    return deployment
      ? `${intentLabel(plan.intent)} · ${deployment.name}`
      : intentLabel(plan.intent);
  }
  subtitle(entry: InboxEntry) {
    if (entry.kind === "plan")
      return this.page.targetName(entry.plan.target_id);
    const record = entry.change;
    const state = effectiveState(record);
    const parts = [
      changeIntentLabel(record.intent),
      this.page.targetName(record.target_id),
    ];
    if (state !== "awaiting_approval") parts.push(changeStateLabel(state));
    return parts.join(" · ");
  }
  /** The target, and the state when it is not waiting for an approval. */
  where(entry: InboxEntry) {
    const target =
      entry.kind === "plan" ? entry.plan.target_id : entry.change.target_id;
    const parts = [this.page.targetName(target)];
    if (entry.kind === "change") {
      const state = effectiveState(entry.change);
      if (state === "approved") parts.push(changeStateLabel(state));
    }
    return parts.join(" · ");
  }
  requester(entry: InboxEntry) {
    return entry.kind === "change"
      ? this.page.personName(entry.change.requested_by)
      : "Plan only";
  }
  private planOf(entry: InboxEntry): Plan | null {
    return entry.kind === "plan"
      ? entry.plan
      : (this.page.inbox.plan(entry.change.plan_id) ?? null);
  }
  risks(entry: InboxEntry) {
    const plan = this.planOf(entry);
    if (!plan) return "Unknown";
    return plan.risks.map(riskName).join(", ") || "None reported";
  }
  digest(entry: InboxEntry) {
    return entry.kind === "plan"
      ? entry.plan.digest
      : (entry.change.plan_digest ?? "");
  }
  /** When the plan's target was last checked. */
  observedAt(entry: InboxEntry): string | null {
    const id =
      entry.kind === "plan"
        ? entry.plan.observation_id
        : entry.change.observation_id;
    const observation = this.store.observations.find((item) => item.id === id);
    if (observation) return observation.observed_at;
    if (entry.kind === "change")
      return (
        entry.change.history.find((item) => item.state === "ready_to_plan")
          ?.at ?? null
      );
    return null;
  }
  iso(value: string) {
    return timeIso(value);
  }
  absolute(value: string) {
    return timeAbsolute(value);
  }
  relative(value: string) {
    return timeRelative(value);
  }
  canApprove(targetId: string) {
    return this.host.can("deployment.approve", targetId);
  }
  fourEyes(record: ChangeRequest) {
    return blockedByFourEyes(
      record,
      this.host.identity?.principal_id,
      this.page.targetStrict(record.target_id),
    );
  }
  canReplan(record: ChangeRequest) {
    return (
      this.host.can("deployment.plan", record.target_id) && replannable(record)
    );
  }
  review(entry: InboxEntry) {
    if (entry.kind === "change") return this.page.openChange(entry.change);
    return this.openPlan(entry.plan);
  }
  uncheckedText(count: number) {
    return `${count} ${count === 1 ? "plan" : "plans"} could not be checked for an approval.`;
  }
  ngOnDestroy() {
    clearTimeout(this.timer);
  }
}
