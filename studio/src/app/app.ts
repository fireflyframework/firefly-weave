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
  HostListener,
  signal,
  inject,
  ChangeDetectorRef,
} from "@angular/core";
import { Router } from "@angular/router";
import { Icon } from "./icon";
import { FFlowModule } from "@foblex/flow";
import { TaskForm, Schema } from "./task-form";
import {
  ConnectionAssistant,
  ConnectionConfiguration,
} from "./connection-assistant";
import { HomeDashboard } from "./home-dashboard";
import { StepPropertyGrid } from "./property-grid";
import { ApiError, StudioApi, Validation, Session } from "./api";
import {
  Kind,
  kinds,
  StructuredCanvasAdapter,
  Workflow,
  Node,
  Target,
  Step,
} from "./model";
import { stringify } from "yaml";
interface Identity {
  principal_id: string;
  kind: string;
  grants: {
    role: string;
    scope: {
      tenant_id: string;
      project_id?: string | null;
      environment_id?: string | null;
    } | null;
    resources: string[];
    capabilities: string[];
  }[];
  workspaces: {
    id: string;
    name: string;
    projects: {
      id: string;
      name: string;
      environments: { id: string; name: string }[];
    }[];
  }[];
  truncated: boolean;
}
type View =
  | "home"
  | "workflows"
  | "designer"
  | "runs"
  | "tasks"
  | "email"
  | "connections"
  | "workers"
  | "settings";
@Component({
  selector: "weave-studio",
  standalone: true,
  imports: [
    Icon,
    FFlowModule,
    TaskForm,
    StepPropertyGrid,
    HomeDashboard,
    ConnectionAssistant,
  ],
  templateUrl: "./app.html",
})
export class App {
  private cdr = inject(ChangeDetectorRef);
  private router = inject(Router);
  crypto = crypto;
  String = String;
  Math = Math;
  connectionStatus: Record<string, unknown> | null = null;
  connectionBusy = false;
  loginTimer: ReturnType<typeof setTimeout> | null = null;
  loginGeneration = 0;
  importGeneration = 0;
  connectionRevision = 0;
  identity: Identity | null = null;
  scopeChoice = "";
  taskData: Record<string, unknown> = {};
  emailDetail: Record<string, unknown> | null = null;
  emailSubmission: Record<string, unknown> | null = null;
  runHistory: Record<string, unknown> | null = null;
  runCanvas: StructuredCanvasAdapter | null = null;
  runLifecycle: Record<string, unknown> | null = null;
  runIncludeArchived = false;
  debugSession: Record<string, unknown> | null = null;
  pendingMutation: {
    path: string;
    method: string;
    body: unknown;
    revision?: number;
    key: string;
    label: string;
  } | null = null;

