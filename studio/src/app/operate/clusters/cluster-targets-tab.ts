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
// The Targets tab: one card per target with its adapter and runner
// presence, then the environment's deployments.
import { ChangeDetectionStrategy, Component } from "@angular/core";
import { Icon } from "../../icon";
import { toneAttribute } from "../../status-labels";
import { ClusterDeploymentList } from "./cluster-deployment-list";
import { ClusterSection } from "./cluster-section";
import {
  adapterIcon,
  runnerLine,
  runnerTone,
  targetRunner,
} from "./cluster-model";

@Component({
  selector: "weave-cluster-targets-tab",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [ClusterDeploymentList, Icon],
  styleUrls: ["./clusters.css", "../operate.css"],
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
      @if (store.loading.has("targets") && !store.targets.length) {
        <p role="status">Loading targets…</p>
      }
      @if (store.errors.targets) {
        <p role="alert">{{ store.errors.targets }}</p>
      }
      <ul class="resource-cards">
        @for (item of store.targets; track item.id) {
          @let runner = runnerOf(item.id);
          <li>
            <span class="target-name"
              ><weave-icon [name]="icon(item.adapter)" [size]="16" /><button
                (click)="openTarget(item)"
              >
                {{ item.name }}
              </button></span
            ><span>{{ adapterLabels[item.adapter] }} · {{ item.boundary }}</span
            ><span>{{ item.disabled ? "Disabled" : "Registered" }}</span
            ><span
              class="status-pill runner-line"
              [attr.data-tone]="presenceTone(runner)"
              >{{ line(runner) }}</span
            >
          </li>
        } @empty {
          @if (!store.loading.has("targets") && !store.errors.targets) {
            <li>No deployment target has been returned in this environment.</li>
          }
        }
      </ul>
      @if (store.errors.runners) {
        <p role="alert">
          Runner presence could not be loaded. {{ store.errors.runners }}
        </p>
      }
      @if (store.cursors.targets) {
        <button (click)="loadMore('targets')">Load more targets</button>
      }
    </section>
    <section class="operations-panel">
      <h2>Deployments</h2>
      <weave-cluster-deployment-list [page]="page" />
    </section>
  `,
})
export class ClusterTargetsTab extends ClusterSection {
  readonly icon = adapterIcon;
  runnerOf(targetId: string) {
    return targetRunner(this.store.runners, targetId);
  }
  line(runner: ReturnType<typeof targetRunner>) {
    return runnerLine(runner);
  }
  presenceTone(runner: ReturnType<typeof targetRunner>) {
    return toneAttribute(runner ? runnerTone(runner) : "neutral");
  }
}
