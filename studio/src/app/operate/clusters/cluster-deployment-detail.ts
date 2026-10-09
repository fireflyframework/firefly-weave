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
// One desired deployment: its components, the plan builder and its plans.
import { ChangeDetectionStrategy, Component } from "@angular/core";
import { DeploymentPlanBuilder } from "../../operations/deployment-plan-builder";
import { ClusterSection } from "./cluster-section";

@Component({
  selector: "weave-cluster-deployment-detail",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [DeploymentPlanBuilder],
  styleUrl: "./clusters.css",
  template: `
    @if (deployment; as deployment) {
      <section class="operations-panel">
        <p class="eyebrow">DESIRED DEPLOYMENT</p>
        <h2 tabindex="-1">{{ deployment.name }}</h2>
        <p>Revision {{ deployment.revision }} · {{ deployment.ownership }}</p>
        <p class="hint">
          Desired components are intent, not observed availability. Imported
          intent requires explicit review of a deployment plan before changes.
          Ownership here records declared intent; it is not automatically
          changed by a successful job.
        </p>
        <div class="actions">
          @if (can("target.manage")) {
            <button [disabled]="store.mutating" (click)="editDeployment()">
              Edit desired deployment
            </button>
          }
          @if (can("deployment.plan")) {
            <button
              [disabled]="
                store.mutating || !relatedTarget || relatedTarget.disabled
              "
              (click)="planning = true"
            >
              Create plan
            </button>
          }
          <button (click)="readTarget(deployment.target_id)">
            View target and observation
          </button>
        </div>
        <ul class="resource-cards">
          @for (component of deployment.components; track component.name) {
            <li>
              <strong>{{ component.name }}</strong
              ><span
                >{{ component.kind }} · {{ component.replicas }} desired
                replicas</span
              ><code>{{ component.image }}</code
              ><span
                >Runner configuration reference:
                {{ component.configuration }}</span
              >
            </li>
          }
        </ul>
      </section>
      @if (planning && relatedTarget) {
        <section class="operations-panel">
          <weave-deployment-plan-builder
            [target]="relatedTarget"
            [deployment]="deployment"
            [observations]="store.observations"
            [busy]="store.mutating"
            [error]="store.mutationError"
            (submit)="createPlan($event)"
            (cancel)="cancelDraft()"
          />
          @if (store.cursors.observations) {
            <button (click)="loadMore('observations')">
              Load more observations
            </button>
          }
        </section>
      }
      <section class="operations-panel">
        <h2>Plans</h2>
        @if (store.errors.plans) {
          <p role="alert">{{ store.errors.plans }}</p>
        }
        <ul class="resource-cards">
          @for (plan of store.plans; track plan.id) {
            <li>
              <button (click)="openPlan(plan)">
                {{ plan.intent }} · {{ plan.created_at }}</button
              ><span>{{
                fresh(plan.expires_at) ? "Review required" : "Expired"
              }}</span
              ><code>{{ plan.digest }}</code>
            </li>
          } @empty {
            <li>No plan has been returned for this deployment.</li>
          }
        </ul>
      </section>
    }
  `,
})
export class ClusterDeploymentDetail extends ClusterSection {}