  api = new StudioApi();
  model = new StructuredCanvasAdapter();
  tick = signal(0);
  view: View = "home";
  tab = "Designer";
  collapsed = false;
  windowWidth = window.innerWidth;
  showPalette = false;
  paletteWidth = window.innerWidth <= 1440 ? 192 : 208;
  inspectorWidth = window.innerWidth <= 1440 ? 296 : 320;
  resizing: {
    pane: "palette" | "inspector";
    x: number;
    initial: number;
  } | null = null;
  nav: { id: View; label: string }[] = [
    { id: "home", label: "Home" },
    { id: "workflows", label: "Workflows" },
    { id: "runs", label: "Runs" },
    { id: "tasks", label: "My Tasks" },
    { id: "email", label: "Email" },
    { id: "connections", label: "Connections" },
    { id: "workers", label: "Workers" },
    { id: "settings", label: "Settings" },
  ];
  kinds = kinds;
  paletteQuery = "";
  search = "";
  libraryCollection = "workflows";
  pairCode = "";
  paired = false;
  connecting = true;
  message = "";
  busy = "";
  error = "";
  sourceBuffer = this.model.source;
  inspectorBuffer = "";
  showOutline = false;
  showInspector = false;
  diagnostics: Validation | null = null;
  validationSource = "";
  homeTasks: Record<string, unknown>[] = [];
  homeRuns: Record<string, unknown>[] = [];
  records: Record<string, unknown>[] = [];
  nextCursor: string | null = null;
  selectedRecord: Record<string, unknown> | null = null;
  loading = false;
  draftId: string = crypto.randomUUID();
  draftRevision: number | undefined;
  saveState = "Unsaved";
  published: Record<string, unknown> | null = null;
  activation: Record<string, unknown> | null = null;
  conflict: Record<string, unknown> | null = null;
  selectedTarget = "root:0";
  adminPrincipals: Record<string, unknown>[] = [];
  adminMembers: Record<string, unknown>[] = [];
  adminPrincipalCursor: string | null = null;
  adminMemberCursor: string | null = null;
  adminGeneration = 0;
  adminLoading = false;
  adminUncertainCreate = false;
  principalKind = "human";
  principalId = "";
  memberRole = "developer";
  memberScope = "environment";
  memberResources = "";
  identityProvider = "";
  identityIssuer = "";
  identitySubject = "";
  memberRoles = [
    { value: "developer", label: "Workflow editor" },
    { value: "viewer", label: "Viewer" },
    { value: "operator", label: "Run operator" },
    { value: "execution_manager", label: "Execution manager" },
    { value: "deployer", label: "Deployer" },
    { value: "task_participant", label: "Task participant" },
    { value: "task_manager", label: "Task manager" },
    { value: "tenant_admin", label: "Tenant administrator" },
    { value: "worker", label: "Worker" },
    { value: "email_reader", label: "Email reader" },
    { value: "email_sender", label: "Email sender" },
    { value: "email_manager", label: "Email manager" },
  ];
  actionVersions: Record<string, unknown>[] = [];
  actionContract: Record<string, unknown> | null = null;
  actionInputMode = "expression";
  actionInitialInput: Record<string, unknown> = {};
  actionNextCursor: string | null = null;
  actionLoading = false;
  workflowProperties = structuredClone(this.model.definition);
  workflowBuffer = JSON.stringify(this.model.definition);
  workflowValid = true;
  propertyStep: Step | null = null;
  propertyValid = true;
  advancedProperties = false;
  zoom = 1;
  pan = { x: 0, y: 0 };
  dragPreview: { kind: Kind; point: { x: number; y: number } } | null = null;
  dragNode: {
    id: string;
    start: { x: number; y: number };
    point: { x: number; y: number };
    pointer: number;
  } | null = null;
  connectingNode = "";
  reply = "";
  decisionData = "{}";
  decision = "approve";
  commandKey: string | null = null;
  unknownCommand = false;
  taskFilter = "";
  runFilters = { business_key: "", correlation_key: "", status: "" };
  showStartRun = false;
  runStart = {
    activationId: "",
    input: "{}",
    businessKey: "",
    correlationKey: "",
  };
  availableActivations: Record<string, unknown>[] = [];
  runStatuses = [
    "queued",
    "running",
    "waiting",
    "suspended",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
  ];
  private listSequence = 0;
  taskConflict = false;
  private cachedTick = -1;
  private cachedNodes: Node[] = [];
  private cachedBoundaries = this.model.boundaries();
  private nodeById = new Map<string, Node>();
  private visualLinks: { from: string; to: string }[] = [];
  constructor() {
    void this.initialize();
  }
  async initialize() {
    try {
      const s = await this.api.pair();
      this.paired = s.paired;
      if (s.paired) {
        await this.loadIdentity();
        const initial = location.pathname.split("/")[1] as View;
        if (this.nav.some((n) => n.id === initial)) this.view = initial;
        await this.refresh();
      }
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
      this.connecting = false;
    }
  }
  async pair() {
    this.busy = "pair";
    try {
      await this.api.pair(this.pairCode);
      this.paired = true;
      this.pairCode = "";
      this.error = "";
      await this.loadIdentity();
      await this.refresh();
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  get profile() {
    return this.api.session.profile;
  }
  get nodes() {
    const revision = this.tick();
    if (revision !== this.cachedTick) {
      this.cachedTick = revision;
      this.cachedNodes = this.model.nodes();
      this.visualLinks = this.model.visualConnections();
      this.cachedBoundaries = this.model.boundaries();
      this.nodeById = new Map(this.cachedNodes.map((n) => [n.step.id, n]));
    }
    return this.cachedNodes;
  }
  get targets() {
    this.tick();
    return this.model.targets();
  }
  get selected() {
    return this.nodes.find((n) => n.step.id === this.model.selected);
  }
  get palette() {
    return this.kinds.filter((k) =>
      this.label(k).toLowerCase().includes(this.paletteQuery.toLowerCase()),
    );
  }
  get visibleRecords() {
    return this.records.filter((r) =>
      JSON.stringify(r).toLowerCase().includes(this.search.toLowerCase()),
    );
  }
  label(kind: string) {
    return (
      (
        {
          action: "Call an integration",
          transform: "Transform",
          switch: "Decision",
          parallel: "Parallel",
          wait: "Wait for time",
          signal: "Wait for signal",
          humanTask: "Human task",
          fail: "Fail",
        } as Record<string, string>
      )[kind] ?? kind
    );
  }
  description(kind: string) {
    return (
      (
        {
          action: "Connector or worker action",
          transform: "Shape and map data",
          switch: "Choose a named outcome",
          parallel: "Run independent branches",
          wait: "Continue after a duration",
          signal: "Wait for an external message",
          humanTask: "Ask a person to decide",
          fail: "Stop with a clear reason",
        } as Record<string, string>
      )[kind] ?? ""
    );
  }
  value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  changed() {
    this.tick.update((n) => n + 1);
    this.workflowProperties = structuredClone(this.model.definition);
    this.workflowBuffer = JSON.stringify(this.model.definition);
    this.sourceBuffer = this.model.source;
    this.saveState = "Unsaved";
    this.diagnostics = null;
    if (
      this.model.selected &&
      window.innerWidth >= 768 &&
      this.tab === "Designer"
    )
      this.showInspector = true;
    this.propertyStep = this.selected
      ? structuredClone(this.selected.step)
      : null;
    this.inspectorBuffer = this.selected
      ? JSON.stringify(this.selected.step, null, 2)
      : "";
    if (
      this.selected?.step.kind === "action" &&
      !this.actionContract &&
      !this.actionLoading
    )
      void this.loadActionCatalog();
  }
  perform(fn: () => void) {
    try {
      fn();
      this.changed();
      this.error = "";
    } catch (e) {
      this.fail(e);
    }
  }
  fail(e: unknown) {
    this.error = e instanceof Error ? e.message : String(e);
    this.message = "";
  }
  async navigate(view: View) {
    if (
      this.view === "designer" &&
      view !== "designer" &&
      this.saveState === "Unsaved" &&
      !confirm("Leave the designer? Your edits remain in this session.")
    )
      return;
    this.view = view;
    await this.router.navigateByUrl(
      view === "designer" ? `/workflows/${this.draftId}/designer` : `/${view}`,
    );
    this.selectedRecord = null;
    this.search = "";
    this.error = "";
    if (view !== "designer") await this.refresh();
    else this.scheduleFit();
  }
  newWorkflow() {
    this.importGeneration++;
    this.model.replace({
      apiVersion: "weave/v1alpha1",
      kind: "Workflow",
      metadata: { name: "untitled-workflow", version: "1.0.0" },
      spec: {
        inputSchema: { type: "object" },
        outputSchema: { type: "object" },
        steps: [],
        output: { literal: {} },
      },
    });
    this.draftId = crypto.randomUUID();
    this.draftRevision = undefined;
    this.view = "designer";
    void this.router.navigateByUrl(`/workflows/${this.draftId}/designer`);
    this.tab = "Designer";
    this.published = null;
    this.activation = null;
    this.changed();
    this.scheduleFit();
  }
  select(node: Node) {
    this.propertyValid = true;
    this.advancedProperties = false;
    this.model.selected = node.step.id;
    this.propertyStep = structuredClone(node.step);
    this.actionContract = null;
    this.actionInputMode = "expression";
    if (node.step.kind === "action") void this.loadActionCatalog();
    this.inspectorBuffer = JSON.stringify(node.step, null, 2);
    this.showInspector = true;
    this.message = `Selected ${node.step.id}, ${this.label(node.step.kind)}, ${node.owner}`;
  }
  insert(kind: Kind) {
    const n = this.selected;
    this.perform(() =>
      this.model.insert(kind, n?.owner ?? "root", n ? n.index + 1 : undefined),
    );
  }
  remove() {
    if (this.model.selected)
      this.perform(() => this.model.remove(this.model.selected));
  }
  undo() {
    this.model.undo();
    this.changed();
  }
  redo() {
    this.model.redo();
    this.changed();
  }
  selectTab(tab: string) {
    this.tab = tab;
    if (tab !== "Designer" && window.innerWidth <= 1280)
      this.showInspector = false;
  }
  paneResize(event: PointerEvent, pane: "palette" | "inspector") {
    this.resizing = {
      pane,
      x: event.clientX,
      initial: pane === "palette" ? this.paletteWidth : this.inspectorWidth,
    };
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  }
  paneMove(event: PointerEvent) {
    const r = this.resizing;
    if (!r) return;
    const delta = (event.clientX - r.x) * (r.pane === "palette" ? 1 : -1);
    this.setPane(r.pane, r.initial + delta);
  }
  setPane(pane: "palette" | "inspector", width: number) {
    const available =
      this.windowWidth - (this.collapsed ? 64 : 224) - 48 - 32 - 480;
    const maximum =
      pane === "palette"
        ? Math.min(256, available - this.inspectorWidth)
        : Math.min(420, available - this.paletteWidth);
    if (pane === "palette")
      this.paletteWidth = Math.max(176, Math.min(maximum, width));
    else this.inspectorWidth = Math.max(280, Math.min(maximum, width));
  }
  paneKey(event: KeyboardEvent, pane: "palette" | "inspector") {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      const width =
        pane === "palette" ? this.paletteWidth : this.inspectorWidth;
      this.setPane(
        pane,
        width +
          (event.key === "ArrowRight" ? 16 : -16) *
            (pane === "palette" ? 1 : -1),
      );
    }
    if (event.key === "Home") {
      if (pane === "palette") this.paletteWidth = 208;
      else this.inspectorWidth = 320;
    }
  }
  @HostListener("window:resize") resize() {
    this.windowWidth = window.innerWidth;
  }
  updateSource() {
    this.perform(() =>
      this.model.setSource(this.sourceBuffer, this.model.format),
    );
  }
  changeFormat(event: Event) {
    const format = this.value(event) as "yaml" | "json";
    if (this.model.error) return;
    if (
      this.model.hasComments &&
      !confirm("JSON cannot preserve YAML comments. Normalize this source?")
    )
      return;
    this.model.format = format;
    this.sourceBuffer =
      format === "json"
        ? JSON.stringify(this.model.definition, null, 2)
        : stringify(this.model.definition);
    this.updateSource();
  }
  branchNames() {
    return this.selected?.step.kind === "parallel"
      ? Object.keys(this.selected.step["branches"] as Record<string, unknown>)
      : [];
  }
  caseIndexes() {
    return this.selected?.step.kind === "switch"
      ? (this.selected.step["cases"] as unknown[]).map((_, i) => i)
      : [];
  }
  manageBranch(
    operation: "add" | "remove" | "rename" | "up" | "down",
    name?: string,
    event?: Event,
  ) {
    if (!this.propertyValid || !this.selected) return;
    const id = this.selected.step.id;
    this.perform(() => {
      if (this.inspectorBuffer !== JSON.stringify(this.selected!.step, null, 2))
        this.model.update(id, this.inspectorBuffer);
      this.model.editBranches(
        id,
        operation,
        name,
        event ? this.value(event) : undefined,
      );
    });
  }
  updateWorkflowOptions() {
    if (!this.workflowValid) return;
    this.perform(() =>
      this.model.updateWorkflow(JSON.parse(this.workflowBuffer) as Workflow),
    );
  }
  switchPropertyMode() {
    if (this.advancedProperties) {
      try {
        const step = JSON.parse(this.inspectorBuffer) as Step;
        if (
          step.id !== this.selected?.step.id ||
          step.kind !== this.selected?.step.kind
        )
          throw Error("Step identity and kind cannot change in the inspector.");
        this.propertyStep = step;
      } catch (e) {
        this.fail(e);
        return;
      }
    }
    this.advancedProperties = !this.advancedProperties;
    this.propertyValid = true;
  }
  get canManageMembers() {
    if (!this.profile?.tenantId) return false;
    if (this.can("grant.admin")) return true;
    return (
      !!this.profile &&
      !!this.identity?.grants.some(
        (g) =>
          g.capabilities.includes("grant.manage") &&
          g.scope?.tenant_id === this.profile!.tenantId &&
          !g.scope.project_id &&
          !g.scope.environment_id &&
          !g.resources.length,
      )
    );
  }
  get memberPath() {
    return `/studio/api/api/v1/tenants/${encodeURIComponent(this.profile!.tenantId!)}/members`;
  }
  async loadAdministration(kind?: "principals" | "members") {
    if (!this.profile) return;
    this.adminLoading = true;
    const generation = ++this.adminGeneration,
      profile = this.profile,
      identity = this.identity;
    const current = () =>
      generation === this.adminGeneration &&
      profile === this.profile &&
      identity === this.identity;
    try {
      const jobs = [];
      if (this.can("grant.admin") && (!kind || kind === "principals"))
        jobs.push(
          this.api
            .request<{
              items: Record<string, unknown>[];
              next_cursor: string | null;
            }>(
              `/studio/api/api/v1/admin/principals?limit=50${kind && this.adminPrincipalCursor ? "&cursor=" + encodeURIComponent(this.adminPrincipalCursor) : ""}`,
            )
            .then((page) => {
              if (!current()) return;
              this.adminPrincipals = kind
                ? [...this.adminPrincipals, ...page.items]
                : page.items;
              this.adminPrincipalCursor = page.next_cursor;
            }),
        );
      if (this.canManageMembers && (!kind || kind === "members"))
        jobs.push(
          this.api
            .request<{
              items: Record<string, unknown>[];
              next_cursor: string | null;
            }>(
              `${this.memberPath}?limit=50${kind && this.adminMemberCursor ? "&cursor=" + encodeURIComponent(this.adminMemberCursor) : ""}`,
            )
            .then((page) => {
              if (!current()) return;
              this.adminMembers = kind
                ? [...this.adminMembers, ...page.items]
                : page.items;
              this.adminMemberCursor = page.next_cursor;
            }),
        );
      const results = await Promise.allSettled(jobs);
      if (!current()) return;
      for (const result of results)
        if (result.status === "rejected") this.fail(result.reason);
    } finally {
      if (current()) this.adminLoading = false;
      this.cdr.markForCheck();
    }
  }
  async administrationCommand(
    command: "create" | "grant" | "link" | "status" | "revoke",
    record?: Record<string, unknown>,
  ) {
    if (this.busy || (command === "create" && this.adminUncertainCreate))
      return;
    const form = document.querySelector<HTMLFormElement>(
      command === "create"
        ? ".principal-create-form"
        : command === "grant"
          ? ".member-grant-form"
          : ".principal-link-form",
    );
    if (
      ["create", "grant", "link"].includes(command) &&
      form &&
      !form.reportValidity()
    )
      return;
    if (
      command === "status" &&
      !confirm(
        `${record!["active"] ? "Deactivate" : "Activate"} principal ${record!["id"]}?`,
      )
    )
      return;
    if (
      command === "revoke" &&
      !confirm(
        `Revoke ${record!["role"]} binding ${record!["id"]} for principal ${record!["principal_id"]} in scope ${JSON.stringify(record!["scope"])}?`,
      )
    )
      return;
    this.busy = "administration";
    try {
      let path = "",
        body: unknown;
      if (command === "create") {
        path = "/studio/api/api/v1/admin/principals";
        body = { kind: this.principalKind };
      }
      if (command === "grant") {
        path = this.memberPath;
        body = {
          principal_id: this.principalId,
          role: this.memberRole,
          ...(this.memberScope !== "tenant"
            ? { project_id: this.profile!.projectId }
            : {}),
          ...(this.memberScope === "environment"
            ? { environment_id: this.profile!.environmentId }
            : {}),
          resources: this.memberResources
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
        };
      }
      if (command === "link") {
        path = `/studio/api/api/v1/admin/principals/${encodeURIComponent(this.principalId)}/identity-links`;
        body = {
          provider_id: this.identityProvider,
          issuer: this.identityIssuer,
          subject: this.identitySubject,
        };
      }
      if (command === "status") {
        path = `/studio/api/api/v1/admin/principals/${record!["id"]}/status`;
        body = { active: !record!["active"] };
      }
      if (command === "revoke")
        path = `${this.memberPath}/${record!["id"]}/revoke`;
      const result = await this.api.request<Record<string, unknown>>(
        path,
        "POST",
        body,
      );
      if (command === "create") this.principalId = String(result["id"]);
      this.message =
        command === "create"
          ? `Created principal ${result["id"]}. Link its CIAM identity separately.`
          : "Access change accepted by the API.";
      await this.loadIdentity();
      await this.loadAdministration();
    } catch (e) {
      if (
        command === "create" &&
        (!(e instanceof ApiError) || e.status >= 500 || e.status === 408)
      ) {
        this.adminUncertainCreate = true;
        this.error =
          "Principal creation outcome is unknown. Refresh the directory and reconcile the created UUID before issuing another create command.";
      } else this.fail(e);
    } finally {
      this.busy = "";
      this.cdr.markForCheck();
    }
  }
  stepEdit(value: unknown) {
    const step = value as Step;
    if (step.kind === "action" && this.actionInputMode === "fields")
      step["with"] = (JSON.parse(this.inspectorBuffer) as Step)["with"];
    this.inspectorBuffer = JSON.stringify(step, null, 2);
  }
  async loadActionCatalog(append = false) {
    if (!this.can("catalog.read")) return;
    this.actionLoading = true;
    const scope = this.profile,
      nodeId = this.selected?.step.id;
    try {
      const page = await this.api.page(
        "actions",
        false,
        append ? (this.actionNextCursor ?? undefined) : undefined,
      );
      if (scope !== this.profile || nodeId !== this.selected?.step.id) return;
      this.actionVersions = append
        ? [
            ...this.actionVersions,
            ...page.items.filter((item) => !item["retired"]),
          ]
        : page.items.filter((item) => !item["retired"]);
      this.actionNextCursor = page.next_cursor;
      const action = this.actionVersions.find(
        (item) =>
          `${item["name"]}@${item["version"]}` === this.selected?.step["uses"],
      );
      if (action) await this.chooseCatalogAction(String(action["id"]), false);
    } catch (e) {
      this.fail(e);
    } finally {
      this.actionLoading = false;
      this.cdr.markForCheck();
    }
  }
  actionSpec() {
    return (this.actionContract?.["spec"] ?? {}) as Record<string, unknown>;
  }
  actionImplementation() {
    return (this.actionSpec()["implementation"] ?? {}) as Record<
      string,
      unknown
    >;
  }
  actionInputSchema() {
    return (this.actionSpec()["inputSchema"] ?? {}) as Schema;
  }
  actionInputFields() {
    return (
      this.actionInputSchema().type === "object" &&
      Object.keys(this.actionInputSchema().properties ?? {}).length > 0
    );
  }
  async chooseCatalogAction(id: string, replace = true) {
    if (!id || !this.selected || this.selected.step.kind !== "action") return;
    const nodeId = this.selected.step.id,
      scope = this.profile;
    try {
      const result = await this.api.request<Record<string, unknown>>(
        `${this.api.project}/actions/${id}/export`,
      );
      if (this.selected?.step.id !== nodeId || scope !== this.profile) return;
      this.actionContract = result["document"] as Record<string, unknown>;
      const step = JSON.parse(this.inspectorBuffer) as Step;
      if (replace) step["uses"] = `${result["name"]}@${result["version"]}`;
      const literal = (step["with"] as { literal?: unknown })?.literal;
      this.actionInitialInput =
        literal && typeof literal === "object" && !Array.isArray(literal)
          ? structuredClone(literal as Record<string, unknown>)
          : {};
      for (const [name, schema] of Object.entries(
        this.actionInputSchema().properties ?? {},
      ))
        if (
          schema.type === "boolean" &&
          this.actionInitialInput[name] === undefined
        )
          this.actionInitialInput[name] = false;
      this.actionInputMode =
        this.actionInputFields() && literal && typeof literal === "object"
          ? "fields"
          : "expression";
      this.propertyStep = step;
      this.inspectorBuffer = JSON.stringify(step, null, 2);
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
    }
  }
  actionInputChange(data: Record<string, unknown>) {
    const step = JSON.parse(this.inspectorBuffer) as Step;
    step["with"] = { literal: data };
    this.inspectorBuffer = JSON.stringify(step, null, 2);
  }
  actionInputModeChange(event: Event) {
    this.actionInputMode = this.value(event);
    const step = JSON.parse(this.inspectorBuffer) as Step;
    if (this.actionInputMode === "fields")
      step["with"] = { literal: this.actionInitialInput };
    this.propertyStep = step;
    this.inspectorBuffer = JSON.stringify(step, null, 2);
  }
  useActionOutput() {
    if (
      !this.selected ||
      !this.actionContract ||
      this.selected.owner !== "root"
    )
      return;
    const doc = structuredClone(this.model.definition);
    doc.spec.output = { ref: `/steps/${this.selected.step.id}/output` };
    doc.spec["outputSchema"] = structuredClone(
      this.actionSpec()["outputSchema"] ?? {},
    );
    this.perform(() => {
      this.model.update(this.selected!.step.id, this.inspectorBuffer);
      this.model.updateWorkflow(doc);
    });
  }
  serializeStep(step: unknown) {
    return JSON.stringify(step, null, 2);
  }
  updateStep() {
    const form = document.querySelector<HTMLFormElement>(".action-input-form");
    if (this.actionInputMode === "fields" && form && !form.reportValidity())
      return;
    this.perform(() =>
      this.model.update(this.model.selected, this.inspectorBuffer),
    );
  }
  renameWorkflow(event: Event) {
    this.perform(() => this.model.renameWorkflow(this.value(event)));
  }
  dragPalette(event: DragEvent, kind: Kind) {
    event.dataTransfer?.setData("application/weave-step", kind);
    if (event.dataTransfer) event.dataTransfer.effectAllowed = "copy";
    this.dragPreview = { kind, point: { x: 0, y: 0 } };
  }
  allowDrop(event: DragEvent) {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  }
  drop(event: DragEvent, target?: Target) {
    event.preventDefault();
    event.stopPropagation();
    const kind = event.dataTransfer?.getData("application/weave-step") as Kind;
    if (!this.kinds.includes(kind)) return;
    this.perform(() => {
      if (target) this.model.insert(kind, target.owner, target.index);
      else this.model.addUnplaced(kind);
    });
    this.dragPreview = null;
    this.message = target
      ? `Inserted ${this.label(kind)} ${target.label.toLowerCase()}`
      : "Unplaced step created. Place it in the sequence before exporting.";
  }
  pointerDown(event: PointerEvent, node: Node) {
    if (event.button !== 0 || this.model.readonly) return;
    this.select(node);
    this.dragNode = {
      id: node.step.id,
      start: { x: event.clientX, y: event.clientY },
      point: node.point,
      pointer: event.pointerId,
    };
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  }
  pointerMove(event: PointerEvent) {
    if (!this.dragNode) return;
    const dx = (event.clientX - this.dragNode.start.x) / this.zoom,
      dy = (event.clientY - this.dragNode.start.y) / this.zoom;
    this.dragNode = {
      ...this.dragNode,
      point: { x: this.dragNode.point.x + dx, y: this.dragNode.point.y + dy },
      start: { x: event.clientX, y: event.clientY },
    };
  }
  pointerUp(event: PointerEvent) {
    if (!this.dragNode) return;
    const drag = this.dragNode;
    this.dragNode = null;
    this.perform(() => this.model.position(drag.id, drag.point, !event.altKey));
    this.message = "Node position updated. Workflow semantics are unchanged.";
  }
  nodePoint(node: Node) {
    return this.dragNode?.id === node.step.id
      ? this.dragNode.point
      : node.point;
  }
  cancelGesture() {
    this.dragNode = null;
    this.dragPreview = null;
    this.connectingNode = "";
  }
  connect(id: string) {
    this.connectingNode = id;
    this.message =
      "Choose an insertion target to move this step in the structured sequence.";
  }
  connectTo(target: Target) {
    if (!this.connectingNode) return;
    this.perform(() =>
      this.model.move(this.connectingNode, target.owner, target.index),
    );
    this.connectingNode = "";
  }
  moveSelected() {
    const split = this.selectedTarget.lastIndexOf(":");
    const owner = this.selectedTarget.slice(0, split),
      index = Number(this.selectedTarget.slice(split + 1));
    this.perform(() => {
      if (this.model.unplaced.some((s) => s.id === this.model.selected))
        this.model.place(this.model.selected, owner, index);
      else this.model.move(this.model.selected, owner, index);
    });
  }
  setZoom(delta: number) {
    this.zoom = Math.max(0.25, Math.min(2, this.zoom + delta));
  }
  needsFit = false;
  scheduleFit() {
    this.needsFit = true;
    this.cdr.detectChanges();
  }
  fitAfterRender() {
    if (!this.needsFit) return;
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (this.view === "designer" && this.tab === "Designer") {
          this.fit();
          this.needsFit = false;
          this.cdr.markForCheck();
        }
      }),
    );
  }
  fit() {
    const element = document.querySelector(".canvas");
    if (!element) {
      this.zoom = 1;
      this.pan = { x: 0, y: 0 };
      return;
    }
    const bounds = this.model.boundaries();
    const points = [
      ...this.nodes.map((n) => n.point),
      bounds.start,
      bounds.end,
    ];
    const minX = Math.min(...points.map((p) => p.x)),
      minY = Math.min(...points.map((p) => p.y));
    const width = Math.max(...points.map((p) => p.x)) + 208 - minX,
      height = Math.max(...points.map((p) => p.y)) + 64 - minY;
    this.zoom = Math.max(
      0.1,
      Math.min(
        1,
        (element.clientWidth - 96) / width,
        (element.clientHeight - 128) / height,
      ),
    );
    this.pan = {
      x: (element.clientWidth - width * this.zoom) / 2 - minX * this.zoom,
      y: (element.clientHeight - height * this.zoom) / 2 - minY * this.zoom,
    };
  }
  panCanvas(event: WheelEvent) {
    event.preventDefault();
    if (event.ctrlKey || event.metaKey)
      this.setZoom(event.deltaY < 0 ? 0.1 : -0.1);
    else
      this.pan = { x: this.pan.x - event.deltaX, y: this.pan.y - event.deltaY };
  }
  graphHeight() {
    return Math.max(1000, ...this.nodes.map((n) => n.point.y)) + 240;
  }
  graphWidth() {
    return Math.max(1000, ...this.nodes.map((n) => n.point.x)) + 320;
  }
  connectionPath(
    model: StructuredCanvasAdapter,
    from: string,
    to: string,
    nodes: Node[],
  ) {
    const boundaries =
      model === this.model ? this.cachedBoundaries : model.boundaries();
    const point = (id: string) =>
      id === "$start"
        ? boundaries.start
        : id === "$end"
          ? boundaries.end
          : model === this.model
            ? this.nodePoint(this.nodeById.get(id)!)
            : nodes.find((n) => n.step.id === id)!.point;
    const a = point(from),
      b = point(to);
    const y = a.y + (from === "$start" ? 48 : 64);
    return `M ${a.x + 104} ${y} L ${a.x + 104} ${y + 24} L ${b.x + 104} ${y + 24} L ${b.x + 104} ${b.y}`;
  }
  edge(node: Node) {
    return this.visualLinks
      .filter((link) => link.from === node.step.id)
      .map((link) =>
        this.connectionPath(this.model, link.from, link.to, this.nodes),
      )
      .join(" ");
  }
  boundaryStartEdge() {
    const nodes = this.nodes;
    const link = this.visualLinks.find((link) => link.from === "$start")!;
    return this.connectionPath(this.model, link.from, link.to, nodes);
  }
  runEdges() {
    if (!this.runCanvas) return [];
    const nodes = this.runNodes();
    return this.runCanvas
      .visualConnections()
      .map((link) =>
        this.connectionPath(this.runCanvas!, link.from, link.to, nodes),
      );
  }
  async validate() {
    if (this.model.unplaced.length) {
      this.error = "Place or remove all unplaced steps before validation.";
      return;
    }
    const source = this.model.source;
    this.busy = "validate";
    try {
      const result = await this.api.validate(source, this.model.format);
      if (source !== this.model.source) return;
      this.validationSource = source;
      this.diagnostics = result;
      this.message = result.validationOk
        ? result.partial
          ? "Local validation passed. Catalog checks remain pending."
          : "Validation passed."
        : "Validation found errors.";
      this.error = "";
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  async refreshHome() {
    this.homeTasks = [];
    this.homeRuns = [];
    const sequence = ++this.listSequence;
    const scope = this.profile;
    if (!scope || !this.paired) return;
    this.loading = true;
    const current = () =>
      sequence === this.listSequence &&
      this.view === "home" &&
      scope === this.profile;
    const jobs: Promise<unknown>[] = [];
    const taskReadable =
      this.can("human_task.read") ||
      this.identity?.grants.some((g) =>
        g.resources.some((id) => this.can("human_task.read", id)),
      );
    if (taskReadable)
      jobs.push(
        Promise.all([
          this.api.page("human-tasks", true, undefined, { status: "ready" }),
          this.api.page("human-tasks", true, undefined, { status: "claimed" }),
        ]).then((pages) => {
          if (current())
            this.homeTasks = pages.flatMap((p) => p.items).slice(0, 5);
        }),
      );
    if (this.can("run.read"))
      jobs.push(
        this.api.page("runs", true).then((page) => {
          if (current()) this.homeRuns = page.items.slice(0, 5);
        }),
      );
    const results = await Promise.allSettled(jobs);
    if (!current()) return;
    for (const result of results)
      if (result.status === "rejected") this.fail(result.reason);
    this.loading = false;
    this.cdr.markForCheck();
  }
  async refresh(append = false) {
    if (this.view === "settings") {
      await this.loadConnectionStatus();
      await this.loadAdministration();
      return;
    }
    if (this.view === "home") {
      await this.refreshHome();
      return;
    }
    if (!this.profile || !this.paired) {
      this.records = [];
      return;
    }
    this.loading = true;
    const sequence = ++this.listSequence;
    const requestedView = this.view;
    const collections: Partial<Record<View, [string, boolean]>> = {
      workflows: [this.libraryCollection, false],
      runs: ["runs", true],
      connections: ["connections", true],
      workers: ["workers", true],
      tasks: ["human-tasks", true],
      email: ["email/conversations", true],
    };
    const c = collections[this.view];
    if (!c) {
      this.loading = false;
      return;
    }
    try {
      const result = await this.api.page(
        c[0],
        c[1],
        append ? (this.nextCursor ?? undefined) : undefined,
        this.view === "runs"
          ? {
              ...this.runFilters,
              include_archived: this.runIncludeArchived ? "true" : "",
            }
          : this.view === "tasks" && this.taskFilter
            ? { status: this.taskFilter }
            : {},
      );
      if (sequence !== this.listSequence || requestedView !== this.view) return;
      this.records = append ? [...this.records, ...result.items] : result.items;
      this.nextCursor = result.next_cursor;
      this.error = "";
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
      this.loading = false;
    }
  }
  async open(record: Record<string, unknown>) {
    this.emailDetail = null;
    this.emailSubmission = null;
    this.runHistory = null;
    this.runCanvas = null;
    this.runLifecycle = null;
    this.taskData = {};
    this.selectedRecord = record;
    if (this.view === "workflows") {
      this.busy = "load";
      try {
        const detail = await this.api.request<Record<string, unknown>>(
          `${this.api.project}/${this.libraryCollection}/${record["id"]}${this.libraryCollection === "drafts" ? "" : "/export"}`,
        );
        const d = detail["definition"] ?? detail["document"];
        if (d && typeof d === "object") {
          this.model.replace(d as Workflow);
          this.changed();
          this.view = "designer";
          if (this.libraryCollection === "drafts") {
            this.draftId = String(detail["id"]);
            this.draftRevision = Number(detail["revision"]);
            this.published = null;
            this.saveState = "Saved to API";
          } else {
            this.published = record;
            this.saveState = "Published version";
          }
          void this.router.navigateByUrl(`/workflows/${this.draftId}/designer`);
        } else if (typeof detail["source"] === "string") {
          this.model.setSource(
            detail["source"],
            detail["format"] === "json" ? "json" : "yaml",
          );
          this.changed();
          this.view = "designer";
          this.published = record;
        } else
          throw Error(
            "This export cannot be opened. Its source remains available through the API.",
          );
      } catch (e) {
        this.fail(e);
      } finally {
        this.cdr.markForCheck();
        this.busy = "";
        this.scheduleFit();
      }
    } else await this.readDetail();
  }
  async readDetail() {
    if (!this.selectedRecord) return;
    const collection =
      this.view === "tasks"
        ? "human-tasks"
        : this.view === "email"
          ? "email/conversations"
          : "runs";
    try {
      const id = this.selectedRecord["id"];
      const detail = await this.api.request<Record<string, unknown>>(
        `${this.api.environment}/${collection}/${id}`,
      );
      if (this.view === "email") {
        this.emailDetail = detail;
        this.selectedRecord = detail["conversation"] as Record<string, unknown>;
      } else {
        this.selectedRecord = detail;
        this.taskConflict = false;
      }
      if (this.view === "runs") {
        this.runLifecycle = await this.api.request<Record<string, unknown>>(
          `${this.api.environment}/runs/${id}/lifecycle`,
        );
        this.runHistory = await this.api.request<Record<string, unknown>>(
          `${this.api.environment}/runs/${id}/history?limit=100`,
        );
        const activation = detail["activation"] as
          | { request?: { version_id?: string } }
          | undefined;
        if (activation?.request?.version_id) {
          const exported = await this.api.request<{ document: Workflow }>(
            `${this.api.project}/workflows/${activation.request.version_id}/export`,
          );
          const canvas = new StructuredCanvasAdapter();
          canvas.setSource(JSON.stringify(exported.document), "json");
          canvas.readonly = true;
          this.runCanvas = canvas;
        }
      }
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
    }
  }
  async save() {
    if (this.model.readonly || this.model.unplaced.length) {
      this.error = "Resolve source errors and unplaced steps before saving.";
      return;
    }
    this.busy = "save";
    this.saveState = "Saving";
    const source = this.model.source;
    try {
      const result = await this.safeMutation<{
        id: string;
        revision: number;
        document: Workflow;
      }>(
        `${this.api.project}/drafts/${this.draftId}`,
        "PUT",
        { document: this.model.definition },
        this.draftRevision,
        "Save draft",
      );
      this.draftRevision = result.revision;
      this.saveState =
        source === this.model.source ? "Saved to API" : "Unsaved";
      this.message = `Draft saved at revision ${result.revision}`;
      this.error = "";
    } catch (e) {
      this.saveState = "Save failed";
      if (e instanceof ApiError && e.status === 409) {
        this.conflict = { local: this.model.definition, server: e.detail };
        this.error = "Draft revision conflict. Your local buffer is retained.";
      } else this.fail(e);
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  async publish() {
    if (this.model.readonly || this.model.unplaced.length) return;
    await this.validate();
    if (
      !this.diagnostics?.validationOk ||
      this.validationSource !== this.model.source
    )
      return;
    if (
      !confirm(
        `Publish ${this.model.definition.metadata.name} ${this.model.definition.metadata.version}? This creates an immutable version.`,
      )
    )
      return;
    this.busy = "publish";
    try {
      this.published = await this.safeMutation<Record<string, unknown>>(
        `${this.api.project}/workflows`,
        "POST",
        { source: this.model.source, format: this.model.format },
        undefined,
        "Publish workflow",
      );
      this.message =
        "Workflow version published. Activate it separately for an environment.";
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  async activate() {
    if (!this.published || !this.profile) return;
    const raw = prompt(
      "Connection and worker release bindings (JSON). Use only authorized release IDs.",
      '{"connection_revision_ids":{},"worker_release_ids":{},"assignment_binding_ids":{}}',
    );
    if (raw === null) return;
    try {
      const bindings = JSON.parse(raw);
      const scope = {
        tenant_id: this.profile.tenantId,
        project_id: this.profile.projectId,
        environment_id: this.profile.environmentId,
      };
      if (
        !confirm(
          `Activate ${this.published["name"]} in ${this.profile.environmentId}? Review this environment before continuing.`,
        )
      )
        return;
      this.busy = "activate";
      this.activation = await this.safeMutation<Record<string, unknown>>(
        `${this.api.environment}/activations`,
        "POST",
        {
          ...bindings,
          version_id: this.published["id"],
          artifact_digest: this.published["digest"],
          scope,
        },
      );
      this.message = "Version activated in the selected environment.";
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  async startRun() {
    if (!this.profile) return;
    this.runStart = {
      activationId: String(this.activation?.["id"] ?? ""),
      input: "{}",
      businessKey: "",
      correlationKey: "",
    };
    this.showStartRun = true;
    try {
      const result = await this.api.page("activations", true);
      this.availableActivations = result.items.filter((a) => !a["retired"]);
      if (
        this.activation &&
        !this.availableActivations.some(
          (a) => a["id"] === this.activation!["id"],
        )
      )
        this.availableActivations.unshift(this.activation);
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
    }
  }
  async confirmStartRun() {
    if (!this.runStart.activationId) return;
    try {
      const body = {
        activation_id: this.runStart.activationId,
        input: JSON.parse(this.runStart.input),
        ...(this.runStart.businessKey
          ? { business_key: this.runStart.businessKey }
          : {}),
        ...(this.runStart.correlationKey
          ? { correlation_key: this.runStart.correlationKey }
          : {}),
      };
      const run = await this.safeMutation<Record<string, unknown>>(
        `${this.api.environment}/runs`,
        "POST",
        body,
        undefined,
        "Start run",
      );
      this.showStartRun = false;
      this.message = `Run ${run["id"]} started`;
      await this.navigate("runs");
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
    }
  }
  applyRunFilters() {
    this.nextCursor = null;
    this.records = [];
    this.selectedRecord = null;
    void this.refresh();
  }
  clearRunFilters() {
    this.runIncludeArchived = false;
    this.runFilters = { business_key: "", correlation_key: "", status: "" };
    this.applyRunFilters();
  }
  applyTaskFilter(event: Event) {
    this.taskFilter = this.value(event);
    this.nextCursor = null;
    this.records = [];
    this.selectedRecord = null;
    void this.refresh();
  }
  async simulate() {
    this.busy = "simulate";
    try {
      const result = await this.api.request<Validation>(
        `${this.api.project}/compiler/compile`,
        "POST",
        { source: this.model.source, format: this.model.format },
      );
      if (!result.artifact) {
        this.diagnostics = result;
        throw Error(
          "Simulation needs a fully compiled artifact. Resolve catalog diagnostics first.",
        );
      }
      const raw = prompt(
        "Simulation input and action mocks (JSON)",
        '{"input":{},"mocks":{}}',
      );
      if (raw === null) return;
      this.debugSession = await this.api.request<Record<string, unknown>>(
        `${this.api.project}/debug/sessions`,
        "POST",
        {
          artifact: result.artifact,
          ...JSON.parse(raw),
          now: new Date().toISOString(),
        },
      );
      this.message =
        "Simulation session created. No real external effects are executed.";
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  async debugCommand(kind: string) {
    if (!this.debugSession) return;
    try {
      this.debugSession = await this.api.request<Record<string, unknown>>(
        `${this.api.project}/debug/sessions/${this.debugSession["id"]}/commands`,
        "POST",
        { kind },
        { "If-Match": `"${this.debugSession["revision"]}"` },
      );
    } catch (e) {
      this.fail(e);
    }
  }
  async export() {
    if (this.model.error || this.model.unplaced.length) {
      this.error = "Fix source and place every step before exporting.";
      return;
    }
    const filename = this.model.definition.metadata.name;
    this.download(
      `${filename}.${this.model.format}`,
      this.model.source,
      this.model.format === "json" ? "application/json" : "application/yaml",
    );
    this.download(
      `${filename}.layout.json`,
      JSON.stringify(
        {
          draftId: this.draftId,
          draftRevision: this.draftRevision,
          schemaVersion: 1,
          semanticDigest: await this.semanticDigest(),
          ...this.model.layout,
          viewport: { ...this.pan, zoom: this.zoom },
        },
        null,
        2,
      ),
      "application/json",
    );
  }
  async semanticDigest() {
    const canonical = (v: unknown): unknown =>
      Array.isArray(v)
        ? v.map(canonical)
        : v !== null && typeof v === "object"
          ? Object.fromEntries(
              Object.entries(v as Record<string, unknown>)
                .sort(([a], [b]) => a.localeCompare(b, "en"))
                .map(([k, val]) => [k, canonical(val)]),
            )
          : v;
    const digest = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(
        JSON.stringify(canonical(this.model.definition)),
      ),
    );
    return Array.from(new Uint8Array(digest), (n) =>
      n.toString(16).padStart(2, "0"),
    ).join("");
  }
  download(name: string, text: string, type: string) {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async import(event: Event) {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";
    if (file) await this.importWorkflow(file);
  }
  async importWorkflow(file: File) {
    if (file.size > 1024 * 1024) {
      this.error = "Workflow source is limited to 1 MiB.";
      return;
    }
    const generation = ++this.importGeneration;
    let source: string;
    try {
      source = await file.text();
    } catch {
      if (generation === this.importGeneration) {
        this.error =
          "The workflow file could not be read. Your current draft is preserved.";
        this.cdr.markForCheck();
      }
      return;
    }
    if (generation !== this.importGeneration) return;
    this.model = new StructuredCanvasAdapter();
    this.draftId = crypto.randomUUID();
    this.draftRevision = undefined;
    this.published = null;
    this.activation = null;
    this.model.setSource(
      source,
      file.name.toLowerCase().endsWith(".json") ? "json" : "yaml",
    );
    this.view = "designer";
    void this.router.navigateByUrl(`/workflows/${this.draftId}/designer`);
    this.changed();
    this.scheduleFit();
  }
  pretty(value: unknown) {
    return JSON.stringify(value, null, 2);
  }
  recordName(r: Record<string, unknown>) {
    if (this.view === "runs")
      return String(r["business_key"] ?? r["id"] ?? "Execution");
    return String(
      r["name"] ??
        (r["document"] as Workflow | undefined)?.metadata?.name ??
        r["title"] ??
        r["subject"] ??
        r["id"] ??
        "Unavailable",
    );
  }
  recordStatus(r: Record<string, unknown>) {
    return String(
      r["status"] ??
        (typeof r["state"] === "object"
          ? (r["state"] as Record<string, unknown>)["status"]
          : r["state"]) ??
        r["version"] ??
        (r["document"]
          ? "Draft"
          : this.view === "email"
            ? "Conversation"
            : "Unavailable"),
    );
  }

  async loadIdentity() {
    if (!this.profile) return;
    try {
      this.identity = await this.api.request<Identity>(
        "/studio/api/api/v1/identity",
      );
    } catch {
      this.identity = null;
    } finally {
      this.cdr.markForCheck();
    }
  }
  can(capability: string, resource?: string) {
    const p = this.profile;
    if (!p || !this.identity) return false;
    return this.identity.grants.some(
      (g) =>
        g.capabilities.includes(capability) &&
        (!g.scope ||
          (g.scope.tenant_id === p.tenantId &&
            (!g.scope.project_id || g.scope.project_id === p.projectId) &&
            (!g.scope.environment_id ||
              g.scope.environment_id === p.environmentId))) &&
        (!g.resources.length || (!!resource && g.resources.includes(resource))),
    );
  }
  get scopes() {
    return (
      this.identity?.workspaces.flatMap((t) =>
        t.projects.flatMap((p) =>
          p.environments.map((e) => ({
            value: [t.id, p.id, e.id].join("/"),
            label: `${t.name} / ${p.name} / ${e.name}`,
          })),
        ),
      ) ?? []
    );
  }
  async loadConnectionStatus() {
    const generation = this.loginGeneration;
    try {
      const status =
        await this.api.request<Record<string, unknown>>("/studio/connection");
      if (generation === this.loginGeneration) this.connectionStatus = status;
    } catch {
      if (generation === this.loginGeneration) this.connectionStatus = null;
    } finally {
      this.cdr.markForCheck();
    }
  }
  clearWorkspace() {
    this.importGeneration++;
    this.listSequence++;
    this.adminGeneration++;
    this.loginGeneration++;
    if (this.loginTimer) clearTimeout(this.loginTimer);
    this.loginTimer = null;
    this.homeTasks = [];
    this.homeRuns = [];
    this.adminPrincipals = [];
    this.adminMembers = [];
    this.adminMemberCursor = null;
    this.adminPrincipalCursor = null;
    this.principalId = "";
    this.memberResources = "";
    this.identityProvider = "";
    this.identityIssuer = "";
    this.identitySubject = "";
    this.actionVersions = [];
    this.actionContract = null;
    this.records = [];
    this.selectedRecord = null;
    this.emailDetail = null;
    this.emailSubmission = null;
    this.runHistory = null;
    this.runCanvas = null;
    this.runLifecycle = null;
    this.reply = "";
    this.taskData = {};
    this.identity = null;
    this.scopeChoice = "";
    this.published = null;
    this.activation = null;
    this.debugSession = null;
    this.model = new StructuredCanvasAdapter();
    this.tick.update((n) => n + 1);
    this.propertyStep = null;
    this.advancedProperties = false;
    this.propertyValid = true;
    this.workflowValid = true;
    this.diagnostics = null;
    this.validationSource = "";
    this.inspectorBuffer = "";
    this.needsFit = false;
    this.sourceBuffer = this.model.source;
    this.workflowProperties = structuredClone(this.model.definition);
    this.workflowBuffer = JSON.stringify(this.model.definition);
    this.draftId = crypto.randomUUID();
    this.draftRevision = undefined;
    this.saveState = "Unsaved";
  }
  connectionFail(error: unknown) {
    const detail =
      error instanceof ApiError &&
      typeof error.detail === "object" &&
      error.detail !== null
        ? (error.detail as Record<string, unknown>)
        : {};
    const code = String(detail["code"] ?? detail["error_code"] ?? "");
    const status = error instanceof ApiError ? error.status : 0;
    const explanation =
      code === "WV-AUTH-STORE"
        ? "The native credential store is unavailable. Unlock your operating system keyring and try again."
        : code === "WV-STUDIO-CONNECTION-CHANGED"
          ? "The connection changed during this request. Check the selected API connection again."
          : status === 401
            ? "Sign in again. If provider sign-in succeeds but the API still refuses access, ask an administrator to verify your identity link and active status."
            : status === 403
              ? "This identity does not have workspace access. Ask an administrator to grant the required scoped permissions."
              : status === 502 || status === 0
                ? "The API or identity provider could not be reached. Check the reviewed URLs, network connection and TLS configuration, then try again."
                : "The connection could not be completed. Review the login configuration and try again.";
    this.error = explanation + (code ? ` Support code: ${code}.` : "");
    this.cdr.markForCheck();
  }
  async configureConnection(configuration: ConnectionConfiguration) {
    if (this.pendingMutation || this.adminUncertainCreate) {
      this.error =
        "Reconcile the pending command or principal creation before changing connection.";
      return;
    }
    if (
      this.saveState === "Unsaved" &&
      !confirm(
        "Change API connection and clear this draft? Export your changes first.",
      )
    )
      return;
    ++this.loginGeneration;
    this.connectionBusy = true;
    try {
      this.api.session = await this.api.request(
        "/studio/connection/configure",
        "POST",
        configuration,
      );
      this.clearWorkspace();
      this.connectionRevision++;
      await this.loadConnectionStatus();
      this.message =
        "Connection reviewed. Sign in, check the API, then choose an authorized workspace.";
    } catch (e) {
      this.connectionFail(e);
    } finally {
      this.connectionBusy = false;
      this.cdr.markForCheck();
    }
  }
  async startConnectionLogin() {
    const generation = ++this.loginGeneration;
    this.connectionBusy = true;
    try {
      const login = await this.api.request<Record<string, unknown>>(
        "/studio/connection/login/start",
        "POST",
      );
      if (generation !== this.loginGeneration) return;
      this.connectionStatus = { ...this.connectionStatus, login };
      this.pollConnectionLogin(String(login["id"]), generation);
    } catch (e) {
      if (generation === this.loginGeneration) this.connectionFail(e);
    } finally {
      this.connectionBusy = false;
      this.cdr.markForCheck();
    }
  }
  pollConnectionLogin(id: string, generation: number) {
    if (this.loginTimer) clearTimeout(this.loginTimer);
    this.loginTimer = setTimeout(async () => {
      if (generation !== this.loginGeneration) return;
      try {
        const login = await this.api.request<Record<string, unknown>>(
          `/studio/connection/login/${encodeURIComponent(id)}`,
        );
        if (generation !== this.loginGeneration) return;
        this.connectionStatus = { ...this.connectionStatus, login };
        if (["starting", "awaiting_user"].includes(String(login["state"])))
          this.pollConnectionLogin(id, generation);
        else if (login["state"] === "authenticated")
          await this.checkConnection();
      } catch (e) {
        if (generation === this.loginGeneration) this.connectionFail(e);
      } finally {
        this.cdr.markForCheck();
      }
    }, 1500);
  }
  async cancelConnectionLogin(id: string) {
    this.loginGeneration++;
    if (this.loginTimer) clearTimeout(this.loginTimer);
    this.loginTimer = null;
    const generation = this.loginGeneration;
    this.connectionBusy = true;
    try {
      const login = await this.api.request<Record<string, unknown>>(
        `/studio/connection/login/${encodeURIComponent(id)}/cancel`,
        "POST",
      );
      if (generation !== this.loginGeneration) return;
      this.connectionStatus = { ...this.connectionStatus, login };
    } catch (e) {
      if (generation === this.loginGeneration) this.connectionFail(e);
    } finally {
      this.connectionBusy = false;
      this.cdr.markForCheck();
    }
  }
  async checkConnection() {
    const generation = this.loginGeneration;
    this.connectionBusy = true;
    try {
      const result = await this.api.request<{
        session: Session;
        identity: Identity;
      }>("/studio/connection/test", "POST");
      if (generation !== this.loginGeneration) return;
      this.api.session = result.session;
      this.identity = result.identity;
      await this.loadConnectionStatus();
      this.message = this.scopes.length
        ? "Signed in. Choose one of your authorized workspaces below."
        : "Authenticated, but no workspace is visible to this identity. Ask an administrator for scoped access.";
    } catch (e) {
      if (generation === this.loginGeneration) this.connectionFail(e);
    } finally {
      this.connectionBusy = false;
      this.cdr.markForCheck();
    }
  }
  async switchScope() {
    if (!this.scopeChoice) return;
    if (this.pendingMutation || this.adminUncertainCreate) {
      this.error = "Reconcile the pending command before changing workspace.";
      return;
    }
    if (
      this.saveState === "Unsaved" &&
      !confirm(
        "Change workspace and clear this authoring session? Export your edits before switching.",
      )
    )
      return;
    const [tenantId, projectId, environmentId] = this.scopeChoice.split("/");
    try {
      this.api.session = await this.api.request("/studio/scope", "POST", {
        tenantId,
        projectId,
        environmentId,
      });
      this.clearWorkspace();
      await this.loadIdentity();
      await this.refresh();
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
    }
  }
  taskSchema() {
    return (this.selectedRecord?.["form_schema"] ?? {}) as Schema;
  }
  taskDecisions() {
    return (this.selectedRecord?.["decisions"] ?? []) as string[];
  }
  taskOwned() {
    return this.selectedRecord?.["claimant_id"] === this.identity?.principal_id;
  }
  async taskCommand(command: string, decision?: string) {
    if (this.taskConflict) throw Error("Reload this task before acting again.");
    if (!this.selectedRecord) return;
    const r = this.selectedRecord;
    if (command === "complete") {
      const form = document.querySelector<HTMLFormElement>(
        ".human-decision-form",
      );
      if (form && !form.reportValidity()) return;
    }
    const body: Record<string, unknown> = { expected_revision: r["revision"] };
    if (decision) {
      body["decision"] = decision;
      body["data"] = this.taskData;
    }
    try {
      const result = await this.safeMutation(
        `${this.api.environment}/human-tasks/${r["id"]}/${command}`,
        "POST",
        body,
        undefined,
        command === "complete" ? `Submit ${decision}` : command,
      );
      this.selectedRecord = result;
      this.records = this.records.map((x) =>
        x["id"] === result["id"] ? result : x,
      );
      this.message =
        command === "complete"
          ? "Decision recorded."
          : `Task ${command} accepted.`;
    } catch (e) {
      if (e instanceof ApiError && (e.status === 409 || e.status === 403)) {
        this.taskConflict = true;
        this.error =
          "This task changed or your authorization was revoked. Your form is retained. Reload current state before acting again.";
      } else this.fail(e);
    }
  }
  messages() {
    return (this.emailDetail?.["messages"] ?? []) as Record<string, unknown>[];
  }
  mail(message: Record<string, unknown>) {
    return (message["mail"] ?? {}) as Record<string, unknown>;
  }
  async sendReply() {
    if (!this.selectedRecord || !this.reply.trim()) return;
    const parent = this.messages()
      .filter((m) => m["direction"] === "inbound")
      .at(-1);
    if (!parent) {
      this.error = "This conversation has no inbound message to reply to.";
      return;
    }
    const body = {
      request_id: crypto.randomUUID(),
      connection_revision_id: this.selectedRecord["connection_revision_id"],
      parent_message_id: parent["id"],
      text: this.reply,
      reply_all: false,
    };
    try {
      this.emailSubmission = await this.safeMutation(
        `${this.api.environment}/email/conversations/${this.selectedRecord["id"]}/reply`,
        "POST",
        body,
        undefined,
        "Queue email reply",
      );
      this.message = "Reply queued. Execute the queued submission to send it.";
      this.reply = "";
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
    }
  }
  async executeEmail() {
    if (!this.emailSubmission || this.emailSubmission["state"] !== "queued")
      return;
    try {
      this.emailSubmission = await this.safeMutation(
        `${this.api.environment}/email/submissions/${this.emailSubmission["id"]}/execute`,
        "POST",
        {},
        undefined,
        "Send queued email",
      );
      this.message = `Email status: ${this.emailSubmission["state"]}.`;
      await this.readDetail();
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
    }
  }
  async reconcileEmail() {
    if (!this.emailSubmission) return;
    try {
      this.emailSubmission = await this.api.request(
        `${this.api.environment}/email/submissions/${this.emailSubmission["id"]}`,
      );
      await this.readDetail();
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
    }
  }
  async safeMutation<
    T extends Record<string, unknown> = Record<string, unknown>,
  >(
    path: string,
    method: string,
    body: unknown,
    revision: number | undefined = undefined,
    label: string = "Command",
  ) {
    if (this.pendingMutation)
      throw Error("Resolve the pending mutation before another action.");
    const pending = {
      path,
      method,
      body: structuredClone(body),
      revision,
      key: crypto.randomUUID(),
      label,
    };
    this.pendingMutation = pending;
    this.busy = label;
    try {
      const result = await this.api.mutate<T>(
        path,
        method,
        body,
        revision,
        pending.key,
      );
      this.pendingMutation = null;
      this.unknownCommand = false;
      return result;
    } catch (e) {
      if (e instanceof ApiError && e.status < 500 && e.status !== 408) {
        this.pendingMutation = null;
        throw e;
      }
      this.unknownCommand = true;
      throw Error(
        `${label} has an unknown outcome. Its request identity is retained. Reload current state or reconcile this exact request.`,
      );
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  async retryExact() {
    const p = this.pendingMutation;
    if (!p) return;
    this.busy = "Reconcile";
    try {
      const r = await this.api.mutate<Record<string, unknown>>(
        p.path,
        p.method,
        p.body,
        p.revision,
        p.key,
      );
      this.pendingMutation = null;
      this.unknownCommand = false;
      if (p.path.includes("/human-tasks/")) this.selectedRecord = r;
      else if (p.path.includes("/email/")) this.emailSubmission = r;
      if (p.path.includes("/drafts/")) {
        this.draftRevision = Number(r["revision"]);
        this.saveState =
          JSON.stringify((p.body as { document: unknown }).document) ===
          JSON.stringify(this.model.definition)
            ? "Saved to API"
            : "Unsaved";
      } else if (p.path.endsWith("/workflows")) this.published = r;
      else if (p.path.endsWith("/activations")) this.activation = r;
      this.message = "Original command reconciled.";
      this.error = "";
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  async runControl(command: string) {
    if (!this.selectedRecord) return;
    const state = this.selectedRecord["state"] as Record<string, unknown>;
    const reason = prompt(`Reason to ${command} this run`);
    if (!reason) return;
    try {
      this.selectedRecord = await this.safeMutation(
        `${this.api.environment}/runs/${this.selectedRecord["id"]}/${command}`,
        "POST",
        { expected_revision: state["control_revision"] ?? 0, reason },
        undefined,
        command,
      );
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
    }
  }

  runTerminal() {
    return ["succeeded", "failed", "cancelled", "timed_out"].includes(
      String(
        (
          this.selectedRecord?.["state"] as Record<string, unknown> | undefined
        )?.["status"],
      ),
    );
  }
  async lifecycleCommand(command: "archive" | "restore" | "purge") {
    if (!this.selectedRecord || !this.runLifecycle) return;
    const id = String(this.selectedRecord["id"]);
    const reason = prompt(`Audit reason to ${command} this execution`);
    if (!reason) return;
    const body: Record<string, unknown> = {
      expected_revision: this.runLifecycle["revision"],
      reason,
    };
    if (command === "purge") {
      const typed = prompt(
        `Permanently purge execution content? Audit receipts remain. Type the exact run ID to confirm: ${id}`,
      );
      if (typed === null) return;
      if (typed !== id) {
        this.error =
          "The confirmation run ID does not match. No purge was submitted.";
        return;
      }
      body["confirm_run_id"] = typed;
    }
    try {
      await this.safeMutation(
        `${this.api.environment}/runs/${id}/${command}`,
        "POST",
        body,
        undefined,
        `${command} execution`,
      );
      this.runLifecycle = await this.api.request(
        `${this.api.environment}/runs/${id}/lifecycle`,
      );
      this.message =
        command === "purge"
          ? "Execution content purged; retained audit receipts remain."
          : `Execution ${command} accepted.`;
      await this.refresh();
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
    }
  }
  runNodes() {
    return this.runCanvas?.nodes() ?? [];
  }
  runActive(id: string) {
    const state = this.selectedRecord?.["state"] as
      | { active?: string[] }
      | undefined;
    return state?.active?.includes(id) ?? false;
  }
  runReason() {
    const state = this.selectedRecord?.["state"] as
      | {
          manual_paused?: boolean;
          status?: string;
          incident?: string;
          active?: string[];
        }
      | undefined;
    if (!state) return "State unavailable";
    if (state.manual_paused) return "Paused by an operator";
    if (state.incident) return "Blocked by a technical incident";
    if (state.status !== "waiting") return state.status ?? "State unavailable";
    const node = this.runNodes().find((n) => state.active?.includes(n.step.id));
    return node
      ? ((
          {
            wait: "Waiting for time",
            signal: "Waiting for a signal",
            humanTask: "Waiting for a person",
            action: "Waiting for action work",
          } as Record<string, string>
        )[node.step.kind] ?? "Waiting for runtime continuation")
      : "Waiting — inspect history for the reason";
  }
  @HostListener("window:popstate") popstate() {
    const segment = location.pathname.split("/")[1] as View;
    this.view = this.nav.some((n) => n.id === segment) ? segment : "home";
    void this.refresh();
  }
  @HostListener("window:keydown", ["$event"]) key(event: KeyboardEvent) {
    const target = event.target as HTMLElement;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
    if (event.key === "Escape") {
      this.cancelGesture();
      this.showInspector = false;
    }
    if (this.view !== "designer") return;
    if ((event.metaKey || event.ctrlKey) && event.key === "z") {
      event.preventDefault();
      event.shiftKey ? this.redo() : this.undo();
    }
    if ((event.metaKey || event.ctrlKey) && event.key === "s") {
      event.preventDefault();
      void this.save();
    }
    if (event.key === "Delete") this.remove();
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const i = this.nodes.findIndex((n) => n.step.id === this.model.selected);
      const n =
        this.nodes[
          Math.max(
            0,
            Math.min(
              this.nodes.length - 1,
              i + (event.key === "ArrowDown" ? 1 : -1),
            ),
          )
        ];
      if (n) this.select(n);
    }
  }
}
