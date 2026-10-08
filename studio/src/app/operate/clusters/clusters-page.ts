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
  OnInit,
  inject,
} from "@angular/core";
import { NavigationEnd, Router } from "@angular/router";
import type { App } from "../../app";
import type { LumiOperationAttachment } from "../../lumi/lumi-state";
import { describeError, type PlainError } from "../../errors";
import { shortId } from "../../format";
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
  type Runner,
} from "../../operations/deployment-contracts";
import { ApprovalsInbox } from "./approvals-inbox";
import { ClusterApprovalsTab } from "./cluster-approvals-tab";
import { ClusterDeploymentDetail } from "./cluster-deployment-detail";
import { intentLabel } from "./cluster-model";
import { ClusterJobDetail } from "./cluster-job-detail";
import { ClusterJobsTab } from "./cluster-jobs-tab";
import { ClusterPlanDetail } from "./cluster-plan-detail";
import { ClusterRunnersTab } from "./cluster-runners-tab";
import { ClusterTargetDetail } from "./cluster-target-detail";
import { ClusterTargetsTab } from "./cluster-targets-tab";
import {
  clustersRecordPath,
  clustersRoute,
  clustersTabPath,
  viewFromPath,
  type ClusterCollection,
  type ClusterTab,
} from "../operate-routes";
import { OperateState } from "../operate-state";
import { Poller, browserEnvironment } from "../operate-store";
import { RefreshStatus } from "../refresh-status";

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
    ClusterTargetsTab,
    ClusterApprovalsTab,
    ClusterJobsTab,
    ClusterRunnersTab,
    OperateState,
    RefreshStatus,
  ],
  styleUrls: ["./clusters.css", "../operate.css"],
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
        <weave-refresh-status
          [updatedAt]="poller.lastSuccessAt"
          [busy]="poller.busy || store.mutating"
          (refresh)="reload()"
        />
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
          @if (error ? errorCode : store.mutationErrorCode; as code) {
            <small class="support-code">Support code: {{ code }}</small>
          }
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
        <div
          class="operate-tabs"
          role="tablist"
          aria-label="Clusters sections"
          (keydown)="tabKey($event)"
        >
          @for (item of tabs; track item[0]) {
            <button
              type="button"
              role="tab"
              [id]="'clusters-tab-' + item[0]"
              [attr.aria-controls]="'clusters-panel-' + item[0]"
              [attr.aria-selected]="tab === item[0]"
              [attr.tabindex]="tab === item[0] ? 0 : -1"
              (click)="selectTab(item[0])"
            >
              {{ tabLabel(item[0], item[1]) }}
            </button>
          }
        </div>
        <div
          role="tabpanel"
          [id]="'clusters-panel-' + tab"
          [attr.aria-labelledby]="'clusters-tab-' + tab"
        >
          @switch (tab) {
            @case ("targets") {
              <weave-cluster-targets-tab [page]="this" />
            }
            @case ("approvals") {
              <weave-cluster-approvals-tab [page]="this" />
            }
            @case ("jobs") {
              <weave-cluster-jobs-tab [page]="this" />
            }
            @case ("runners") {
              <weave-cluster-runners-tab [page]="this" />
            }
          }
        </div>
      } @else if (recordError; as failed) {
        @if (failed.status === 403) {
          <weave-operate-state
            kind="access"
            [heading]="recordAccessHeading()"
            capability="deployment.read"
          />
        } @else {
          <weave-operate-state
            kind="error"
            [message]="failed.message"
            [code]="failed.code"
            (retry)="reload()"
          />
        }
      } @else {
        <p role="status">Loading selected resource…</p>
      }
    }
  `,
})
export class ClustersPage implements DoCheck, OnInit, OnDestroy {
  @Input({ required: true }) host!: App;
  private cdr = inject(ChangeDetectorRef);
  private element = inject<ElementRef<HTMLElement>>(ElementRef);
  private router = inject(Router);
  private navigated = this.router.events.subscribe((event) => {
    if (event instanceof NavigationEnd) this.addressChanged();
  });
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
  errorCode = "";
  /** Why the record this address names could not be read. */
  recordError: PlainError | null = null;
  /** The address the page shows or is reading: "targets/ID" or "?tab=jobs". */
  private shown = "";
  private scopeKey = "";
  private alive = true;
  private detailSequence = 0;
  private jobReadSequence = 0;
  /** Clusters reloads every 15 s, and every 2 s while a job runs. */
  readonly poller = new Poller(
    () => this.pollOnce(),
    15000,
    browserEnvironment,
    () => this.waiting,
  );
  readonly inbox = new ApprovalsInbox((path) => this.host.api.request(path));
  tab: ClusterTab = "targets";
  readonly tabs: [ClusterTab, string][] = [
    ["targets", "Targets"],
    ["approvals", "Approvals"],
    ["jobs", "Jobs"],
    ["runners", "Runners"],
  ];
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
    this.errorCode = "";
    this.recordError = null;
    this.store.setScope(scope, this.canRead, authority);
    this.inbox.clear();
    if (scope && this.canRead) void this.readLocation();
  }
  ngOnInit() {
    this.poller.start(false);
  }
  ngOnDestroy() {
    this.alive = false;
    this.navigated.unsubscribe();
    this.poller.stop();
    this.detailSequence++;
    this.store.setScope("", false);
  }
  @HostListener("window:popstate") locationChanged() {
    if (viewFromPath(location.pathname)?.view === "clusters")
      void this.readLocation();
  }
  /**
   * The Clusters entry or a link moved the address (the router fires no
   * popstate): the page follows it unless it already shows that address.
   */
  private addressChanged() {
    if (viewFromPath(location.pathname)?.view !== "clusters") return;
    if (!this.host.profile || !this.canRead) return;
    const { collection, id, tab } = clustersRoute(
      location.pathname,
      location.search,
    );
    if (this.addressKey(collection, id, tab) !== this.shown)
      void this.readLocation();
  }
  private addressKey(
    collection: ClusterCollection | null,
    id: string,
    tab: ClusterTab,
  ) {
    return collection ? `${collection}/${id}` : `?tab=${tab}`;
  }
  private async readLocation() {
    this.resetDetails();
    const { collection, id, tab } = clustersRoute(
      location.pathname,
      location.search,
    );
    this.tab = tab;
    this.shown = this.addressKey(collection, id, tab);
    if (!collection) {
      this.screen = "overview";
      await this.poller.refresh();
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
      // The address stays: a refusal names the access it needs, anything
      // else says why with its code and Try again, beside Back to Clusters.
      if (seq === this.detailSequence) {
        this.recordError = describeError(error);
        this.cdr.markForCheck();
      }
    }
  }
  /** A record's screen whose record has not loaded (yet, or at all). */
  private get recordMissing() {
    return (
      this.screen !== "overview" &&
      !this.target &&
      !this.deployment &&
      !this.plan &&
      !this.job
    );
  }
  /** "You don't have access to this target". */
  recordAccessHeading() {
    return `You don't have access to this ${this.screen}`;
  }
  /** Refresh and Try again: a record that could not be read is read again. */
  reload() {
    return this.recordMissing ? this.readLocation() : this.poller.refresh();
  }
  /** Shows a failure above the page with its support code. */
  private showError(error: unknown) {
    const plain = describeError(error);
    this.error = plain.message;
    this.errorCode = plain.code;
  }
  private resetDetails() {
    this.target = null;
    this.deployment = null;
    this.plan = null;
    this.job = null;
    this.approval = null;
    this.error = "";
    this.errorCode = "";
    this.recordError = null;
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
    this.shown = this.addressKey(null, "", this.tab);
    await this.router.navigateByUrl(clustersTabPath(this.tab));
    await this.poller.refresh();
  }
  async selectTab(tab: ClusterTab) {
    if (tab === this.tab) return;
    this.tab = tab;
    this.shown = this.addressKey(null, "", tab);
    await this.router.navigateByUrl(clustersTabPath(tab), { replaceUrl: true });
    await this.poller.refresh();
  }
  /** Arrow keys, Home and End move between the tabs (WAI-ARIA tabs pattern). */
  tabKey(event: KeyboardEvent) {
    const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
    // Held with Alt, Control or Meta, the key is the browser's: Alt+Left
    // is Back.
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    const order = this.tabs.map((item) => item[0]);
    const index = order.indexOf(this.tab);
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? order.length - 1
          : (index + (event.key === "ArrowRight" ? 1 : -1) + order.length) %
            order.length;
    void this.selectTab(order[next]);
    queueMicrotask(() =>
      document.getElementById(`clusters-tab-${order[next]}`)?.focus(),
    );
  }
  /**
   * Scheduled reloads wait while a form is open, a command is running or
   * there is nothing to read, so a reload never resets what a person is
   * typing and never claims a load that did not happen.
   */
  private get waiting() {
    return (
      !this.host.profile ||
      !this.canRead ||
      this.editing ||
      this.editingAuthority ||
      this.registering ||
      this.planning ||
      this.store.mutating ||
      this.recordMissing
    );
  }
  /** One load of what is on screen; throws when any of it failed. */
  private async pollOnce() {
    const loaded = await this.refresh();
    // 2 s while an open job runs, else 15 s: kept right after every load.
    this.poller.setInterval(
      this.screen === "job" && this.job && !terminalJobs.has(this.job.state)
        ? 2000
        : 15000,
    );
    if (!loaded) throw new Error("Clusters could not load every section.");
  }
  /**
   * "Approvals (2)": the open plans this person may approve. No count while
   * the inbox has not loaded or its last read failed, rather than a stale one.
   */
  tabLabel(tab: ClusterTab, label: string) {
    if (tab !== "approvals" || !this.inbox.loaded || this.inbox.error)
      return label;
    const now = Date.now();
    const count = this.inbox.rows.filter(
      (plan) =>
        Date.parse(plan.expires_at) > now &&
        this.host.can("deployment.approve", plan.target_id),
    ).length;
    return count ? `${label} (${count})` : label;
  }
  async approveFromInbox(plan: Plan) {
    if (
      !this.host.can("deployment.approve", plan.target_id) ||
      !this.fresh(plan.expires_at)
    )
      return;
    const confirmed = await this.host.dialogs.confirm({
      title: "Approve this plan?",
      message: `You approve "${intentLabel(plan.intent)}" on ${this.targetName(plan.target_id)}, digest ${plan.digest.slice(0, 12)}. Applying checks this approval and the target again.`,
      confirmLabel: "Approve plan",
    });
    if (!confirmed) return;
    const result = await this.store.mutate<Approval>(
      "deployment-plans/" + plan.id + "/approve",
      { digest: plan.digest },
    );
    if (!result) return;
    this.host.notify("Plan approved.");
    await this.poller.refresh();
  }
  /** Opens a deployment from the inbox, reading it when it isn't loaded. */
  async openDeploymentById(id: string) {
    const seq = ++this.detailSequence;
    try {
      const value =
        this.store.deployments.find((item) => item.id === id) ??
        (await this.store.read("deployments", id));
      if (value && seq === this.detailSequence)
        await this.openDeployment(value);
    } catch (error) {
      if (seq === this.detailSequence) this.showError(error);
    }
  }
  /**
   * Loads the approvals inbox; false when its plans, or the approval of any
   * plan it lists, could not be read.
   */
  private async loadInbox(): Promise<boolean> {
    try {
      await this.inbox.load(this.host.api.environment);
      return this.inbox.unchecked === 0;
    } catch {
      return false;
    }
  }
  /** A target's name, or "Target 1a2b3c4d" while targets are loading. */
  targetName(id: string) {
    return (
      this.store.targets.find((target) => target.id === id)?.name ??
      `Target ${shortId(id)}`
    );
  }
  async revokeRunner(runner: Runner) {
    if (runner.revoked || !this.host.can("target.manage", runner.target_id))
      return;
    const confirmed = await this.host.dialogs.confirm({
      title: "Revoke this runner?",
      message: `The runner for ${this.targetName(runner.target_id)} can't claim jobs any more, and jobs it is running lose their lease. A revoked runner is never revived: start a new runner to replace it.`,
      confirmLabel: "Revoke runner",
      danger: true,
    });
    if (!confirmed) return;
    const result = await this.store.mutate<Runner>(
      `deployment-runners/${runner.id}/revoke`,
      undefined,
    );
    if (!result) return;
    this.host.notify("Runner revoked.");
    await this.poller.refresh();
  }
  async openTarget(value: Target, navigate = true) {
    this.resetDetails();
    this.target = value;
    this.screen = "target";
    this.shown = this.addressKey("targets", value.id, this.tab);
    if (navigate)
      await this.router.navigateByUrl(clustersRecordPath("targets", value.id));
    this.focusDetail();
    await this.poller.refresh();
  }
  async openDeployment(value: Deployment, navigate = true) {
    this.resetDetails();
    this.deployment = value;
    this.screen = "deployment";
    this.shown = this.addressKey("deployments", value.id, this.tab);
    if (navigate)
      await this.router.navigateByUrl(
        clustersRecordPath("deployments", value.id),
      );
    this.focusDetail();
    await this.poller.refresh();
  }
  async openPlan(value: Plan, navigate = true) {
    this.resetDetails();
    this.plan = value;
    this.screen = "plan";
    this.shown = this.addressKey("plans", value.id, this.tab);
    if (navigate)
      await this.router.navigateByUrl(clustersRecordPath("plans", value.id));
    this.focusDetail();
    await this.poller.refresh();
  }
  async openJob(value: Job, navigate = true) {
    this.resetDetails();
    this.job = value;
    this.screen = "job";
    this.shown = this.addressKey("jobs", value.id, this.tab);
    if (navigate)
      await this.router.navigateByUrl(clustersRecordPath("jobs", value.id));
    this.focusDetail();
    await this.poller.refresh();
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
      if (seq === this.detailSequence) this.showError(error);
    }
  }
  /** Loads what is on screen; false when any section of it failed. */
  async refresh(): Promise<boolean> {
    if (!this.canRead || !this.host.profile) return false;
    // A record that could not be read has nothing to reload, and the tabs
    // behind it are not on screen: nothing loads, so nothing claims it did.
    if (this.recordMissing) return false;
    let failed = false;
    const arrived = (...names: Collection[]) => {
      if (names.some((name) => this.store.errors[name])) failed = true;
    };
    if (this.target) {
      const names: Collection[] = ["observations", "runners", "deployments"];
      await Promise.all(
        names.map((c) =>
          this.store.load(c, false, { target_id: this.target!.id }),
        ),
      );
      arrived(...names);
    } else if (this.deployment) {
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
            failed = true;
            if (seq === this.detailSequence) this.showError(error);
          }),
      ]);
      arrived("plans", "observations");
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
        failed = true;
        if (seq === this.detailSequence) this.showError(error);
      }
      if (
        this.job?.state === "reconciliation_required" &&
        seq === this.detailSequence
      ) {
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
              failed = true;
              if (seq === this.detailSequence) this.showError(error);
            }),
        ]);
        arrived("observations");
      }
    } else if (this.plan) {
      const seq = this.detailSequence;
      try {
        const result = await this.store.approval(this.plan.id);
        if (seq === this.detailSequence) this.approval = result;
      } catch (error) {
        failed = true;
        if (seq === this.detailSequence) this.showError(error);
      }
    } else {
      const names = (
        {
          targets: ["targets", "deployments", "runners"],
          jobs: ["jobs"],
          runners: ["runners", "targets"],
          approvals: ["targets", "deployments"],
        } as Record<ClusterTab, Collection[]>
      )[this.tab];
      // The inbox loads, and counts, only on the tab that shows it: its
      // failure never holds back another tab.
      const inbox = this.tab === "approvals" ? this.loadInbox() : true;
      await Promise.all(names.map((c) => this.store.load(c)));
      arrived(...names);
      if (!(await inbox)) failed = true;
    }
    this.cdr.markForCheck();
    return !failed;
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
      if (seq === this.detailSequence) this.showError(error);
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
      if (seq === this.detailSequence) this.showError(error);
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
      if (seq === this.detailSequence) this.showError(error);
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
