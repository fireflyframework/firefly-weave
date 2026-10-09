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
// One immutable plan: its digest, steps and risks, approval and apply.
import { ChangeDetectionStrategy, Component } from "@angular/core";
import { ClusterSection } from "./cluster-section";

@Component({
  selector: "weave-cluster-plan-detail",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [],
  styleUrl: "./clusters.css",
  template: `
    @if (plan; as plan) {
      <section class="operations-panel">
        <p class="eyebrow">IMMUTABLE PLAN</p>
        <h2 tabindex="-1">Review {{ plan.intent }}</h2>
        <p>
          Target revision {{ plan.target_revision }} · Deployment revision
          {{ plan.deployment_revision }}
        </p>
        <dl>
          <dt>Reviewed digest</dt>
          <dd>
            <code>{{ plan.digest }}</code>
          </dd>
          <dt>Observation digest</dt>
          <dd>
            <code>{{ plan.observation_digest }}</code>
          </dd>
          <dt>Expires</dt>
          <dd>{{ plan.expires_at }}</dd>
          <dt>Estimated cost / duration</dt>
          <dd>Not available</dd>
        </dl>
        @if (!fresh(plan.expires_at)) {
          <p class="notice">
            @if (recoverApply) {
              This plan has expired. An unchanged recovery request can retrieve
              an earlier accepted result; a new apply remains blocked.
            } @else {
              This plan has expired. Create and review a new plan before
              applying.
            }
          </p>
        }
        <h3>Changes and preconditions</h3>
        <ul class="resource-cards">
          @for (step of plan.steps; track step.component.name) {
            <li>
              <strong>{{ step.action }} {{ step.component.name }}</strong
              ><span
                >{{ step.component.kind }} ·
                {{ step.component.replicas }} desired replicas</span
              ><code>{{ step.component.image }}</code
              ><span
                >Expected external version:
                {{ step.expected_version || "Resource must not exist" }}</span
              >
            </li>
          }
        </ul>
        <h3>Operational risks</h3>
        <ul>
          @for (risk of plan.risks; track risk) {
            <li>{{ riskLabel(risk) }}</li>
          } @empty {
            <li>No additional risks were reported by the planner.</li>
          }
        </ul>
        <p class="hint">
          Approval and apply use this exact digest. The server rechecks
          authority, revisions and observation. Cancellation cannot undo
          external effects.
        </p>
        @if (approval) {
          <p role="status">Approval recorded for {{ approval.digest }}</p>
          <p class="hint">
            Recorded approval is historical evidence. Apply rechecks the
            approver’s current authority and all plan preconditions.
          </p>
        } @else {
          <p class="hint">
            Approval has not been confirmed in this view. Applying requires a
            recorded approval for this exact digest; the server checks it again.
          </p>
        }
        <div class="actions">
          @if (can("deployment.approve")) {
            <button
              [disabled]="store.mutating || !fresh(plan.expires_at)"
              (click)="approve()"
            >
              Approve reviewed plan
            </button>
          }
          @if (can("deployment.apply")) {
            <button
              class="primary"
              [disabled]="
                store.mutating || (!fresh(plan.expires_at) && !recoverApply)
              "
              (click)="apply()"
            >
              {{
                recoverApply ? "Recover apply result" : "Apply approved plan"
              }}
            </button>
          }
        </div>
      </section>
    }
  `,
})
export class ClusterPlanDetail extends ClusterSection {}
