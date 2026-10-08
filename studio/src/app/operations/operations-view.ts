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
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  DoCheck,
  ElementRef,
  HostListener,
  Input,
  OnDestroy,
  inject,
} from "@angular/core";
import { NgTemplateOutlet } from "@angular/common";
import { Router } from "@angular/router";
import type { App } from "../app";
import type { LumiOperationAttachment } from "../lumi/lumi-state";
import { describeError } from "../errors";
import {
  DeploymentReconciliation,
  type ReconcileRequest,
} from "./deployment-reconciliation";
import { DeploymentEditor } from "./deployment-editor";
import {
  DeploymentPlanBuilder,
  type PlanRequest,
} from "./deployment-plan-builder";
import type { Schema } from "../task-schema";
import { RunnerSetup } from "./runner-setup";
import type {
  RunnerApplication,
  AdmittedWorkerRelease,
} from "./deployment-onboarding";
import { TargetWizard } from "./target-wizard";
import { OperationsStore } from "./deployment-store";
import {
  adapterLabels,
  observationState,
  terminalJobs,
  type Target,
  type TargetRequest,
  type TargetUpdate,
  type Deployment,
  type DeploymentRequest,
  type Plan,
  type Job,
  type Approval,
  type Collection,
} from "./deployment-contracts";

