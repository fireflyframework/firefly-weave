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
// The change request wizard: what to change, the details, a review, then
// the progress of the change once it is created. WizardState is pure and
// tested; the component renders it. The server checks every value again.
import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  OnChanges,
  Output,
} from "@angular/core";
import type {
  ChangeIntent,
  ChangeRequestCreate,
  Deployment,
  DesiredComponent,
  Observation,
  ObservedResource,
  Target,
} from "../../operations/deployment-contracts";
import type { AdmittedWorkerRelease } from "../../operations/deployment-onboarding";
import type { WorkerStatus } from "../workers/workers-model";
import {
  changeIntentLabel,
  defaultPlanSeconds,
  planLifetimeError,
  wizardSteps,
} from "./change-request-model";

/** One worker a scale-down would remove, with the tasks it still holds. */
export interface RemovalPreview {
  host: string;
  activeLeases: number | null;
}

/** The choices the intent step offers for a target. */
export function intentChoices(
  target: Target | null,
): { value: ChangeIntent; label: string; enabled: boolean }[] {
  const offered = new Set(target?.capabilities ?? []);
  return (
    [
      ["scale_workers", "Scale workers"],
      ["update", "Update image or resources"],
      ["deploy", "Deploy (Compose only)"],
      ["restart_workers", "Restart workers"],
    ] as [ChangeIntent, string][]
  ).map(([value, label]) => ({
    value,
    label,
    enabled:
      offered.has(value) &&
      (value !== "deploy" || target?.adapter === "docker-compose"),
  }));
}

export class WizardState {
  step = 0;
  intent: ChangeIntent | null = null;
  targetId = "";
  deploymentId = "";
  components = new Set<string>();
  replicas = 1;
  timeoutMinutes = 5;
  drainFirst = true;
  releaseId = "";
  image = "";
  cpuMillis: number | null = null;
  memoryMib: number | null = null;
  title = "";
  reason = "";
  /** Minutes; null keeps the default (10, or 15 when the change lets workers finish). */
  planMinutes: number | null = null;
  constructor(
    public targets: readonly Target[] = [],
    public deployments: readonly Deployment[] = [],
    public observations: readonly Observation[] = [],
    public workers: readonly WorkerStatus[] = [],
    public releases: readonly AdmittedWorkerRelease[] = [],
  ) {}

