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
import { Router } from "@angular/router";
import type { App } from "../../app";
import type { LumiOperationAttachment } from "../../lumi/lumi-state";
import { describeError } from "../../errors";
import type { ReconcileRequest } from "../../operations/deployment-reconciliation";
import { DeploymentEditor } from "../../operations/deployment-editor";
import type { PlanRequest } from "../../operations/deployment-plan-builder";
import type { Schema } from "../../task-schema";
import type {
  RunnerApplication,
  AdmittedWorkerRelease,
} from "../../operations/deployment-onboarding";
import { TargetWizard } from "../../operations/target-wizard";
import { OperationsStore } from "../../operations/deployment-store";
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
} from "../../operations/deployment-contracts";
import { ClusterDeploymentDetail } from "./cluster-deployment-detail";
import { ClusterJobDetail } from "./cluster-job-detail";
import { ClusterOverview } from "./cluster-overview";
import { ClusterPlanDetail } from "./cluster-plan-detail";
import { ClusterTargetDetail } from "./cluster-target-detail";
import { clustersRecordPath, clustersRoute } from "../operate-routes";

@Component({
  selector: "weave-clusters-page",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [
    TargetWizard,
    DeploymentEditor,
    ClusterTargetDetail,
    ClusterDeploymentDetail,
    ClusterPlanDetail,
    ClusterJobDetail,
    ClusterOverview,
  ],
  styleUrl: "./clusters.css",
  template: `
    <header class="operations-heading">
      <div>
        <h1>Clusters</h1>
        <p class="subtitle">
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
          Refresh
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
        <h2>Sign in to view Clusters</h2>
        <p>
          A current platform identity is required before target information can
          be requested.
        </p>
      </section>
    } @else if (!canRead) {
      <section class="empty-state">
        <h2>Clusters access is required</h2>
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
        <p class="notice" data-tone="danger" role="alert">
          {{ error || store.mutationError }}
        </p>
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
        <button class="tertiary" (click)="overview()">Back to Clusters</button>
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
        <weave-cluster-target-detail [page]="this" />
      } @else if (screen === "deployment" && deployment) {
        <weave-cluster-deployment-detail [page]="this" />
      } @else if (screen === "plan" && plan) {
        <weave-cluster-plan-detail [page]="this" />
      } @else if (screen === "job" && job) {
        <weave-cluster-job-detail [page]="this" />
      } @else if (screen === "overview") {
        <weave-cluster-overview [page]="this" />
      } @else {
        <p role="status">Loading selected resource…</p>
      }
    }
  `,
})
export class ClustersPage implements DoCheck, OnDestroy {
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
    const { collection, id } = clustersRoute(
      location.pathname,
      location.search,
    );
    if (!collection) {
      this.screen = "overview";
      await this.refresh();
      return;
    }
    this.screen = (
      {
        targets: "target",
        deployments: "deployment",
        plans: "plan",
        jobs: "job",
      } as const
    )[collection];
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
    await this.router.navigateByUrl("/operate/clusters");
    await this.refresh();
  }
  async openTarget(value: Target, navigate = true) {
    this.resetDetails();
    this.target = value;
    this.screen = "target";
    if (navigate)
      await this.router.navigateByUrl(clustersRecordPath("targets", value.id));
    this.focusDetail();
    await this.refresh();
  }
  async openDeployment(value: Deployment, navigate = true) {
    this.resetDetails();
    this.deployment = value;
    this.screen = "deployment";
    if (navigate)
      await this.router.navigateByUrl(
        clustersRecordPath("deployments", value.id),
      );
    this.focusDetail();
    await this.refresh();
  }
  async openPlan(value: Plan, navigate = true) {
    this.resetDetails();
    this.plan = value;
    this.screen = "plan";
    if (navigate)
      await this.router.navigateByUrl(clustersRecordPath("plans", value.id));
    this.focusDetail();
    await this.refresh();
  }
  async openJob(value: Job, navigate = true) {
    this.resetDetails();
    this.job = value;
    this.screen = "job";
    if (navigate)
      await this.router.navigateByUrl(clustersRecordPath("jobs", value.id));
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