@Component({
  selector: "weave-operations-view",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [
    TargetWizard,
    RunnerSetup,
    NgTemplateOutlet,
    DeploymentEditor,
    DeploymentPlanBuilder,
    DeploymentReconciliation,
  ],
  styleUrl: "./operations-view.css",
  template: `
    <header class="operations-heading">
      <div>
        <p class="eyebrow">CONTAINER OPERATIONS</p>
        <h1>Operations</h1>
        <p>
          Targets, deployments and their verified observations in this
          environment.
        </p>
      </div>
      @if (host.profile && canRead) {
        @if (host.can("lumi.use") && lumiAttachments.length) {
          <button type="button" (click)="host.lumiOpen = true">
            Explain with Weave AI
          </button>
        }
        <button type="button" [disabled]="store.mutating" (click)="refresh()">
          Refresh Operations
        </button>
      }
    </header>
    @if (!host.profile) {
      <section class="empty-state">
        <h2>Connect to manage deployment targets</h2>
        <p>
          Infrastructure state comes from authorized outbound runners. Local
          workflow authoring does not connect to Docker, Kubernetes or a cloud
          account.
        </p>
        <button class="primary" (click)="host.openWizard('server')">
          Connect to a platform
        </button>
      </section>
    } @else if (host.signInEnded || !host.identity) {
      <section class="empty-state">
        <h2>Sign in to view Operations</h2>
        <p>
          A current platform identity is required before target information can
          be requested.
        </p>
      </section>
    } @else if (!canRead) {
      <section class="empty-state">
        <h2>Operations access is required</h2>
        <p>
          Ask an administrator for deployment.read in this environment or on the
          target you manage.
        </p>
      </section>
    } @else {
      <div class="operations-scope">
        <strong>{{ host.profile.environmentId }}</strong
        ><span
          >API and scheduler: one instance. Worker replicas are managed
          separately.</span
        >
      </div>
      @if (error || store.mutationError) {
        <p class="notice" role="alert">{{ error || store.mutationError }}</p>
      }
      @if (store.mutationUncertain) {
        <p class="notice">
          The response was lost; the command may have been accepted. Retrying
          unchanged values in this view reuses the same request identity to
          recover its original result. Studio does not automatically retry this
          uncertain result.
        </p>
      }
      @if (screen !== "overview") {
        <button class="tertiary" (click)="overview()">
          Back to Operations
        </button>
      }
      @if (editingAuthority && target && targetUpdateSchema) {
        <section class="operations-panel">
          <h2>Edit target authority</h2>
          <weave-target-wizard
            [existing]="target"
            [canonicalUpdateSchema]="targetUpdateSchema"
            [busy]="store.mutating"
            [blocked]="store.mutationErrorCode === 'WV-REVISION'"
            [error]="store.mutationError"
            (update)="saveTargetAuthority($event)"
            (cancel)="cancelDraft()"
          />
          @if (store.mutationErrorCode === "WV-REVISION") {
            <p class="notice">
              The reviewed revision changed. Reload the target, then review a
              new change before saving.
            </p>
            <button
              [disabled]="store.mutating"
              (click)="reloadTargetAuthority()"
            >
              Reload current target and discard my draft
            </button>
          }
        </section>
      } @else if (editing && editorTarget && deploymentSchema) {
        <section class="operations-panel">
          <h2 tabindex="-1">
            {{
              deployment
                ? "Edit desired deployment"
                : "Record desired deployment"
            }}
          </h2>
          <weave-deployment-editor
            [schema]="deploymentSchema"
            [target]="editorTarget"
            [existing]="deployment"
            [observation]="latestObservation"
            [releases]="workerReleases"
            [releaseMessage]="releaseMessage"
            [busy]="store.mutating"
            [error]="store.mutationError"
            (submit)="saveDeployment($event)"
            (cancel)="cancelDraft()"
          />
        </section>
      } @else if (registering) {
        <section class="operations-panel">
          <h2>Register an existing target</h2>
          <weave-target-wizard
            [applications]="applications"
            [applicationsMessage]="applicationsMessage"
            (administration)="host.navigate('settings')"
            [busy]="store.mutating"
            [error]="store.mutationError"
            (submit)="createTarget($event)"
            (cancel)="cancelDraft()"
          />
        </section>
      } @else if (screen === "target" && target) {
        <section class="operations-panel">
          <p class="eyebrow">DEPLOYMENT TARGET</p>
          <h2 tabindex="-1">{{ target.name }}</h2>
          <dl>
            <dt>Runtime</dt>
            <dd>{{ adapterLabels[target.adapter] }}</dd>
            <dt>External identity</dt>
            <dd>{{ target.external_identity }}</dd>
            <dt>Allowed boundary</dt>
            <dd>{{ target.boundary }}</dd>
            <dt>Authority revision</dt>
            <dd>{{ target.revision }}</dd>
            <dt>Permitted operations</dt>
            <dd>{{ target.capabilities.join(", ") }}</dd>
          </dl>
          @if (target.disabled) {
            <p class="notice">
              This target is disabled. No new operation can start.
            </p>
          }
          <p class="hint">
            Permitted operations are policy, not proof of a working adapter. A
            runner must independently support the operation within its local
            authority.
          </p>
          @if (can("target.manage")) {
            <button [disabled]="store.mutating" (click)="editTargetAuthority()">
              Edit target authority
            </button>
          }
          @if (can("deployment.plan")) {
            <button
              class="primary"
              [disabled]="store.mutating || target.disabled"
              (click)="observe()"
            >
              Observe target
            </button>
          }
        </section>
        <section class="operations-panel">
          <weave-runner-setup
            [target]="target"
            [baseUrl]="host.profile!.baseUrl"
            [registered]="runnerPresent"
            [observed]="observationState(latestObservation) === 'current'"
            (administration)="host.navigate('settings')"
          />
        </section>
        <section class="operations-panel">
          <h2>Observed resources</h2>
          @if (store.loading.has("observations")) {
            <p role="status">Loading observation…</p>
          } @else if (store.errors.observations) {
            <p role="alert">{{ store.errors.observations }}</p>
          } @else if (latestObservation; as observation) {
            <p>
              <strong>{{ observationState(observation) }}</strong> · Observed
              {{ observation.observed_at }} · Expires
              {{ observation.expires_at }}
            </p>
            <p class="hint">
              An observation is a bounded snapshot. It does not guarantee
              current availability.
            </p>
            <ul class="resource-cards">
              @for (
                resource of observation.resources;
                track resource.external_identity
              ) {
                <li>
                  <strong>{{ resource.name }}</strong
                  ><span>{{ resource.kind }} · {{ resource.state }}</span
                  ><span
                    >{{ resource.ready_replicas }} ready of
                    {{ resource.replicas }} observed replicas</span
                  ><code>{{
                    resource.image || "Image identity unavailable"
                  }}</code>
                </li>
              } @empty {
                <li>No resources were reported in this observation.</li>
              }
            </ul>
          } @else {
            <p>
              No observation yet. Request one after installing the authorized
              runner.
            </p>
          }
        </section>
        <section class="operations-panel">
          <h2>Outbound runners</h2>
          <p class="hint">
            Current contact is presence, not container health or available
            worker capacity.
          </p>
          @if (store.errors.runners) {
            <p role="alert">{{ store.errors.runners }}</p>
          }
          <ul class="resource-cards">
            @for (runner of store.runners; track runner.id) {
              <li>
                <code>{{ runner.id }}</code
                ><strong>{{
                  runner.revoked
                    ? "Revoked"
                    : fresh(runner.expires_at)
                      ? "Recent contact"
                      : "Contact expired"
                }}</strong
                ><span>Last contact {{ runner.last_seen }}</span
                ><span
                  >Reported capabilities:
                  {{ runner.capabilities.join(", ") }}</span
                >
              </li>
            } @empty {
              <li>No runner registration has been returned.</li>
            }
          </ul>
        </section>
        <section class="operations-panel">
          <h2>Deployments</h2>
          @if (can("target.manage")) {
            <button
              [disabled]="store.mutating || target.disabled"
              (click)="editDeployment()"
            >
              Record desired deployment
            </button>
          }
          <ng-container [ngTemplateOutlet]="deploymentList" />
        </section>
      } @else if (screen === "deployment" && deployment) {
        <section class="operations-panel">
          <p class="eyebrow">DESIRED DEPLOYMENT</p>
          <h2 tabindex="-1">{{ deployment.name }}</h2>
          <p>Revision {{ deployment.revision }} · {{ deployment.ownership }}</p>
          <p class="hint">
            Desired components are intent, not observed availability. Imported
            intent requires explicit review of a deployment plan before changes.
            Ownership here records declared intent; it is not automatically
            changed by a successful job.
          </p>
          <div class="actions">
            @if (can("target.manage")) {
              <button [disabled]="store.mutating" (click)="editDeployment()">
                Edit desired deployment
              </button>
            }
            @if (can("deployment.plan")) {
              <button
                [disabled]="
                  store.mutating || !relatedTarget || relatedTarget.disabled
                "
                (click)="planning = true"
              >
                Create plan
              </button>
            }
            <button (click)="readTarget(deployment.target_id)">
              View target and observation
            </button>
          </div>
          <ul class="resource-cards">
            @for (component of deployment.components; track component.name) {
              <li>
                <strong>{{ component.name }}</strong
                ><span
                  >{{ component.kind }} · {{ component.replicas }} desired
                  replicas</span
                ><code>{{ component.image }}</code
                ><span
                  >Runner configuration reference:
                  {{ component.configuration }}</span
                >
              </li>
            }
          </ul>
        </section>
        @if (planning && relatedTarget) {
          <section class="operations-panel">
            <weave-deployment-plan-builder
              [target]="relatedTarget"
              [deployment]="deployment"
              [observations]="store.observations"
              [busy]="store.mutating"
              [error]="store.mutationError"
              (submit)="createPlan($event)"
              (cancel)="cancelDraft()"
            />
            @if (store.cursors.observations) {
              <button (click)="loadMore('observations')">
                Load more observations
              </button>
            }
          </section>
        }
        <section class="operations-panel">
          <h2>Plans</h2>
          @if (store.errors.plans) {
            <p role="alert">{{ store.errors.plans }}</p>
          }
          <ul class="resource-cards">
            @for (plan of store.plans; track plan.id) {
              <li>
                <button (click)="openPlan(plan)">
                  {{ plan.intent }} · {{ plan.created_at }}</button
                ><span>{{
                  fresh(plan.expires_at) ? "Review required" : "Expired"
                }}</span
                ><code>{{ plan.digest }}</code>
              </li>
            } @empty {
              <li>No plan has been returned for this deployment.</li>
            }
          </ul>
        </section>
      } @else if (screen === "plan" && plan) {
        <section class="operations-panel">
          <p class="eyebrow">IMMUTABLE PLAN</p>
          <h2 tabindex="-1">Review {{ plan.intent }}</h2>
          <p>
            Target revision {{ plan.target_revision }} · Deployment revision
            {{ plan.deployment_revision }}
          </p>
          <dl>
            <dt>Reviewed digest</dt>
            <dd>
              <code>{{ plan.digest }}</code>
            </dd>
            <dt>Observation digest</dt>
            <dd>
              <code>{{ plan.observation_digest }}</code>
            </dd>
            <dt>Expires</dt>
            <dd>{{ plan.expires_at }}</dd>
            <dt>Estimated cost / duration</dt>
            <dd>Not available</dd>
          </dl>
          @if (!fresh(plan.expires_at)) {
            <p class="notice">
              @if (recoverApply) {
                This plan has expired. An unchanged recovery request can
                retrieve an earlier accepted result; a new apply remains
                blocked.
              } @else {
                This plan has expired. Create and review a new plan before
                applying.
              }
            </p>
          }
          <h3>Changes and preconditions</h3>
          <ul class="resource-cards">
            @for (step of plan.steps; track step.component.name) {
              <li>
                <strong>{{ step.action }} {{ step.component.name }}</strong
                ><span
                  >{{ step.component.kind }} ·
                  {{ step.component.replicas }} desired replicas</span
                ><code>{{ step.component.image }}</code
                ><span
                  >Expected external version:
                  {{ step.expected_version || "Resource must not exist" }}</span
                >
              </li>
            }
          </ul>
          <h3>Operational risks</h3>
          <ul>
            @for (risk of plan.risks; track risk) {
              <li>{{ riskLabel(risk) }}</li>
            } @empty {
              <li>No additional risks were reported by the planner.</li>
            }
          </ul>
          <p class="hint">
            Approval and apply use this exact digest. The server rechecks
            authority, revisions and observation. Cancellation cannot undo
            external effects.
          </p>
          @if (approval) {
            <p role="status">Approval recorded for {{ approval.digest }}</p>
            <p class="hint">
              Recorded approval is historical evidence. Apply rechecks the
              approver’s current authority and all plan preconditions.
            </p>
          } @else {
            <p class="hint">
              Approval has not been confirmed in this view. Applying requires a
              recorded approval for this exact digest; the server checks it
              again.
            </p>
          }
          <div class="actions">
            @if (can("deployment.approve")) {
              <button
                [disabled]="store.mutating || !fresh(plan.expires_at)"
                (click)="approve()"
              >
                Approve reviewed plan
              </button>
            }
            @if (can("deployment.apply")) {
              <button
                class="primary"
                [disabled]="
                  store.mutating || (!fresh(plan.expires_at) && !recoverApply)
                "
                (click)="apply()"
              >
                {{
                  recoverApply ? "Recover apply result" : "Apply approved plan"
                }}
              </button>
            }
          </div>
        </section>
      } @else if (screen === "job" && job) {
        <section class="operations-panel">
          <p class="eyebrow">DURABLE OPERATION</p>
          <h2 tabindex="-1">
            {{
              job.kind === "observe"
                ? "Observe target"
                : "Apply deployment plan"
            }}
          </h2>
          <p role="status">
            <strong>{{ job.state.replaceAll("_", " ") }}</strong>
          </p>
          <dl>
            <dt>Job ID</dt>
            <dd>
              <code>{{ job.id }}</code>
            </dd>
            <dt>Created</dt>
            <dd>{{ job.created_at }}</dd>
            <dt>Deadline</dt>
            <dd>{{ job.deadline }}</dd>
            <dt>Target revision</dt>
            <dd>{{ job.target_revision }}</dd>
          </dl>
          @if (job.state === "reconciliation_required") {
            <p class="notice">
              The external result is uncertain. Observe the target and review
              what changed before creating another plan. Do not retry blindly.
            </p>
          }
          @if (job.receipt; as receipt) {
            <h3>Operation receipt</h3>
            <p>{{ receipt.code.replaceAll("_", " ") }}</p>
            <ul>
              @for (resource of receipt.changed_resources; track resource) {
                <li>{{ resource }}</li>
              }
            </ul>
            @if (receipt.external_effects_may_continue) {
              <p class="notice">
                External effects may continue. Cancellation is not a rollback.
              </p>
            }
          }
          @if (
            job.state === "reconciliation_required" &&
            can("deployment.apply") &&
            can("deployment.approve") &&
            relatedTarget
          ) {
            <weave-deployment-reconciliation
              [job]="job"
              [target]="relatedTarget"
              [observations]="store.observations"
              [busy]="store.mutating"
              (submit)="reconcile($event)"
            />
            @if (store.cursors.observations) {
              <button (click)="loadMore('observations')">
                Load more observations
              </button>
            }
          }
          @if (can("deployment.cancel") && !terminalJobs.has(job.state)) {
            <button [disabled]="store.mutating" (click)="cancelJob()">
              Cancel operation
            </button>
          }
          <button
            [disabled]="store.mutating"
            (click)="readTarget(job.target_id)"
          >
            View target and observation
          </button>
        </section>
      } @else if (screen === "overview") {
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
          @if (store.loading.has("targets")) {
            <p role="status">Loading targets…</p>
          }
          @if (store.errors.targets) {
            <p role="alert">{{ store.errors.targets }}</p>
          }
          <ul class="resource-cards">
            @for (item of store.targets; track item.id) {
              <li>
                <button (click)="openTarget(item)">{{ item.name }}</button
                ><span
                  >{{ adapterLabels[item.adapter] }} · {{ item.boundary }}</span
                ><span>{{ item.disabled ? "Disabled" : "Registered" }}</span>
              </li>
            } @empty {
              @if (!store.loading.has("targets") && !store.errors.targets) {
                <li>
                  No deployment target has been returned in this environment.
                </li>
              }
            }
          </ul>
          @if (store.cursors.targets) {
            <button (click)="loadMore('targets')">Load more targets</button>
          }
        </section>
        <section class="operations-panel">
          <h2>Deployments</h2>
          <ng-container [ngTemplateOutlet]="deploymentList" />
        </section>
        <section class="operations-panel">
          <h2>Operations history</h2>
          @if (store.errors.jobs) {
            <p role="alert">{{ store.errors.jobs }}</p>
          }
          <ul class="resource-cards">
            @for (item of store.jobs; track item.id) {
              <li>
                <button (click)="openJob(item)">
                  {{ item.kind }} · {{ item.created_at }}</button
                ><span>{{ item.state.replaceAll("_", " ") }}</span>
              </li>
            } @empty {
              <li>No operation has been returned.</li>
            }
          </ul>
          @if (store.cursors.jobs) {
            <button (click)="loadMore('jobs')">Load more operations</button>
          }
        </section>
      } @else {
        <p role="status">Loading selected resource…</p>
      }
      <ng-template #deploymentList>
        @if (store.errors.deployments) {
          <p role="alert">{{ store.errors.deployments }}</p>
        }
        <ul class="resource-cards">
          @for (item of store.deployments; track item.id) {
            <li>
              <button (click)="openDeployment(item)">{{ item.name }}</button
              ><span
                >{{ item.ownership }} · desired revision
                {{ item.revision }}</span
              >
            </li>
          } @empty {
            <li>No deployment has been returned.</li>
          }
        </ul>
        @if (store.cursors.deployments) {
          <button (click)="loadMore('deployments')">
            Load more deployments
          </button>
        }
      </ng-template>
    }
  `,
})
export class OperationsView implements DoCheck, OnDestroy {
  @Input({ required: true }) host!: App;
  private cdr = inject(ChangeDetectorRef);
  private element = inject<ElementRef<HTMLElement>>(ElementRef);
  private router = inject(Router);
  readonly store = new OperationsStore(
    {
      request: (path, method, body, headers) =>
        this.host.api.request(path, method, body, headers),
    },
    () => this.cdr.markForCheck(),
  );
  readonly adapterLabels = adapterLabels;
  readonly terminalJobs = terminalJobs;
  readonly observationState = observationState;
  screen: "overview" | "target" | "deployment" | "plan" | "job" = "overview";
  target: Target | null = null;
  deployment: Deployment | null = null;
  plan: Plan | null = null;
  job: Job | null = null;
  approval: Approval | null = null;
  registering = false;
  applications: RunnerApplication[] = [];
  applicationsMessage = "";
  workerReleases: AdmittedWorkerRelease[] = [];
  releaseMessage = "";
  get runnerPresent() {
    return this.store.runners.some(
      (r) =>
        r.target_id === this.target?.id &&
        !r.revoked &&
        this.fresh(r.expires_at),
    );
  }
  async startRegistration() {
    this.registering = true;
    this.applications = [];
    this.applicationsMessage = this.host.can("grant.admin")
      ? "Loading application accounts…"
      : "An administrator can select an application account. Ask them to prepare its identity link and give you its application ID.";
    if (!this.host.can("grant.admin")) return;
    const seq = this.detailSequence;
    try {
      let cursor: string | null = null;
      const applications: RunnerApplication[] = [];
      let pages = 0;
      do {
        pages++;
        const page: {
          items: Record<string, unknown>[];
          next_cursor: string | null;
        } = await this.host.api.request(
          `/studio/api/api/v1/admin/principals?limit=100${cursor ? "&cursor=" + encodeURIComponent(cursor) : ""}`,
        );
        if (seq !== this.detailSequence || !this.registering) return;
        applications.push(
          ...page.items
            .filter(
              (item) =>
                item["kind"] === "application" && item["active"] === true,
            )
            .map((item) => ({
              value: String(item["id"]),
              label: String(
                item["display_name"] ||
                  item["name"] ||
                  `Application ${String(item["id"]).slice(0, 8)}`,
              ),
            })),
        );
        cursor = page.next_cursor;
      } while (cursor && pages < 10);
      this.applications = applications;
      this.applicationsMessage = cursor
        ? "Showing the first 1,000 accounts. Use an administrator-provided ID if the dedicated application is not listed."
        : applications.length
          ? "Choose the dedicated machine application. Its provider credentials stay on the runner."
          : "No active application accounts are available. Create and link a dedicated application in account setup, then return here.";
    } catch (error) {
      if (seq === this.detailSequence)
        this.applicationsMessage = describeError(error).message;
    }
    this.cdr.markForCheck();
  }
  editingAuthority = false;
  targetUpdateSchema: Schema | null = null;
  editing = false;
  planning = false;
  relatedTarget: Target | null = null;
  editorTarget: Target | null = null;
  deploymentSchema: Schema | null = null;
  error = "";
  private scopeKey = "";
  private alive = true;
  private detailSequence = 0;
  private jobReadSequence = 0;
  private poll = setInterval(() => {
    this.cdr.markForCheck();
    if (
      this.alive &&
      document.visibilityState === "visible" &&
      this.job &&
      !terminalJobs.has(this.job.state) &&
      !this.store.mutating
    )
      void this.refresh();
  }, 10000);
  get canRead() {
    return (
      !this.host.signInEnded &&
      (this.host.can("deployment.read") ||
        !!this.host.identity?.grants.some((g) =>
          g.resources.some((id) => this.host.can("deployment.read", id)),
        ))
    );
  }
  can(capability: string) {
    return this.host.can(
      capability,
      this.target?.id ??
        this.deployment?.target_id ??
        this.plan?.target_id ??
        this.job?.target_id,
    );
  }
  get lumiAttachments(): LumiOperationAttachment[] {
    if (!this.canRead || !this.can("deployment.read")) return [];
    const targetId =
      this.target?.id ??
      this.deployment?.target_id ??
      this.plan?.target_id ??
      this.job?.target_id;
    if (!targetId) return [];
    const items: LumiOperationAttachment[] = [
      {
        kind: "deployment-target",
        id: targetId,
        label: "Include selected target",
      },
    ];
    if (this.deployment)
      items.push({
        kind: "deployment",
        id: this.deployment.id,
        label: "Include desired deployment",
      });
    if (this.plan)
      items.push({
        kind: "deployment-plan",
        id: this.plan.id,
        label: "Include reviewed plan",
      });
    if (this.job)
      items.push({
        kind: "deployment-job",
        id: this.job.id,
        label: "Include selected operation",
      });
    const observationId =
      this.plan?.observation_id ??
      this.job?.observation_id ??
      [...this.store.observations]
        .filter((item) => item.target_id === targetId)
        .sort((a, b) => b.observed_at.localeCompare(a.observed_at))[0]?.id;
    if (observationId)
      items.push({
        kind: "deployment-observation",
        id: observationId,
        label: "Include selected observation",
      });
    return items;
  }
  fresh(expires: string) {
    return Date.parse(expires) > Date.now();
  }
  get latestObservation() {
    return (
      [...this.store.observations].sort((a, b) =>
        b.observed_at.localeCompare(a.observed_at),
      )[0] ?? null
    );
  }
  ngDoCheck() {
    let scope = "";
    try {
      if (this.host.profile) scope = this.host.api.environment;
    } catch {
      /* No authorized environment yet. */
    }
    const authority = JSON.stringify(this.host.identity);
    const key = scope + ":" + this.canRead + ":" + authority;
    if (key === this.scopeKey) return;
    this.scopeKey = key;
    this.detailSequence++;
    this.target = null;
    this.deployment = null;
    this.plan = null;
    this.job = null;
    this.approval = null;
    this.registering = false;
    this.editing = false;
    this.editingAuthority = false;
    this.planning = false;
    this.relatedTarget = null;
    this.applications = [];
    this.workerReleases = [];
    this.editorTarget = null;
    this.error = "";
    this.store.setScope(scope, this.canRead, authority);
    if (scope && this.canRead) void this.readLocation();
  }
  ngOnDestroy() {
    this.alive = false;
    clearInterval(this.poll);
    this.detailSequence++;
    this.store.setScope("", false);
  }
  @HostListener("window:popstate") locationChanged() {
    void this.readLocation();
  }
  private async readLocation() {
    this.resetDetails();
    const match =
      /^\/operations\/(targets|deployments|plans|jobs)\/([0-9a-f-]{36})$/.exec(
        location.pathname,
      );
    if (!match) {
      this.screen = "overview";
      await this.refresh();
      return;
    }
    const [_, collection, id] = match;
    this.screen = (
      {
        targets: "target",
        deployments: "deployment",
        plans: "plan",
        jobs: "job",
      } as const
    )[collection as "targets" | "deployments" | "plans" | "jobs"];
    const seq = ++this.detailSequence;
    try {
      const value = await this.store.read(collection as "targets", id);
      if (!this.alive || seq !== this.detailSequence || !value) return;
      if (collection === "targets") await this.openTarget(value, false);
      else if (collection === "deployments")
        await this.openDeployment(value as unknown as Deployment, false);
      else if (collection === "plans")
        await this.openPlan(value as unknown as Plan, false);
      else await this.openJob(value as unknown as Job, false);
    } catch (error) {
      if (seq === this.detailSequence) {
        this.error = describeError(error).message;
        this.cdr.markForCheck();
      }
    }
  }
  private resetDetails() {
    this.target = null;
    this.deployment = null;
    this.plan = null;
    this.job = null;
    this.approval = null;
    this.error = "";
    this.registering = false;
    this.editing = false;
    this.editingAuthority = false;
    this.planning = false;
    this.relatedTarget = null;
    this.editorTarget = null;
    this.detailSequence++;
  }
  async overview() {
    this.resetDetails();
    this.screen = "overview";
    await this.router.navigateByUrl("/operations");
    await this.refresh();
  }
  async openTarget(value: Target, navigate = true) {
    this.resetDetails();
    this.target = value;
    this.screen = "target";
    if (navigate)
      await this.router.navigateByUrl("/operations/targets/" + value.id);
    this.focusDetail();
    await this.refresh();
  }
  async openDeployment(value: Deployment, navigate = true) {
    this.resetDetails();
    this.deployment = value;
    this.screen = "deployment";
    if (navigate)
      await this.router.navigateByUrl("/operations/deployments/" + value.id);
    this.focusDetail();
    await this.refresh();
  }
  async openPlan(value: Plan, navigate = true) {
    this.resetDetails();
    this.plan = value;
    this.screen = "plan";
    if (navigate)
      await this.router.navigateByUrl("/operations/plans/" + value.id);
    this.focusDetail();
    await this.refresh();
  }
  async openJob(value: Job, navigate = true) {
    this.resetDetails();
    this.job = value;
    this.screen = "job";
    if (navigate)
      await this.router.navigateByUrl("/operations/jobs/" + value.id);
    this.focusDetail();
    await this.refresh();
  }
  private focusDetail() {
    const seq = this.detailSequence;
    this.cdr.detectChanges();
    requestAnimationFrame(() => {
      if (!this.alive || seq !== this.detailSequence) return;
      const heading = this.element.nativeElement.querySelector<HTMLElement>(
        ".operations-panel h2[tabindex]",
      );
      heading?.focus();
    });
  }
  async readTarget(id: string) {
    const seq = ++this.detailSequence;
    try {
      const result = await this.store.read("targets", id);
      if (result && seq === this.detailSequence) await this.openTarget(result);
    } catch (error) {
      if (seq === this.detailSequence)
        this.error = describeError(error).message;
    }
  }
  async refresh() {
    if (!this.canRead || !this.host.profile) return;
    if (this.target)
      await Promise.all(
        ["observations", "runners", "deployments"].map((c) =>
          this.store.load(c as Collection, false, {
            target_id: this.target!.id,
          }),
        ),
      );
    else if (this.deployment) {
      const deployment = this.deployment;
      const seq = this.detailSequence;
      await Promise.all([
        this.store.load("plans", false, {
          target_id: deployment.target_id,
          deployment_id: deployment.id,
        }),
        this.store.load("observations", false, {
          target_id: deployment.target_id,
        }),
        this.store
          .read("targets", deployment.target_id)
          .then((value) => {
            if (seq === this.detailSequence) this.relatedTarget = value;
          })
          .catch((error) => {
            if (seq === this.detailSequence)
              this.error = describeError(error).message;
          }),
      ]);
    } else if (this.job) {
      const selected = this.job;
      const seq = this.detailSequence;
      const read = ++this.jobReadSequence;
      try {
        const result = await this.store.read("jobs", selected.id);
        if (
          result &&
          seq === this.detailSequence &&
          read === this.jobReadSequence
        )
          this.job = result;
      } catch (error) {
        if (seq === this.detailSequence)
          this.error = describeError(error).message;
      }
      if (
        this.job?.state === "reconciliation_required" &&
        seq === this.detailSequence
      )
        await Promise.all([
          this.store.load("observations", false, {
            target_id: selected.target_id,
          }),
          this.store
            .read("targets", selected.target_id)
            .then((value) => {
              if (seq === this.detailSequence) this.relatedTarget = value;
            })
            .catch((error) => {
              if (seq === this.detailSequence)
                this.error = describeError(error).message;
            }),
        ]);
    } else if (this.plan) {
      const seq = this.detailSequence;
      try {
        const result = await this.store.approval(this.plan.id);
        if (seq === this.detailSequence) this.approval = result;
      } catch (error) {
        if (seq === this.detailSequence)
          this.error = describeError(error).message;
      }
    } else if (!this.plan)
      await Promise.all(
        ["targets", "deployments", "jobs"].map((c) =>
          this.store.load(c as Collection),
        ),
      );
    this.cdr.markForCheck();
  }
  loadMore(collection: Collection) {
    const target_id =
      this.target?.id ?? this.deployment?.target_id ?? this.job?.target_id;
    return this.store.load(collection, true, {
      ...(target_id ? { target_id } : {}),
      ...(collection === "plans" && this.deployment
        ? { deployment_id: this.deployment.id }
        : {}),
    });
  }
  cancelDraft() {
    if (this.store.mutating) return;
    this.editing = false;
    this.editingAuthority = false;
    this.registering = false;
    this.planning = false;
    this.store.cancelMutation();
  }
  async editTargetAuthority() {
    if (!this.target || !this.can("target.manage") || this.store.mutating)
      return;
    const seq = this.detailSequence;
    try {
      const schema = await this.host.api.request<Schema>(
        "/studio/contracts/deployment-target-update",
      );
      if (seq !== this.detailSequence) return;
      this.targetUpdateSchema = schema;
      this.editingAuthority = true;
      this.store.cancelMutation();
      this.cdr.markForCheck();
    } catch (error) {
      if (seq === this.detailSequence)
        this.error = describeError(error).message;
    }
  }
  async saveTargetAuthority(request: TargetUpdate) {
    if (
      !this.target ||
      !this.can("target.manage") ||
      this.store.mutationErrorCode === "WV-REVISION"
    )
      return;
    const seq = this.detailSequence;
    const result = await this.store.mutate<Target>(
      "deployment-targets/" + this.target.id,
      request,
      "PUT",
      this.target.revision,
    );
    if (result && seq === this.detailSequence) await this.openTarget(result);
  }
  async reloadTargetAuthority() {
    if (!this.target || this.store.mutating) return;
    const id = this.target.id;
    const seq = ++this.detailSequence;
    try {
      const target = await this.store.read("targets", id);
      if (target && seq === this.detailSequence) {
        this.store.cancelMutation();
        await this.openTarget(target);
      }
    } catch (error) {
      if (seq === this.detailSequence)
        this.error = describeError(error).message;
    } finally {
      if (this.alive) this.cdr.markForCheck();
    }
  }
  async editDeployment() {
    if (!this.can("target.manage")) return;
    const seq = this.detailSequence;
    const target = this.target ?? this.relatedTarget;
    if (!target || target.disabled) return;
    try {
      const schema = await this.host.api.request<Schema>(
        "/studio/contracts/deployment",
      );
      if (seq !== this.detailSequence) return;
      this.workerReleases = [];
      this.releaseMessage =
        "Selecting admitted worker releases requires catalog.read in this environment. Ask an administrator if you need worker components.";
      if (this.host.can("catalog.read")) {
        try {
          let cursor: string | undefined;
          let pages = 0;
          do {
            pages++;
            const page = await this.host.api.page(
              "worker-releases",
              true,
              cursor,
            );
            if (seq !== this.detailSequence) return;
            this.workerReleases.push(
              ...(page.items as unknown as AdmittedWorkerRelease[]),
            );
            cursor = page.next_cursor ?? undefined;
          } while (cursor && pages < 20);
          this.releaseMessage = cursor
            ? "Only the first 1,000 admitted releases are shown. Use the CLI for a release outside this list."
            : this.workerReleases.length
              ? "Select the admitted worker release paired with this image in your build receipt. Its configuration digest differs from the registry manifest digest; Studio cannot verify that pairing."
              : "No worker releases have been admitted in this environment. Admit a release before adding worker components.";
        } catch (error) {
          this.releaseMessage = describeError(error).message;
        }
      }
      if (seq !== this.detailSequence) return;
      this.deploymentSchema = schema;
      this.editorTarget = target;
      this.editing = true;
      this.cdr.markForCheck();
    } catch (error) {
      if (seq === this.detailSequence)
        this.error = describeError(error).message;
    }
  }
  async saveDeployment(request: DeploymentRequest) {
    if (!this.can("target.manage")) return;
    const seq = this.detailSequence;
    const current = this.deployment;
    const result = await this.store.mutate<Deployment>(
      "deployments" + (current ? "/" + current.id : ""),
      request,
      current ? "PUT" : "POST",
      current?.revision,
    );
    if (result && seq === this.detailSequence)
      await this.openDeployment(result);
  }
  async createPlan(request: PlanRequest) {
    if (!this.can("deployment.plan")) return;
    const seq = this.detailSequence;
    const result = await this.store.mutate<Plan>("deployment-plans", request);
    if (result && seq === this.detailSequence) await this.openPlan(result);
  }
  async createTarget(request: TargetRequest) {
    if (!this.can("target.manage")) return;
    const seq = this.detailSequence;
    const target = await this.store.mutate<Target>(
      "deployment-targets",
      request,
    );
    if (target && seq === this.detailSequence) await this.openTarget(target);
  }
  async observe() {
    if (!this.target || !this.can("deployment.plan")) return;
    const seq = this.detailSequence;
    const job = await this.store.mutate<Job>("deployment-observations", {
      target_id: this.target.id,
    });
    if (job && seq === this.detailSequence) await this.openJob(job);
  }
  async approve() {
    if (
      !this.plan ||
      !this.can("deployment.approve") ||
      !this.fresh(this.plan.expires_at)
    )
      return;
    const seq = this.detailSequence;
    const result = await this.store.mutate<Approval>(
      "deployment-plans/" + this.plan.id + "/approve",
      { digest: this.plan.digest },
    );
    if (seq === this.detailSequence) this.approval = result;
  }
  get recoverApply() {
    return (
      !!this.plan &&
      this.store.canRecover("deployment-plans/" + this.plan.id + "/apply", {
        digest: this.plan.digest,
      })
    );
  }
  async apply() {
    if (
      !this.plan ||
      !this.can("deployment.apply") ||
      (!this.fresh(this.plan.expires_at) && !this.recoverApply)
    )
      return;
    const seq = this.detailSequence;
    const job = await this.store.mutate<Job>(
      "deployment-plans/" + this.plan.id + "/apply",
      { digest: this.plan.digest },
    );
    if (job && seq === this.detailSequence) await this.openJob(job);
  }
  async cancelJob() {
    if (
      !this.job ||
      !this.can("deployment.cancel") ||
      terminalJobs.has(this.job.state)
    )
      return;
    const seq = this.detailSequence;
    const job = await this.store.mutate<Job>(
      "deployment-jobs/" + this.job.id + "/cancel",
      { reason: "operator_cancelled" },
      "POST",
      this.job.revision,
    );
    if (job && seq === this.detailSequence) {
      this.job = job;
      this.jobReadSequence++;
    }
  }
  async reconcile(request: ReconcileRequest) {
    if (
      !this.job ||
      this.job.state !== "reconciliation_required" ||
      !this.can("deployment.apply") ||
      !this.can("deployment.approve")
    )
      return;
    const seq = this.detailSequence;
    const result = await this.store.mutate<Job>(
      "deployment-jobs/" + this.job.id + "/reconcile",
      request,
    );
    if (result && seq === this.detailSequence) {
      this.job = result;
      this.jobReadSequence++;
    }
  }
  riskLabel(risk: string) {
    return (
      (
        {
          service_interruption:
            "Service interruption is possible. Schedule the change appropriately.",
          external_effects:
            "External resources may change; cancellation is not rollback.",
          adoption: "Existing resources will be adopted into management.",
          worker_drain: "Worker reduction requires draining admitted work.",
        } as Record<string, string>
      )[risk] ?? risk
    );
  }
}
