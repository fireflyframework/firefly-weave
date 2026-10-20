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
// The Change requests tab: every change request in the environment, newest
// first, with its state, target and requester, filtered by state and target
// in the address.
import { ChangeDetectionStrategy, Component } from "@angular/core";
import { toneAttribute } from "../../status-labels";
import type {
  ChangeRequest,
  ChangeState,
} from "../../operations/deployment-contracts";
import { OperateState } from "../operate-state";
import { timeAbsolute, timeIso, timeRelative } from "../operate-time";
import { ClusterSection } from "./cluster-section";
import {
  changeIntentLabel,
  changeStateLabel,
  changeStateTone,
  effectiveState,
} from "./change-request-model";

export const STATE_FILTERS: [string, string, ChangeState[]][] = [
  ["", "All states", []],
  [
    "open",
    "In progress",
    [
      "observing",
      "ready_to_plan",
      "awaiting_approval",
      "approved",
      "draining",
      "applying",
    ],
  ],
  ["approval", "Waiting for approval", ["awaiting_approval"]],
  [
    "ended",
    "Ended",
    [
      "succeeded",
      "failed",
      "reconciliation_required",
      "expired",
      "declined",
      "cancelled",
      "superseded",
    ],
  ],
];

@Component({
  selector: "weave-cluster-change-requests-tab",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [OperateState],
  styleUrls: ["./clusters.css", "../operate.css"],
  template: `
    <section class="operations-panel">
      <div class="panel-heading">
        <div>
          <h2>Change requests</h2>
          <p>Every change to a target, from the request to the result.</p>
        </div>
        @if (host.can("deployment.plan")) {
          <button class="primary" (click)="page.openWizard()">
            New change request
          </button>
        }
      </div>
      <div class="operate-filters change-filters">
        <label class="field"
          >State
          <select
            name="state"
            (change)="page.filterChanges(value($event), page.changeTarget)"
          >
            @for (item of filters; track item[0]) {
              <option
                [value]="item[0]"
                [selected]="item[0] === page.changeFilter"
              >
                {{ item[1] }}
              </option>
            }
          </select>
        </label>
        <label class="field"
          >Target
          <select
            name="target"
            (change)="page.filterChanges(page.changeFilter, value($event))"
          >
            <option value="">All targets</option>
            @for (item of store.targets; track item.id) {
              <option
                [value]="item.id"
                [selected]="item.id === page.changeTarget"
              >
                {{ item.name }}
              </option>
            }
          </select>
        </label>
      </div>
      @let list = page.changes;
      @if (list.loaded && store.errors.targets) {
        <weave-operate-state
          kind="partial"
          [message]="
            'Target names could not be loaded. ' + store.errors.targets
          "
          [code]="store.errorInfo.targets?.code ?? ''"
          (retry)="page.poller.refresh()"
        />
      }
      @if (list.error?.status === 403) {
        <weave-operate-state
          kind="access"
          heading="You don't have access to change requests"
          capability="deployment.read"
        />
      } @else if (list.error) {
        <weave-operate-state
          kind="error"
          [message]="list.error.message"
          [code]="list.error.code"
          (retry)="page.poller.refresh()"
        />
      } @else if (!list.loaded) {
        <weave-operate-state kind="loading" noun="change requests" />
      } @else if (!list.rows.length) {
        <weave-operate-state
          kind="empty"
          heading="No change requests"
          text="A change request scales, updates or restarts workers on a target, with an approval before anything changes."
        />
      } @else {
        <div
          class="resource-table change-table"
          role="table"
          aria-label="Change requests"
        >
          <div role="rowgroup">
            <div class="table-head" role="row">
              <span role="columnheader">Change</span
              ><span role="columnheader" data-wide>Kind</span
              ><span role="columnheader">State</span
              ><span role="columnheader" data-wide>Target</span
              ><span role="columnheader" data-wide>Requested by</span
              ><span role="columnheader" class="end">Updated</span>
            </div>
          </div>
          <div role="rowgroup">
            @for (row of list.rows; track row.id) {
              @let state = shown(row);
              <div class="resource-row" role="row">
                <span role="cell" class="change-title"
                  ><button
                    type="button"
                    class="row-open"
                    (click)="page.openChange(row)"
                  >
                    {{ row.title }}
                  </button></span
                ><span role="cell" data-wide>{{ intent(row) }}</span
                ><span role="cell"
                  ><span class="status-pill" [attr.data-tone]="tone(state)">{{
                    label(state)
                  }}</span></span
                ><span role="cell" data-wide>{{
                  page.targetName(row.target_id)
                }}</span
                ><span role="cell" data-wide>{{
                  page.personName(row.requested_by)
                }}</span
                ><span role="cell" class="end numeric"
                  ><time
                    [attr.datetime]="iso(updated(row))"
                    [attr.title]="absolute(updated(row))"
                    >{{ relative(updated(row)) }}</time
                  ></span
                >
              </div>
            }
          </div>
        </div>
        @if (list.cursor) {
          <button (click)="page.loadMoreChanges()">
            Load more change requests
          </button>
        }
      }
    </section>
  `,
})
export class ClusterChangeRequestsTab extends ClusterSection {
  readonly filters = STATE_FILTERS;
  shown(row: ChangeRequest) {
    return effectiveState(row);
  }
  label(state: ChangeState) {
    return changeStateLabel(state);
  }
  tone(state: ChangeState) {
    return toneAttribute(changeStateTone(state));
  }
  intent(row: ChangeRequest) {
    return changeIntentLabel(row.intent);
  }
  updated(row: ChangeRequest) {
    return row.history.at(-1)?.at ?? row.created_at;
  }
  iso(value: string) {
    return timeIso(value);
  }
  absolute(value: string) {
    return timeAbsolute(value);
  }
  relative(value: string) {
    return timeRelative(value);
  }
  value(event: Event) {
    return (event.target as HTMLSelectElement).value;
  }
}
