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
// The deployments the page has loaded, with Load more.
import { ChangeDetectionStrategy, Component } from "@angular/core";
import { ClusterSection } from "./cluster-section";

@Component({
  selector: "weave-cluster-deployment-list",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [],
  styleUrl: "./clusters.css",
  template: `
    @if (store.errors.deployments) {
      <p role="alert">{{ store.errors.deployments }}</p>
    }
    <ul class="resource-cards">
      @for (item of store.deployments; track item.id) {
        <li>
          <button (click)="openDeployment(item)">{{ item.name }}</button
          ><span
            >{{ item.ownership }} · desired revision {{ item.revision }}</span
          >
        </li>
      } @empty {
        <li>No deployment has been returned.</li>
      }
    </ul>
    @if (store.cursors.deployments) {
      <button (click)="loadMore('deployments')">Load more deployments</button>
    }
  `,
})
export class ClusterDeploymentList extends ClusterSection {}
