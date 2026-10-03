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
import { Component, input, output } from "@angular/core";
import { Select } from "../forms/ui/select";
import type { Job, Observation, Target } from "./deployment-contracts";
export interface ReconcileRequest {
  plan_digest: string;
  observation_id: string;
  external_operation_stopped: true;
}
@Component({
  selector: "weave-deployment-reconciliation",
  standalone: true,
  imports: [Select],
  template: `<h3>Reconcile an uncertain result</h3>
    <p>
      First stop or verify completion of the external operation, then collect a
      complete settled observation. This records the outcome as reconciled and
      failed; it does not undo effects or apply another plan.
    </p>
    <weave-select
      label="Settled observation"
      placeholder="Choose settled evidence…"
      [options]="choices"
      [value]="observationId"
      (choose)="observationId = $event"
    />
    @if (!choices.length) {
      <p class="notice">
        No fresh settled observation collected after this uncertainty is
        available. Ask the authorized runner operator to verify the external
        operation, then observe the target.
      </p>
    }
    <label
      ><input
        type="checkbox"
        [checked]="confirmed"
        (change)="confirmed = $any($event.target).checked"
      />I verified the external operation has stopped</label
    >
    <button [disabled]="busy() || !valid" (click)="save()">
      Record reconciliation
    </button>`,
  styles: `
    :host {
      display: grid;
      gap: 14px;
      padding-top: 18px;
      margin-top: 18px;
      border-top: 1px solid var(--line);
    }
    label {
      display: flex;
      gap: 10px;
      align-items: flex-start;
    }
  `,
})
export class DeploymentReconciliation {
  job = input.required<Job>();
  target = input.required<Target>();
  observations = input.required<Observation[]>();
  busy = input(false);
  submit = output<ReconcileRequest>();
  observationId = "";
  confirmed = false;
  get choices() {
    const job = this.job();
    return this.observations()
      .filter(
        (o) =>
          o.target_id === job.target_id &&
          o.target_revision === this.target().revision &&
          o.complete &&
          o.settled &&
          Date.parse(o.expires_at) > Date.now() &&
          !!job.reconciliation_started_at &&
          Date.parse(o.observed_at) > Date.parse(job.reconciliation_started_at),
      )
      .map((o) => ({
        value: o.id,
        label: o.observed_at + " · " + o.id.slice(0, 8),
      }));
  }
  get valid() {
    return (
      this.job().state === "reconciliation_required" &&
      !!this.job().plan_digest &&
      this.confirmed &&
      this.choices.some((o) => o.value === this.observationId)
    );
  }
  save() {
    if (this.valid && !this.busy())
      this.submit.emit({
        plan_digest: this.job().plan_digest!,
        observation_id: this.observationId,
        external_operation_stopped: true,
      });
  }
}
