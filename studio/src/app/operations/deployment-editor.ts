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
import {
  observedComponents,
  type AdmittedWorkerRelease,
  type ComponentDraft,
} from "./deployment-onboarding";
import { Select } from "../forms/ui/select";
import { AiSetupWizard } from "../integrations/ai-setup-wizard";
import { TaskForm } from "../task-form";
import { missingBound } from "../forms/core/form-model";
import { fieldsOf } from "../forms/core/resolve";
import type { Schema } from "../task-schema";
import { missingRequired } from "../task-schema";
import type {
  Observation,
  Deployment,
  DeploymentRequest,
  Target,
} from "./deployment-contracts";

@Component({
  selector: "weave-deployment-editor",
  standalone: true,
  imports: [AiSetupWizard, TaskForm, Select],
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
        @if (!existing && importable.length) {
          <fieldset>
            <legend>Choose observed components</legend>
            @for (item of importable; track item.name) {
              <label
                ><input
                  type="checkbox"
                  [attr.aria-label]="'Import ' + item.name"
                  [checked]="selectedNames.has(item.name)"
                  (change)="selectObserved(item.name, $event)"
                />{{ item.name }} · {{ item.kind }} ·
                {{ item.replicas }} replicas</label
              >
            }
          </fieldset>
          <button
            type="button"
            [disabled]="!selectedNames.size"
            (click)="importSelected()"
          >
            Use selected observed components
          </button>
          <p class="hint">
            This copies the selected names, immutable images and replica counts
            into your draft. Enter the local configuration alias, CPU and memory
            limits explicitly. Observations do not report those limits. Other
            resources require manual review.
          </p>
        }
        <p>
          Use immutable image digests and named configuration references already
          allowed by the runner. Keep secrets in the runner environment. Unset
          optional limits for new components use the canonical defaults shown in
          review. Imported components require an explicit CPU and memory choice.
        </p>
        <weave-task-form
          [schema]="componentsSchema"
          [initialData]="componentsInitial"
          (dataChange)="changeComponents($event)"
          (validityChange)="componentsValid = $event"
        />
        <p class="hint">{{ releaseMessage }}</p>
        @for (item of items; track $index; let index = $index) {
          @if (item.kind === "worker") {
            <weave-select
              [label]="'Admitted worker release for ' + item.name"
              [options]="releaseOptions()"
              [value]="item.worker_release_id ?? ''"
              (choose)="chooseRelease(index, $event)"
            />
          }
        }
        <p class="hint">
          API/scheduler requires exactly one instance. Workers require an
          admitted worker release. Reducing replicas terminates containers;
          drain worker instances before reducing a pool. Run database migrations
          separately with the deployment upgrade runbook.
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
  @Input() observation: Observation | null = null;
  @Input() releases: AdmittedWorkerRelease[] = [];
  @Input() releaseMessage = "";
  selectedNames = new Set<string>();
  get importable() {
    return this.observation?.complete &&
      this.observation.target_id === this.target.id &&
      this.observation.target_revision === this.target.revision &&
      Date.parse(this.observation.expires_at) > Date.now()
      ? observedComponents(this.observation.resources)
      : [];
  }
  selectObserved(name: string, event: Event) {
    if ((event.target as HTMLInputElement).checked)
      this.selectedNames.add(name);
    else this.selectedNames.delete(name);
  }
  importSelected() {
    const selected = this.importable.filter((item) =>
      this.selectedNames.has(item.name),
    );
    const existing = this.items.filter(
      (item) => !this.selectedNames.has(item.name),
    );
    this.componentsSchema = structuredClone(this.componentsSchema);
    const definition = (
      this.componentsSchema as Schema & { $defs?: Record<string, Schema> }
    ).$defs?.["ComponentSpec"];
    if (definition?.properties) {
      definition.required = [
        ...new Set([
          ...(definition.required ?? []),
          "cpu_millis",
          "memory_mib",
        ]),
      ];
      for (const name of ["cpu_millis", "memory_mib"]) {
        delete (definition.properties[name] as Schema & { default?: unknown })
          .default;
        delete this.componentDefaults[name];
      }
    }
    this.setComponents([...existing, ...selected]);
  }
  releaseOptions() {
    return this.releases.map((release) => ({
      value: release.id,
      label:
        release.capabilities
          .map((c) => c.taskType + "@" + c.taskVersion)
          .join(", ") +
        " · configuration digest " +
        release.image_digest.slice(0, 19) +
        " · " +
        release.id.slice(0, 8),
    }));
  }
  chooseRelease(index: number, id: string) {
    const items = this.items;
    if (!this.releaseOptions().some((option) => option.value === id)) return;
    items[index] = { ...items[index], worker_release_id: id };
    this.setComponents(items);
  }
  private setComponents(items: ComponentDraft[]) {
    this.componentsInitial = {
      components: items.map((item) =>
        Object.fromEntries(
          Object.entries(item).filter(
            ([key, value]) =>
              !(["cpu_millis", "memory_mib"].includes(key) && value == null),
          ),
        ),
      ),
    };
    this.components = structuredClone(this.componentsInitial);
  }
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
      ...structuredClone(this.schema),
      properties: { components: properties["components"] },
      required: ["components"],
    };
    const componentDefinition = (
      this.componentsSchema as Schema & { $defs?: Record<string, Schema> }
    ).$defs?.["ComponentSpec"];
    if (componentDefinition?.properties)
      delete componentDefinition.properties["worker_release_id"];
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
  changeComponents(value: Record<string, unknown>) {
    const prior = this.items;
    const items = (value["components"] ?? []) as ComponentDraft[];
    this.components = {
      ...value,
      components: items.map((item) => ({
        ...item,
        worker_release_id:
          item.kind === "worker"
            ? (prior.find(
                (old) => old.name === item.name && old.image === item.image,
              )?.worker_release_id ?? null)
            : null,
      })),
    };
  }
  get items() {
    return ((this.components["components"] ?? []) as ComponentDraft[]).map(
      (item) => ({ ...this.componentDefaults, ...item }),
    );
  }
  get valid() {
    const identity =
      this.identityValid &&
      !missingRequired(this.identitySchema, this.identity).length;
    return (
      identity &&
      (this.step === 0 ||
        (this.componentsValid &&
          this.items.length > 0 &&
          this.items.every(
            (item) =>
              item.cpu_millis != null &&
              item.memory_mib != null &&
              !missingBound(
                this.schema.properties?.["components"]?.items,
                { literal: item },
                { root: this.schema },
              ).length &&
              (item.kind !== "worker" || !!item.worker_release_id),
          )))
    );
  }
  save() {
    if (this.valid && !this.busy)
      this.submit.emit({
        target_id: this.target.id,
        name: String(this.identity["name"]),
        ownership: this.identity["ownership"] as DeploymentRequest["ownership"],
        components: this.items.map((item) => ({
          ...item,
          cpu_millis: Number(item.cpu_millis),
          memory_mib: Number(item.memory_mib),
        })),
      });
  }
}
