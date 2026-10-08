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
// One deployment target: its authority, runner setup, observation, runners and deployments.
import { ChangeDetectionStrategy, Component } from "@angular/core";
import { RunnerSetup } from "../../operations/runner-setup";
import { ClusterDeploymentList } from "./cluster-deployment-list";
import { ClusterSection } from "./cluster-section";

@Component({
  selector: "weave-cluster-target-detail",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [RunnerSetup, ClusterDeploymentList],
  styleUrl: "./clusters.css",
  template: `
    @if (target; as target) {
      <section class="operations-panel">
        <p class="eyebrow">DEPLOYMENT TARGET</p>
        <h2 tabindex="-1">{{ target.name }}</h2>
        <dl>
          <dt>Runtime</dt>
          <dd>{{ adapterLabels[target.adapter] }}</dd>
          <dt>External identity</dt>
          <dd>{{ target.external_identity }}</dd>
          <dt>Allowed boundary</dt>
          <dd>{{ target.boundary }}</dd>
          <dt>Authority revision</dt>
          <dd>{{ target.revision }}</dd>
          <dt>Permitted operations</dt>
          <dd>{{ target.capabilities.join(", ") }}</dd>
        </dl>
        @if (target.disabled) {
          <p class="notice">
            This target is disabled. No new operation can start.
          </p>
        }
        <p class="hint">
          Permitted operations are policy, not proof of a working adapter. A
          runner must independently support the operation within its local
          authority.
        </p>
        @if (can("target.manage")) {
          <button [disabled]="store.mutating" (click)="editTargetAuthority()">
            Edit target authority
          </button>
        }
        @if (can("deployment.plan")) {
          <button
            class="primary"
            [disabled]="store.mutating || target.disabled"
            (click)="observe()"
          >
            Observe target
          </button>
        }
      </section>
      <section class="operations-panel">
        <weave-runner-setup
          [target]="target"
          [baseUrl]="host.profile!.baseUrl"
          [registered]="runnerPresent"
          [observed]="observationState(latestObservation) === 'current'"
          (administration)="host.navigate('settings')"
        />
      </section>
      <section class="operations-panel">
        <h2>Observed resources</h2>
        @if (store.loading.has("observations")) {
          <p role="status">Loading observation…</p>
        } @else if (store.errors.observations) {
          <p role="alert">{{ store.errors.observations }}</p>
        } @else if (latestObservation; as observation) {
          <p>
            <strong>{{ observationState(observation) }}</strong> · Observed
            {{ observation.observed_at }} · Expires
            {{ observation.expires_at }}
          </p>
          <p class="hint">
            An observation is a bounded snapshot. It does not guarantee current
            availability.
          </p>
          <ul class="resource-cards">
            @for (
              resource of observation.resources;
              track resource.external_identity
            ) {
              <li>
                <strong>{{ resource.name }}</strong
                ><span>{{ resource.kind }} · {{ resource.state }}</span
                ><span
                  >{{ resource.ready_replicas }} ready of
                  {{ resource.replicas }} observed replicas</span
                ><code>{{
                  resource.image || "Image identity unavailable"
                }}</code>
              </li>
            } @empty {
              <li>No resources were reported in this observation.</li>
            }
          </ul>
        } @else {
          <p>
            No observation yet. Request one after installing the authorized
            runner.
          </p>
        }
      </section>
      <section class="operations-panel">
        <h2>Outbound runners</h2>
        <p class="hint">
          Current contact is presence, not container health or available worker
          capacity.
        </p>
        @if (store.errors.runners) {
          <p role="alert">{{ store.errors.runners }}</p>
        }
        <ul class="resource-cards">
          @for (runner of store.runners; track runner.id) {
            <li>
              <code>{{ runner.id }}</code
              ><strong>{{
                runner.revoked
                  ? "Revoked"
                  : fresh(runner.expires_at)
                    ? "Recent contact"
                    : "Contact expired"
              }}</strong
              ><span>Last contact {{ runner.last_seen }}</span
              ><span
                >Reported capabilities:
                {{ runner.capabilities.join(", ") }}</span
              >
            </li>
          } @empty {
            <li>No runner registration has been returned.</li>
          }
        </ul>
      </section>
      <section class="operations-panel">
        <h2>Deployments</h2>
        @if (can("target.manage")) {
          <button
            [disabled]="store.mutating || target.disabled"
            (click)="editDeployment()"
          >
            Record desired deployment
          </button>
        }
        <weave-cluster-deployment-list [page]="page" />
      </section>
    }
  `,
})
export class ClusterTargetDetail extends ClusterSection {}
