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
// The Jobs tab: observe and apply jobs across the environment's targets.
import { ChangeDetectionStrategy, Component } from "@angular/core";
import { ClusterSection } from "./cluster-section";

@Component({
  selector: "weave-cluster-jobs-tab",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: "./clusters.css",
  template: `
    <section class="operations-panel">
      <h2>Jobs</h2>
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
          @if (store.loaded("jobs")) {
            <li>No job has been returned.</li>
          } @else if (!store.errors.jobs) {
            <li role="status">Loading jobs…</li>
          }
        }
      </ul>
      @if (store.cursors.jobs) {
        <button (click)="loadMore('jobs')">Load more jobs</button>
      }
    </section>
  `,
})
export class ClusterJobsTab extends ClusterSection {}
