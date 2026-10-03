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
import {
  Component,
  OnChanges,
  SimpleChanges,
  input,
  output,
} from "@angular/core";
import { AiSetupWizard } from "../integrations/ai-setup-wizard";
import { TaskForm } from "../task-form";
import type { Schema } from "../task-schema";
import { canonicalJson } from "../forms/core/json";
import { Select } from "../forms/ui/select";
import {
  adapterLabels,
  type Adapter,
  type Capability,
  type TargetRequest,
  type Target,
  type TargetUpdate,
} from "./deployment-contracts";

@Component({
  selector: "weave-target-wizard",
  standalone: true,
  imports: [AiSetupWizard, Select, TaskForm],
  template: `
    <weave-ai-setup-wizard
      [(step)]="step"
      [labels]="['Destination', 'Authority', 'Review']"
      [headings]="[
        existing()
          ? 'Review the existing target boundary'
          : 'Identify an existing container target',
        'Bind a dedicated outbound runner',
        existing()
          ? 'Review target authority changes'
          : 'Review target registration',
      ]"
      [nextLabels]="['Continue to authority', 'Review target']"
      [progressLabel]="existing() ? 'Target authority' : 'Target registration'"
      [finishLabel]="existing() ? 'Save target authority' : 'Register target'"
      [canContinue]="valid && !blocked()"
      [busy]="busy()"
      (finish)="save()"
    >
      <div ai-model>
        @if (existing(); as target) {
          <dl>
            <dt>Name</dt>
            <dd>{{ target.name }}</dd>
            <dt>Runtime</dt>
            <dd>{{ adapterLabels[target.adapter] }}</dd>
            <dt>External identity / boundary</dt>
            <dd>{{ target.external_identity }} / {{ target.boundary }}</dd>
            <dt>Current authority revision</dt>
            <dd>{{ target.revision }}</dd>
          </dl>
          <p>
            The runtime identity and boundary are fixed. Register a separate
            target for another destination.
          </p>
        } @else {
          <p>
            Registering a target records its identity and permitted scope. It
            does not provision resources, start containers or prove
            connectivity.
          </p>
          <label
            >Target name<input
              aria-label="Target name"
              maxlength="100"
              [value]="name"
              (input)="name = value($event)"
          /></label>
          <weave-select
            label="Container runtime"
            [options]="adapters"
            [value]="adapter"
            (choose)="chooseAdapter($event)"
          />
          <label
            >External identity<input
              aria-label="External identity"
              maxlength="512"
              [value]="external"
              (input)="external = value($event)"
          /></label>
          <p class="hint">
            Use the exact non-secret runtime identity configured in the runner
            policy. Kubernetes also covers local clusters, AKS, EKS and GKE.
          </p>
          <label
            >Allowed boundary<input
              aria-label="Allowed boundary"
              maxlength="100"
              [value]="boundary"
              (input)="boundary = value($event)"
          /></label>
          <p class="hint">
            A Compose project, Kubernetes namespace or managed-container
            boundary. Registration cannot expand the runner's local policy.
          </p>
        }
      </div>
      <div ai-connection>
        @if (existing()) {
          <weave-task-form
            [schema]="editSchema"
            [initialData]="authorityInitial"
            (dataChange)="authority = $event"
            (validityChange)="authorityValid = $event"
          />
          <p class="hint">
            Capabilities are permitted operations, not evidence of adapter
            support. Observe must remain permitted. Disabling prevents new
            operations.
          </p>
        } @else {
          <label
            >Runner principal ID<input
              aria-label="Runner principal ID"
              maxlength="36"
              [value]="principal"
              (input)="principal = value($event)"
          /></label>
          <p class="hint">
            An administrator creates a dedicated application principal with
            runner grants. Install its outbound runner near this target. Keep
            infrastructure credentials and Docker/Kubernetes access on that
            runner; never paste them here.
          </p>
          <fieldset>
            <legend>Permitted operations</legend>
            <label><input type="checkbox" checked disabled />Observe</label>
            @for (item of capabilities; track item.value) {
              <label
                ><input
                  type="checkbox"
                  [checked]="allowed.includes(item.value)"
                  (change)="toggle(item.value, $event)"
                />{{ item.label }}</label
              >
            }
          </fieldset>
          <p class="hint">
            These are permissions, not verified features. An operation also
            requires a runner that supports it and an approved plan.
          </p>
        }
      </div>
      <div ai-review>
        @if (existing(); as target) {
          <dl>
            <dt>Target</dt>
            <dd>{{ target.name }}</dd>
            <dt>Authority revision</dt>
            <dd>{{ target.revision }}</dd>
            <dt>Runner principal</dt>
            <dd>
              {{ target.runner_principal_id }} →
              {{ authority["runner_principal_id"] }}
            </dd>
            <dt>Permitted operations</dt>
            <dd>
              {{ target.capabilities.join(", ") }} →
              {{ updateValue.capabilities.join(", ") }}
            </dd>
            <dt>Availability policy</dt>
            <dd>
              {{ target.disabled ? "Disabled" : "Enabled" }} →
              {{ authority["disabled"] ? "Disabled" : "Enabled" }}
            </dd>
          </dl>
          <p class="notice">
            Saving changes the authority revision and invalidates current runner
            leases and prior plan preconditions. External operations already
            dispatched may continue. Disabling does not stop containers or roll
            back effects.
          </p>
        } @else {
          <dl>
            <dt>Name</dt>
            <dd>{{ name }}</dd>
            <dt>Runtime</dt>
            <dd>{{ adapterLabels[adapter] }}</dd>
            <dt>Identity / boundary</dt>
            <dd>{{ external }} / {{ boundary }}</dd>
            <dt>Runner principal</dt>
            <dd>{{ principal }}</dd>
            <dt>Permitted operations</dt>
            <dd>{{ allowed.join(", ") }}</dd>
          </dl>
          <p>
            After registration, request an observation to discover the existing
            resources. Import remains separate from adopting management or
            applying a deployment.
          </p>
        }
      </div>
      @if (error()) {
        <p ai-error role="alert">{{ error() }}</p>
      }
      <button
        ai-secondary
        type="button"
        [disabled]="busy()"
        (click)="cancel.emit()"
      >
        {{ existing() ? "Cancel authority changes" : "Cancel registration" }}
      </button>
    </weave-ai-setup-wizard>
  `,
  styles: `
    :host {
      display: block;
      min-width: 0;
    }
    label {
      display: grid;
      gap: 6px;
      margin: 12px 0;
    }
    input {
      min-width: 0;
      max-width: 100%;
    }
    fieldset {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    fieldset label {
      display: flex;
      gap: 8px;
    }
    dd {
      margin: 0 0 12px;
      overflow-wrap: anywhere;
    }
    dt {
      font-weight: 600;
    }
  `,
})
export class TargetWizard implements OnChanges {
  existing = input<Target | null>(null);
  canonicalUpdateSchema = input<Schema | null>(null);
  blocked = input(false);
  update = output<TargetUpdate>();
  editSchema: Schema = {};
  authorityInitial: Record<string, unknown> = {};
  authority: Record<string, unknown> = {};
  authorityValid = true;
  ngOnChanges(changes: SimpleChanges) {
    if (!changes["existing"] && !changes["canonicalUpdateSchema"]) return;
    const target = this.existing();
    if (!target) return;
    const schema = this.canonicalUpdateSchema();
    this.editSchema = {
      ...schema,
      required: [...new Set([...(schema?.required ?? []), "disabled"])],
    };
    this.authorityInitial = {
      runner_principal_id: target.runner_principal_id,
      capabilities: [...target.capabilities],
      disabled: target.disabled,
    };
    this.authority = structuredClone(this.authorityInitial);
    this.authorityValid = true;
    this.step = 0;
  }
  get updateValue(): TargetUpdate {
    return {
      runner_principal_id: String(this.authority["runner_principal_id"] ?? ""),
      capabilities: (this.authority["capabilities"] ?? []) as Capability[],
      disabled: this.authority["disabled"] === true,
    };
  }

