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
// The Approvals tab: plans waiting for an approval, soonest expiry first,
// each with a countdown that ticks in the browser.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnDestroy,
  inject,
} from "@angular/core";
import { toneAttribute } from "../../status-labels";
import type { Plan } from "../../operations/deployment-contracts";
import { OperateState } from "../operate-state";
import { ClusterSection } from "./cluster-section";
import { countdown, intentLabel, riskName } from "./cluster-model";

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
        Plans waiting for an approval, soonest expiry first. Approving signs the
        exact digest; apply checks the approval and the target again.
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
        <weave-operate-state kind="loading" noun="plans" />
      } @else {
        @if (store.errors.targets || store.errors.deployments) {
          <p role="alert">
            Target and deployment names could not be loaded.
            {{ store.errors.targets || store.errors.deployments }}
          </p>
        }
        @if (inbox.unchecked) {
          <weave-operate-state
            kind="partial"
            [message]="uncheckedText(inbox.unchecked)"
            (retry)="page.poller.refresh()"
          />
        }
        @if (!inbox.rows.length) {
          <weave-operate-state
            kind="empty"
            heading="Nothing to approve"
            text="Plans appear here while they wait for an approval. Create one from a deployment."
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
                ><span role="columnheader">Expires in</span
                ><span role="columnheader" data-wide>Risks</span
                ><span role="columnheader" data-wide>Digest</span
                ><span role="columnheader"
                  ><span class="sr-only">Actions</span></span
                >
              </div>
            </div>
            <div role="rowgroup">
              @for (plan of inbox.rows; track plan.id) {
                @let time = clock(plan);
                <div
                  class="resource-row"
                  role="row"
                  [class.expired]="time.expired"
                >
                  <span role="cell" class="row-text"
                    ><strong>{{ change(plan) }}</strong
                    ><small>{{ page.targetName(plan.target_id) }}</small></span
                  ><span role="cell" class="countdown"
                    ><span
                      class="status-pill numeric"
                      [attr.data-tone]="tone(time.tone)"
                      [attr.aria-label]="
                        time.expired ? 'Expired' : 'Expires in ' + time.text
                      "
                      >{{ time.text }}</span
                    >
                    @if (time.note) {
                      <small>{{ time.note }}</small>
                    }</span
                  ><span role="cell" data-wide>{{ riskList(plan) }}</span
                  ><span role="cell" data-wide
                    ><code [attr.title]="plan.digest">{{
                      plan.digest.slice(0, 12)
                    }}</code></span
                  ><span role="cell" class="approval-actions">
                    <button
                      type="button"
                      class="tertiary sm"
                      [attr.aria-label]="'Review ' + change(plan)"
                      (click)="openPlan(plan)"
                    >
                      Review
                    </button>
                    @if (time.expired) {
                      <button
                        type="button"
                        class="sm"
                        (click)="page.openDeploymentById(plan.deployment_id)"
                      >
                        Open deployment
                      </button>
                    } @else if (canApprove(plan)) {
                      <button
                        type="button"
                        class="primary sm"
                        [attr.aria-label]="'Approve plan ' + change(plan)"
                        [disabled]="store.mutating"
                        (click)="page.approveFromInbox(plan)"
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
              Studio read the first 1,000 plans. Older plans are not checked.
            </p>
          }
        }
      }
    </section>
  `,
})
export class ClusterApprovalsTab extends ClusterSection implements OnDestroy {
  private cdr = inject(ChangeDetectorRef);
  // The countdowns tick between loads, on the very second each plan's end
  // falls on, so "Expired" shows the moment a plan ends.
  private timer = setTimeout(() => this.tick(), 0);
  private tick() {
    this.cdr.markForCheck();
    const now = Date.now();
    const waits = (this.page?.inbox.rows ?? []).map((plan) => {
      const left = Date.parse(plan.expires_at) - now;
      return left > 0 ? left % 1000 || 1000 : 1000;
    });
    this.timer = setTimeout(() => this.tick(), Math.min(1000, ...waits));
  }
  clock(plan: Plan) {
    return countdown(plan.expires_at);
  }
  tone(tone: Parameters<typeof toneAttribute>[0]) {
    return toneAttribute(tone);
  }
  change(plan: Plan) {
    const deployment = this.store.deployments.find(
      (item) => item.id === plan.deployment_id,
    );
    return deployment
      ? `${intentLabel(plan.intent)} · ${deployment.name}`
      : intentLabel(plan.intent);
  }
  riskList(plan: Plan) {
    return plan.risks.map(riskName).join(", ") || "None reported";
  }
  canApprove(plan: Plan) {
    return this.host.can("deployment.approve", plan.target_id);
  }
  uncheckedText(count: number) {
    return `${count} ${count === 1 ? "plan" : "plans"} could not be checked for an approval.`;
  }
  ngOnDestroy() {
    clearTimeout(this.timer);
  }
}
