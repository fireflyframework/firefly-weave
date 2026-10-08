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
// The environment's targets, deployments and job history.
import { ChangeDetectionStrategy, Component } from "@angular/core";
import { ClusterDeploymentList } from "./cluster-deployment-list";
import { ClusterSection } from "./cluster-section";

@Component({
  selector: "weave-cluster-overview",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [ClusterDeploymentList],
  styleUrl: "./clusters.css",
  template: `
    <section class="operations-panel">
      <div class="panel-heading">
        <div>
          <h2>Deployment targets</h2>
          <p>Existing container runtimes and their explicit boundaries.</p>
        </div>
        @if (can("target.manage")) {
          <button class="primary" (click)="startRegistration()">
            Register target
          </button>
        }
      </div>
      @if (store.loading.has("targets")) {
        <p role="status">Loading targets…</p>
      }
      @if (store.errors.targets) {
        <p role="alert">{{ store.errors.targets }}</p>
      }
      <ul class="resource-cards">
        @for (item of store.targets; track item.id) {
          <li>
            <button (click)="openTarget(item)">{{ item.name }}</button
            ><span>{{ adapterLabels[item.adapter] }} · {{ item.boundary }}</span
            ><span>{{ item.disabled ? "Disabled" : "Registered" }}</span>
          </li>
        } @empty {
          @if (!store.loading.has("targets") && !store.errors.targets) {
            <li>No deployment target has been returned in this environment.</li>
          }
        }
      </ul>
      @if (store.cursors.targets) {
        <button (click)="loadMore('targets')">Load more targets</button>
      }
    </section>
    <section class="operations-panel">
      <h2>Deployments</h2>
      <weave-cluster-deployment-list [page]="page" />
    </section>
    <section class="operations-panel">
      <h2>Operations history</h2>
      @if (store.errors.jobs) {
        <p role="alert">{{ store.errors.jobs }}</p>
      }
      <ul class="resource-cards">
        @for (item of store.jobs; track item.id) {
          <li>
            <button (click)="openJob(item)">
              {{ item.kind }} · {{ item.created_at }}</button
            ><span>{{ item.state.replaceAll("_", " ") }}</span>
          </li>
        } @empty {
          <li>No operation has been returned.</li>
        }
      </ul>
      @if (store.cursors.jobs) {
        <button (click)="loadMore('jobs')">Load more operations</button>
      }
    </section>
  `,
})
export class ClusterOverview extends ClusterSection {}
