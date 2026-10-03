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
import type {
  Deployment,
  Observation,
  Plan,
  Target,
} from "./deployment-contracts";

export interface PlanRequest {
  deployment_id: string;
  deployment_revision: number;
  observation_id: string;
  intent: Plan["intent"];
  ttl_seconds: number;
}
@Component({
  selector: "weave-deployment-plan-builder",
  standalone: true,
  imports: [Select],
  template: `
    <h3>Generate an immutable plan</h3>
    <p>
      Desired revision {{ deployment().revision }} will be checked against one
      complete, unexpired observation of target revision
      {{ target().revision }}.
    </p>
    <weave-select
      label="Operation"
      [options]="operations"
      [value]="intent"
      (choose)="intent = $any($event)"
    />
    <weave-select
      label="Observation"
      placeholder="Choose a current observation…"
      [options]="choices"
      [value]="observationId"
      (choose)="observationId = $event"
    />
    @if (!choices.length) {
      <p class="notice">
        No complete, current observation is available. Observe the target before
        planning.
      </p>
    }
    <p class="hint">
      Plan validity is at most five minutes and never outlives its observation.
      Cost and downtime estimates are unavailable. Infrastructure changes start
      only after review, approval and apply.
    </p>
    @if (error()) {
      <p role="alert">{{ error() }}</p>
    }
    <div class="actions">
      <button class="primary" [disabled]="busy() || !valid" (click)="save()">
        Generate reviewable plan</button
      ><button [disabled]="busy()" (click)="cancel.emit()">Cancel plan</button>
    </div>
  `,
  styles: `
    :host {
      display: grid;
      gap: 14px;
    }
    .actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
  `,
})
export class DeploymentPlanBuilder {
  target = input.required<Target>();
  deployment = input.required<Deployment>();
  observations = input.required<Observation[]>();
  busy = input(false);
  error = input("");
  submit = output<PlanRequest>();
  cancel = output<void>();
  intent: Plan["intent"] = "deploy";
  observationId = "";
  get operations() {
    const labels = {
      deploy: "Deploy / adopt deployment",
      update: "Update deployment",
      scale_workers: "Scale worker replicas",
    };
    return Object.entries(labels)
      .filter(
        ([value]) =>
          this.target().capabilities.includes(value as Plan["intent"]) &&
          (value !== "scale_workers" ||
            this.deployment().components.some((c) => c.kind === "worker")),
      )
      .map(([value, label]) => ({ value, label }));
  }
  get choices() {
    return this.observations()
      .filter(
        (o) =>
          o.target_id === this.target().id &&
          o.target_revision === this.target().revision &&
          o.complete &&
          Date.parse(o.expires_at) > Date.now(),
      )
      .map((o) => ({
        value: o.id,
        label: "Current · " + o.observed_at + " · " + o.id.slice(0, 8),
      }));
  }
  get valid() {
    return (
      !this.target().disabled &&
      this.operations.some((o) => o.value === this.intent) &&
      this.choices.some((o) => o.value === this.observationId)
    );
  }
  save() {
    if (this.valid && !this.busy())
      this.submit.emit({
        deployment_id: this.deployment().id,
        deployment_revision: this.deployment().revision,
        observation_id: this.observationId,
        intent: this.intent,
        ttl_seconds: 300,
      });
  }
}
