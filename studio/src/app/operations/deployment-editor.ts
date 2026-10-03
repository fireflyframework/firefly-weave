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
import { Component, Input, OnChanges, output } from "@angular/core";
import { AiSetupWizard } from "../integrations/ai-setup-wizard";
import { TaskForm } from "../task-form";
import { fieldsOf } from "../forms/core/resolve";
import type { Schema } from "../task-schema";
import { missingRequired } from "../task-schema";
import type {
  Deployment,
  DeploymentRequest,
  Target,
} from "./deployment-contracts";

@Component({
  selector: "weave-deployment-editor",
  standalone: true,
  imports: [AiSetupWizard, TaskForm],
  template: `
    <weave-ai-setup-wizard
      [(step)]="step"
      [labels]="['Intent', 'Components', 'Review']"
      [headings]="[
        'Record deployment intent',
        'Set desired components',
        'Review desired state',
      ]"
      [nextLabels]="['Continue to components', 'Review desired state']"
      progressLabel="Desired deployment"
      finishLabel="Save desired deployment"
      [canContinue]="valid"
      [busy]="busy"
      (finish)="save()"
    >
      <div ai-model>
        <p>
          Target: <strong>{{ target.name }}</strong
          >. This saves metadata only; it does not start or change containers.
        </p>
        <weave-task-form
          [schema]="identitySchema"
          [initialData]="identityInitial"
          (dataChange)="identity = $event"
          (validityChange)="identityValid = $event"
        />
        <p class="hint">
          Imported intent requires explicit plan review before changes. Managed
          intent also requires a reviewed deployment plan.
        </p>
      </div>
      <div ai-connection>
        <p>
          Use immutable image digests and named configuration references already
          allowed by the runner. Keep secrets in the runner environment. Unset
          optional limits use the canonical defaults shown in review.
        </p>
        <weave-task-form
          [schema]="componentsSchema"
          [initialData]="componentsInitial"
          (dataChange)="components = $event"
          (validityChange)="componentsValid = $event"
        />
        <p class="hint">
          API/scheduler and migrations require exactly one instance. Workers
          require an admitted worker release. Reducing replicas terminates
          containers; drain worker instances before reducing a pool.
        </p>
      </div>
      <div ai-review>
        <dl>
          <dt>Target</dt>
          <dd>{{ target.name }}</dd>
          <dt>Deployment</dt>
          <dd>{{ identity["name"] }}</dd>
          <dt>Management intent</dt>
          <dd>{{ identity["ownership"] }}</dd>
          <dt>Source revision</dt>
          <dd>{{ existing?.revision ?? "New deployment" }}</dd>
        </dl>
        <ul>
          @for (item of items; track $index) {
            <li>
              <strong>{{ item.name }}</strong> · {{ item.kind }} ·
              {{ item.replicas }} desired replicas<br /><code>{{
                item.image
              }}</code
              ><br />Configuration reference: {{ item.configuration }} ·
              {{ item.cpu_millis }} CPU millicores · {{ item.memory_mib }} MiB
              memory
            </li>
          }
        </ul>
        <p>
          Next, collect a fresh observation and generate an immutable plan.
          Saving this desired state grants no infrastructure authority.
        </p>
      </div>
      @if (error) {
        <p ai-error role="alert">{{ error }}</p>
      }
      <button ai-secondary [disabled]="busy" (click)="cancel.emit()">
        Cancel desired changes
      </button>
    </weave-ai-setup-wizard>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    dd {
      margin: 4px 0 12px;
    }
    li {
      margin: 12px 0;
      overflow-wrap: anywhere;
    }
    code {
      font-size: 12px;
      overflow-wrap: anywhere;
    }
  `,
})
export class DeploymentEditor implements OnChanges {
  @Input({ required: true }) schema!: Schema;
  @Input({ required: true }) target!: Target;
  @Input() existing: Deployment | null = null;
  @Input() busy = false;
  @Input() error = "";
  submit = output<DeploymentRequest>();
  cancel = output<void>();
  step = 0;
  identityValid = true;
  componentsValid = true;
  identitySchema: Schema = {};
  componentsSchema: Schema = {};
  identityInitial: Record<string, unknown> = {};
  componentsInitial: Record<string, unknown> = {};
  identity: Record<string, unknown> = {};
  components: Record<string, unknown> = {};
  private componentDefaults: Record<string, unknown> = {};
  ngOnChanges(changes: Record<string, unknown>) {
    if (!changes["schema"] && !changes["existing"] && !changes["target"])
      return;
    const properties = this.schema.properties ?? {};
    this.componentDefaults = Object.fromEntries(
      fieldsOf(properties["components"]?.items, { root: this.schema })
        .filter((field) => field.default !== undefined)
        .map((field) => [field.key, field.default]),
    );
    this.identitySchema = {
      ...this.schema,
      properties: {
        name: this.existing
          ? { ...properties["name"], enum: [this.existing.name] }
          : properties["name"],
        ownership: properties["ownership"],
      },
      required: ["name", "ownership"],
    };
    this.componentsSchema = {
      ...this.schema,
      properties: { components: properties["components"] },
      required: ["components"],
    };
    this.identityInitial = {
      name: this.existing?.name ?? "",
      ownership: this.existing?.ownership ?? "imported",
    };
    this.componentsInitial = {
      components: this.existing
        ? structuredClone(this.existing.components)
        : [],
    };
    this.identity = structuredClone(this.identityInitial);
    this.components = structuredClone(this.componentsInitial);
    this.step = 0;
    this.identityValid = true;
    this.componentsValid = true;
  }
  get items() {
    return (
      (this.components["components"] ?? []) as DeploymentRequest["components"]
    ).map((item) => ({ ...this.componentDefaults, ...item }));
  }
  get valid() {
    const identity =
      this.identityValid &&
      !missingRequired(this.identitySchema, this.identity).length;
    return (
      identity &&
      (this.step === 0 || (this.componentsValid && this.items.length > 0))
    );
  }
  save() {
    if (this.valid && !this.busy)
      this.submit.emit({
        target_id: this.target.id,
        name: String(this.identity["name"]),
        ownership: this.identity["ownership"] as DeploymentRequest["ownership"],
        components: structuredClone(this.items),
      });
  }
}
