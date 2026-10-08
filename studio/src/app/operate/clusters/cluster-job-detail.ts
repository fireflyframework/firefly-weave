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
// One durable job: its state, receipt, reconciliation and cancellation.
import { ChangeDetectionStrategy, Component } from "@angular/core";
import { DeploymentReconciliation } from "../../operations/deployment-reconciliation";
import { ClusterSection } from "./cluster-section";

@Component({
  selector: "weave-cluster-job-detail",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [DeploymentReconciliation],
  styleUrl: "./clusters.css",
  template: `
    @if (job; as job) {
      <section class="operations-panel">
        <p class="eyebrow">DURABLE OPERATION</p>
        <h2 tabindex="-1">
          {{
            job.kind === "observe" ? "Observe target" : "Apply deployment plan"
          }}
        </h2>
        <p role="status">
          <strong>{{ job.state.replaceAll("_", " ") }}</strong>
        </p>
        <dl>
          <dt>Job ID</dt>
          <dd>
            <code>{{ job.id }}</code>
          </dd>
          <dt>Created</dt>
          <dd>{{ job.created_at }}</dd>
          <dt>Deadline</dt>
          <dd>{{ job.deadline }}</dd>
          <dt>Target revision</dt>
          <dd>{{ job.target_revision }}</dd>
        </dl>
        @if (job.state === "reconciliation_required") {
          <p class="notice">
            The external result is uncertain. Observe the target and review what
            changed before creating another plan. Do not retry blindly.
          </p>
        }
        @if (job.receipt; as receipt) {
          <h3>Operation receipt</h3>
          <p>{{ receipt.code.replaceAll("_", " ") }}</p>
          <ul>
            @for (resource of receipt.changed_resources; track resource) {
              <li>{{ resource }}</li>
            }
          </ul>
          @if (receipt.external_effects_may_continue) {
            <p class="notice">
              External effects may continue. Cancellation is not a rollback.
            </p>
          }
        }
        @if (
          job.state === "reconciliation_required" &&
          can("deployment.apply") &&
          can("deployment.approve") &&
          relatedTarget
        ) {
          <weave-deployment-reconciliation
            [job]="job"
            [target]="relatedTarget"
            [observations]="store.observations"
            [busy]="store.mutating"
            (submit)="reconcile($event)"
          />
          @if (store.cursors.observations) {
            <button (click)="loadMore('observations')">
              Load more observations
            </button>
          }
        }
        @if (can("deployment.cancel") && !terminalJobs.has(job.state)) {
          <button [disabled]="store.mutating" (click)="cancelJob()">
            Cancel operation
          </button>
        }
        <button [disabled]="store.mutating" (click)="readTarget(job.target_id)">
          View target and observation
        </button>
      </section>
    }
  `,
})
export class ClusterJobDetail extends ClusterSection {}