  get target(): Target | null {
    return this.targets.find((item) => item.id === this.targetId) ?? null;
  }
  get deployment(): Deployment | null {
    return (
      this.deployments.find(
        (item) =>
          item.id === this.deploymentId && item.target_id === this.targetId,
      ) ?? null
    );
  }
  /** Deployments of the chosen target. */
  get targetDeployments(): Deployment[] {
    return this.deployments.filter((item) => item.target_id === this.targetId);
  }
  /** Components the intent may select: workers only for scaling and restarting. */
  get selectable() {
    const all = this.deployment?.components ?? [];
    return this.intent === "scale_workers" || this.intent === "restart_workers"
      ? all.filter((item) => item.kind === "worker")
      : all;
  }
  get selected() {
    return this.selectable.filter((item) => this.components.has(item.name));
  }
  /** The newest complete observation of the chosen target. */
  get observation(): Observation | null {
    return (
      [...this.observations]
        .filter((item) => item.target_id === this.targetId && item.complete)
        .sort((a, b) => b.observed_at.localeCompare(a.observed_at))[0] ?? null
    );
  }
  observed(name: string): ObservedResource | null {
    return (
      this.observation?.resources.find((item) => item.name === name) ?? null
    );
  }
  /** Whether the change lets the workers it removes or restarts finish first. */
  get drains(): boolean {
    if (this.intent === "restart_workers") return this.drainFirst;
    if (this.intent !== "scale_workers") return false;
    return this.selected.some((item) => this.replicas < item.replicas);
  }
  /**
   * The workers a scale-down removes, by the platform's rule: stopped
   * containers go first, then the highest container numbers. Shown so a
   * person sees whose tasks must finish; the plan names the exact set.
   */
  get removals(): RemovalPreview[] {
    if (this.intent !== "scale_workers") return [];
    const result: RemovalPreview[] = [];
    for (const component of this.selected) {
      const resource = this.observed(component.name);
      if (!resource) continue;
      const members = resource.members ?? [];
      const stopped = Math.max(0, resource.replicas - members.length);
      const count = Math.max(0, resource.replicas - this.replicas - stopped);
      const ordered = [...members].sort(
        (a, b) =>
          (b.ordinal ?? 0) - (a.ordinal ?? 0) || b.host.localeCompare(a.host),
      );
      for (const member of ordered.slice(0, count)) {
        const worker = this.workers.find(
          (item) => item.identity?.host === member.host,
        );
        result.push({
          host: member.host,
          activeLeases: worker ? worker.active_leases : null,
        });
      }
    }
    return result;
  }
  get planSeconds(): number {
    return this.planMinutes === null
      ? defaultPlanSeconds(this.drains)
      : this.planMinutes * 60;
  }
  get lifetimeError(): string | null {
    return planLifetimeError(
      this.drains,
      this.timeoutMinutes * 60,
      this.planSeconds,
    );
  }
  /** Why Next or Create is not available yet, or "". */
  get blocker(): string {
    if (this.step === 0) {
      if (!this.targetId) return "Choose a target.";
      if (!this.intent) return "Choose what to change.";
      if (!this.deploymentId) return "Choose a deployment.";
      if (!this.selected.length) return "Choose at least one component.";
      return "";
    }
    if (this.step === 1) {
      if (
        this.intent === "scale_workers" &&
        !(
          Number.isInteger(this.replicas) &&
          this.replicas >= 0 &&
          this.replicas <= 100
        )
      )
        return "Choose from 0 to 100 workers.";
      if (
        this.drains &&
        !(
          Number.isInteger(this.timeoutMinutes) &&
          this.timeoutMinutes >= 1 &&
          this.timeoutMinutes <= 10
        )
      )
        return "Give the workers from 1 to 10 minutes to finish.";
      if (
        this.intent === "update" &&
        !this.releaseId &&
        this.cpuMillis === null &&
        this.memoryMib === null
      )
        return "Choose a release, CPU or memory.";
      return "";
    }
    if (this.step === 2) {
      if (!this.title.trim()) return "Give the change a title.";
      if (this.title.length > 120) return "Keep the title to 120 characters.";
      if (this.reason.length > 2000)
        return "Keep the reason to 2,000 characters.";
      return this.lifetimeError ?? "";
    }
    return "";
  }
  next(): boolean {
    if (this.step >= 2 || this.blocker) return false;
    if (this.step === 0 && !this.title) this.title = this.suggestedTitle();
    this.step++;
    return true;
  }
  back(): boolean {
    if (this.step === 0 || this.step === 3) return false;
    this.step--;
    return true;
  }
  choose(intent: ChangeIntent) {
    this.intent = intent;
    this.components = new Set(this.selectable.map((item) => item.name));
    const first = this.selected[0];
    if (first)
      this.replicas = this.observed(first.name)?.replicas ?? first.replicas;
  }
  suggestedTitle(): string {
    const names = this.selected.map((item) => item.name).join(", ");
    if (this.intent === "scale_workers")
      return `Scale ${names} to ${this.replicas}`;
    return `${this.intent ? changeIntentLabel(this.intent) : "Change"}: ${names}`;
  }
  /** The request Create sends, or null while something still blocks it. */
  request(): ChangeRequestCreate | null {
    const deployment = this.deployment;
    if (this.step !== 2 || this.blocker || !deployment || !this.intent)
      return null;
    const desired = this.selected.flatMap((item): DesiredComponent[] => {
      if (this.intent === "scale_workers")
        return [{ name: item.name, replicas: this.replicas }];
      if (this.intent === "update")
        return [
          {
            name: item.name,
            ...(this.releaseId ? { worker_release_id: this.releaseId } : {}),
            ...(this.image ? { image: this.image } : {}),
            ...(this.cpuMillis !== null ? { cpu_millis: this.cpuMillis } : {}),
            ...(this.memoryMib !== null ? { memory_mib: this.memoryMib } : {}),
          },
        ];
      return [];
    });
    return {
      target_id: this.targetId,
      deployment_id: deployment.id,
      expected_deployment_revision: deployment.revision,
      intent: this.intent,
      components: this.selected.map((item) => item.name),
      ...(desired.length ? { desired } : {}),
      drain: {
        mode: this.drains ? "drain_first" : "none",
        timeout_seconds: this.timeoutMinutes * 60,
      },
      title: this.title.trim(),
      reason: this.reason,
      ...(this.planMinutes !== null
        ? { plan_ttl_seconds: this.planSeconds }
        : {}),
    };
  }
}

