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
// The Runners tab: every runner registration in the environment, its
// presence and last contact, and Revoke.
import { ChangeDetectionStrategy, Component } from "@angular/core";
import { Icon } from "../../icon";
import { shortId } from "../../format";
import { toneAttribute } from "../../status-labels";
import type { Runner } from "../../operations/deployment-contracts";
import { OperateState } from "../operate-state";
import { timeAbsolute, timeIso, timeRelative } from "../operate-time";
import { ClusterSection } from "./cluster-section";
import { adapterIcon, runnerLabel, runnerTone } from "./cluster-model";

@Component({
  selector: "weave-cluster-runners-tab",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, OperateState],
  styleUrls: ["./clusters.css", "../operate.css"],
  template: `
    <section class="operations-panel">
      <h2>Runners</h2>
      <p class="hint">
        Online means the runner made contact in the last 90 seconds. Contact is
        presence, not container health or worker capacity.
      </p>
      @if (store.errors.runners) {
        @if (store.errorInfo.runners?.status === 403) {
          <weave-operate-state
            kind="access"
            heading="You don't have access to runners"
            capability="deployment.read"
          />
        } @else {
          <weave-operate-state
            kind="partial"
            [message]="store.errors.runners"
            [code]="store.errorInfo.runners?.code ?? ''"
            (retry)="page.poller.refresh()"
          />
        }
      } @else if (!store.loaded("runners")) {
        <weave-operate-state kind="loading" noun="runners" />
      } @else if (!store.runners.length) {
        <weave-operate-state
          kind="empty"
          heading="No runners yet"
          text="A runner registers when it starts with its target's identity. Each target's setup shows the command."
        />
      } @else {
        <div
          class="resource-table runner-table"
          role="table"
          aria-label="Runners"
        >
          <div role="rowgroup">
            <div class="table-head" role="row">
              <span role="columnheader">Runner</span
              ><span role="columnheader">Status</span
              ><span role="columnheader" data-wide>Last contact</span
              ><span role="columnheader" data-wide>Reported operations</span
              ><span role="columnheader"
                ><span class="sr-only">Actions</span></span
              >
            </div>
          </div>
          <div role="rowgroup">
            @for (runner of store.runners; track runner.id) {
              <div class="resource-row" role="row">
                <span role="cell" class="runner-main"
                  ><weave-icon [name]="icon(runner)" [size]="16" /><span
                    class="row-text"
                    ><code [attr.title]="runner.id">{{ short(runner.id) }}</code
                    ><small
                      >{{ targetName(runner.target_id) }} ·
                      {{ adapterLabels[runner.adapter] }}</small
                    ><small class="runner-contact"
                      >Last contact
                      <time
                        [attr.datetime]="iso(runner.last_seen)"
                        [attr.title]="absolute(runner.last_seen)"
                        >{{ relative(runner.last_seen) }}</time
                      >
                      · Reported operations:
                      {{ runner.capabilities.join(", ") }}</small
                    ></span
                  ></span
                ><span role="cell"
                  ><span class="status-pill" [attr.data-tone]="tone(runner)">{{
                    label(runner)
                  }}</span></span
                ><span role="cell" data-wide
                  ><time
                    [attr.datetime]="iso(runner.last_seen)"
                    [attr.title]="absolute(runner.last_seen)"
                    >{{ relative(runner.last_seen) }}</time
                  ></span
                ><span role="cell" data-wide>{{
                  runner.capabilities.join(", ")
                }}</span
                ><span role="cell" class="runner-actions">
                  @if (!runner.revoked && canRevoke(runner)) {
                    <button
                      type="button"
                      class="tertiary danger sm"
                      [attr.aria-label]="'Revoke runner ' + short(runner.id)"
                      [disabled]="store.mutating"
                      (click)="page.revokeRunner(runner)"
                    >
                      <weave-icon name="revoke" [size]="16" />Revoke
                    </button>
                  }
                </span>
              </div>
            }
          </div>
        </div>
        @if (store.cursors.runners) {
          <button class="load-more" (click)="loadMore('runners')">
            Load more runners
          </button>
        }
      }
    </section>
  `,
})
export class ClusterRunnersTab extends ClusterSection {
  readonly short = shortId;
  readonly iso = timeIso;
  readonly absolute = timeAbsolute;
  readonly relative = timeRelative;
  icon(runner: Runner) {
    return adapterIcon(runner.adapter);
  }
  label(runner: Runner) {
    return runnerLabel(runner);
  }
  tone(runner: Runner) {
    return toneAttribute(runnerTone(runner));
  }
  targetName(id: string) {
    return this.page.targetName(id);
  }
  canRevoke(runner: Runner) {
    return this.host.can("target.manage", runner.target_id);
  }
}