  busy = input(false);
  error = input("");
  submit = output<TargetRequest>();
  cancel = output<void>();
  step = 0;
  name = "";
  adapter: Adapter = "docker-compose";
  external = "";
  boundary = "";
  principal = "";
  allowed: Capability[] = ["observe"];
  readonly adapterLabels = adapterLabels;
  readonly adapters = Object.entries(adapterLabels).map(([value, label]) => ({
    value,
    label,
  }));
  readonly capabilities: { value: Capability; label: string }[] = [
    { value: "deploy", label: "Deploy" },
    { value: "update", label: "Update" },
    { value: "scale_workers", label: "Scale workers" },
  ];
  value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  chooseAdapter(value: string) {
    if (value in adapterLabels) this.adapter = value as Adapter;
  }
  toggle(capability: Capability, event: Event) {
    this.allowed = (event.target as HTMLInputElement).checked
      ? [...this.allowed, capability]
      : this.allowed.filter((c) => c !== capability);
  }
  get valid() {
    if (this.existing()) {
      if (this.step === 0) return true;
      const value = this.updateValue;
      return (
        this.authorityValid &&
        /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(
          value.runner_principal_id,
        ) &&
        value.capabilities.includes("observe") &&
        new Set(value.capabilities).size === value.capabilities.length &&
        canonicalJson(value) !== canonicalJson(this.authorityInitial)
      );
    }
    const name = /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$/;
    const identity = /^[a-zA-Z0-9/][a-zA-Z0-9_./:@-]{0,511}$/;
    const destination =
      name.test(this.name) &&
      name.test(this.boundary) &&
      identity.test(this.external);
    return (
      destination &&
      (this.step === 0 ||
        /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(this.principal))
    );
  }
  save() {
    if (!this.valid || this.busy() || this.blocked()) return;
    if (this.existing()) {
      this.update.emit(this.updateValue);
      return;
    }
    if (this.valid && !this.busy())
      this.submit.emit({
        name: this.name,
        adapter: this.adapter,
        external_identity: this.external,
        boundary: this.boundary,
        runner_principal_id: this.principal,
        capabilities: [...this.allowed],
      });
  }
}