@Component({
  selector: "weave-change-request-wizard",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrls: ["./clusters.css", "../operate.css"],
  template: `
    <ol class="wizard-steps" aria-label="Steps">
      @for (label of steps; track label; let index = $index) {
        <li
          [attr.aria-current]="state.step === index ? 'step' : null"
          [class.done]="state.step > index"
        >
          <span class="step-number">{{ index + 1 }}</span
          >{{ label }}
        </li>
      }
    </ol>
    <form class="wizard-body" (submit)="$event.preventDefault()">
      @switch (state.step) {
        @case (0) {
          <label class="field"
            >Target
            <select
              name="target"
              [value]="state.targetId"
              (change)="pickTarget(value($event))"
            >
              <option value="">Choose a target</option>
              @for (item of state.targets; track item.id) {
                <option
                  [value]="item.id"
                  [selected]="item.id === state.targetId"
                >
                  {{ item.name }}
                </option>
              }
            </select>
          </label>
          <fieldset class="field">
            <legend>What do you want to change?</legend>
            <div class="choice-grid">
              @for (choice of choices(); track choice.value) {
                <label class="choice" [class.disabled]="!choice.enabled">
                  <input
                    type="radio"
                    name="intent"
                    [value]="choice.value"
                    [checked]="state.intent === choice.value"
                    [disabled]="!choice.enabled"
                    (change)="state.choose(choice.value)"
                  />{{ choice.label }}
                </label>
              }
            </div>
          </fieldset>
          <label class="field"
            >Deployment
            <select
              name="deployment"
              [value]="state.deploymentId"
              (change)="pickDeployment(value($event))"
            >
              <option value="">Choose a deployment</option>
              @for (item of state.targetDeployments; track item.id) {
                <option
                  [value]="item.id"
                  [selected]="item.id === state.deploymentId"
                >
                  {{ item.name }}
                </option>
              }
            </select>
          </label>
          @if (state.selectable.length) {
            <fieldset class="field">
              <legend>Components</legend>
              @for (item of state.selectable; track item.name) {
                <label class="check"
                  ><input
                    type="checkbox"
                    [checked]="state.components.has(item.name)"
                    (change)="toggle(item.name, checked($event))"
                  />{{ item.name }}</label
                >
              }
            </fieldset>
          }
        }
        @case (1) {
          @if (state.intent === "scale_workers") {
            <label class="field"
              >Workers
              <span class="stepper">
                <button
                  type="button"
                  class="sm"
                  aria-label="One worker fewer"
                  [disabled]="state.replicas <= 0"
                  (click)="state.replicas = state.replicas - 1"
                >
                  −
                </button>
                <input
                  type="number"
                  name="replicas"
                  min="0"
                  max="100"
                  inputmode="numeric"
                  [value]="state.replicas"
                  (input)="state.replicas = number($event)"
                />
                <button
                  type="button"
                  class="sm"
                  aria-label="One worker more"
                  [disabled]="state.replicas >= 100"
                  (click)="state.replicas = state.replicas + 1"
                >
                  +
                </button>
              </span>
            </label>
            @if (state.drains) {
              <section
                class="drain-preview"
                aria-label="Workers this change removes"
              >
                <h3>Workers this change removes</h3>
                <p class="hint">
                  These workers stop taking new tasks and finish what they hold
                  before they are removed.
                </p>
                <ul>
                  @for (item of state.removals; track item.host) {
                    <li>
                      <code>{{ item.host }}</code
                      ><span>{{ leases(item.activeLeases) }}</span>
                    </li>
                  } @empty {
                    <li>
                      Stopped containers are removed first; no running worker is
                      removed.
                    </li>
                  }
                </ul>
              </section>
            }
          }
          @if (state.intent === "update") {
            <label class="field"
              >Worker release
              <select
                name="release"
                [value]="state.releaseId"
                (change)="state.releaseId = value($event)"
              >
                <option value="">Keep the current release</option>
                @for (item of releases; track item.id) {
                  <option [value]="item.id">
                    {{ item.image_digest.slice(0, 19) }}
                  </option>
                }
              </select>
            </label>
            <div class="field-row">
              <label class="field"
                >CPU (millicores)
                <input
                  type="number"
                  name="cpu"
                  min="100"
                  max="16000"
                  (input)="state.cpuMillis = optional($event)"
                />
              </label>
              <label class="field"
                >Memory (MiB)
                <input
                  type="number"
                  name="memory"
                  min="128"
                  max="65536"
                  (input)="state.memoryMib = optional($event)"
                />
              </label>
            </div>
          }
          @if (state.intent === "restart_workers") {
            <label class="check"
              ><input
                type="checkbox"
                name="drain-first"
                [checked]="state.drainFirst"
                (change)="state.drainFirst = checked($event)"
              />Let workers finish their tasks first</label
            >
          }
          @if (state.drains) {
            <label class="field"
              >Time to finish (minutes)
              <input
                type="number"
                name="timeout"
                min="1"
                max="10"
                [value]="state.timeoutMinutes"
                (input)="state.timeoutMinutes = number($event)"
              />
            </label>
          }
          <p class="hint">The runner's local limits may be lower.</p>
        }
        @case (2) {
          <dl class="review">
            <dt>Change</dt>
            <dd>
              {{ state.intent ? intentLabel(state.intent) : "" }} on
              {{ state.target?.name }}
            </dd>
            @for (item of state.selected; track item.name) {
              <dt>{{ item.name }}</dt>
              <dd>
                Now
                {{ state.observed(item.name)?.replicas ?? "unknown" }} running ·
                {{ desiredText(item.name, item.replicas) }}
              </dd>
            }
            <dt>Plan lifetime</dt>
            <dd>
              {{ state.planSeconds / 60 }} minutes.
              @if (state.drains) {
                It must cover the time to finish plus 3 minutes.
              }
            </dd>
          </dl>
          <label class="field"
            >Title
            <input
              name="title"
              maxlength="120"
              [value]="state.title"
              (input)="state.title = value($event)"
            />
          </label>
          <label class="field"
            >Reason <span class="hint">(optional)</span>
            <textarea
              name="reason"
              rows="3"
              maxlength="2000"
              [value]="state.reason"
              (input)="state.reason = value($event)"
            ></textarea>
          </label>
          <label class="field"
            >Plan lifetime (minutes)
            <input
              type="number"
              name="plan-minutes"
              min="1"
              max="15"
              [value]="state.planSeconds / 60"
              (input)="state.planMinutes = number($event)"
            />
          </label>
        }
      }
      @if (state.blocker && state.step < 3) {
        <p class="hint wizard-blocker" role="status">{{ state.blocker }}</p>
      }
      @if (error) {
        <p class="notice" data-tone="danger" role="alert">{{ error }}</p>
      }
      <div class="actions wizard-actions">
        <button type="button" class="tertiary" (click)="cancel.emit()">
          Cancel
        </button>
        @if (state.step > 0) {
          <button type="button" (click)="state.back()">Back</button>
        }
        @if (state.step < 2) {
          <button
            type="button"
            class="primary"
            [disabled]="!!state.blocker"
            (click)="state.next()"
          >
            Next
          </button>
        } @else {
          <button
            type="button"
            class="primary"
            [disabled]="!!state.blocker || busy"
            (click)="submit()"
          >
            Create change request
          </button>
        }
      </div>
    </form>
  `,
})
export class ChangeRequestWizard implements OnChanges {
  @Input() targets: Target[] = [];
  @Input() deployments: Deployment[] = [];
  @Input() observations: Observation[] = [];
  @Input() workers: WorkerStatus[] = [];
  @Input() releases: AdmittedWorkerRelease[] = [];
  @Input() targetId = "";
  @Input() deploymentId = "";
  @Input() busy = false;
  @Input() error = "";
  @Output() create = new EventEmitter<ChangeRequestCreate>();
  @Output() cancel = new EventEmitter<void>();
  @Output() targetChange = new EventEmitter<string>();
  readonly steps = wizardSteps.slice(0, 3);
  readonly intentLabel = changeIntentLabel;
  state = new WizardState();
  ngOnChanges() {
    const fresh = !this.state.targetId && !this.state.intent;
    Object.assign(this.state, {
      targets: this.targets,
      deployments: this.deployments,
      observations: this.observations,
      workers: this.workers,
      releases: this.releases,
    });
    if (fresh && this.targetId) {
      this.state.targetId = this.targetId;
      this.state.deploymentId =
        this.deploymentId ||
        (this.state.targetDeployments.length === 1
          ? this.state.targetDeployments[0].id
          : "");
    }
  }
  choices() {
    return intentChoices(this.state.target);
  }
  pickTarget(id: string) {
    this.state.targetId = id;
    this.state.intent = null;
    this.state.components.clear();
    this.state.deploymentId =
      this.state.targetDeployments.length === 1
        ? this.state.targetDeployments[0].id
        : "";
    this.targetChange.emit(id);
  }
  pickDeployment(id: string) {
    this.state.deploymentId = id;
    if (this.state.intent) this.state.choose(this.state.intent);
  }
  toggle(name: string, on: boolean) {
    if (on) this.state.components.add(name);
    else this.state.components.delete(name);
  }
  desiredText(name: string, current: number) {
    if (this.state.intent === "scale_workers")
      return `${this.state.replicas} after the change`;
    if (this.state.intent === "restart_workers")
      return `${current} restarted in place`;
    return "updated";
  }
  leases(count: number | null) {
    if (count === null) return "No registered worker matches";
    return count === 1 ? "1 task in progress" : `${count} tasks in progress`;
  }
  submit() {
    const request = this.state.request();
    if (request) this.create.emit(request);
  }
  value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }
  number(event: Event) {
    return Number((event.target as HTMLInputElement).value);
  }
  optional(event: Event) {
    const text = (event.target as HTMLInputElement).value;
    return text === "" ? null : Number(text);
  }
}
