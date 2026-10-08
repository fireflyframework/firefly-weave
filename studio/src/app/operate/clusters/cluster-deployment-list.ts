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
import { OperateState } from "../operate-state";
import { ClusterSection } from "./cluster-section";

@Component({
  selector: "weave-cluster-deployment-list",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [OperateState],
  styleUrl: "./clusters.css",
  template: `
    @if (store.errors.deployments) {
      @if (store.errorInfo.deployments?.status === 403) {
        <weave-operate-state
          kind="access"
          heading="You don't have access to deployments"
          capability="deployment.read"
        />
      } @else {
        <weave-operate-state
          kind="partial"
          [message]="store.errors.deployments"
          [code]="store.errorInfo.deployments?.code ?? ''"
          (retry)="page.poller.refresh()"
        />
      }
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
        @if (loaded) {
          <li>No deployment has been returned.</li>
        } @else if (!store.errors.deployments) {
          <li role="status">Loading deployments…</li>
        }
      }
    </ul>
    @if (store.cursors.deployments) {
      <button (click)="loadMore('deployments')">Load more deployments</button>
    }
  `,
})
export class ClusterDeploymentList extends ClusterSection {
  /** Whether the deployments on screen are the ones this view asked for. */
  get loaded() {
    return this.store.loaded(
      "deployments",
      this.target ? { target_id: this.target.id } : {},
    );
  }
}
