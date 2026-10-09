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
import { changeSlot, slotValidation } from "./integrations/connection-slots";
import {
  Component,
  HostListener,
  Injector,
  afterEveryRender,
  afterNextRender,
  signal,
  viewChild,
  inject,
  ChangeDetectorRef,
} from "@angular/core";
import type { FileAccess } from "./forms/core/file-reference";
import { Title } from "@angular/platform-browser";
import { NavigationEnd, Router } from "@angular/router";
import { Icon } from "./icon";
import { AnchoredPopover } from "./forms/ui/anchored-popover";
import { type Schema, missingRequired } from "./task-schema";
import { ConnectionWizard } from "./connection-wizard";
import {
  ConnectionClient,
  ConnectionResult,
  ConnectionStatus,
  Identity,
  LoginEvent,
  LoginWatcher,
  PlatformAction,
  StartPreference,
  SwitchKind,
  VerifiedPlatform,
  WizardStep,
  WorkspaceChange,
  accountName,
  isConnectionStatus,
  loginOutcome,
  platformState,
  revocationNote,
  sessionUsable,
  signOutReport,
  signedInText,
  stateLabel,
} from "./connection";
import { HomeDashboard } from "./home-dashboard";
import { ApiError, StudioApi, Validation, Session } from "./api";
import { Autofocus, DialogHost, DialogService, Modal } from "./dialog";
import { describeError, PlainError } from "./errors";
import {
  ExportedFile,
  exportFile,
  exportNotice,
  isDesktopShell,
} from "./export-file";
import { type ActivationBindings, ActivationDialog } from "./activation-dialog";
import {
  StepPicker,
  type AnchorRect,
  type PickerAction,
  type StepPickerChoice,
} from "./designer/step-picker";
import type { CanvasHost, WorkflowSection } from "./editor/canvas/canvas-host";
import { setupPhrase, type StepFacts } from "./editor/canvas/tile-facts";
import type { CanvasRun } from "./editor/canvas/run-state";
import type { Json, KindContext } from "./editor/ndv/registry";
import { StartRunDialog, type StartRunRequest } from "./run/start-run-dialog";
import type { SuggestedChange } from "./designer/diagnostics-list";
import type { DiagnosticLocation } from "./designer/diagnostic-location";
import type { ContractGap } from "./designer/contract-gaps";
import type { ReferenceContext } from "./forms/core/reference-context";
import type { InferSchema } from "./forms/ui/schema-designer";
import type {
  ValidationService,
  ValidationSnapshot,
} from "./designer/validation";
import type {
  DebugCommandBody,
  DebugCreateBody,
  DebugSessionData,
  SimulationNodes,
} from "./designer/simulation/simulation-plan";
import {
  Kind,
  kinds,
  ownerLabel,
  StructuredCanvasAdapter,
  Workflow,
  Node,
  Placeholder,
  Point,
  Target,
  Step,
} from "./model";
import { parse, stringify } from "yaml";
import {
  incompleteChip,
  stepIssues,
  stepSummary,
} from "./designer/canvas-summary";
import {
  conditionSummary,
  shortLabel,
  withConditionDiagnostics,
} from "./designer/conditions";
import {
  stepKindDescriptions,
  stepKindGroups,
  stepKindKeywords,
  stepKindLabels,
} from "./designer/step-kinds";
import {
  OVERVIEW_BELOW,
  fitAll as fitAllView,
  readableFit,
  reveal,
  revealGroup,
  usableViewport,
  wheelFactor,
  zoomAt,
  type Bounds,
} from "./designer/viewport";
import type { RowMenuItem } from "./row-menu";
import { DesignerView } from "./designer/designer-view";
// Integration and template UI loads lazily (@defer): only types are imported
// from these modules besides the component classes listed in `imports`.
import { IntegrationDialogs } from "./integrations/integration-dialogs";
import { TemplateGallery } from "./templates/template-gallery";
import { sheetWhen } from "./modal-sheet";
import { ToastHost, ToastService, type ToastAction } from "./toast";
import { LocalDrafts, type LocalDraftEntry } from "./local-drafts";
import {
  CHOICE_NOT_KEPT,
  editorNextEnabled,
  setEditorNext,
} from "./editor/state/editor-flag";
import {
  editorNavExpanded,
  setEditorNavExpanded,
} from "./editor/state/canvas-preferences";
import { runStatus } from "./status-labels";
import {
  loadingWorkspace,
  shortId,
  workspaceFallback,
  workspaceLong,
  workspaceShort,
} from "./format";
// The operations pages and Settings load lazily (@defer): only these classes.
import { OperationsView } from "./operations/operations-view";
import { RecordsView } from "./operations/records-view";
import { SettingsPage } from "./settings/settings-page";
import { LumiPanel } from "./lumi/lumi-panel";
import { canvasSheet, keyPlatform } from "./editor/state/canvas-commands";
import type {
  BuilderTab,
  HttpActionUse,
  UseContext,
} from "./integrations/http-action-builder";
import type { NewChoice } from "./templates/new-menu";
import {
  compatibleSlots as slotsFitting,
  planSlot,
} from "./integrations/slot-binding";
/** No contract gaps yet: the same empty list, so caches keyed on it hold. */
const noContractGaps: (ContractGap & { stepId: string })[] = [];
export type View =
  | "home"
  | "workflows"
  | "designer"
  | "runs"
  | "tasks"
  | "email"
  | "connections"
  | "workers"
  | "operations"
  | "settings"
  | "connect";
type PlatformNoticeKind =
  | "expired"
  | "not-linked"
  | "offline"
  | "store"
  | "changed"
  | "revoked"
  | "workspace"
  | "sign-out-incomplete"
  | "signing-in"
  | "sign-in-failed"
  | "revocation";
interface ConnectionSlot {
  name: string;
  connector: string;
  required: boolean;
}
type CatalogState = "idle" | "loading" | "ready" | "error";
/** Commands whose buttons explain why they are unavailable. */
type Command = "save" | "publish" | "simulate" | "activate" | "run";
/** Where the open workflow is in its life: what the one primary button does next. */
export type Lifecycle = "local" | "unsaved" | "saved" | "published" | "active";
/** The lifecycle command each state leads to. */
const nextCommand: Record<Exclude<Lifecycle, "local">, Command> = {
  unsaved: "save",
  saved: "publish",
  published: "activate",
  active: "run",
};
const commandLabels: Record<Command, string> = {
  save: "Save draft",
  publish: "Publish…",
  activate: "Activate…",
  run: "Start run…",
  simulate: "Simulate",
};
/** What a running command's button says: "Publishing…". */
const busyLabels: Record<string, string> = {
  save: "Saving…",
  publish: "Publishing…",
  activate: "Activating…",
  validate: "Validating…",
  simulate: "Simulating…",
};
/** The narrowest window that shows the editor's navigation in full. */
const EDITOR_NAV_FULL_MIN = 900;
/** The inspector's width: 360–480 px by default, at most 640 px. */
const INSPECTOR_MIN = 360;
const INSPECTOR_MAX = 640;
const inspectorWidthKey = "weave.studio.inspectorWidth";
const inspectorSectionsKey = "weave.studio.inspectorSections";
const defaultInspectorWidth = () =>
  Math.round(Math.min(480, Math.max(INSPECTOR_MIN, window.innerWidth * 0.25)));
/** Reads a stored value; storage may be missing or refuse (privacy modes). */
function stored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function store(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Not kept: a convenience only.
  }
}
/** JSON with sorted keys, so equal documents compare equal. */
const canonicalJson = (value: unknown): string =>
  JSON.stringify(value, (_, v: unknown) =>
    v && typeof v === "object" && !Array.isArray(v)
      ? Object.fromEntries(
          Object.entries(v as Record<string, unknown>).sort(([a], [b]) =>
            a < b ? -1 : a > b ? 1 : 0,
          ),
        )
      : v,
  );
/** The next patch version: 1.4.2 becomes 1.4.3. */
const nextPatch = (version: string) => {
  const match = /^(\d+)\.(\d+)\.(\d+)/.exec(version);
  return match ? `${match[1]}.${match[2]}.${Number(match[3]) + 1}` : "1.0.1";
};
const resourceName = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
/** "/workflows/:id/designer": a workflow open in the designer. */
const designerPath = /^\/workflows\/([^/]+)\/designer\/?$/;
const versionedName =
  /^[A-Za-z0-9][A-Za-z0-9_.-]*@(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:[-+][0-9A-Za-z.+-]*)?$/;
const placeholderAction = "your-action@1.0.0";
const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
/** True when an action input can be edited field by field (an object input). */
const fieldable = (input: unknown) =>
  input === undefined ||
  (isRecord(input) &&
    Object.keys(input).length === 1 &&
    (isRecord(input["literal"]) || isRecord(input["object"])));
/**
 * The schema without its secret (`x-secret`, `writeOnly`) fields: no
 * workflow input may set them, so they are never "missing".
 */
const withoutSecrets = (schema: Schema): Schema => ({
  ...schema,
  properties: Object.fromEntries(
    Object.entries(schema.properties ?? {})
      .filter(([, field]) => {
        const marks = field as Record<string, unknown>;
        return marks["x-secret"] !== true && marks["writeOnly"] !== true;
      })
      .map(([name, field]) => [name, withoutSecrets(field)]),
  ),
});
/** "Approve" for the unknown-outcome banner. */
const decisionVerb = (decision: string) =>
  decision ? decision.charAt(0).toUpperCase() + decision.slice(1) : "Decide";
/** "Approved", "Rejected", or "Decided" for other decisions. */
const decisionPast = (decision: string) =>
  ({ approve: "Approved", reject: "Rejected" })[decision] ??
  `Decided "${decision}" for`;
const sessionLostMessage =
  "Your Studio session ended. Export your workflow to keep your edits, then quit and reopen Firefly Weave Studio.";
const noHiddenFields: string[] = [];
const actionPickerHiddenFields = ["connection", "uses"];
const actionFieldsPickerHiddenFields = ["connection", "with", "uses"];
const sideEffects: Record<string, string> = {
  read_only: "Read only — safe to retry",
  idempotent: "Idempotent — safe to retry",
  idempotency_key: "Retried with an idempotency key",
  non_idempotent: "Not idempotent — never retried automatically",
};
@Component({
  selector: "weave-studio",
  standalone: true,
  imports: [
    Icon,
    AnchoredPopover,
    HomeDashboard,
    ConnectionWizard,
    DialogHost,
    Modal,
    Autofocus,
    ActivationDialog,
    StepPicker,
    StartRunDialog,
    IntegrationDialogs,
    TemplateGallery,
    ToastHost,
    DesignerView,
    RecordsView,
    OperationsView,
    SettingsPage,
    LumiPanel,
  ],
  templateUrl: "./app.html",
})
export class App implements CanvasHost {
  private cdr = inject(ChangeDetectorRef);
  private router = inject(Router);
  private historyPosition: number = history.state?.weavePosition ?? 0;
  private historyUrl = location.href;
  private restoringHistory = false;
  private injector = inject(Injector);
  dialogs = inject(DialogService);
  crypto = crypto;
  String = String;
  Math = Math;
  connectionStatus: ConnectionStatus | null = null;
  importGeneration = 0;
  identity: Identity | null = null;
  // The connection wizard; a new key recreates it at the requested step.
  wizard: {
    key: number;
    start: WizardStep;
    platform: string;
    server: string;
    switchAccount: boolean;
  } | null = null;
  private wizardKey = 0;
  private wizardReturn: View = "home";
  // Non-blocking platform state shown under the top bar; local work continues.
  platformNotice: {
    kind: PlatformNoticeKind;
    name: string;
    server?: string;
    /** A plain sentence about the outcome, for "sign-in-failed". */
    detail?: string;
  } | null = null;
  lumiOpen = false;
  private readonly operationsView = viewChild(OperationsView);
  get lumiOperationAttachments() {
    return this.view === "operations"
      ? (this.operationsView()?.lumiAttachments ?? [])
      : [];
  }
  lumiSettingsRequested = false;
  platformMenuOpen = false;
  platformBusy = "";
  // Fences platform checks and status reads against later platform changes.
  private platformGeneration = 0;
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
  private currentFileAccess: FileAccess | null = null;
  private currentTaskFileAccess: FileAccess | null = null;
  private fileAccessKey() {
    return JSON.stringify([
      this.profile,
      this.identity?.principal_id,
      this.can("file.read"),
      this.can("file.manage"),
    ]);
  }
  get fileAccess(): FileAccess | null {
    if (!this.profile) return null;
    const key = this.fileAccessKey();
    if (this.currentFileAccess?.key !== key)
      this.currentFileAccess = {
        api: this.api,
        key,
        canRead: this.can("file.read"),
        canManage: this.can("file.manage"),
        active: () => key === this.fileAccessKey(),
      };
    return this.currentFileAccess;
  }
  private taskFileAccessKey() {
    return JSON.stringify([
      this.fileAccessKey(),
      this.selectedRecord?.["id"],
      this.selectedRecord?.["revision"],
      this.selectedRecord?.["status"],
      this.selectedRecord?.["claimant_id"],
      this.taskConflict,
    ]);
  }
  get taskFileAccess(): FileAccess | null {
    const task = this.selectedRecord;
    if (!this.profile || this.view !== "tasks" || !task) return null;
    const key = this.taskFileAccessKey();
    if (this.currentTaskFileAccess?.key !== key)
      this.currentTaskFileAccess = {
        api: this.api,
        key,
        canRead: this.can("human_task.read", String(task["id"])),
        canManage:
          this.taskOwned() &&
          task["status"] === "claimed" &&
          !this.taskConflict,
        task: { id: String(task["id"]), revision: Number(task["revision"]) },
        active: () => this.view === "tasks" && key === this.taskFileAccessKey(),
      };
    return this.currentTaskFileAccess;
  }
  connection = new ConnectionClient(this.api);
  /** Follows a pending sign-in, also after the person leaves the wizard. */
  loginWatcher = new LoginWatcher(this.connection);
  // Settings: how Studio starts (the host keeps it next to the saved platforms).
  startNote = "";
  startError: { message: string; code: string } | null = null;
  /** The latest "When Studio opens" choice; only its outcome is shown. */
  private startSequence = 0;
  /** Saves run one after another, so the host keeps the last choice. */
  private startQueue: Promise<unknown> = Promise.resolve();
  model = new StructuredCanvasAdapter();
  tick = signal(0);
  /** Visible outcomes: one toast at a time. */
  readonly toasts = inject(ToastService);
  private title = inject(Title);
  /** Workflows kept on this computer (localStorage). */
  readonly localDrafts = new LocalDrafts();
  /** "On this computer", newest first; refreshed whenever it is shown. */
  localList: LocalDraftEntry[] = [];
  /** Storage refused a draft: the notice says so once, until dismissed. */
  storageNotice = false;
  private storageNoticeShown = false;
  private localTimer: ReturnType<typeof setTimeout> | null = null;
  /** Published versions by ID: the name and version a run belongs to. */
  workflowVersions = new Map<string, { name: string; version: string }>();
  /** Runs read in this workspace, by ID: the workflow behind a task. */
  knownRuns = new Map<string, Record<string, unknown>>();
  private versionsScope: unknown = null;
  private currentView: View = "home";
  /** Focus before a view change: the new view's h1 takes it unless it moved. */
  private focusBeforeView: Element | null = null;
  private viewFocusPending = false;
  get view(): View {
    return this.currentView;
  }
  set view(view: View) {
    if (view === this.currentView) return;
    this.currentView = view;
    this.focusBeforeView = document.activeElement;
    this.viewFocusPending = true;
  }
  tab = "Designer";
  collapsed = false;
  /**
   * In the editor the navigation follows its own choice: the one this viewer
   * saved, otherwise expanded from 1440 px and the 64 px rail below, so the
   * canvas gets the room. Every other view uses `collapsed`.
   */
  get navCollapsed() {
    return this.view === "designer"
      ? !(this.editorNavChoice ?? editorNavExpanded(this.windowWidth))
      : this.collapsed;
  }
  /**
   * The editor shows its expanded navigation in full down to 900 px, even
   * where other views narrow to the rail. Below that the editor's toolbar no
   * longer fits beside it (Save to file is cut off), so it stays the rail.
   * This is what the sidebar draws, in the editor and in the width sums.
   */
  get navFull() {
    return (
      this.view === "designer" &&
      !this.navCollapsed &&
      this.windowWidth >= EDITOR_NAV_FULL_MIN
    );
  }
  /**
   * The navigation button. In the editor below 900 px the navigation cannot
   * expand, so no button claims it can; the saved choice stays for wider windows.
   */
  get navToggleOffered() {
    return this.view !== "designer" || this.windowWidth >= EDITOR_NAV_FULL_MIN;
  }
  toggleNav() {
    if (this.view !== "designer") {
      this.collapsed = !this.collapsed;
      return;
    }
    const expand = this.navCollapsed;
    this.editorNavChoice = expand;
    if (!setEditorNavExpanded(expand) && !this.navNoticeShown) {
      this.navNoticeShown = true;
      this.notify(CHOICE_NOT_KEPT);
    }
  }
  /** The editor's navigation choice made in this session; it holds when browser storage refuses to keep it. */
  private editorNavChoice: boolean | null = null;
  private navNoticeShown = false;
  windowWidth = window.innerWidth;
  /** Where the side panels cover the page and act as modal sheets. */
  readonly sheetWhen = sheetWhen;
  /** The selected step's card: focus returns there when the inspector closes. */
  readonly selectedNodeElement = () =>
    this.model.selected
      ? document.querySelector<HTMLElement>(
          `[data-step="${CSS.escape(this.model.selected)}"] :is(.node-body, .tile-body)`,
        )
      : null;
  showPalette = false;
  paletteWidth = window.innerWidth <= 1440 ? 192 : 208;
  /** The inspector's width; a width the person chose is kept in this browser. */
  inspectorWidth = (() => {
    const kept = Number(stored(inspectorWidthKey));
    return Number.isFinite(kept) &&
      kept >= INSPECTOR_MIN &&
      kept <= INSPECTOR_MAX
      ? kept
      : defaultInspectorWidth();
  })();
  readonly inspectorLimits = { min: INSPECTOR_MIN, max: INSPECTOR_MAX };
  /** Why a command the person pressed can't run; read under the toolbar. */
  commandNote = "";
  /** The "Keyboard shortcuts" sheet. */
  shortcutsOpen = false;
  /**
   * "Try the new editor" (Settings › Preferences), or `?editor=next` in the
   * address when Studio opened.
   */
  editorNext = editorNextEnabled();
  setEditorNextPreference(enabled: boolean) {
    this.editorNext = enabled;
    if (!setEditorNext(enabled)) this.notify(CHOICE_NOT_KEPT);
    this.cdr.markForCheck();
  }
  /**
   * The diagnostics strip shows its problems; the person can fold it to its
   * 36 px status line (it is that line when there is nothing to list).
   */
  diagnosticsOpen = true;
  /** Narrow screens open on the outline, with a way to the canvas. */
  outlineNotice = false;
  resizing: {
    pane: "palette" | "inspector";
    x: number;
    initial: number;
  } | null = null;
  /** Home, then "Build" (1–2), then "Operate" (3–6); Settings sits at the bottom. */
  nav: { id: View; label: string }[] = [
    { id: "home", label: "Home" },
    { id: "workflows", label: "Workflows" },
    { id: "connections", label: "Connections" },
    { id: "runs", label: "Runs" },
    { id: "tasks", label: "My tasks" },
    { id: "email", label: "Email" },
    { id: "workers", label: "Workers" },
    { id: "operations", label: "Operations" },
    { id: "settings", label: "Settings" },
  ];
  readonly navGroups = [
    { label: "", items: this.nav.slice(0, 1) },
    { label: "Build", items: this.nav.slice(1, 3) },
    { label: "Operate", items: this.nav.slice(3, 8) },
  ];
  kinds = kinds;
  paletteQuery = "";
  search = "";
  /**
   * The Workflows tab: "workflows" (Published), "drafts", or "local" (On this
   * computer). Until one is chosen: Published when connected, otherwise local.
   */
  get libraryCollection(): "workflows" | "drafts" | "local" {
    return this.libraryChoice ?? (this.profile ? "workflows" : "local");
  }
  set libraryCollection(tab: "workflows" | "drafts" | "local") {
    this.libraryChoice = tab;
  }
  private libraryChoice: "workflows" | "drafts" | "local" | null = null;
  pairCode = "";
  paired = false;
  connecting = true;
  message = "";
  busy = "";
  error = "";
  sourceBuffer = this.model.source;
  inspectorBuffer = "";
  showOutline = false;
  /**
   * Whether the inspector shows. Above 1280 px it is a column that stays
   * until the person hides it (Show/Hide inspector); narrower, it opens for
   * a step and closes again. Showing it anywhere shows it in both.
   */
  get showInspector() {
    return this.windowWidth > 1280 ? this.wideInspector : this.inspectorOpen;
  }
  set showInspector(open: boolean) {
    this.inspectorOpen = open;
    if (open) this.wideInspector = true;
    else if (this.windowWidth > 1280) this.wideInspector = false;
  }
  private inspectorOpen = false;
  private wideInspector = true;
  diagnostics: Validation | null = null;
  /** The definition the shown diagnostics point into. */
  diagnosticsDefinition: unknown = null;
  /** The diagnostics bar: tone, headline and "N errors · M warnings". */
  validationView = {
    tone: "idle",
    headline: "Not validated yet.",
    countLine: "",
  };
  /** Live validation (WP-06); its module loads on first use. */
  private validation: ValidationService | null = null;
  private validationModule: Promise<
    typeof import("./designer/validation")
  > | null = null;
  private summarize: typeof import("./designer/validation").summarize | null =
    null;
  /** The last check result already shown, so a stale one never returns. */
  private shownResult: unknown = null;
  /** The open simulation: the compiled artifact it runs. */
  simulation: { artifact: Record<string, unknown> } | null = null;
  simulationError: PlainError | null = null;
  /** Where the simulated run is, for the canvas highlights. */
  simNodes: SimulationNodes = {
    status: "",
    current: [],
    active: [],
    breakpoints: [],
    done: [],
  };
  homeTasks: Record<string, unknown>[] = [];
  homeRuns: Record<string, unknown>[] = [];
  /** Home "Needs you": tasks ready to claim, failed runs, waiting runs. */
  homeAttention: { kind: "task" | "run"; record: Record<string, unknown> }[] =
    [];
  /** Home "Recent workflows" from the platform's drafts. */
  homeDrafts: Record<string, unknown>[] = [];
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
    { value: "file_reader", label: "File reader" },
    { value: "file_manager", label: "File manager" },
    { value: "lumi_user", label: "Weave AI user" },
    { value: "lumi_manager", label: "Weave AI manager" },
    { value: "deployment_reader", label: "Deployment viewer" },
    { value: "deployment_planner", label: "Deployment planner" },
    { value: "deployment_approver", label: "Deployment approver" },
    { value: "deployment_operator", label: "Deployment operator" },
    { value: "deployment_runner", label: "Deployment runner" },
    { value: "worker_operator", label: "Worker operator" },
  ];
  // Published action catalog, loaded once per workspace and shared by every action step.
  actionVersions: Record<string, unknown>[] = [];
  actionNextCursor: string | null = null;
  catalogState: CatalogState = "idle";
  catalogError: { message: string; code: string; status: number } | null = null;
  catalogAppending = false;
  private catalogScope: unknown = null;
  private catalogGeneration = 0;
  // Action contracts by name@version; the inspector shows the one its buffer uses.
  private contractCache = new Map<string, Record<string, unknown>>();
  readonly decisionContracts = new Map<string, Record<string, unknown>>();
  cacheDecisionContract(uses: string, document: Record<string, unknown>) {
    this.decisionContracts.set(uses, document);
    this.tick.update((value) => value + 1);
    this.cdr.markForCheck();
  }
  actionContract: Record<string, unknown> | null = null;
  contractState: CatalogState = "idle";
  contractError = "";
  private contractGeneration = 0;
  /** The action input is edited field by field, or as one custom expression. */
  actionInputMode: "fields" | "expression" = "expression";
  /** The `with` expression the field-by-field form starts from. */
  actionInitialInput: unknown = undefined;
  actionInputValid = signal(true);
  /** Required inputs the field-by-field form reports as unset. */
  actionMissing: string[] = [];
  /** Human-task decision form validity. */
  taskFormValid = true;
  newSlotName = "";
  // Until the person edits it, the slot name field shows the suggested name.
  newSlotNameEdited = false;
  newSlotConnector = "";
  newSlotRequired = true;
  slotError = "";
  workflowProperties = structuredClone(this.model.definition);
  workflowBuffer = JSON.stringify(this.model.definition);
  workflowValid = signal(true);
  propertyStep: Step | null = null;
  propertyValid = signal(true);
  // The model step JSON the inspector buffers were loaded from.
  private inspectorBase = "";
  private inspectorStepId = "";
  inspectorSession = 0;
  private scopeCache: { key: string; value: ReferenceContext | null } = {
    key: "",
    value: null,
  };
  dirty = false;
  errorCode = "";
  sessionNotice = "";
  // The desktop host ended this window's session while it held unsaved edits.
  sessionLost = false;
  // The desktop shell pairs its window itself; it never shows a pairing code.
  readonly desktopShell = isDesktopShell();
  exportFallback: ExportedFile[] | null = null;
  showActivation = false;
  /** Why the last activation failed, shown inside the activation dialog. */
  activationError: { message: string; code: string } | null = null;
  private inspectorTimer: ReturnType<typeof setTimeout> | null = null;
  private pendingInspector: {
    scope: "step" | "workflow" | "rename";
    id: string;
    text: string;
    key: string;
    opened: number;
    continueRevision?: number;
  } | null = null;
  private actionChoiceRevision: number | null = null;
  private committingInspector = false;
  renameDraft = "";
  private draftSavedAt = "";
  advancedProperties = false;
  zoom = 1;
  pan = { x: 0, y: 0 };
  dragPreview: { kind: Kind; point: { x: number; y: number } } | null = null;
  dragNode: {
    id: string;
    start: { x: number; y: number };
    origin: { x: number; y: number };
    point: { x: number; y: number };
    pointer: number;
  } | null = null;
  connectingNode = "";
  dragTarget: Target | null = null;
  private suppressNodeClick = false;
  /** The open step picker and the "+" it inserts at. */
  picker: { target: Target; anchor: HTMLElement | AnchorRect } | null = null;
  /** A press on empty canvas; a release close to it returns to workflow settings. */
  private canvasPress: Point | null = null;
  reply = "";
  decisionData = "{}";
  decision = "approve";
  commandKey: string | null = null;
  unknownCommand = false;
  taskFilter = "";
  runFilters = { business_key: "", correlation_key: "", status: "" };
  showStartRun = false;
  /** The activation the start-run dialog preselects. */
  runStartActivation = "";
  /** The workflow version the start-run dialog preselects (the designer's). */
  runStartPreselect: { workflow: string; version: string } | null = null;
  /** Why the last start failed, shown inside the start-run dialog. */
  runStartError: PlainError | null = null;
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
  private cachedLaneGroups: ReturnType<StructuredCanvasAdapter["laneGroups"]> =
    [];
  private cachedLaneTerminals: ReturnType<
    StructuredCanvasAdapter["terminals"]
  > = [];
  private cachedJunctions: Record<string, Point> = {};
  private nodeById = new Map<string, Node>();
  private visualLinks: { from: string; to: string }[] = [];
  private cachedTargets: Target[] = [];
  private placeholderByOwner = new Map<string, Placeholder>();
  private graphSize = { width: 1320, height: 1240 };
  private edgeCache: {
    tick: number;
    drag: unknown;
    paths: { key: string; from: string; d: string }[];
  } = { tick: -1, drag: null, paths: [] };
  /** Errors and warnings per step from the last diagnostics, counted and in order. */
  private diagnosticSteps = new Map<
    string,
    {
      errors: number;
      warnings: number;
      errorMessages: string[];
      warningMessages: string[];
    }
  >();
  private infoCache: {
    tick: number;
    steps: unknown;
    info: Map<
      string,
      {
        summary: string;
        status: string;
        statusText: string;
        label: string;
        chip: string;
      }
    >;
  } = { tick: -1, steps: null, info: new Map() };
  /** Why the last rename was refused, shown under the Step ID field. */
  renameError = "";
  /** Whether the person edited the step or workflow fields since they loaded. */
  private touched = { step: false, workflow: false };
  private pickerCache: {
    source: unknown;
    actions: PickerAction[];
  } = { source: null, actions: [] };
  /** Bumped whenever an action contract is cached or dropped. */
  private contractRevision = 0;
  private findContractGaps:
    | typeof import("./designer/contract-gaps").contractGaps
    | null = null;
  private gapCache: {
    tick: number;
    revision: number;
    rows: (ContractGap & { stepId: string })[];
  } = { tick: -1, revision: -1, rows: [] };
  get contractIssues() {
    const contractGaps = this.findContractGaps;
    if (!contractGaps) return noContractGaps;
    const tick = this.tick();
    if (
      this.gapCache.tick !== tick ||
      this.gapCache.revision !== this.contractRevision
    ) {
      this.gapCache = {
        tick,
        revision: this.contractRevision,
        rows: this.nodes.flatMap((node) =>
          contractGaps(
            node.step,
            this.contractCache.get(String(node.step["uses"])) ??
              this.decisionContracts.get(String(node.step["uses"])),
            this.model.definition,
          ).map((gap) => ({ ...gap, stepId: node.step.id })),
        ),
      };
    }
    return this.gapCache.rows;
  }
  async openContractIssue(issue: ContractGap & { stepId: string }) {
    await this.openDiagnostic({
      stepId: issue.stepId,
      stepPointer: "",
      fieldPath: "/" + issue.field,
      field: [issue.field],
      dataPath: issue.dataPath,
      exact: true,
    });
    afterNextRender(
      () =>
        requestAnimationFrame(() =>
          this.focusField(issue.field, issue.dataPath),
        ),
      { injector: this.injector },
    );
  }
  openNodeIssue(node: Node) {
    const issue = this.contractIssues.find(
      (issue) => issue.stepId === node.step.id,
    );
    if (issue) void this.openContractIssue(issue);
    else void this.select(node);
  }
  private contractView: {
    revision: number;
    map: ReadonlyMap<string, Record<string, unknown>>;
  } = { revision: -1, map: new Map() };
  /** The open API action builder: where it inserts and its first tab. */
  apiBuilder: { context: UseContext; tab: BuilderTab; step: string } | null =
    null;
  /**
   * The builder's last hand-off (names and the origin only), for the
   * connection form. It belongs to the workflow open when it was made.
   */
  get lastUse(): HttpActionUse | null {
    const handoff = this.handoff;
    return handoff &&
      handoff.model === this.model &&
      handoff.opened === this.model.opened
      ? handoff.use
      : null;
  }
  set lastUse(use: HttpActionUse | null) {
    this.handoff = use
      ? { use, model: this.model, opened: this.model.opened }
      : null;
  }
  private handoff: {
    use: HttpActionUse;
    model: StructuredCanvasAdapter;
    opened: number;
  } | null = null;
  /** The open "New API connection" dialog and its prefill. */
  connectionDialog: {
    kind?: "ai";
    fromBuilder: HttpActionUse["connection"] | null;
    fromWorkflow?: boolean;
  } | null = null;
  showTemplates = false;
  constructor() {
    history.replaceState(
      { ...history.state, weavePosition: this.historyPosition },
      "",
    );
    this.router.events.subscribe((event) => {
      if (!(event instanceof NavigationEnd)) return;
      const navigation = this.router.currentNavigation();
      if (
        navigation?.trigger !== "imperative" ||
        navigation.extras.skipLocationChange
      )
        return;
      if (!navigation.extras.replaceUrl && location.href !== this.historyUrl)
        this.historyPosition++;
      this.historyUrl = location.href;
      history.replaceState(
        { ...history.state, weavePosition: this.historyPosition },
        "",
      );
    });
    this.api.onSessionEnded = () => this.sessionEnded();
    this.loginWatcher.subscribe((event) => this.backgroundLogin(event));
    afterEveryRender(() => {
      const title = this.pageTitle();
      if (this.title.getTitle() !== title) this.title.setTitle(title);
      if (this.viewFocusPending) this.focusView();
    });
    void this.initialize();
  }
  /** "Runs | Firefly Weave Studio": the page title names the view. */
  pageTitle() {
    const suffix = "Firefly Weave Studio";
    if (!this.paired || this.connecting) return suffix;
    if (this.view === "designer")
      return `${this.model.definition.metadata.name} | Designer | ${suffix}`;
    if (this.view === "connect") return `Connect to a platform | ${suffix}`;
    const item = this.nav.find((n) => n.id === this.view);
    return item ? `${item.label} | ${suffix}` : suffix;
  }
  /**
   * After a view change the new view's heading takes focus, so keyboard and
   * screen-reader users start at the top of what changed. Focus that already
   * moved somewhere on purpose (a dialog, a sheet, a step) stays there; the
   * connection wizard focuses its own heading.
   */
  private focusView(attempt = 0) {
    const active = document.activeElement;
    const moved =
      active &&
      active !== document.body &&
      active !== this.focusBeforeView &&
      active.isConnected;
    if (moved || this.view === "connect") {
      this.viewFocusPending = false;
      return;
    }
    const heading = document.querySelector<HTMLElement>("#main h1");
    if (!heading) {
      // A lazily loaded view renders its heading a moment later.
      if (attempt < 20)
        setTimeout(() => {
          if (this.viewFocusPending) this.focusView(attempt + 1);
        }, 50);
      else this.viewFocusPending = false;
      return;
    }
    this.viewFocusPending = false;
    if (!heading.hasAttribute("tabindex")) heading.tabIndex = -1;
    heading.focus({ preventScroll: true });
  }
  /** The skip link: focus the main content without a router navigation. */
  skipToMain(event: Event) {
    event.preventDefault();
    const main = document.getElementById("main");
    const heading = main?.querySelector<HTMLElement>("h1");
    const target = heading ?? main;
    if (!target) return;
    if (target === heading && !heading.hasAttribute("tabindex"))
      heading.tabIndex = -1;
    target.focus();
  }
  /** Shows an outcome as a toast; the toast itself is the live announcement. */
  notify(
    text: string,
    action?: ToastAction,
    tone: "neutral" | "danger" = "neutral",
  ) {
    this.message = "";
    this.toasts.show({ text, action, tone });
    this.cdr.markForCheck();
  }
  async initialize() {
    try {
      const s = await this.api.pair();
      this.paired = s.paired;
      if (s.paired) {
        const designer = designerPath.exec(location.pathname);
        const initial = location.pathname.split("/")[1] as View;
        if (!designer && this.nav.some((n) => n.id === initial))
          this.view = initial;
        // Local authoring is usable at once; the platform check runs behind it.
        this.connecting = false;
        this.cdr.markForCheck();
        // The designer's code loads in the background once Studio is idle,
        // so opening a workflow later shows it at once.
        setTimeout(
          () => void import("./designer/designer-view").catch(() => undefined),
          1500,
        );
        // A reload or a link to a workflow brings it back first.
        if (designer) await this.restoreWorkflow(designer[1]);
        await this.verifyPlatform(true);
        if (this.view !== "designer") await this.refresh();
      }
    } catch (e) {
      this.fail(e);
    } finally {
      this.connecting = false;
      this.cdr.markForCheck();
    }
  }
  async pair() {
    if (!this.pairCode.trim() || this.busy) return;
    this.busy = "pair";
    try {
      await this.api.pair(this.pairCode.trim());
      this.paired = true;
      this.pairCode = "";
      this.error = "";
      this.errorCode = "";
      this.sessionNotice = "";
      this.connecting = false;
      this.cdr.markForCheck();
      await this.verifyPlatform(true);
      // A workflow kept from before the session ended needs the catalog again.
      if (this.selected?.step.kind === "action") void this.loadActionCatalog();
      await this.refresh();
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  /**
   * The host no longer recognizes this window: return to pairing, or let the
   * desktop shell re-pair. Designer edits exist only in this window, so they
   * survive pairing again and are never discarded by an automatic reload.
   */
  sessionEnded() {
    if (!this.paired || this.sessionLost) return;
    this.loginWatcher.stop();
    const desktop = isDesktopShell();
    if (desktop && !this.dirty && !this.recentRepair()) {
      location.reload();
      return;
    }
    this.clearPlatformState();
    if (desktop && this.dirty) {
      this.sessionLost = true;
      this.error = sessionLostMessage;
      this.errorCode = "WV-STUDIO-SESSION";
      this.cdr.markForCheck();
      return;
    }
    this.paired = false;
    this.error = "";
    this.errorCode = "";
    this.sessionNotice = desktop
      ? "Your Studio session ended and the desktop app could not pair again automatically. Quit and reopen Firefly Weave Studio."
      : "Your Studio session ended. Sessions last up to eight hours and end when another browser pairs. Restart weave studio in your terminal for a new pairing code, then enter it here.";
    this.cdr.markForCheck();
  }
  /** Records a desktop re-pairing reload; true when one already happened in the last minute. */
  private recentRepair() {
    try {
      const key = "weave-studio-repair";
      const last = Number(sessionStorage.getItem(key) ?? 0);
      if (Date.now() - last < 60000) return true;
      sessionStorage.setItem(key, String(Date.now()));
    } catch {
      // Without storage a reload loop cannot be ruled out, so do not reload.
      return true;
    }
    return false;
  }
  get profile() {
    return this.api.session.profile;
  }
  /**
   * Layout for the current tick: nodes, empty-branch placeholders, targets,
   * links, boundaries and the drawing size are computed once per change.
   */
  get nodes() {
    const revision = this.tick();
    if (revision !== this.cachedTick) {
      this.cachedTick = revision;
      const layout = this.model.canvasLayout();
      this.cachedNodes = layout.nodes;
      this.visualLinks = layout.edges;
      this.cachedBoundaries = layout.boundaries;
      this.cachedTargets = layout.targets;
      this.cachedLaneGroups = layout.groups;
      this.cachedLaneTerminals = layout.terminals;
      this.cachedJunctions = layout.junctions;
      this.placeholderByOwner = new Map(
        layout.placeholders.map((p) => [p.owner, p]),
      );
      this.nodeById = new Map(this.cachedNodes.map((n) => [n.step.id, n]));
      const points = [
        ...this.cachedNodes.map((n) => n.point),
        ...[...this.placeholderByOwner.values()].map((p) => p.point),
        this.cachedBoundaries.end,
      ];
      this.graphSize = {
        width: Math.max(1000, ...points.map((p) => p.x)) + 320,
        height: Math.max(1000, ...points.map((p) => p.y)) + 240,
      };
    }
    return this.cachedNodes;
  }
  get targets() {
    this.nodes;
    return this.cachedTargets;
  }
  get laneGroups() {
    this.nodes;
    return this.cachedLaneGroups;
  }
  get laneTerminals() {
    this.nodes;
    return this.cachedLaneTerminals;
  }
  /** Edge paths; recomputed when the workflow changes or a node is dragged. */
  get edgePaths() {
    this.nodes;
    const cache = this.edgeCache;
    if (cache.tick !== this.cachedTick || cache.drag !== this.dragNode)
      this.edgeCache = {
        tick: this.cachedTick,
        drag: this.dragNode,
        paths: this.visualLinks.map((link) => ({
          key: `${link.from}>${link.to}`,
          from: link.from,
          d: this.connectionPath(this.model, link.from, link.to, this.nodes),
        })),
      };
    return this.edgeCache.paths;
  }
  /** Published actions offered by the step picker. */
  get pickerActions(): PickerAction[] {
    if (this.pickerCache.source !== this.actionVersions)
      this.pickerCache = {
        source: this.actionVersions,
        actions: this.actionVersions.map((item) => ({
          name: String(item["name"] ?? ""),
          version: String(item["version"] ?? ""),
          ...(item["description"]
            ? { description: String(item["description"]) }
            : {}),
        })),
      };
    return this.pickerCache.actions;
  }
  /** Why the picker lists no published actions, in plain words. */
  get pickerActionsNote() {
    if (!this.profile)
      return "Connect to a platform to insert published actions.";
    if (this.identity && !this.can("catalog.read"))
      return "Your account can't read the published actions in this workspace.";
    if (this.catalogState === "error")
      return "Published actions couldn't be loaded.";
    if (this.catalogState === "ready" && !this.actionVersions.length)
      return "This project has no published actions yet.";
    return "";
  }
  get selected() {
    return this.nodes.find((n) => n.step.id === this.model.selected);
  }
  /**
   * The palette's groups (Logic, Data, Waiting, People, Actions) with the
   * kinds that match the search: names, descriptions and everyday words.
   */
  get palette() {
    const terms = this.paletteQuery.toLowerCase().split(/\s+/).filter(Boolean);
    const fits = (kind: Kind) => {
      const text = [
        stepKindLabels[kind],
        stepKindDescriptions[kind],
        stepKindKeywords[kind],
      ]
        .join(" ")
        .toLowerCase();
      return terms.every((term) => text.includes(term));
    };
    const key = this.paletteQuery;
    if (this.paletteCache.key !== key || !this.paletteCache.groups)
      this.paletteCache = {
        key,
        groups: stepKindGroups
          .map((group) => ({ ...group, kinds: group.kinds.filter(fits) }))
          .filter((group) => group.kinds.length || group.label === "Actions"),
      };
    return this.paletteCache.groups;
  }
  private paletteCache: {
    key: string;
    groups: { label: string; kinds: Kind[] }[] | null;
  } = { key: "", groups: null };
  get visibleRecords() {
    const query = this.view === "runs" ? "" : this.search.toLowerCase();
    return this.records.filter(
      (r) =>
        (!this.taskRunFilter ||
          this.view !== "tasks" ||
          r["run_id"] === this.taskRunFilter) &&
        (!query || JSON.stringify(r).toLowerCase().includes(query)),
    );
  }
  label(kind: string) {
    return stepKindLabels[kind as Kind] ?? kind;
  }
  description(kind: string) {
    return stepKindDescriptions[kind as Kind] ?? "";
  }
  value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  changed() {
    this.tick.update((n) => n + 1);
    if (!this.committingInspector) {
      this.workflowProperties = structuredClone(this.model.definition);
      this.workflowBuffer = JSON.stringify(this.model.definition);
      this.touched.workflow = false;
    }
    this.sourceBuffer = this.model.source;
    this.saveState = "Unsaved";
    this.workflowOpen = true;
    this.lastEdit = new Date().toISOString();
    this.keepLocally();
    // Diagnostics stay until the next check; layout-only changes keep them.
    this.checkLater();
    if (
      this.model.selected &&
      window.innerWidth >= 768 &&
      this.tab === "Designer"
    )
      this.showInspector = true;
    // A field commit keeps the mounted controls and their invalid local drafts.
    if (this.committingInspector) {
      this.inspectorBase = this.selected
        ? JSON.stringify(this.selected.step)
        : "";
      return;
    }
    // Layout-only or unrelated changes keep local inspector edits.
    const step = this.selected?.step;
    if (
      !step ||
      step.id !== this.inspectorStepId ||
      JSON.stringify(step) !== this.inspectorBase
    )
      this.loadInspector();
  }
  /** Loads the workflow settings fields from the model, dropping their edits. */
  private loadWorkflowSettings() {
    this.workflowProperties = structuredClone(this.model.definition);
    this.workflowBuffer = JSON.stringify(this.model.definition);
    this.workflowValid.set(true);
    this.touched.workflow = false;
  }
  private inspectorTopWatch: ResizeObserver | null = null;
  /**
   * Scrolls the inspector to its top. A hidden overlay inspector is scrolled
   * once it shows again, because showing it restores its old position.
   */
  private inspectorToTop() {
    this.inspectorTopWatch?.disconnect();
    this.inspectorTopWatch = null;
    const body = document.querySelector<HTMLElement>(".inspector-body");
    if (!body) return;
    body.scrollTo(0, 0);
    if (body.clientHeight > 0 || typeof ResizeObserver === "undefined") return;
    const watch = new ResizeObserver(() => {
      if (body.clientHeight === 0) return;
      body.scrollTo(0, 0);
      watch.disconnect();
      if (this.inspectorTopWatch === watch) this.inspectorTopWatch = null;
    });
    this.inspectorTopWatch = watch;
    watch.observe(body);
  }
  /** Loads the inspector buffers from the selected model step. */
  loadInspector() {
    this.inspectorSession++;
    this.cancelInspectorCommit();
    this.invalidInspectorFields.clear();
    this.actionChoiceRevision = null;
    this.renameDraft = this.selected?.step.id ?? "";
    this.renameError = "";
    this.touched.step = false;
    const step = this.selected?.step;
    // Another step (or the workflow settings) starts at the top of the
    // inspector, not where the previous one was scrolled to. After the next
    // render: a narrow-layout inspector may still be hidden now, and the
    // browser restores a hidden pane's old scroll position when it shows.
    if ((step?.id ?? "") !== this.inspectorStepId)
      afterNextRender(() => this.inspectorToTop(), {
        injector: this.injector,
      });
    this.inspectorStepId = step?.id ?? "";
    this.inspectorBase = step ? JSON.stringify(step) : "";
    this.propertyStep = step ? structuredClone(step) : null;
    this.inspectorBuffer = step ? JSON.stringify(step, null, 2) : "";
    this.propertyValid.set(true);
    this.actionInputValid.set(true);
    this.advancedProperties = false;
    this.slotError = "";
    this.newSlotName = "";
    this.newSlotNameEdited = false;
    if (step?.kind !== "action") {
      this.actionContract = null;
      if (
        this.nodes.some(
          (node) => node.step.kind === "action" || node.step.kind === "llm",
        )
      )
        void this.loadActionCatalog();
      return;
    }
    this.actionContract = null;
    this.actionInputMode = "expression";
    this.actionMissing = [];
    void this.loadActionCatalog();
    this.syncActionContract();
  }
  perform(fn: () => void) {
    try {
      fn();
      this.dirty = true;
      this.changed();
      this.error = "";
      this.errorCode = "";
    } catch (e) {
      this.fail(e);
    }
  }
  fail(e: unknown) {
    const plain = describeError(e);
    // An ended session already returned to pairing with its own explanation.
    if (plain.code === "WV-STUDIO-SESSION" && !this.paired) return;
    if (this.platformSignInEnded(plain)) return;
    // A desktop window that kept its edits keeps explaining how to recover.
    this.error =
      plain.code === "WV-STUDIO-SESSION" && this.sessionLost
        ? sessionLostMessage
        : plain.message;
    this.errorCode = plain.code;
    this.message = "";
  }
  /**
   * A platform call found the sign-in gone (the provider ended it, or the
   * account isn't linked): a calm notice with "Sign in again" replaces the
   * error, and local work carries on. Pairing problems keep their own path.
   */
  private platformSignInEnded(plain: PlainError) {
    if (
      plain.status !== 401 ||
      !this.profile ||
      plain.code.startsWith("WV-STUDIO-")
    )
      return false;
    // After a sign-out the platform refuses calls as expected: Studio stays
    // "Signed out" instead of reporting an expired session.
    if (plain.code === "WV-AUTH-NOT-LINKED" || !this.platformSignedOut)
      this.platformNotice = {
        kind: plain.code === "WV-AUTH-NOT-LINKED" ? "not-linked" : "expired",
        name: this.profile.name,
      };
    this.error = "";
    this.errorCode = "";
    this.message = "";
    void this.refreshConnectionStatus();
    return true;
  }
  async navigate(view: View) {
    if (
      this.view === "designer" &&
      view !== "designer" &&
      !this.leaveInspector()
    )
      return;
    if (
      this.view === "designer" &&
      view !== "designer" &&
      this.dirty &&
      this.saveState === "Unsaved" &&
      !(await this.confirmLeave())
    )
      return;
    if (view !== "connect") this.releaseWizard();
    this.platformMenuOpen = false;
    // Another list's rows never show under this view's heading while it loads.
    if (view !== this.view) {
      this.records = [];
      this.nextCursor = null;
      this.taskRunFilter = "";
    }
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
  /**
   * Leaving a workflow with edits. A local one is kept on this computer, so
   * the question says where to find it again.
   */
  private async confirmLeave() {
    if (this.profile || this.draftRevision !== undefined || this.published)
      return this.dialogs.confirm({
        title: "Leave the designer?",
        message:
          "Your edits stay in this Studio window, but they are not saved to a platform. Save a draft, or use Save to file, to keep a copy.",
        confirmLabel: "Leave designer",
        cancelLabel: "Keep editing",
      });
    this.flushLocalSave();
    return this.dialogs.confirm({
      title: `Leave ${this.model.definition.metadata.name}?`,
      message: this.localDrafts.failed
        ? "Studio can't keep drafts in this browser. Save to file to keep your work."
        : this.desktopShell
          ? "It's kept under Workflows › On this computer until you quit Firefly Weave Studio. Save to file to keep a copy."
          : "It's kept on this computer. You'll find it under Workflows › On this computer. Save to file to keep a copy you can share.",
      confirmLabel: "Leave",
      cancelLabel: "Keep editing",
    });
  }
  // --- local drafts -----------------------------------------------------
  /** Autosave: local work is kept on this computer 800 ms after a change. */
  private keepLocally() {
    if (this.profile) return;
    if (this.localTimer) clearTimeout(this.localTimer);
    this.localTimer = setTimeout(() => this.flushLocalSave(true), 800);
  }
  /** Saves the open workflow now when an autosave is waiting. */
  flushLocalSave(due = false) {
    if (!this.localTimer) return;
    if (!due) clearTimeout(this.localTimer);
    this.localTimer = null;
    if (this.profile || !this.dirty) return;
    const savedAt = new Date().toISOString();
    const { name, version } = this.model.definition.metadata;
    const saved = this.localDrafts.save(
      { id: this.draftId, name, version, savedAt },
      {
        source: this.model.source,
        format: this.model.format,
        layout: this.model.layout,
        savedAt,
      },
    );
    if (!saved) this.storageFailed();
    else {
      this.keptLocally = true;
      this.draftSavedAt = savedAt;
      if (this.view === "workflows" || this.view === "home")
        this.refreshLocalList();
    }
    // The status chip moves from "Saving…" to "Kept on this computer".
    this.cdr.markForCheck();
  }
  /** Storage refused a draft: one danger notice explains the way out. */
  private storageFailed() {
    if (!this.storageNoticeShown) {
      this.storageNoticeShown = true;
      this.storageNotice = true;
    }
    this.cdr.markForCheck();
  }
  refreshLocalList() {
    const list = this.localDrafts.list();
    this.localList = list ?? [];
    if (!list) this.storageFailed();
    this.cdr.markForCheck();
  }
  /** A workflow was opened in this window (new, imported, from a list). */
  private workflowOpen = false;
  /** When the open workflow last changed. */
  private lastEdit = "";
  /**
   * Home's "Continue editing": the workflow open in this window, otherwise
   * the newest one kept on this computer.
   */
  get resume(): { name: string; savedAt: string; open: boolean } | null {
    const latest = this.localList[0];
    const next =
      this.workflowOpen && this.lastEdit
        ? {
            name: this.model.definition.metadata.name,
            savedAt: this.lastEdit,
            open: true,
          }
        : latest
          ? { name: latest.name, savedAt: latest.savedAt, open: false }
          : null;
    // The same object while nothing changed, so the binding stays stable.
    const kept = this.resumeView;
    if (
      kept === next ||
      (kept &&
        next &&
        kept.name === next.name &&
        kept.savedAt === next.savedAt &&
        kept.open === next.open)
    )
      return kept;
    return (this.resumeView = next);
  }
  private resumeView: { name: string; savedAt: string; open: boolean } | null =
    null;
  continueEditing() {
    if (this.resume?.open) void this.navigate("designer");
    else if (this.localList[0]) this.openLocalDraft(this.localList[0].id);
  }
  /** Edits not yet kept anywhere: on this computer or on the platform. */
  get unsavedWork() {
    if (this.inspectorProblem) return true;
    if (!this.dirty) return false;
    if (this.profile || this.draftRevision !== undefined || this.published)
      return true;
    return !!this.localTimer || this.localDrafts.failed;
  }
  /** The reload guard: the browser asks before edits that aren't kept go. */
  @HostListener("window:beforeunload", ["$event"])
  beforeUnload(event: BeforeUnloadEvent) {
    if (!this.paired) return;
    this.flushInspector();
    this.flushLocalSave();
    if (!this.unsavedWork) return;
    event.preventDefault();
    event.returnValue = "";
  }
  /** Opens a workflow kept on this computer; false when it isn't there. */
  openLocalDraft(id: string): boolean {
    const document = this.localDrafts.read(id);
    if (!document) {
      if (this.localDrafts.failed) this.storageFailed();
      return false;
    }
    this.flushLocalSave();
    this.importGeneration++;
    const model = new StructuredCanvasAdapter();
    model.setSource(document.source, document.format);
    model.clearHistory();
    const layout = document.layout as Record<string, unknown> | null;
    if (isRecord(layout) && isRecord(layout["positions"]))
      model.layout = {
        ...model.layout,
        ...(structuredClone(layout) as Partial<typeof model.layout>),
      };
    // The viewport comes back with the workflow, unless it is the default.
    const kept = usableViewport(model.layout.viewport);
    this.restoredView =
      !!kept && (kept.zoom !== 1 || kept.pan.x !== 0 || kept.pan.y !== 0);
    this.keptLocally = true;
    this.draftSavedAt = document.savedAt;
    this.model = model;
    this.draftId = id;
    this.draftRevision = undefined;
    this.published = null;
    this.activation = null;
    this.view = "designer";
    this.openTab();
    void this.router.navigateByUrl(`/workflows/${id}/designer`);
    this.resetValidation();
    this.changed();
    this.dirty = false;
    this.scheduleFit();
    this.cdr.markForCheck();
    return true;
  }
  /**
   * A deep link or a reload of /workflows/:id/designer: the workflow comes
   * back from this computer, or from the platform's draft when connected.
   */
  async restoreWorkflow(id: string): Promise<boolean> {
    if (this.openLocalDraft(id)) return true;
    if (this.profile)
      try {
        const detail = await this.api.request<Record<string, unknown>>(
          `${this.api.project}/drafts/${encodeURIComponent(id)}`,
        );
        const definition = detail["definition"] ?? detail["document"];
        if (isRecord(definition)) {
          this.openPlatformDraft(detail, definition as unknown as Workflow);
          return true;
        }
      } catch {
        // Not on the platform either: explained below.
      }
    await this.navigate("workflows");
    this.notify(
      "That workflow isn't available. It may have been deleted, or it belongs to another workspace.",
      undefined,
      "danger",
    );
    return false;
  }
  /** Opens a platform draft in the designer. */
  private openPlatformDraft(
    detail: Record<string, unknown>,
    document: Workflow,
  ) {
    this.flushLocalSave();
    this.model.replace(document);
    this.resetValidation();
    this.changed();
    this.dirty = false;
    this.view = "designer";
    this.activation = null;
    this.draftId = String(detail["id"]);
    this.draftRevision = Number(detail["revision"]);
    this.published = null;
    this.saveState = "Draft saved";
    this.openTab();
    void this.router.navigateByUrl(`/workflows/${this.draftId}/designer`);
    this.scheduleFit();
  }
  /** "Delete from this computer", with Undo. */
  async deleteLocalDraft(entry: LocalDraftEntry) {
    if (
      !(await this.dialogs.confirm({
        title: `Delete ${entry.name} from this computer?`,
        message:
          "Studio removes this draft from this browser. Files you saved stay where they are.",
        confirmLabel: "Delete",
        danger: true,
      }))
    )
      return;
    const removed = this.localDrafts.remove(entry.id);
    if (!removed) {
      if (this.localDrafts.failed) this.storageFailed();
      return;
    }
    this.refreshLocalList();
    this.notify(`Deleted ${entry.name} from this computer.`, {
      label: "Undo",
      run: () => {
        if (!this.localDrafts.restore(removed)) return this.storageFailed();
        this.refreshLocalList();
        this.notify(`Restored ${entry.name}.`);
      },
    });
  }
  newWorkflow() {
    if (this.view === "designer" && !this.leaveInspector()) return;
    this.flushLocalSave();
    this.importGeneration++;
    this.keptLocally = false;
    this.restoredView = false;
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
    this.openTab();
    this.published = null;
    this.activation = null;
    this.resetValidation();
    this.changed();
    this.dirty = false;
    this.scheduleFit();
  }
  /**
   * Selects a step and shows it in the inspector. `reveal: false` selects
   * without opening the inspector (focus moving through the canvas).
   */
  async select(node: Node, reveal = true) {
    if (this.suppressNodeClick) {
      this.suppressNodeClick = false;
      return;
    }
    // Selecting the step already in the inspector keeps its unapplied edits.
    if (
      this.model.selected === node.step.id &&
      this.inspectorStepId === node.step.id
    ) {
      if (reveal) {
        this.showInspector = true;
        this.revealSelected();
      }
      return;
    }
    if (!this.leaveInspector()) return;
    if (reveal) this.showInspector = true;
    this.message = `Selected ${node.step.id}, ${this.label(node.step.kind)}, ${ownerLabel(node.owner, this.stepsById()).replace(/^Main sequence$/, "main sequence")}.`;
    this.model.selected = node.step.id;
    this.loadInspector();
    if (reveal) this.revealSelected();
    this.cdr.markForCheck();
  }
  private stepsById() {
    this.nodes;
    return new Map([...this.nodeById].map(([id, n]) => [id, n.step]));
  }
  /**
   * Keyboard focus reached a step: it becomes the selection (not while the
   * inspector holds edits for another step) and pans into view.
   */
  nodeFocused(node: Node) {
    // Keyboard navigation supersedes the import's still-pending initial fit.
    this.needsFit = false;
    this.afterFocusScroll(() => this.revealStep(node.step.id));
    if (this.model.selected !== node.step.id && !this.editsPending)
      void this.select(node, this.windowWidth > 767 && this.showInspector);
  }
  /** Enter on a step opens it in the inspector and moves focus there. */
  nodeKey(event: KeyboardEvent, node: Node) {
    if (event.key !== "Enter" || event.metaKey || event.ctrlKey) return;
    event.preventDefault();
    void this.select(node).then(() => {
      if (this.model.selected !== node.step.id) return;
      this.focusLater(
        () =>
          document.querySelector<HTMLElement>("#step-name-input") ??
          document.querySelector<HTMLElement>(".inspector-header h2"),
      );
    });
  }
  async insert(kind: Kind) {
    this.showPalette = false;
    if (this.editingLocked || !(await this.ensureApplied())) return;
    const n = this.selected;
    this.perform(() =>
      this.model.insert(kind, n?.owner ?? "root", n ? n.index + 1 : undefined),
    );
    if (!this.error) this.revealSelected();
    this.cdr.markForCheck();
  }
  /** What unapplied inspector edits belong to: a step, the workflow settings, or nothing. */
  get editsPending(): "step" | "workflow" | "" {
    if (this.model.readonly) return "";
    // An invalid value counts only once the person has edited the inspector:
    // a step stored with a missing or invalid value has no unapplied edits.
    if (this.selected)
      return this.inspectorDirty ||
        (this.touched.step &&
          (!this.propertyValid() ||
            (this.actionInputMode === "fields" &&
              !!this.actionContract &&
              !this.actionInputValid())))
        ? "step"
        : "";
    if (this.touched.workflow && !this.workflowValid()) return "workflow";
    try {
      return canonicalJson(JSON.parse(this.workflowBuffer)) !==
        canonicalJson(this.model.definition)
        ? "workflow"
        : "";
    } catch {
      return "workflow";
    }
  }
  /**
   * Notes that the person edited the step or workflow fields (typing, a
   * choice, or a button inside them), so an invalid value there is theirs.
   */
  touchInspector(scope: "step" | "workflow", event?: Event) {
    if (
      event &&
      !(event.target as HTMLElement | null)?.closest?.("button, [role=button]")
    )
      return;
    this.touched[scope] = true;
  }
  /** A specialized inspector may own an explicit Apply/Cancel draft. */
  inspectorDraftGuard: (() => string) | null = null;
  private invalidInspectorFields = new Set<string>();
  async ensureApplied(): Promise<boolean> {
    return this.leaveInspector();
  }
  private cancelInspectorCommit() {
    if (this.inspectorTimer) clearTimeout(this.inspectorTimer);
    this.inspectorTimer = null;
    this.pendingInspector = null;
  }
  private queueInspector(
    scope: "step" | "workflow" | "rename",
    text: string,
    key: string,
    delay = 300,
  ) {
    if (this.editingLocked || this.model.readonly) return;
    if (this.pendingInspector && this.pendingInspector.key !== key)
      this.flushInspector();
    this.cancelInspectorCommit();
    this.pendingInspector = {
      scope,
      text,
      key,
      id: this.model.selected,
      opened: this.model.opened,
    };
    this.inspectorTimer = setTimeout(() => this.flushInspector(), delay);
  }
  flushInspector() {
    const pending = this.pendingInspector;
    this.cancelInspectorCommit();
    if (!pending || pending.opened !== this.model.opened || this.editingLocked)
      return;
    this.committingInspector = true;
    try {
      // Explicit field and schema row reordering must survive a commit.
      const compare = JSON.stringify;
      if (pending.scope === "rename") {
        if (pending.id === pending.text) return;
        this.model.renameStep(pending.id, pending.text);
        this.inspectorStepId = this.model.selected;
        this.renameDraft = this.model.selected;
        // Renaming must not remount the grid and erase invalid sibling text.
        try {
          const draft = this.bufferStep();
          draft.id = this.model.selected;
          this.inspectorBuffer = JSON.stringify(draft, null, 2);
        } catch {
          // Advanced JSON may still contain an unfinished edit.
        }
      } else if (pending.scope === "step") {
        const step = this.model
          .nodes()
          .find((node) => node.step.id === pending.id)?.step;
        if (!step || compare(step) === compare(JSON.parse(pending.text)))
          return;
        const next = JSON.parse(pending.text) as Step;
        const answers = step["decisions"];
        const nextAnswers = next["decisions"];
        if (
          pending.key === "decisions" &&
          step.kind === "humanTask" &&
          Array.isArray(answers) &&
          Array.isArray(nextAnswers) &&
          answers.length === nextAnswers.length
        ) {
          this.model.batch(() => {
            for (let index = 0; index < answers.length; index++)
              if (answers[index] !== nextAnswers[index])
                this.model.renameDecision(
                  pending.id,
                  answers[index],
                  nextAnswers[index],
                );
            this.model.update(pending.id, pending.text);
          });
        } else if (pending.continueRevision !== undefined)
          this.model.continueEdit(pending.continueRevision, () =>
            this.model.update(pending.id, pending.text),
          );
        else this.model.update(pending.id, pending.text);
      } else {
        const value = JSON.parse(pending.text) as Workflow;
        if (compare(value) === compare(this.model.definition)) return;
        this.model.updateWorkflow(value);
      }
      this.dirty = true;
      this.changed();
    } catch (error) {
      this.renameError = describeError(error).message;
    } finally {
      this.committingInspector = false;
      this.cdr.markForCheck();
    }
  }
  inspectorFieldValidity(change: { path: string; valid: boolean }) {
    if (change.valid) this.invalidInspectorFields.delete(change.path);
    else this.invalidInspectorFields.add(change.path);
    if (!change.valid && this.pendingInspector?.key === change.path)
      this.cancelInspectorCommit();
  }
  inspectorInteraction(event: Event) {
    const target = event.target as HTMLElement;
    if (
      event.type === "change" &&
      target.matches("select, input[type=checkbox], input[type=radio]")
    )
      this.flushInspector();
    if (
      event.type === "focusout" &&
      target.closest("[data-field], [data-path]") !==
        ((event as FocusEvent).relatedTarget as HTMLElement | null)?.closest(
          "[data-field], [data-path]",
        )
    )
      this.flushInspector();
    if (event.type === "click" && target.closest("button, [role=option]"))
      this.flushInspector();
  }
  workflowEdit(change: { value: unknown; path: string }) {
    this.workflowBuffer = JSON.stringify(change.value);
    this.queueInspector("workflow", this.workflowBuffer, change.path);
  }
  editInspectorJson(event: Event) {
    this.inspectorBuffer = this.value(event);
    this.touched.step = true;
    try {
      const step = JSON.parse(this.inspectorBuffer) as Step;
      if (
        step.id !== this.selected?.step.id ||
        step.kind !== this.selected?.step.kind
      )
        throw Error("Keep the step name and kind unchanged here.");
      this.propertyValid.set(true);
      this.queueInspector("step", this.inspectorBuffer, "advanced");
    } catch {
      this.propertyValid.set(false);
      this.cancelInspectorCommit();
    }
  }
  private get inspectorProblem(): string {
    return (
      this.inspectorDraftGuard?.() ||
      (this.renameError ||
      this.invalidInspectorFields.size ||
      this.inspectorDirty ||
      (this.touched.step &&
        this.actionInputMode === "fields" &&
        !!this.actionContract &&
        !this.actionInputValid()) ||
      (!this.selected && this.editsPending)
        ? "Fix the invalid fields before continuing. Your edits are still here."
        : "")
    );
  }
  private inspectorNotice = "";
  private leaveInspector(): boolean {
    this.flushInspector();
    const problem = this.inspectorProblem;
    if (!problem) {
      if (this.error === this.inspectorNotice) this.error = "";
      this.inspectorNotice = "";
      return true;
    }
    this.inspectorNotice = problem;
    this.error = problem;
    this.errorCode = "";
    this.showInspector = true;
    this.cdr.markForCheck();
    return false;
  }
  closeInspector() {
    this.flushInspector();
    this.showInspector = false;
  }
  /** Returns the inspector to the workflow settings. */
  async deselect() {
    if (!this.model.selected) return true;
    if (!this.leaveInspector()) return false;
    this.model.selected = "";
    this.loadInspector();
    this.loadWorkflowSettings();
    this.message = "Showing the workflow settings.";
    this.cdr.markForCheck();
    return true;
  }
  async openWorkflowSettings() {
    if (await this.deselect()) this.showInspector = true;
    this.cdr.markForCheck();
  }
  /** Remembers a press on empty canvas (not on a step, target or control). */
  canvasPointerDown(event: PointerEvent) {
    const target = event.target as HTMLElement | null;
    this.canvasPress =
      event.button === 0 &&
      !target?.closest?.(
        "button, a, input, select, textarea, .graph-node, .canvas-tools",
      )
        ? { x: event.clientX, y: event.clientY }
        : null;
  }
  /** A click on empty canvas (moved less than 4 px) shows the workflow settings. */
  canvasPointerUp(event: PointerEvent) {
    const press = this.canvasPress;
    this.canvasPress = null;
    if (
      press &&
      Math.hypot(event.clientX - press.x, event.clientY - press.y) < 4
    )
      void this.deselect();
  }
  /** Shows the inspector and moves focus to the first field with a problem. */
  private focusInvalidField() {
    this.showInspector = true;
    this.message = "Fix the highlighted field, then try again.";
    this.cdr.markForCheck();
    afterNextRender(
      () =>
        // After the closing dialog has returned focus to its opener.
        setTimeout(() => {
          const inspector = document.querySelector(".inspector");
          const problem = inspector?.querySelector<HTMLElement>(
            ".property-error, .action-input-form :invalid, [aria-invalid='true']",
          );
          const field = problem?.matches("input, select, textarea")
            ? problem
            : problem
                ?.closest("td, .value-editor, .expression-editor, label")
                ?.querySelector<HTMLElement>("input, select, textarea");
          field?.focus();
        }),
      { injector: this.injector },
    );
  }
  /** Why a platform command is unavailable, or "" when it can run. */
  blocker(command: Command): string {
    const verb = {
      save: "save drafts",
      publish: "publish workflows",
      simulate: "run simulations",
      activate: "activate versions",
      run: "start runs",
    }[command];
    if (!this.profile) return `Connect to a platform to ${verb}.`;
    if (!this.identity)
      return "Studio is still checking what your account can do.";
    if (command === "activate" && !this.published)
      return "Publish this version before activating it.";
    const capability = {
      save: "definition.write",
      publish: "definition.publish",
      simulate: "simulate",
      activate: "release.activate",
      run: "run.start",
    }[command];
    if (!this.can(capability))
      return `Your account can't ${verb} in this workspace.`;
    if (command === "run" && this.view === "designer" && !this.activation)
      return "Activate this version to start runs.";
    if (
      this.model.readonly &&
      (command === "save" || command === "publish" || command === "simulate")
    )
      return "Fix the workflow source first.";
    if (this.busy !== "" || (this.unknownCommand && command !== "simulate"))
      return "Wait for the current command to finish.";
    return "";
  }
  /** Where the open workflow is: local, unsaved, saved, published or active. */
  get lifecycle(): Lifecycle {
    if (!this.profile) return "local";
    if (this.dirty) return "unsaved";
    if (this.activation) return "active";
    if (this.published) return "published";
    if (this.draftRevision !== undefined) return "saved";
    return "unsaved";
  }
  /** The one primary command: the next step of the lifecycle. */
  get primaryCommand(): Command | "export" {
    const state = this.lifecycle;
    return state === "local" ? "export" : nextCommand[state];
  }
  /** What the primary button says, "Publishing…" while it runs. */
  get primaryLabel() {
    const command = this.primaryCommand;
    if (command === "export") return "Save to file";
    return busyLabels[this.busy] && this.busy === command
      ? busyLabels[this.busy]
      : commandLabels[command];
  }
  /** Why the primary command can't run now, or "". */
  get primaryBlocker() {
    const command = this.primaryCommand;
    return command === "export" ? "" : this.blocker(command);
  }
  /** The other lifecycle commands, offered as secondary ones in More. */
  get secondaryCommands(): Command[] {
    if (!this.profile) return [];
    const primary = this.primaryCommand;
    return (["save", "publish", "activate", "run"] as Command[]).filter(
      (command) => command !== primary,
    );
  }
  /**
   * The status chip beside the version: "Unsaved changes", "Saving…",
   * "Draft saved", "Kept on this computer", "Published 1.0.0" or
   * "Active in Production". Null for a new local workflow with no edits.
   */
  get statusChip(): { text: string; tone: string } | null {
    if (this.inspectorProblem) return { text: "Unsaved", tone: "warning" };
    const version = String(
      this.published?.["version"] ?? this.model.definition.metadata.version,
    );
    if (!this.profile) {
      if (this.localDrafts.failed) return { text: "Not saved", tone: "danger" };
      // Opening a workflow arms the autosave too; it keeps only edits.
      if ((this.dirty && this.localTimer) || this.pendingInspector)
        return { text: "Unsaved", tone: "warning" };
      if (this.dirty || this.keptLocally)
        return { text: this.savedDraftLabel(), tone: "" };
      return null;
    }
    if (this.busy === "save") return { text: "Saving…", tone: "" };
    if (this.saveState === "Not saved")
      return { text: "Not saved", tone: "danger" };
    switch (this.lifecycle) {
      case "active":
        return {
          text: `Active in ${this.workspaceNames?.environment || "this environment"}`,
          tone: "success",
        };
      case "published":
        return { text: `Published ${version}`, tone: "info" };
      case "saved":
        return { text: this.savedDraftLabel(), tone: "" };
      default:
        return { text: "Unsaved", tone: "warning" };
    }
  }
  private savedDraftLabel() {
    return this.draftSavedAt
      ? `Draft saved ${new Date(this.draftSavedAt).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false })}`
      : "Draft saved";
  }
  /** The open workflow has a copy kept on this computer. */
  private keptLocally = false;
  /**
   * Runs a lifecycle command, or says why it can't run: the reason shows as
   * a status line under the toolbar, and the button keeps focus.
   */
  async runCommand(command: Command | "export" | "validate") {
    if (command === "export") {
      this.commandNote = "";
      return this.export();
    }
    if (command === "validate") {
      if (this.busy) {
        this.commandNote = "Wait for the current command to finish.";
        return;
      }
      this.commandNote = "";
      return this.validate(true);
    }
    const reason = this.blocker(command);
    if (reason) {
      this.commandNote = reason;
      this.cdr.markForCheck();
      return;
    }
    this.commandNote = "";
    // The next step supersedes the last outcome ("Published … Activate"):
    // its toast would otherwise sit over the dialog this command opens.
    this.toasts.dismiss();
    if (command === "save") return this.save();
    if (command === "publish") return this.publish();
    if (command === "activate") return this.activate();
    if (command === "simulate") return this.simulate();
    return this.startRun();
  }
  /** The toolbar's "More" menu: secondary commands, Save to file, and on phones the rest. */
  get moreItems(): RowMenuItem[] {
    const narrow = this.windowWidth <= 767;
    const items: RowMenuItem[] = [];
    if (narrow) {
      items.push(
        {
          label: "Undo",
          disabled: !this.model.canUndo || this.editingLocked,
          run: () => this.undo(),
        },
        {
          label: "Redo",
          disabled: !this.model.canRedo || this.editingLocked,
          run: () => this.redo(),
        },
        {
          label: "Simulate",
          detail: this.blocker("simulate"),
          run: () => void this.runCommand("simulate"),
        },
      );
    }
    for (const command of this.secondaryCommands)
      items.push({
        label: commandLabels[command],
        detail: this.blocker(command),
        run: () => void this.runCommand(command),
      });
    if (this.profile)
      items.push({ label: "Save to file", run: () => void this.export() });
    if (narrow)
      items.push(
        {
          label: this.showInspector ? "Hide inspector" : "Show inspector",
          run: () => this.toggleInspector(),
        },
        {
          label: "Back to workflows",
          run: () => void this.navigate("workflows"),
        },
      );
    const key = JSON.stringify(
      items.map((i) => [i.label, i.detail, i.disabled]),
    );
    if (key !== this.moreCache.key) this.moreCache = { key, items };
    return this.moreCache.items;
  }
  private moreCache: { key: string; items: RowMenuItem[] } = {
    key: "",
    items: [],
  };
  toggleInspector() {
    if (this.showInspector) this.closeInspector();
    else this.showInspector = true;
    if (this.showInspector) this.revealSelected();
  }
  /** Simulating: the workflow can't change until the simulation stops. */
  get editingLocked() {
    return !!this.simulationSession;
  }
  /**
   * Deletes a step (the focused one for Delete and Backspace, otherwise the
   * selected one); a group with steps inside asks first. A toast offers
   * Undo, and focus moves to the next step, or the "+" left in its place.
   */
  async remove(id = this.model.selected) {
    if (!id || this.editingLocked) return;
    // Where focus goes next: the step after it, else the one before it on
    // the same branch, else the "+" left where it was.
    const node = this.nodes.find((n) => n.step.id === id);
    const siblings = this.nodes.filter((n) => n.owner === node?.owner);
    const position = siblings.findIndex((n) => n.step.id === id);
    const next =
      siblings[position + 1]?.step.id ?? siblings[position - 1]?.step.id ?? "";
    const owner = node?.owner ?? "root";
    if (!(await this.deleteSteps([id]))) return;
    this.focusLater(
      () =>
        (next
          ? document.querySelector<HTMLElement>(
              `[data-step="${CSS.escape(next)}"] :is(.node-body, .tile-body)`,
            )
          : null) ??
        document.querySelector<HTMLElement>(
          `.insertion-target[data-owner="${CSS.escape(owner)}"]`,
        ) ??
        document.querySelector<HTMLElement>(".canvas"),
    );
  }
  /**
   * Deletes one step or several as one undo step: groups with steps inside
   * ask first, and a toast offers Undo while the workflow is still as the
   * deletion left it. False when nothing was deleted.
   */
  private async deleteSteps(ids: readonly string[]): Promise<boolean> {
    const one = ids.length === 1 ? ids[0] : "";
    const contained = ids.reduce(
      (sum, id) => sum + this.model.containedSteps(id),
      0,
    );
    const inside = contained === 1 ? "1 step" : `${contained} steps`;
    if (
      contained &&
      !(await this.dialogs.confirm(
        one
          ? {
              title: `Delete ${one} and the ${inside} inside it?`,
              message:
                "The group and every step in its branches are removed from the workflow. You can undo this.",
              confirmLabel: "Delete group",
              danger: true,
            }
          : {
              title: `Delete ${ids.length} steps and the ${inside} inside them?`,
              message:
                "The groups and every step in their branches are removed from the workflow. You can undo this.",
              confirmLabel: "Delete steps",
              danger: true,
            },
      ))
    )
      return false;
    // Their inspector edits go with them.
    if (ids.includes(this.model.selected)) this.loadInspector();
    this.perform(() =>
      one
        ? this.model.remove(one, { contents: contained > 0 })
        : this.model.removeSteps(ids),
    );
    if (this.error) {
      this.cdr.markForCheck();
      return false;
    }
    const what = one || `${ids.length} steps`;
    const revision = this.model.revision;
    this.notify(`Deleted ${what}.`, {
      label: "Undo",
      run: () => {
        if (this.model.revision !== revision || !this.model.canUndo) {
          this.notify(
            `${one || "These steps"} can't come back from here: the workflow changed since. Use Undo in the toolbar.`,
          );
          return;
        }
        this.undo();
        this.notify(`Restored ${what}.`);
        if (one) this.focusStep(one);
      },
    });
    return true;
  }
  /** Makes a copy of a step right after it (the inspector's ⋯ menu). */
  async duplicate(id = this.model.selected) {
    if (!id || this.editingLocked || !(await this.ensureApplied())) return;
    let copy = "";
    this.perform(() => (copy = this.model.duplicate(id)));
    if (this.error || !copy) return;
    this.notify(`Duplicated ${id} as ${copy}.`);
    this.loadInspector();
    this.revealSelected();
    this.focusStep(copy);
  }
  /**
   * "Move to…": the step waits for a "+" on the canvas. Focus goes to the
   * first "+", whose name says where the step would go.
   */
  startMove(id = this.model.selected) {
    if (!id || this.editingLocked || this.model.readonly) return;
    this.connectingNode = id;
    if (this.windowWidth <= 767) this.showInspector = false;
    this.notify(
      `Choose where ${id} goes: select a + on the canvas. Escape cancels.`,
    );
    this.focusLater(() =>
      document.querySelector<HTMLElement>(
        ".insertion-target, .canvas-v2 [data-insert]",
      ),
    );
  }
  /** Steps nested in a group's branches, from this tick's layout. */
  containedSteps(id: string) {
    const prefix = `${id}/`;
    let count = 0;
    for (const node of this.nodes) {
      let owner = node.owner;
      // A node belongs to the group when an owner up its chain is the group.
      while (owner !== "root" && !owner.startsWith(prefix))
        owner = this.nodeById.get(owner.split("/")[0])?.owner ?? "root";
      if (owner !== "root") count++;
    }
    return count;
  }
  /** Start and End positions for this tick. */
  get boundaries() {
    this.nodes;
    return this.cachedBoundaries;
  }
  /**
   * "Step name" commits on Enter or when the field loses focus, so a typed
   * name is never dropped. The field carries the ID it was rendered for:
   * by the time it loses focus, another step may already be selected.
   */
  inputRename(event: Event) {
    const input = event.target as HTMLInputElement;
    this.renameDraft = input.value;
    const id = input.dataset["stepId"] ?? "";
    this.renameError = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(input.value)
      ? ""
      : "Use letters, numbers, dots, underscores or hyphens.";
    if (this.renameError) {
      if (this.pendingInspector?.scope === "rename")
        this.cancelInspectorCommit();
      return;
    }
    if (id && id !== input.value)
      this.queueInspector("rename", input.value, "id");
  }
  commitRename(event: Event) {
    this.inputRename(event);
    this.flushInspector();
  }
  /** Renames a step and every reference to it. */
  async renameStep(id: string, value: string) {
    this.renameError = "";
    if (!id || value.trim() === id || this.editingLocked) return;
    if (!(await this.ensureApplied())) return;
    try {
      const count = this.model.renameStep(id, value);
      this.dirty = true;
      this.changed();
      this.error = "";
      this.errorCode = "";
      this.message = `Renamed ${id} to ${value.trim()}${count ? ` and updated ${count === 1 ? "1 reference" : `${count} references`}` : ""}.`;
    } catch (e) {
      this.renameError = describeError(e).message;
    } finally {
      this.cdr.markForCheck();
    }
  }
  async renameSelected(value: string) {
    const id = this.selected?.step.id;
    if (id) await this.renameStep(id, value);
  }
  get dirtyStep() {
    return "";
  }
  /** The inspector's ⋯ menu: Move to…, Duplicate, Delete step. */
  get stepMenu(): RowMenuItem[] {
    const id = this.model.selected;
    const locked = this.model.readonly || this.editingLocked;
    const contained = id ? this.containedSteps(id) : 0;
    const items: RowMenuItem[] = [];
    if (this.selected?.step.kind === "humanTask")
      items.push({
        label: `Add a path for each answer (${this.decisionList()})`,
        disabled: locked,
        run: () => void this.branchOnDecision(),
      });
    items.push(
      { label: "Move to…", disabled: locked, run: () => this.startMove(id) },
      {
        label: "Duplicate",
        disabled: locked,
        run: () => void this.duplicate(id),
      },
      {
        label: contained
          ? `Delete group with ${contained === 1 ? "1 step" : `${contained} steps`}`
          : "Delete step",
        danger: true,
        disabled: locked,
        run: () => void this.remove(id),
      },
    );
    // The items act on this step: a new selection gets new items.
    const key = JSON.stringify([id, items.map((i) => [i.label, i.disabled])]);
    if (key !== this.stepMenuCache.key) this.stepMenuCache = { key, items };
    return this.stepMenuCache.items;
  }
  private stepMenuCache: { key: string; items: RowMenuItem[] } = {
    key: "",
    items: [],
  };
  /**
   * Inspector sections (Action, Connection, Input, Output, Properties) are
   * open unless the person closed them for this kind of step; that choice
   * is kept in this browser.
   */
  private closedSections: Record<string, string[]> = (() => {
    try {
      const value = JSON.parse(stored(inspectorSectionsKey) ?? "{}");
      return value && typeof value === "object" ? value : {};
    } catch {
      return {};
    }
  })();
  sectionOpen(section: string) {
    const kind = this.selected?.step.kind ?? "workflow";
    return !(this.closedSections[kind] ?? []).includes(section);
  }
  sectionToggled(section: string, event: Event) {
    const open = (event.target as HTMLDetailsElement).open;
    const kind = this.selected?.step.kind ?? "workflow";
    const closed = new Set(this.closedSections[kind] ?? []);
    if (open === !closed.has(section)) return;
    if (open) closed.delete(section);
    else closed.add(section);
    this.closedSections = { ...this.closedSections, [kind]: [...closed] };
    store(inspectorSectionsKey, JSON.stringify(this.closedSections));
  }
  /** The "Keyboard shortcuts" sheet. */
  readonly classicShortcuts = [
    { keys: "↑ ↓", does: "Select the previous or next step" },
    { keys: "Enter", does: "Edit the focused step in the inspector" },
    { keys: "A or /", does: "Add a step after the focused step" },
    { keys: "Delete", does: "Delete the focused step (Backspace too)" },
    { keys: "⌘/Ctrl Z", does: "Undo" },
    { keys: "⌘/Ctrl Shift Z", does: "Redo" },
    { keys: "⌘/Ctrl S", does: "Save the draft, or save to file locally" },
    { keys: "+ −", does: "Zoom in or out" },
    { keys: "⌘/Ctrl 0", does: "Zoom to 100%" },
    { keys: "Shift 1", does: "Fit all steps on screen" },
    { keys: "Escape", does: "Stop moving a step, or close the inspector" },
    { keys: "?", does: "Show these shortcuts" },
  ];
  get shortcuts() {
    return this.editorNext
      ? canvasSheet(keyPlatform()).flatMap((section) =>
          section.entries.map((entry) => ({
            keys: entry.keys.join(" "),
            does: entry.label,
          })),
        )
      : this.classicShortcuts;
  }
  /** "Main sequence" or "Case 1 of route": where a step sits. */
  placeLabel(owner: string) {
    return ownerLabel(owner, this.stepsById());
  }
  outlineOwner(node: Node) {
    return this.placeLabel(node.owner);
  }
  /**
   * A branch's name on the canvas: a decision case's condition ("Amount >
   * 1000"), "Otherwise", or a parallel branch's name; shortened to 28
   * characters for the label (the title has it whole).
   */
  branchLabel(owner: string, short = false) {
    const split = owner.indexOf("/");
    const step = this.nodeById.get(owner.slice(0, split))?.step;
    const name = owner.slice(split + 1);
    let text = name;
    if (step?.kind === "switch")
      text =
        name === "default"
          ? "Otherwise"
          : conditionSummary(
              (
                (step["cases"] as { when?: unknown }[] | undefined)?.[
                  Number(name.slice(5)) - 1
                ] ?? {}
              ).when,
              this.model.definition,
            );
    return short ? shortLabel(text) : text;
  }
  shortLabel(text: string) {
    return shortLabel(text);
  }
  /** A decision case's condition in a few words. */
  caseSummary(index: number) {
    const cases = this.selected?.step["cases"] as
      | { when?: unknown }[]
      | undefined;
    return conditionSummary(cases?.[index]?.when, this.model.definition);
  }
  /** The diagnostics strip's icon: none before the first result. */
  get diagnosticsIcon() {
    if (this.contractIssues.length) return "warning";
    const tone = this.validationView.tone;
    if (tone === "errors" || tone === "failed") return "failCircle";
    if (tone === "warnings") return "warning";
    if (tone === "passed") return "check";
    if (tone === "checked") return "help";
    return "";
  }
  /**
   * The strip's one status line. Without errors, steps that still need an
   * action are counted: "No errors · 4 steps still need an action."
   */
  get statusLine() {
    const view = this.validationView;
    const gaps = this.contractIssues.length;
    if (gaps)
      return `${gaps} ${gaps === 1 ? "item needs" : "items need"} attention${view.countLine ? ` · ${view.countLine}` : ""}`;
    if (view.tone === "passed" || view.tone === "checked") {
      const waiting = this.nodes.filter(
        (n) => incompleteChip(n.step) === "Choose an action",
      ).length;
      if (waiting)
        return `No errors · ${waiting === 1 ? "1 step still needs" : `${waiting} steps still need`} an action.`;
    }
    if (view.tone === "checked" && this.validation?.snapshot.mode === "local") {
      const context = this.validationContext();
      if (context.signedOut)
        return "Checked locally — Sign in again to check actions and connections against the project.";
      if (context.connected && !context.canCompile)
        return "Checked locally — Your account can't check actions and connections against the project catalog.";
      return "Checked locally — Validate to check against the project";
    }
    return view.headline;
  }
  /** "approve, reject": a human task's decisions. */
  decisionList() {
    const step = this.selected?.step;
    const decisions = Array.isArray(step?.["decisions"])
      ? (step["decisions"] as unknown[]).map(String)
      : ["approve", "reject"];
    return decisions.join(", ");
  }
  /** Adds a decision after the selected human task, one case per decision. */
  async branchOnDecision(id = this.selected?.step.id) {
    if (!id || !(await this.ensureApplied())) return;
    this.perform(() => this.model.branchOnDecision(id));
    if (!this.error)
      this.message = `Added ${this.model.selected} with a path for each answer of ${id}.`;
    this.revealSelected();
    this.cdr.markForCheck();
  }
  /**
   * Diagnostics for the panel; node badges follow their pointers into
   * `definition` (the Source text while it is typed, otherwise the model).
   */
  showDiagnostics(
    result: Validation | null,
    definition: unknown = this.model.definition,
  ) {
    this.diagnostics = result;
    this.diagnosticsDefinition = definition;
    this.diagnosticSteps = new Map();
    if (!result?.diagnostics?.some((d) => typeof d["path"] === "string"))
      return;
    void import("./designer/diagnostic-location").then(
      ({ locateDiagnostic }) => {
        if (this.diagnostics !== result) return;
        const steps = new Map<
          string,
          {
            errors: number;
            warnings: number;
            errorMessages: string[];
            warningMessages: string[];
          }
        >();
        for (const d of result.diagnostics) {
          const path = d["path"];
          if (typeof path !== "string") continue;
          const { stepId } = locateDiagnostic(path, definition);
          if (stepId === "$workflow") continue;
          const entry = steps.get(stepId) ?? {
            errors: 0,
            warnings: 0,
            errorMessages: [],
            warningMessages: [],
          };
          if (d.severity === "warning") {
            entry.warnings++;
            entry.warningMessages.push(d.message);
          } else if (d.severity !== "info") {
            entry.errors++;
            entry.errorMessages.push(d.message);
          }
          steps.set(stepId, entry);
        }
        this.diagnosticSteps = steps;
        this.cdr.markForCheck();
      },
    );
  }
  /** The validation service, created once its module has loaded. */
  private async validator() {
    this.validationModule ??= import("./designer/validation");
    const module = await this.validationModule;
    this.summarize = module.summarize;
    if (!this.findContractGaps) {
      this.findContractGaps = module.contractGaps;
      this.contractRevision++;
      this.cdr.markForCheck();
    }
    // Decision cases without a condition are named by Studio itself: the
    // compiler reports such a step only as a whole.
    const conditions =
      (source: string, format: string) => (result: Validation) =>
        withConditionDiagnostics(result, this.parseSource(source, format));
    return (this.validation ??= new module.ValidationService({
      transport: {
        local: (source, format) =>
          this.whenFree(() => this.api.validate(source, format)).then(
            conditions(source, format),
          ),
        compile: (source, format) =>
          this.api
            .request<Validation>(
              `${this.api.project}/compiler/compile`,
              "POST",
              { source, format },
            )
            .then(conditions(source, format)),
      },
      context: () => this.validationContext(),
      onChange: (snapshot) => this.validationChanged(snapshot),
    }));
  }
  /**
   * The Studio host analyzes two documents at a time; a check it turns away
   * as busy is asked again shortly instead of reported as a failure.
   */
  private async whenFree<T>(request: () => Promise<T>, retries = 2) {
    for (let attempt = 0; ; attempt++)
      try {
        return await request();
      } catch (e) {
        if (
          attempt >= retries ||
          !(e instanceof ApiError) ||
          e.code !== "WV-STUDIO-BUSY"
        )
          throw e;
        await new Promise((resolve) =>
          setTimeout(resolve, 500 * (attempt + 1)),
        );
      }
  }
  /** The definition in a source text, or null when it doesn't parse. */
  private parseSource(source: string, format: string): unknown {
    try {
      return format === "json" ? JSON.parse(source) : parse(source);
    } catch {
      return null;
    }
  }
  private validationContext() {
    let scope = "";
    try {
      scope = this.profile ? this.api.project : "";
    } catch {
      scope = "";
    }
    return {
      connected: !!scope,
      canCompile: !!scope && this.can("compile"),
      signedOut: !!scope && this.signInEnded,
      scope,
    };
  }
  private validationChanged(snapshot: ValidationSnapshot) {
    if (snapshot.result && snapshot.result !== this.shownResult) {
      this.shownResult = snapshot.result;
      const source = snapshot.source ?? this.model.source;
      let definition: unknown = this.model.definition;
      if (source !== this.model.source)
        try {
          definition = parse(source);
        } catch {
          definition = this.model.definition;
        }
      this.showDiagnostics(snapshot.result as Validation, definition);
    }
    this.validationView = this.summarize!(snapshot, this.validationContext());
    this.cdr.markForCheck();
  }
  /** Checks `source` after a pause; the Source tab passes the text being typed. */
  private checkLater(source = this.model.source) {
    if (!this.paired) return;
    const format = this.model.format;
    void this.validator()
      .then((service) => service.schedule(source, format))
      .catch(() => undefined);
  }
  sourceInput(event: Event) {
    this.sourceBuffer = this.value(event);
    this.checkLater(this.sourceBuffer);
  }
  /**
   * Another workflow opened: forget the previous one's diagnostics. The
   * service is replaced, so a check of the previous workflow that is still
   * running can never report on this one.
   */
  private resetValidation() {
    this.validation?.dispose();
    this.validation = null;
    this.shownResult = null;
    this.showDiagnostics(null);
    this.validationView = {
      tone: "idle",
      headline: "Not validated yet.",
      countLine: "",
    };
    this.closeSimulation();
  }
  /** Opens the step (or the workflow settings) a diagnostic names and focuses its field. */
  async openDiagnostic(location: DiagnosticLocation) {
    if (this.tab !== "Designer") this.selectTab("Designer");
    if (location.stepId === "$workflow") {
      if (!(await this.deselect())) return;
    } else {
      const node = this.nodes.find((n) => n.step.id === location.stepId);
      if (!node) {
        this.message = `Step ${location.stepId} isn't in the workflow anymore.`;
        return;
      }
      await this.select(node);
      if (this.model.selected !== location.stepId) return;
    }
    this.showInspector = true;
    this.cdr.markForCheck();
    const key =
      location.field.join("/") ||
      (location.stepId === "$workflow" ? "metadata/name" : "id");
    // In the field-by-field action input, the data keys name the field.
    this.focusField(key, location.field[0] === "with" ? location.dataPath : []);
  }
  /**
   * Focuses a field once the lazily rendered inspector shows it; inside an
   * action input form, the deepest rendered field of `dataPath`.
   */
  private focusField(key: string, dataPath: string[] = [], attempts = 40) {
    const field = document.querySelector(
      `.inspector [data-field="${CSS.escape(key)}"]`,
    );
    // An input before the Value | Data | Formula buttons beside it.
    const first = (scope: Element | null | undefined) =>
      scope?.querySelector<HTMLElement>(
        "input:not([disabled]), select:not([disabled]), textarea:not([disabled])",
      ) ?? scope?.querySelector<HTMLElement>("button:not([disabled])");
    let control: HTMLElement | null | undefined = null;
    for (let depth = dataPath.length; depth > 0 && !control; depth--)
      control = first(
        field?.querySelector(
          `[data-path="${CSS.escape(dataPath.slice(0, depth).join("\u001f"))}"]`,
        ),
      );
    control ??= first(field);
    if (control) control.focus();
    else if (attempts)
      setTimeout(() => this.focusField(key, dataPath, attempts - 1), 50);
  }
  /**
   * Applies a compiler's suggested edit as one undoable change. A suggestion
   * for Source text that isn't applied yet applies that text first; one made
   * for an older version of the workflow is refused, since it would undo
   * the edits made since.
   */
  async applySuggestion(change: SuggestedChange) {
    if (!(await this.ensureApplied())) return;
    const typed = this.sourceBuffer !== this.model.source;
    let current: unknown = this.model.definition;
    if (typed)
      try {
        current = parse(this.sourceBuffer);
      } catch {
        current = undefined;
      }
    if (
      JSON.stringify(current) !== JSON.stringify(this.diagnosticsDefinition)
    ) {
      this.error =
        "The workflow changed after this suggestion was made. Wait for the next check, then try again.";
      this.errorCode = "";
      this.message = "";
      this.cdr.markForCheck();
      return;
    }
    if (typed) {
      this.updateSource();
      if (this.error || this.model.error) {
        this.cdr.markForCheck();
        return;
      }
    }
    this.perform(() =>
      change.stepId === "$workflow"
        ? this.model.updateWorkflow(change.value as Workflow)
        : this.model.update(change.stepId, JSON.stringify(change.value)),
    );
    if (!this.error) this.message = "Applied the suggested edit.";
    this.cdr.markForCheck();
  }
  /**
   * What a node shows: a summary line, a status (error, warning or
   * incomplete) and its accessible name.
   */
  nodeInfo(node: Node) {
    this.nodes;
    if (
      this.infoCache.tick !== this.cachedTick ||
      this.infoCache.steps !== this.diagnosticSteps
    )
      this.infoCache = {
        tick: this.cachedTick,
        steps: this.diagnosticSteps,
        info: new Map(),
      };
    let info = this.infoCache.info.get(node.step.id);
    if (!info) {
      const summary = stepSummary(node.step, this.model.definition);
      const gaps = this.contractIssues.filter(
        (gap) => gap.stepId === node.step.id,
      );
      const issues = [
        ...stepIssues(node.step),
        ...gaps.map((gap) => gap.label),
      ];
      const found = this.diagnosticSteps.get(node.step.id);
      const counts = [
        found?.errors
          ? `${found.errors} ${found.errors === 1 ? "error" : "errors"}`
          : "",
        found?.warnings
          ? `${found.warnings} ${found.warnings === 1 ? "warning" : "warnings"}`
          : "",
      ].filter(Boolean);
      const status = found?.errors
        ? "error"
        : found?.warnings
          ? "warning"
          : issues.length
            ? "incomplete"
            : "";
      const where =
        node.owner === "root"
          ? "main sequence"
          : `in ${this.placeLabel(node.owner)}`;
      // On the card: "2 problems", or what an incomplete step needs next.
      const problems = (found?.errors ?? 0) + (found?.warnings ?? 0);
      const chip =
        status === "error" || status === "warning"
          ? `${problems} ${problems === 1 ? "problem" : "problems"}`
          : status === "incomplete"
            ? gaps[0]?.label || incompleteChip(node.step) || "Needs attention"
            : "";
      const statusText = [
        counts.length ? `Has ${counts.join(" and ")}.` : "",
        issues.length ? `Needs attention: ${issues.join(" ")}` : "",
      ]
        .filter(Boolean)
        .join(" ");
      info = {
        summary,
        status,
        statusText,
        chip,
        label: [
          `${node.step.id}, ${this.label(node.step.kind)}, ${where}.`,
          summary ? `${summary}.` : "",
          statusText,
        ]
          .filter(Boolean)
          .join(" "),
      };
      this.infoCache.info.set(node.step.id, info);
    }
    return info;
  }
  undo() {
    if (this.editingLocked) return;
    this.flushInspector();
    this.model.undo();
    this.dirty = true;
    this.changed();
  }
  redo() {
    if (this.editingLocked) return;
    this.cancelInspectorCommit();
    this.model.redo();
    this.dirty = true;
    this.changed();
  }
  // ------------------------------------------- the left-to-right canvas
  private factsCache: {
    tick: number;
    steps: unknown;
    gaps: unknown;
    facts: Map<string, StepFacts>;
  } = { tick: -1, steps: null, gaps: null, facts: new Map() };
  /** Errors, warnings and setup per step; the same map until they change. */
  canvasFacts(): ReadonlyMap<string, StepFacts> {
    const gaps = this.contractIssues;
    const tick = this.tick();
    const cache = this.factsCache;
    if (
      cache.tick === tick &&
      cache.steps === this.diagnosticSteps &&
      cache.gaps === gaps
    )
      return cache.facts;
    const facts = new Map<string, StepFacts>();
    for (const node of this.nodes) {
      const found = this.diagnosticSteps.get(node.step.id);
      const chip = incompleteChip(node.step);
      const setup = [
        ...gaps
          .filter((gap) => gap.stepId === node.step.id)
          .map((gap) => setupPhrase(gap.label)),
        ...(chip ? [setupPhrase(chip)] : []),
      ];
      const errors = found?.errorMessages ?? [];
      const warnings = found?.warningMessages ?? [];
      if (errors.length || warnings.length || setup.length)
        facts.set(node.step.id, { errors, warnings, setup });
    }
    this.factsCache = { tick, steps: this.diagnosticSteps, gaps, facts };
    return facts;
  }
  private runCache: {
    nodes: SimulationNodes | null;
    session: unknown;
    run: CanvasRun | null;
  } = { nodes: null, session: null, run: null };
  /** Today's simulation as the canvas draws it; null when none is open. */
  canvasRun(): CanvasRun | null {
    const session = this.simulationSession;
    if (
      this.runCache.nodes !== this.simNodes ||
      this.runCache.session !== session
    )
      this.runCache = {
        nodes: this.simNodes,
        session,
        run: session
          ? {
              mode: "simulated",
              status: this.simNodes.status,
              current: this.simNodes.current,
              active: this.simNodes.active,
              done: this.simNodes.done,
            }
          : null,
      };
    return this.runCache.run;
  }
  kindContext(): KindContext {
    return {
      workflow: this.model.definition,
      features: [],
      actionContract: (uses) =>
        (this.catalogContracts.get(uses) as Json | undefined) ?? null,
      tableContract: (uses) =>
        (this.decisionContracts.get(uses) as Json | undefined) ?? null,
      workflowContract: () => null,
    };
  }
  async selectStep(id: string, open: boolean) {
    const node = this.nodes.find((n) => n.step.id === id);
    if (node) await this.select(node, open);
  }
  /** The workflow settings, focused on Inputs or Result. */
  async openWorkflowSection(section: WorkflowSection) {
    if (!(await this.deselect())) return;
    this.showInspector = true;
    this.cdr.markForCheck();
    this.focusField(section);
  }
  openPicker(
    insert: { owner: string; index: number },
    label: string,
    anchor: HTMLElement | AnchorRect,
  ) {
    if (this.editingLocked || this.model.readonly) return;
    this.picker = {
      target: { ...insert, point: { x: 0, y: 0 }, label },
      anchor,
    };
    if (this.profile) void this.loadActionCatalog();
    this.cdr.markForCheck();
  }
  isPickerOpenAt(insert: { owner: string; index: number }) {
    return (
      this.picker?.target.owner === insert.owner &&
      this.picker.target.index === insert.index
    );
  }
  closePicker() {
    this.picker = null;
    this.cdr.markForCheck();
  }
  moveStep(id: string, insert: { owner: string; index: number }) {
    this.connectingNode = "";
    this.moveTo(id, { ...insert, point: { x: 0, y: 0 }, label: "" });
  }
  /** Selects a step and moves focus into its details: their title, or the step name to rename it. */
  async openStep(id: string, focus: "details" | "rename") {
    const node = this.nodes.find((n) => n.step.id === id);
    if (!node) return;
    await this.select(node);
    if (this.model.selected !== id) return;
    this.focusLater(() =>
      focus === "rename"
        ? document.querySelector<HTMLElement>("#step-name-input")
        : document.querySelector<HTMLElement>(".inspector-header h2"),
    );
  }
  /** Deletes steps as one undo step; one step goes through `remove`, which moves focus to its neighbor. */
  async removeSteps(ids: readonly string[]) {
    if (!ids.length || this.editingLocked) return;
    if (ids.length === 1) return this.remove(ids[0]);
    if (!(await this.deleteSteps(ids))) return;
    this.focusLater(() => document.querySelector<HTMLElement>(".canvas-v2"));
  }
  /** Copies a step, or a run of steps, right after itself, as one undo step. */
  async duplicateSteps(ids: readonly string[]) {
    if (!ids.length || this.editingLocked) return;
    if (ids.length === 1) return this.duplicate(ids[0]);
    if (!(await this.ensureApplied())) return;
    let copies: string[] = [];
    this.perform(() => (copies = this.model.duplicateRun(ids)));
    if (this.error || !copies.length) return;
    this.notify(`Duplicated ${ids.length} steps.`);
    this.loadInspector();
    this.focusStep(copies[0]);
  }
  /** A step kind dropped on a "+" of the left-to-right canvas. */
  dropStep(event: DragEvent, insert: { owner: string; index: number }) {
    return this.drop(event, { ...insert, point: { x: 0, y: 0 }, label: "" });
  }
  /** Ctrl/Cmd+S on the canvas: save the draft when connected, or save to a file locally. */
  saveShortcut() {
    void this.runCommand(this.profile ? "save" : "export");
  }
  readonly editorViews = ["Designer", "Source", "Outline"];
  selectTab(tab: string) {
    if (tab !== this.tab && !this.leaveInspector()) return;
    this.tab = tab;
    if (tab === "Designer") this.outlineNotice = false;
    if (tab !== "Designer" && window.innerWidth <= 1280)
      this.showInspector = false;
  }
  /** Designer | Source | Outline: arrow keys, Home and End move between them. */
  viewKey(event: KeyboardEvent) {
    const views = this.editorViews;
    const index = views.indexOf(this.tab);
    const next =
      event.key === "ArrowRight"
        ? (index + 1) % views.length
        : event.key === "ArrowLeft"
          ? (index - 1 + views.length) % views.length
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? views.length - 1
              : -1;
    if (next < 0) return;
    event.preventDefault();
    this.selectTab(views[next]);
    this.focusLater(() =>
      document.getElementById(`view-tab-${views[next].toLowerCase()}`),
    );
  }
  /**
   * A workflow opens on the canvas; below 600 px on the outline, with a
   * note and a way to the canvas.
   */
  private openTab() {
    const narrow = this.windowWidth < 600;
    this.tab = narrow ? "Outline" : "Designer";
    this.outlineNotice = narrow;
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
      this.windowWidth - (this.navFull ? 224 : 64) - 48 - 32 - 400;
    const maximum =
      pane === "palette"
        ? Math.min(256, available - this.inspectorWidth)
        : Math.min(INSPECTOR_MAX, available - this.paletteWidth);
    if (pane === "palette")
      this.paletteWidth = Math.max(176, Math.min(maximum, width));
    else {
      this.inspectorWidth = Math.max(INSPECTOR_MIN, Math.min(maximum, width));
      store(inspectorWidthKey, String(this.inspectorWidth));
    }
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
      else {
        this.inspectorWidth = defaultInspectorWidth();
        store(inspectorWidthKey, String(this.inspectorWidth));
      }
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
  async changeFormat(event: Event) {
    const select = event.target as HTMLSelectElement;
    const format = select.value as "yaml" | "json";
    if (this.model.error) {
      select.value = this.model.format;
      return;
    }
    if (
      this.model.hasComments &&
      !(await this.dialogs.confirm({
        title: "Convert to JSON?",
        message:
          "JSON cannot keep YAML comments. Converting removes the comments from this source.",
        confirmLabel: "Convert and remove comments",
      }))
    ) {
      select.value = this.model.format;
      return;
    }
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
    if (!this.propertyValid() || !this.selected) return;
    this.flushInspector();
    const id = this.selected.step.id;
    this.perform(() => {
      this.model.editBranches(
        id,
        operation,
        name,
        event ? this.value(event) : undefined,
      );
    });
  }
  updateWorkflowOptions() {
    if (!this.workflowValid()) return;
    this.perform(() =>
      this.model.updateWorkflow(JSON.parse(this.workflowBuffer) as Workflow),
    );
  }
  switchPropertyMode() {
    if (!this.leaveInspector()) return;
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
    this.propertyValid.set(true);
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
  roleLabel(role: string) {
    return this.memberRoles.find((r) => r.value === role)?.label ?? role;
  }
  /** "Acme / Payments / Production" for a role's scope, by name when known. */
  scopeLabel(scope: unknown) {
    const s = (scope ?? {}) as Record<string, unknown>;
    const tenant = this.identity?.workspaces.find(
      (w) => w.id === s["tenant_id"],
    );
    const project = tenant?.projects.find((x) => x.id === s["project_id"]);
    const environment = project?.environments.find(
      (x) => x.id === s["environment_id"],
    );
    const p = this.profile;
    const names = this.workspaceNames;
    const tenantName =
      tenant?.name ??
      (s["tenant_id"] === p?.tenantId ? names?.tenant : "") ??
      "";
    if (s["environment_id"]) {
      const environmentName =
        environment?.name ??
        (s["environment_id"] === p?.environmentId ? names?.environment : "");
      const projectName =
        project?.name ??
        (s["project_id"] === p?.projectId ? names?.project : "");
      return environmentName && projectName
        ? workspaceLong({
            tenant: tenantName,
            project: projectName,
            environment: environmentName,
          })
        : `Environment ${shortId(String(s["environment_id"]))}`;
    }
    if (s["project_id"]) {
      const projectName =
        project?.name ??
        (s["project_id"] === p?.projectId ? names?.project : "");
      return projectName
        ? [tenantName, projectName, "all environments"]
            .filter(Boolean)
            .join(" / ")
        : `Project ${shortId(String(s["project_id"]))}`;
    }
    return tenantName ? `All of ${tenantName}` : "Whole tenant";
  }
  /** An account's name: its display name when the API returns one. */
  accountLabel(principal: unknown) {
    const id = String(principal ?? "");
    const known = [...this.adminPrincipals, ...this.adminMembers].find(
      (r) => r["id"] === id || r["principal_id"] === id,
    );
    const name =
      known && (known["display_name"] ?? known["name"] ?? known["email"] ?? "");
    return typeof name === "string" && name ? name : `Account ${shortId(id)}`;
  }
  /** The account the "Assign role" side panel is for; "" names none yet. */
  assignAccount: { id: string } | null = null;
  openAssign(principal = "") {
    this.principalId = principal;
    this.memberResources = "";
    this.assignAccount = { id: principal };
    afterNextRender(
      () =>
        document
          .querySelector<HTMLElement>(
            principal ? "#assign-role" : "#assign-account",
          )
          ?.focus(),
      { injector: this.injector },
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
    const account = this.accountLabel(
      command === "revoke" ? record?.["principal_id"] : record?.["id"],
    );
    if (
      command === "status" &&
      !(await this.dialogs.confirm({
        title: `${record!["active"] ? "Deactivate" : "Activate"} ${account}?`,
        message: record!["active"]
          ? `${account} can't sign in or act until you activate it again. Its roles stay.`
          : `${account} gets back the access its roles give.`,
        confirmLabel: record!["active"] ? "Deactivate" : "Activate",
        danger: !!record!["active"],
      }))
    )
      return;
    const role = this.roleLabel(String(record?.["role"] ?? ""));
    if (
      command === "revoke" &&
      !(await this.dialogs.confirm({
        title: "Remove this role?",
        message: `${account} loses the ${role} role in ${this.scopeLabel(record!["scope"])}.`,
        confirmLabel: "Remove",
        danger: true,
      }))
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
      const target =
        command === "grant" || command === "link"
          ? this.accountLabel(this.principalId)
          : account;
      if (command === "create") {
        const created = String(result["id"]);
        this.principalId = created;
        this.notify(`Created ${this.accountLabel(created)}.`, {
          label: "Assign role",
          run: () => this.openAssign(created),
        });
      } else
        this.notify(
          command === "grant"
            ? `Assigned ${this.roleLabel(this.memberRole)} to ${target}.`
            : command === "link"
              ? `Linked the sign-in to ${target}.`
              : command === "revoke"
                ? `Removed ${role} from ${target}.`
                : `${record!["active"] ? "Deactivated" : "Activated"} ${target}.`,
        );
      if (command === "grant") this.assignAccount = null;
      await this.loadIdentity();
      await this.loadAdministration();
    } catch (e) {
      if (
        command === "create" &&
        (!(e instanceof ApiError) || e.status >= 500 || e.status === 408)
      ) {
        // The Accounts section explains it next to "Create account".
        this.adminUncertainCreate = true;
      } else this.fail(e);
    } finally {
      this.busy = "";
      this.cdr.markForCheck();
    }
  }
  private bufferStep(): Step {
    return JSON.parse(this.inspectorBuffer) as Step;
  }
  /** False while Advanced JSON holds text that is not a step yet. */
  get bufferReadable() {
    try {
      this.bufferStep();
      return true;
    } catch {
      return false;
    }
  }
  /** The buffered step for an edit, or null with an explanation when its JSON is invalid. */
  private editableStep(): Step | null {
    try {
      return this.bufferStep();
    } catch {
      this.error =
        "Fix the step configuration JSON in Advanced JSON before changing the action.";
      this.errorCode = "";
      return null;
    }
  }
  private setBuffer(step: Step, refreshGrid = false, field = "step") {
    this.inspectorBuffer = JSON.stringify(step, null, 2);
    if (refreshGrid) this.propertyStep = structuredClone(step);
    this.queueInspector("step", this.inspectorBuffer, field);
  }
  stepEdit(value: unknown, field = "step") {
    const step = value as Step;
    // A mounted grid can retain the ID from before a live rename.
    if (this.selected) step.id = this.selected.step.id;
    // Fields edited outside the property table keep their buffered values.
    if (step.kind === "action") {
      const buffer = this.bufferStep();
      for (const key of this.inspectorHiddenFields)
        if (buffer[key] === undefined) delete step[key];
        else step[key] = buffer[key];
    }
    this.setBuffer(step, false, field);
    if (step.kind === "action") this.syncActionContract();
  }
  actionVersionEdit(value: unknown) {
    const step = this.editableStep();
    if (!step) return;
    step["uses"] = (value as Step)["uses"];
    this.setBuffer(step, false, "uses");
    this.syncActionContract();
  }
  /**
   * Fields the property table leaves out because the inspector shows them
   * already: the connection slot and, field by field, the input; and the
   * action version when the published actions picker chooses it.
   */
  get inspectorHiddenFields() {
    if (this.selected?.step.kind !== "action") return noHiddenFields;
    return this.actionInputMode === "fields" && this.actionContract
      ? actionFieldsPickerHiddenFields
      : actionPickerHiddenFields;
  }
  /** True when the inspector holds edits that are not applied to the workflow yet. */
  get inspectorDirty() {
    const step = this.selected?.step;
    if (!step || !this.inspectorBuffer) return false;
    try {
      return JSON.stringify(this.bufferStep()) !== JSON.stringify(step);
    } catch {
      return true;
    }
  }
  /**
   * Where the inspector's fields sit, so their data suggestions follow the
   * compiler's scope rules (computed lazily by the forms).
   */
  get referenceContext(): ReferenceContext {
    const key = `${this.tick()}:${this.model.selected}:${this.contractCache.size}`;
    if (this.scopeCache.key !== key || !this.scopeCache.value)
      this.scopeCache = {
        key,
        value: {
          definition: this.model.definition,
          stepId: this.model.selected || "$workflow",
          actionOutput: (uses) =>
            (
              this.contractCache.get(uses)?.["spec"] as Record<string, unknown>
            )?.["outputSchema"],
          decisionOutput: (uses) =>
            (
              this.decisionContracts.get(uses)?.["spec"] as Record<
                string,
                unknown
              >
            )?.["outputSchema"],
        },
      };
    return this.scopeCache.value!;
  }
  /** Example JSON to schema, through the local Studio host (never fetched). */
  inferSchema: InferSchema = async (samples) =>
    (await import("./forms/ui/schema-designer")).inferViaHttpAction(
      (path, body) => this.api.request(path, "POST", body, {}, 35000),
    )(samples);
  /** Loads the published action catalog once per workspace. */
  async loadActionCatalog(append = false, force = false) {
    if (!this.profile || !this.can("catalog.read")) return;
    if (
      !append &&
      !force &&
      this.catalogScope === this.profile &&
      this.catalogState !== "idle" &&
      this.catalogState !== "error"
    ) {
      if (this.catalogState === "ready") void this.loadWorkflowContracts();
      return;
    }
    const generation = ++this.catalogGeneration,
      scope = this.profile;
    this.catalogScope = scope;
    if (append) this.catalogAppending = true;
    else {
      this.catalogState = "loading";
      this.catalogError = null;
    }
    try {
      const page = await this.api.page(
        "actions",
        false,
        append ? (this.actionNextCursor ?? undefined) : undefined,
      );
      if (generation !== this.catalogGeneration || scope !== this.profile)
        return;
      const items = page.items.filter((item) => !item["retired"]);
      this.actionVersions = append ? [...this.actionVersions, ...items] : items;
      this.actionNextCursor = page.next_cursor;
      this.catalogState = "ready";
      this.syncActionContract();
      void this.loadWorkflowContracts();
    } catch (e) {
      if (generation !== this.catalogGeneration) return;
      if (append) this.fail(e);
      else {
        this.catalogState = "error";
        this.catalogError = describeError(e);
      }
    } finally {
      if (generation === this.catalogGeneration) this.catalogAppending = false;
      this.cdr.markForCheck();
    }
  }
  private loadingWorkflowContracts = new Set<string>();
  /** Three bounded readers warm the contracts needed by cards outside the inspector. */
  private async loadWorkflowContracts() {
    const scope = this.profile,
      opened = this.model.opened;
    if (!scope || !this.can("catalog.read")) return;
    const uses = new Set(
      this.nodes
        .filter(
          (node) => node.step.kind === "action" || node.step.kind === "llm",
        )
        .map((node) => String(node.step["uses"] ?? "")),
    );
    const queue = this.actionVersions.filter((entry) => {
      const ref = `${entry["name"]}@${entry["version"]}`;
      return (
        uses.has(ref) &&
        !this.contractCache.has(ref) &&
        this.contractPending !== ref &&
        !this.loadingWorkflowContracts.has(ref)
      );
    });
    for (const entry of queue)
      this.loadingWorkflowContracts.add(`${entry["name"]}@${entry["version"]}`);
    const read = async () => {
      while (queue.length) {
        const entry = queue.shift()!,
          ref = `${entry["name"]}@${entry["version"]}`;
        try {
          if (scope !== this.profile || opened !== this.model.opened) continue;
          const result = await this.api.request<Record<string, unknown>>(
            `${this.api.project}/actions/${encodeURIComponent(String(entry["id"]))}/export`,
          );
          if (
            scope === this.profile &&
            opened === this.model.opened &&
            result["document"] &&
            typeof result["document"] === "object"
          )
            this.cacheContract(
              ref,
              result["document"] as Record<string, unknown>,
            );
        } catch {
          // A catalog read is optional; explicit project validation reports missing access.
        } finally {
          this.loadingWorkflowContracts.delete(ref);
          this.cdr.markForCheck();
        }
      }
    };
    await Promise.all([read(), read(), read()]);
  }
  /** The catalog entry for the action version the inspector buffer uses. */
  get selectedCatalogAction() {
    if (this.selected?.step.kind !== "action") return undefined;
    let uses = "";
    try {
      uses = String(this.bufferStep()["uses"] ?? "");
    } catch {
      return undefined;
    }
    return this.actionVersions.find(
      (item) => `${item["name"]}@${item["version"]}` === uses,
    );
  }
  get integrationState():
    | "offline"
    | "identity"
    | "forbidden"
    | "loading"
    | "error"
    | "empty"
    | "ready" {
    if (!this.profile) return "offline";
    if (!this.identity) return "identity";
    if (!this.can("catalog.read") || this.catalogError?.status === 403)
      return "forbidden";
    if (this.catalogState === "error") return "error";
    if (this.catalogState !== "ready") return "loading";
    return this.actionVersions.length ? "ready" : "empty";
  }
  /**
   * Shows the contract for the action version in the inspector buffer. Results
   * are fenced by a generation and by the buffered version, so a slow or
   * repeated load never restores an action the person moved away from.
   */
  syncActionContract(fresh = false) {
    if (this.selected?.step.kind !== "action") return;
    let uses = "";
    try {
      uses = String(this.bufferStep()["uses"] ?? "");
    } catch {
      return;
    }
    const cached = this.contractCache.get(uses);
    if (cached) {
      if (this.actionContract !== cached) this.showContract(cached, fresh);
      return;
    }
    const item = this.actionVersions.find(
      (entry) => `${entry["name"]}@${entry["version"]}` === uses,
    );
    if (!item) {
      this.contractGeneration++;
      this.contractPending = "";
      this.actionContract = null;
      this.contractState = "idle";
      this.contractError = "";
      return;
    }
    if (this.contractPending === uses) return;
    void this.fetchContract(String(item["id"]), uses, fresh);
  }
  private contractPending = "";
  retryContract() {
    this.contractPending = "";
    this.syncActionContract();
  }
  private async fetchContract(id: string, uses: string, fresh: boolean) {
    const generation = ++this.contractGeneration,
      scope = this.profile,
      opened = this.model.opened,
      nodeId = this.selected?.step.id;
    this.contractPending = uses;
    this.actionContract = null;
    this.contractState = "loading";
    this.contractError = "";
    try {
      const result = await this.api.request<Record<string, unknown>>(
        `${this.api.project}/actions/${encodeURIComponent(id)}/export`,
      );
      const document = result["document"] as Record<string, unknown>;
      if (scope !== this.profile) {
        // A stale workspace's answer must not leave this version marked as loading.
        if (generation === this.contractGeneration) {
          this.contractPending = "";
          this.contractState = "idle";
        }
        return;
      }
      this.contractCache.set(uses, document);
      this.contractRevision++;
      this.infoCache.tick = -1;
      if (generation !== this.contractGeneration) return;
      this.contractPending = "";
      // Contracts are keyed by version: show it only if the inspector still uses it.
      let current = "";
      try {
        current =
          this.selected?.step.kind === "action"
            ? String(this.bufferStep()["uses"] ?? "")
            : "";
      } catch {
        current = "";
      }
      if (current !== uses) {
        this.contractState = "idle";
        return;
      }
      this.showContract(
        document,
        fresh &&
          opened === this.model.opened &&
          nodeId === this.selected?.step.id,
      );
    } catch (e) {
      if (generation !== this.contractGeneration) return;
      this.contractPending = "";
      this.contractState = "error";
      this.contractError = describeError(e).message;
    } finally {
      this.cdr.markForCheck();
    }
  }
  /**
   * Shows a contract. A freshly chosen action also gets boolean defaults in
   * its input; reselecting a step never changes its buffer.
   */
  private showContract(contract: Record<string, unknown>, fresh: boolean) {
    this.actionContract = contract;
    this.contractState = "ready";
    this.contractError = "";
    const step = this.bufferStep();
    let input = step["with"];
    if (fresh && this.actionInputFields() && fieldable(input)) {
      input = this.freshInput(input);
      this.setBuffer({ ...step, with: input });
    }
    // A newly chosen action uses the workflow's only compatible slot (WP-19),
    // also in place of a slot chosen for the previous action that doesn't fit.
    const requirement = this.actionRequirement();
    const current = String(step["connection"] ?? "");
    if (
      fresh &&
      (!current ||
        (requirement &&
          !slotsFitting(requirement, this.workflowSlots).some(
            (slot) => slot.name === current,
          )))
    ) {
      const plan = planSlot(requirement, this.workflowSlots);
      if (plan.connection && !plan.add)
        this.setBuffer({ ...this.bufferStep(), connection: plan.connection });
    }
    this.actionInputMode =
      this.actionInputFields() && fieldable(input) ? "fields" : "expression";
    this.actionInitialInput = input;
    this.actionMissing = [];
    this.actionInputValid.set(true);
    if (fresh && this.pendingInspector) {
      if (this.actionChoiceRevision !== null)
        this.pendingInspector.continueRevision = this.actionChoiceRevision;
      this.flushInspector();
      this.actionChoiceRevision = null;
    }
  }
  /**
   * The input of a newly chosen action: inputs its schema doesn't allow are
   * dropped (allowed extra inputs stay) and required yes/no inputs start
   * at their default or false; bound inputs keep their binding.
   */
  private freshInput(input: unknown): unknown {
    const schema = this.actionInputSchema() as Record<string, unknown>;
    const declared = (schema["properties"] ?? {}) as Record<string, unknown>;
    const patterns = Object.keys(
      (schema["patternProperties"] ?? {}) as Record<string, unknown>,
    );
    const allowed = (key: string) =>
      key in declared ||
      patterns.some((pattern) => {
        try {
          return new RegExp(pattern, "u").test(key);
        } catch {
          return false;
        }
      }) ||
      schema["additionalProperties"] !== false;
    const keep = (data: Record<string, unknown>) =>
      Object.fromEntries(Object.entries(data).filter(([key]) => allowed(key)));
    if (isRecord(input) && isRecord(input["object"])) {
      const fields: Record<string, unknown> = keep(input["object"]);
      for (const [name, value] of Object.entries(this.withDefaults({})))
        if (!(name in fields)) fields[name] = { literal: value };
      return { object: fields };
    }
    const literal =
      isRecord(input) && isRecord(input["literal"]) ? input["literal"] : {};
    return { literal: this.withDefaults(keep(structuredClone(literal))) };
  }
  /** A literal object input, also recovered from a mapped object of literals. */
  private literalInput(input: unknown): Record<string, unknown> | null {
    const value = (input ?? {}) as Record<string, unknown>;
    if (isRecord(value["literal"])) return structuredClone(value["literal"]);
    if (
      isRecord(value["object"]) &&
      Object.values(value["object"]).every(
        (child) => isRecord(child) && "literal" in child,
      )
    )
      return Object.fromEntries(
        Object.entries(value["object"]).map(([key, child]) => [
          key,
          structuredClone((child as { literal: unknown }).literal),
        ]),
      );
    return null;
  }
  /**
   * Required, non-secret yes/no inputs start at their default or false, and
   * required constants at their value; optional ones stay unset (absent is
   * not false).
   */
  private withDefaults(literal: Record<string, unknown>) {
    const data = structuredClone(literal);
    const schema = this.actionInputSchema();
    for (const name of schema.required ?? []) {
      const field = schema.properties?.[name] as
        | Record<string, unknown>
        | undefined;
      if (
        !field ||
        field["x-secret"] === true ||
        field["writeOnly"] === true ||
        data[name] !== undefined
      )
        continue;
      if ("const" in field) data[name] = structuredClone(field["const"]);
      else if (field["type"] === "boolean")
        data[name] =
          typeof field["default"] === "boolean" ? field["default"] : false;
    }
    return data;
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
  actionRequirement() {
    return (this.actionSpec()["connection"] ?? null) as {
      connector: string;
      required?: boolean;
    } | null;
  }
  sideEffectLabel() {
    const effect = String(this.actionSpec()["sideEffect"] ?? "");
    return sideEffects[effect] ?? (effect || "Not declared");
  }
  retryLabel() {
    const retry = (this.actionSpec()["retry"] ?? {}) as Record<string, number>;
    const attempts = retry["maxAttempts"] ?? 1;
    if (attempts <= 1) return "One attempt, no automatic retry";
    return `Up to ${attempts} attempts, waiting ${retry["initialDelaySeconds"] ?? 1}–${retry["maxDelaySeconds"] ?? 30} seconds between them`;
  }
  connectionRequirementLabel() {
    const requirement = this.actionRequirement();
    if (!requirement) return "No connection needed";
    return `${requirement.required === false ? "Optional" : "Requires a"} ${requirement.connector} connection`;
  }
  /** Connection slots declared by the workflow (spec.connections). */
  get workflowSlots(): ConnectionSlot[] {
    const slots = (this.model.definition.spec["connections"] ?? {}) as Record<
      string,
      { connector?: string; required?: boolean }
    >;
    return Object.entries(slots).map(([name, slot]) => ({
      name,
      connector: String(slot?.connector ?? ""),
      required: slot?.required !== false,
    }));
  }
  /** Slots that satisfy the selected action's connection requirement. */
  compatibleSlots() {
    const requirement = this.actionRequirement();
    // The compiler rejects any slot on an action that declares no connection.
    if (this.actionContract && !requirement) return [];
    return this.workflowSlots.filter(
      (slot) =>
        !requirement ||
        (slot.connector === requirement.connector &&
          (requirement.required === false || slot.required)),
    );
  }
  bufferedConnection() {
    try {
      return String(this.bufferStep()["connection"] ?? "");
    } catch {
      return "";
    }
  }
  chooseConnectionSlot(event: Event | string) {
    const name = typeof event === "string" ? event : this.value(event);
    const step = this.editableStep();
    if (!step) return;
    if (name) step["connection"] = name;
    else delete step["connection"];
    this.setBuffer(step);
  }
  suggestedSlotName() {
    const connector = (
      this.actionRequirement()?.connector ??
      this.newSlotConnector ??
      ""
    ).split("@")[0];
    const base =
      connector
        .replace(/[^A-Za-z0-9_.-]/g, "-")
        .replace(/^[^A-Za-z0-9]+/, "") || "connection";
    const used = new Set(this.workflowSlots.map((slot) => slot.name));
    let name = base,
      n = 2;
    while (used.has(name)) name = `${base}-${n++}`;
    return name;
  }
  slotUseCount(name: string) {
    return this.nodes.filter(
      (node) =>
        ["action", "llm"].includes(node.step.kind) &&
        node.step["connection"] === name,
    ).length;
  }
  slotConnectorOptions() {
    return [
      ...new Set([
        ...this.workflowSlots.map((slot) => slot.connector),
        ...(this.actionRequirement()
          ? [this.actionRequirement()!.connector]
          : []),
      ]),
    ];
  }
  async updateConnectionSlot(previous: string, slot: ConnectionSlot) {
    if (
      this.editingLocked ||
      this.model.readonly ||
      !(await this.ensureApplied())
    )
      return;
    this.perform(() => changeSlot(this.model, previous, slot));
    this.cdr.markForCheck();
  }
  async removeConnectionSlot(name: string) {
    if (
      this.editingLocked ||
      this.model.readonly ||
      !(await this.ensureApplied())
    )
      return;
    const count = this.slotUseCount(name);
    if (
      !(await this.dialogs.confirm({
        title: "Remove connection slot?",
        message: `${name} is used by ${count} ${count === 1 ? "step" : "steps"}. Removing it clears those assignments. Steps that require a connection will need another slot.`,
        confirmLabel: "Remove slot",
      }))
    )
      return;
    this.perform(() => changeSlot(this.model, name, null));
    if (this.error) return;
    const revision = this.model.revision;
    this.notify(`Removed connection slot ${name}.`, {
      label: "Undo",
      run: () => {
        if (this.model.revision === revision) this.undo();
        else
          this.notify("The workflow changed since. Use Undo in the toolbar.");
      },
    });
    this.cdr.markForCheck();
  }
  connectionSlotButton() {
    const name =
      this.actionRequirement()
        ?.connector.split("@")[0]
        .replace(/^weave-/, "") ?? "";
    const label =
      name === "postgresql" ? "PostgreSQL" : name === "http" ? "HTTP" : name;
    return label ? `Add a ${label} connection slot` : "Add connection slot";
  }
  /** Declares and selects the slot as a single reversible change. */
  addConnectionSlot() {
    if (this.editingLocked || this.model.readonly) return;
    const requirement = this.actionRequirement();
    const slot = {
      name: (this.newSlotNameEdited
        ? this.newSlotName
        : this.suggestedSlotName()
      ).trim(),
      connector: requirement?.connector ?? this.newSlotConnector.trim(),
      required:
        requirement?.required !== false && !!requirement
          ? true
          : this.newSlotRequired,
    };
    this.slotError = slotValidation(slot, this.workflowSlots);
    if (this.slotError) return;
    const buffered = this.selected ? this.editableStep() : null;
    if (this.selected && !buffered) return;
    this.perform(() =>
      this.model.batch(() => {
        changeSlot(this.model, "", slot);
        if (buffered)
          this.model.update(
            buffered.id,
            JSON.stringify({ ...buffered, connection: slot.name }),
          );
      }),
    );
    if (this.error) return;
    this.newSlotName = "";
    this.newSlotNameEdited = false;
    this.newSlotConnector = "";
    this.message = `Added connection slot ${slot.name}.`;
    this.cdr.markForCheck();
  }
  /** Plain-language reasons the selected action step cannot run yet. */
  integrationIssues() {
    let step: Step;
    try {
      step = this.bufferStep();
    } catch {
      return ["Fix the step configuration JSON."];
    }
    const issues: string[] = [];
    const uses = String(step["uses"] ?? "");
    if (!uses || uses === placeholderAction)
      issues.push(
        this.integrationState === "ready"
          ? "Choose a published action."
          : "Enter the action version to call.",
      );
    else if (
      this.integrationState === "ready" &&
      !this.selectedCatalogAction &&
      !this.catalogAppending
    )
      issues.push(
        `${uses} is not in the published catalog for this project. Choose a published action.`,
      );
    const contract = this.actionContract;
    if (contract) {
      const literal = this.literalInput(step["with"]);
      // Field by field, a field bound to data or a formula counts as set.
      const missing =
        this.actionInputMode === "fields"
          ? this.actionMissing
          : literal && this.actionInputFields()
            ? missingRequired(withoutSecrets(this.actionInputSchema()), literal)
            : [];
      if (missing.length)
        issues.push(`Fill in the required inputs: ${missing.join(", ")}.`);
    }
    const connection = String(step["connection"] ?? "");
    const requirement = contract ? this.actionRequirement() : null;
    const slot = this.workflowSlots.find((s) => s.name === connection);
    if (connection && !slot)
      issues.push(
        `The workflow has no connection slot named ${connection}. Choose another slot or add it.`,
      );
    else if (requirement && !connection && requirement.required !== false)
      issues.push(`Choose a connection slot for ${requirement.connector}.`);
    else if (
      requirement &&
      slot &&
      !this.compatibleSlots().some((s) => s.name === slot.name)
    )
      issues.push(
        `Slot ${slot.name} does not provide a required ${requirement.connector} connection.`,
      );
    else if (contract && !requirement && connection)
      issues.push("This action does not use a connection. Clear the slot.");
    return issues;
  }
  chooseCatalogAction(id: string) {
    if (!id || !this.selected || this.selected.step.kind !== "action") return;
    const item = this.actionVersions.find((entry) => entry["id"] === id);
    if (!item) return;
    const step = this.editableStep();
    if (!step) return;
    this.actionChoiceRevision = null;
    step["uses"] = `${item["name"]}@${item["version"]}`;
    this.setBuffer(step, true);
    this.syncActionContract(true);
    this.flushInspector();
    this.actionChoiceRevision = this.model.revision;
  }
  /** The action version in the inspector buffer, for the action picker. */
  bufferedUses(): string | null {
    try {
      return String(this.bufferStep()["uses"] ?? "") || null;
    } catch {
      return null;
    }
  }
  /** Action contracts already loaded, for the action pickers' details. */
  get catalogContracts(): ReadonlyMap<string, Record<string, unknown>> {
    if (this.contractView.revision !== this.contractRevision)
      this.contractView = {
        revision: this.contractRevision,
        map: new Map(this.contractCache),
      };
    return this.contractView.map;
  }
  /** Caches an action contract, or drops it (null) so it loads again. */
  cacheContract(uses: string, document: Record<string, unknown> | null) {
    if (document) this.contractCache.set(uses, document);
    else this.contractCache.delete(uses);
    this.contractRevision++;
    this.infoCache.tick = -1;
  }
  /** The selected step's slot when it names a weave-http@2.0.0 connection. */
  httpSlot(): string {
    const name = this.bufferedConnection();
    const slot = this.workflowSlots.find((s) => s.name === name);
    return slot?.connector === "weave-http@2.0.0" ? name : "";
  }
  /** Opens the API action builder; "step" fills the selected action step. */
  async openApiBuilder(context: UseContext, tab: BuilderTab = "describe") {
    if (context !== "none" && !(await this.ensureApplied())) return;
    const step =
      context === "step" && this.selected?.step.kind === "action"
        ? this.selected.step.id
        : "";
    this.showPalette = false;
    this.apiBuilder = {
      context: context === "step" && !step ? "workflow" : context,
      tab,
      step,
    };
    this.cdr.markForCheck();
  }
  /** Opens "Connect to an API", prefilled from the builder's hand-off for this slot. */
  openAiConnectionDialog() {
    this.connectionDialog = { kind: "ai", fromBuilder: null };
  }
  openConnectionDialog(slot = "") {
    const use = this.lastUse;
    this.connectionDialog = {
      fromWorkflow: this.view === "designer",
      fromBuilder:
        use && (!slot || use.slot === slot)
          ? use.connection
          : slot
            ? { name: slot, baseUrl: null, auth: null }
            : null,
    };
  }
  /** "New ▾": a blank workflow, a template, or the API action builder. */
  newChoice(choice: NewChoice) {
    if (choice === "blank") this.newWorkflow();
    else if (choice === "template") this.showTemplates = true;
    else void this.openApiBuilder("none");
  }
  /**
   * Focuses a step's node once it is rendered. A dialog closing in the same
   * render returns focus to its opener in a microtask, so this runs after it.
   */
  focusStep(id: string) {
    const selection = this.model.selected;
    this.focusLater(() =>
      this.model.selected === selection
        ? document.querySelector<HTMLElement>(
            `[data-step="${CSS.escape(id)}"] :is(.node-body, .tile-body)`,
          )
        : null,
    );
  }
  /** Moves focus to the inspector's Action section, for example after "Use in this step". */
  focusInspector() {
    this.focusLater(() => document.getElementById("integration-heading"));
  }
  private focusLater(find: () => HTMLElement | null) {
    this.cdr.markForCheck();
    afterNextRender(() => setTimeout(() => find()?.focus()), {
      injector: this.injector,
    });
  }
  /**
   * The API action builder entries: offline it makes a placeholder and a
   * file; connected, its actions need definition.publish (WP-19).
   */
  canCreateIntegration() {
    return !this.profile || this.can("definition.publish");
  }
  refreshView() {
    this.cdr.markForCheck();
  }
  /** Opens a template as a new draft (Home and the "New ▾" menu). */
  async startFromTemplate(choice: { title: string; yaml: string }) {
    const bridge = await import("./integrations/editor-bridge");
    bridge.startFromTemplate(this, choice);
  }
  actionInputChange(expression: unknown) {
    const step = this.editableStep();
    if (!step) return;
    step["with"] = expression;
    this.setBuffer(step);
  }
  /**
   * Switches between field-by-field input and one custom expression for the
   * whole input. Fields can't show some expressions; replacing one asks first.
   * An invalid draft keeps the switch from hiding it.
   */
  async customInput(custom: boolean) {
    if (!this.leaveInspector()) return;
    const step = this.editableStep();
    if (!step) return;
    if (!custom && !fieldable(step["with"])) {
      if (
        !(await this.dialogs.confirm({
          title: "Edit the input field by field?",
          message:
            "The input is one custom expression that fields can't show. Editing field by field replaces it with empty fields.",
          confirmLabel: "Replace with fields",
          cancelLabel: "Keep the expression",
        }))
      )
        return;
      step["with"] = { literal: this.withDefaults({}) };
      this.setBuffer(step, true);
    }
    this.actionInitialInput = step["with"];
    // The whole-input expression starts from the input typed so far.
    this.propertyStep = structuredClone(step);
    this.actionInputMode = custom ? "expression" : "fields";
    this.actionInputValid.set(true);
    this.cdr.markForCheck();
  }
  async copyOutputReference() {
    const reference = `/steps/${this.selected?.step.id}/output`;
    try {
      await navigator.clipboard.writeText(reference);
      this.message = `Copied ${reference}.`;
    } catch {
      this.message = `Copy ${reference} from the inspector.`;
    }
    this.cdr.markForCheck();
  }
  async useActionOutput() {
    if (
      !this.selected ||
      !this.actionContract ||
      this.model.readonly ||
      this.editingLocked ||
      !this.canApply ||
      this.selected.owner !== "root"
    )
      return;
    this.flushInspector();
    const opened = this.model.opened,
      revision = this.model.revision,
      stepId = this.selected.step.id;
    const schema = structuredClone(this.actionSpec()["outputSchema"] ?? {});
    if (
      !(await this.dialogs.confirm({
        title: "Replace the workflow result?",
        message:
          "This uses the action's output and schema as the workflow result. The current result mapping will be replaced. You can undo this change.",
        confirmLabel: "Replace result",
        cancelLabel: "Keep current result",
      }))
    )
      return;
    if (
      opened !== this.model.opened ||
      revision !== this.model.revision ||
      stepId !== this.selected?.step.id ||
      this.editingLocked ||
      this.model.readonly
    ) {
      this.notify(
        "The draft changed while the dialog was open. Review the result again.",
      );
      return;
    }
    const doc = structuredClone(this.model.definition);
    doc.spec.output = { ref: `/steps/${stepId}/output` };
    doc.spec["outputSchema"] = schema;
    this.perform(() => this.model.updateWorkflow(doc));
    if (this.error) return;
    const changedRevision = this.model.revision;
    this.notify("Workflow result updated.", {
      label: "Undo",
      run: () => {
        if (
          this.model.opened === opened &&
          this.model.revision === changedRevision
        )
          this.undo();
        else
          this.notify("The workflow changed since. Use Undo in the toolbar.");
      },
    });
    this.cdr.markForCheck();
  }
  serializeStep(step: unknown) {
    return JSON.stringify(step, null, 2);
  }
  get canApply() {
    return (
      !this.model.readonly &&
      this.propertyValid() &&
      (this.actionInputMode !== "fields" ||
        !this.actionContract ||
        this.actionInputValid())
    );
  }
  updateStep() {
    const form = document.querySelector<HTMLFormElement>(".action-input-form");
    if (
      this.selected?.step.kind === "action" &&
      this.actionInputMode === "fields" &&
      form &&
      !form.reportValidity()
    )
      return;
    if (!this.canApply) return;
    this.perform(() =>
      this.model.update(this.model.selected, this.inspectorBuffer),
    );
    if (!this.error) this.message = "Changes applied.";
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
  async drop(event: DragEvent, target?: Target) {
    event.preventDefault();
    event.stopPropagation();
    const kind = event.dataTransfer?.getData("application/weave-step") as Kind;
    if (!this.kinds.includes(kind) || this.editingLocked) return;
    this.dragPreview = null;
    // The new step takes the inspector, so unapplied edits are settled first.
    if (!(await this.ensureApplied())) return;
    this.perform(() => {
      if (target) this.model.insert(kind, target.owner, target.index);
      else this.model.addUnplaced(kind);
    });
    if (!this.error) {
      this.message = target
        ? `Inserted ${this.model.selected}, ${this.label(kind)}.`
        : "Unplaced step created. Place it in the sequence before exporting.";
      this.revealSelected();
    }
    this.cdr.markForCheck();
  }
  pointerDown(event: PointerEvent, node: Node) {
    if (event.button !== 0 || this.model.readonly || this.editingLocked) return;
    // Switching away from unapplied edits asks first; no drag starts meanwhile.
    const asks = this.model.selected !== node.step.id && !!this.editsPending;
    void this.select(node, false);
    if (asks) return;
    this.dragNode = {
      id: node.step.id,
      start: { x: event.clientX, y: event.clientY },
      origin: { x: event.clientX, y: event.clientY },
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
    this.dragTarget = this.targetAt(event.clientX, event.clientY);
  }
  private targetAt(x: number, y: number): Target | null {
    const elements =
      document.querySelectorAll<HTMLElement>(".insertion-target");
    for (const element of elements) {
      const box = element.getBoundingClientRect();
      if (x < box.left || x > box.right || y < box.top || y > box.bottom)
        continue;
      const target = this.targets.find(
        (candidate) =>
          candidate.owner === element.dataset["owner"] &&
          candidate.index === Number(element.dataset["index"]),
      );
      if (target && this.validMoveTarget(target)) return target;
    }
    return null;
  }
  validMoveTarget(target: Target) {
    const id = this.dragNode?.id ?? this.connectingNode;
    if (!id) return false;
    const node = this.nodeById.get(id);
    if (!node) return true;
    if (
      target.owner === node.owner &&
      (target.index === node.index || target.index === node.index + 1)
    )
      return false;
    let owner = target.owner;
    while (owner !== "root") {
      const parent = owner.split("/")[0];
      if (parent === id) return false;
      owner = this.nodeById.get(parent)?.owner ?? "root";
    }
    return true;
  }
  pointerUp(event: PointerEvent) {
    if (!this.dragNode) return;
    const drag = this.dragNode;
    const target = this.targetAt(event.clientX, event.clientY);
    this.dragNode = null;
    this.dragTarget = null;
    // A click is not a move: it must not checkpoint layout or reset the inspector.
    if (
      Math.hypot(event.clientX - drag.origin.x, event.clientY - drag.origin.y) <
      4
    )
      return;
    this.suppressNodeClick = true;
    setTimeout(() => {
      this.suppressNodeClick = false;
    }, 0);
    if (target) this.moveTo(drag.id, target);
    else this.notify("Drop on a highlighted + to move this step.");
  }
  nodePoint(node: Node) {
    return this.dragNode?.id === node.step.id
      ? this.dragNode.point
      : node.point;
  }
  cancelGesture() {
    this.dragNode = null;
    this.dragTarget = null;
    this.dragPreview = null;
    this.connectingNode = "";
  }
  cancelMove() {
    const moving = this.connectingNode;
    this.cancelGesture();
    if (moving) {
      this.notify(`Stopped moving ${moving}.`);
      this.focusStep(moving);
    }
  }
  connect(id: string) {
    this.connectingNode = id;
    this.message =
      "Choose an insertion target to move this step in the structured sequence.";
  }
  /**
   * A "+" moves the step chosen with "Move" there; otherwise it opens the
   * step picker at that position (a second click closes it).
   */
  connectTo(target: Target, event?: Event) {
    if (this.editingLocked) return;
    if (this.connectingNode) {
      const id = this.connectingNode;
      this.connectingNode = "";
      this.moveTo(id, target);
      return;
    }
    if (this.pickerOpenAt(target)) {
      this.picker = null;
      return;
    }
    const anchor = event?.currentTarget;
    if (this.model.readonly || !(anchor instanceof HTMLElement)) return;
    this.picker = { target, anchor };
    this.connectingNode = "";
    if (this.profile) void this.loadActionCatalog();
  }
  private moveTo(id: string, target: Target) {
    this.perform(() => {
      if (this.model.unplaced.some((step) => step.id === id))
        this.model.place(id, target.owner, target.index);
      else this.model.move(id, target.owner, target.index);
    });
    if (this.error) return;
    const revision = this.model.revision;
    this.notify(`Moved ${id}.`, {
      label: "Undo",
      run: () => {
        if (this.model.revision !== revision)
          return this.notify(
            "The workflow changed since. Use Undo in the toolbar.",
          );
        this.undo();
        this.focusStep(id);
      },
    });
    this.focusStep(id);
  }
  nodeActions(node: Node): RowMenuItem[] {
    const disabled = this.model.readonly || this.editingLocked;
    return [
      ...(node.step.kind === "humanTask"
        ? [
            {
              label: "Add paths for answers",
              disabled,
              run: () => void this.branchOnDecision(node.step.id),
            },
          ]
        : []),
      { label: "Move to…", disabled, run: () => this.startMove(node.step.id) },
      {
        label: "Duplicate",
        disabled,
        run: () => void this.duplicate(node.step.id),
      },
      {
        label: "Delete step",
        disabled,
        danger: true,
        run: () => void this.remove(node.step.id),
      },
    ];
  }
  pickerOpenAt(target: Target) {
    return (
      this.picker?.target.owner === target.owner &&
      this.picker.target.index === target.index
    );
  }
  /** Inserts the picked step at the picker's "+" and focuses the new node. */
  async insertFromPicker(choice: StepPickerChoice) {
    const picker = this.picker;
    this.picker = null;
    if (!picker || !(await this.ensureApplied())) return;
    if (choice.uses) {
      // A published action comes with its connection slot (WP-19).
      const bridge = await import("./integrations/editor-bridge");
      await bridge.insertCatalogAction(
        this,
        { uses: choice.uses },
        picker.target,
        true,
      );
      return;
    }
    this.perform(() =>
      this.model.insert(
        choice.kind,
        picker.target.owner,
        picker.target.index,
        choice.uses ? { uses: choice.uses } : {},
      ),
    );
    if (this.error) return;
    const id = this.model.selected;
    this.message = `Inserted ${id}, ${this.label(choice.kind)}.`;
    this.revealSelected();
    this.cdr.markForCheck();
    afterNextRender(
      () =>
        document
          .querySelector<HTMLElement>(
            `[data-step="${CSS.escape(id)}"] :is(.node-body, .tile-body)`,
          )
          ?.focus(),
      { injector: this.injector },
    );
  }
  /** Where a "+" puts a step, for the picker's title: "after check". */
  pickerPlace(target: Target) {
    return target.label.replace(/^Add a step(?: here,)?\s*/, "");
  }
  private openInsertionAtFocus(element: HTMLElement) {
    if (this.editingLocked || this.model.readonly) return;
    const slot = element.closest<HTMLElement>(".insertion-target");
    const id =
      element.closest("[data-step]")?.getAttribute("data-step") ??
      this.model.selected;
    const node = this.nodeById.get(id);
    const target = slot
      ? this.targets.find(
          (target) =>
            target.owner === slot.dataset["owner"] &&
            target.index === Number(slot.dataset["index"]),
        )
      : node
        ? this.targets.find(
            (target) =>
              target.owner === node.owner && target.index === node.index + 1,
          )
        : this.targets.find(
            (target) => target.owner === "root" && target.index === 0,
          );
    if (!target) return;
    if (this.zoom < 0.4) {
      this.setView(0.4, this.pan);
      afterNextRender(() => this.openInsertionAtFocus(element), {
        injector: this.injector,
      });
      return;
    }
    const anchor =
      document.querySelector<HTMLElement>(
        `.insertion-target[data-owner="${CSS.escape(target.owner)}"][data-index="${target.index}"]`,
      ) ?? document.querySelector<HTMLElement>(".first-step");
    if (!anchor) return;
    this.revealTarget(target);
    this.picker = { target, anchor };
    if (this.profile) void this.loadActionCatalog();
  }
  /** The canvas element and its size, or null while it isn't shown. */
  private canvasBox() {
    const element = document.querySelector<HTMLElement>(
      ".canvas:not(.canvas-v2)",
    );
    return element && element.clientWidth
      ? {
          element,
          width: element.clientWidth,
          height: element.clientHeight,
        }
      : null;
  }
  /** Applies a viewport and keeps it with the workflow's layout. */
  private setView(zoom: number, pan: { x: number; y: number }) {
    this.zoom = zoom;
    this.pan = pan;
    this.model.layout.viewport = { x: pan.x, y: pan.y, zoom };
    this.cdr.markForCheck();
  }
  /** "+" and "−": ×1.2 or ÷1.2 around the canvas center. */
  zoomBy(factor: number) {
    // The person chose a view: a fit still waiting for the canvas yields.
    this.needsFit = false;
    const box = this.canvasBox();
    const center = box
      ? { x: box.width / 2, y: box.height / 2 }
      : { x: 0, y: 0 };
    const view = zoomAt({ zoom: this.zoom, pan: this.pan }, factor, center);
    this.setView(view.zoom, view.pan);
  }
  /** Back to 100 % around the canvas center (⌘/Ctrl 0). */
  zoomReset() {
    this.zoomBy(1 / this.zoom);
  }
  /** Below 60 % the canvas emphasizes names, lanes and problem markers. */
  get overview() {
    return this.zoom < OVERVIEW_BELOW;
  }
  needsFit = false;
  /** The next fit keeps a viewport restored with the workflow instead. */
  private restoredView = false;
  scheduleFit() {
    this.needsFit = true;
    this.cdr.detectChanges();
  }
  fitAfterRender() {
    if (!this.needsFit) return;
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        // A view the person chose meanwhile (zoom, Fit all) stays.
        if (!this.needsFit) return;
        if (this.view === "designer" && this.tab === "Designer") {
          const kept = this.restoredView
            ? usableViewport(this.model.layout.viewport)
            : null;
          this.restoredView = false;
          if (kept) this.setView(kept.zoom, kept.pan);
          else this.fit();
          this.needsFit = false;
          this.cdr.markForCheck();
        }
      }),
    );
  }
  /** The full extent includes lane borders and insertion targets. */
  private graphBounds(): Bounds {
    const bounds = this.boundaries;
    const points = [
      ...this.nodes.map((n) => n.point),
      ...[...this.placeholderByOwner.values()].map((p) => p.point),
      bounds.start,
      bounds.end,
      ...this.laneGroups.map((group) => group.point),
    ];
    return {
      minX: Math.min(...points.map((p) => p.x)),
      minY: Math.min(...points.map((p) => p.y)),
      maxX: Math.max(
        ...points.map((p) => p.x + 240),
        ...this.laneGroups.map((group) => group.point.x + group.width),
      ),
      maxY: Math.max(
        ...points.map((p) => p.y + 72),
        ...this.laneGroups.map((group) => group.point.y + group.height),
      ),
    };
  }
  /**
   * The readable fit, used when a workflow opens and after "Tidy layout":
   * 75–100 %, the main sequence centered, Start near the top.
   */
  fit() {
    const box = this.canvasBox();
    if (!box) {
      this.setView(1, { x: 0, y: 0 });
      return;
    }
    const view = readableFit(
      this.graphBounds(),
      box,
      this.boundaries.start.x + 104,
    );
    this.setView(view.zoom, view.pan);
  }
  /** "Fit all" (⇧1): include the complete workflow, including End. */
  fitAll() {
    this.needsFit = false;
    const box = this.canvasBox();
    if (!box) return;
    const view = fitAllView(this.graphBounds(), box);
    this.setView(view.zoom, view.pan);
  }
  /** Reset saved positions and fit the complete workflow, including a no-op tidy. */
  tidyLayout() {
    if (this.editingLocked) return;
    const changed = Object.keys(this.model.layout.positions).length > 0;
    if (changed) this.perform(() => this.model.autoLayout());
    this.fitAll();
    this.notify(changed ? "Layout tidied" : "Layout is already tidy");
  }
  /** Scroll pans; Ctrl or ⌘ with the wheel (or a pinch) zooms at the pointer. */
  panCanvas(event: WheelEvent) {
    event.preventDefault();
    this.needsFit = false;
    if (event.ctrlKey || event.metaKey) {
      const box = (event.currentTarget as HTMLElement).getBoundingClientRect();
      const view = zoomAt(
        { zoom: this.zoom, pan: this.pan },
        wheelFactor(event.deltaY),
        { x: event.clientX - box.left, y: event.clientY - box.top },
      );
      this.setView(view.zoom, view.pan);
    } else
      this.setView(this.zoom, {
        x: this.pan.x - event.deltaX,
        y: this.pan.y - event.deltaY,
      });
  }
  /**
   * Pans so a step's card sits at least `margin` px inside the canvas, for
   * example after an insert or when keyboard focus reaches it.
   */
  revealStep(id: string, margin = 64) {
    const node = this.nodeById.get(id);
    const box = this.canvasBox();
    if (!node || !box) return;
    const pan = reveal(
      { zoom: this.zoom, pan: this.pan },
      { x: node.point.x, y: node.point.y, width: 240, height: 72 },
      box,
      margin,
    );
    if (pan.x !== this.pan.x || pan.y !== this.pan.y)
      this.setView(this.zoom, pan);
  }
  /**
   * The canvas moves only by its pan. When the browser scrolls it anyway,
   * to show a focused step or "+", the scroll becomes pan: the graph stays
   * where the browser put it, and the next pan starts from there.
   */
  scrollToPan(event: Event) {
    const element = event.target as HTMLElement;
    const dx = element.scrollLeft;
    const dy = element.scrollTop;
    if (!dx && !dy) return;
    element.scrollLeft = 0;
    element.scrollTop = 0;
    this.setView(this.zoom, { x: this.pan.x - dx, y: this.pan.y - dy });
  }
  /**
   * Runs a reveal once the browser's own scroll to show focus has become
   * pan (scroll events come before the next frame's callbacks), so the two
   * don't add up.
   */
  private afterFocusScroll(reveal: () => void) {
    requestAnimationFrame(() => reveal());
  }
  /** Keyboard focus on a "+" pans it into view, like a step. */
  targetFocused(target: Target) {
    this.afterFocusScroll(() => this.revealTarget(target));
  }
  revealTarget(target: Target, margin = 64) {
    const box = this.canvasBox();
    if (!box) return;
    const width = target.empty ? 208 : 24;
    const height = target.empty ? 48 : 20;
    const pan = reveal(
      { zoom: this.zoom, pan: this.pan },
      {
        x: target.point.x - width / 2,
        y: target.point.y - height / 2,
        width,
        height,
      },
      box,
      margin,
    );
    if (pan.x !== this.pan.x || pan.y !== this.pan.y)
      this.setView(this.zoom, pan);
  }
  /** Keeps the selected step visible once the panels settle. */
  revealSelected() {
    const id = this.model.selected;
    if (!id) return;
    // An explicit selection or edit supersedes a still-pending initial fit.
    this.needsFit = false;
    afterNextRender(
      () =>
        requestAnimationFrame(() => {
          if (this.model.selected !== id) return;
          const group = this.laneGroups.find((group) => group.id === id);
          const node = this.nodeById.get(id);
          const box = this.canvasBox();
          if (!group || !node || !box) return this.revealStep(id);
          const top = Math.min(group.point.y, node.point.y);
          const view = revealGroup(
            { zoom: this.zoom, pan: this.pan },
            {
              x: group.point.x,
              y: top,
              width: group.width,
              height: group.point.y + group.height - top,
            },
            box,
          );
          this.setView(view.zoom, view.pan);
        }),
      {
        injector: this.injector,
      },
    );
  }
  graphHeight() {
    this.nodes;
    return this.graphSize.height;
  }
  graphWidth() {
    this.nodes;
    return this.graphSize.width;
  }
  connectionPath(
    model: StructuredCanvasAdapter,
    from: string,
    to: string,
    nodes: Node[],
  ) {
    const boundaries =
      model === this.model ? this.cachedBoundaries : model.boundaries();
    const junctions =
      model === this.model ? this.cachedJunctions : model.junctions();
    const endpoint = (id: string, outgoing: boolean) => {
      if (junctions[id]) return junctions[id];
      const boundary = id === "$start" || id === "$end";
      const placeholder = id.startsWith("$empty:");
      const p =
        id === "$start"
          ? boundaries.start
          : id === "$end"
            ? boundaries.end
            : placeholder
              ? model === this.model
                ? this.placeholderByOwner.get(id.slice(7))!.point
                : model
                    .placeholders()
                    .find((item) => item.owner === id.slice(7))!.point
              : model === this.model
                ? this.nodePoint(this.nodeById.get(id)!)
                : nodes.find((node) => node.step.id === id)!.point;
      return {
        x: p.x + (boundary || placeholder ? 104 : 120),
        y: p.y + (outgoing ? (boundary || placeholder ? 48 : 72) : 0),
      };
    };
    const a = endpoint(from, true),
      b = endpoint(to, false);
    const middle = a.y + Math.max(0, (b.y - a.y) / 2);
    return `M ${a.x} ${a.y} L ${a.x} ${middle} L ${b.x} ${middle} L ${b.x} ${b.y}`;
  }
  /**
   * The Validate command: a fresh compile against the project catalog when
   * connected with `compile`, otherwise the local check. Resolves true when
   * the workflow may be published: a complete, error-free project compile,
   * or, for an account that can't compile, a local check without errors
   * (the platform compiles again when it publishes).
   */
  async validate(explicit = false): Promise<boolean> {
    if (!(await this.ensureApplied())) return false;
    if (this.model.unplaced.length) {
      this.error = "Place or remove all unplaced steps before validation.";
      return false;
    }
    const source = this.model.source,
      format = this.model.format;
    this.busy = "validate";
    try {
      const service = await this.validator();
      const outcome = await service.validateNow(source, format);
      if (source !== this.model.source) return false;
      this.shownResult = outcome.result;
      this.showDiagnostics(outcome.result as Validation);
      const passed =
        outcome.mode === "project"
          ? service.canPublish(source, format)
          : outcome.result.validationOk;
      const problems = (outcome.result.diagnostics ?? []).filter(
        (d) => d.severity !== "info",
      ).length;
      const text = passed
        ? problems
          ? `${problems === 1 ? "1 problem" : `${problems} problems`} found.`
          : "No problems found."
        : outcome.result.validationOk
          ? service.publishBlocker(source, format)
          : `${problems === 1 ? "1 problem" : `${problems || "Some"} problems`} found.`;
      if (explicit) {
        this.notify(text);
        if (problems) this.diagnosticsOpen = true;
      } else this.message = text;
      this.error = "";
      return passed;
    } catch (e) {
      this.fail(e);
      return false;
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  async refreshHome() {
    this.homeTasks = [];
    this.homeRuns = [];
    this.homeAttention = [];
    this.homeDrafts = [];
    this.refreshLocalList();
    const sequence = ++this.listSequence;
    const scope = this.profile;
    if (!scope || !this.paired) return;
    this.loading = true;
    const current = () =>
      sequence === this.listSequence &&
      this.view === "home" &&
      scope === this.profile;
    const jobs: Promise<unknown>[] = [];
    let ready: Record<string, unknown>[] = [];
    let failed: Record<string, unknown>[] = [];
    let waiting: Record<string, unknown>[] = [];
    if (this.canReadTasks)
      jobs.push(
        Promise.all([
          this.api.page("human-tasks", true, undefined, { status: "ready" }),
          this.api.page("human-tasks", true, undefined, { status: "claimed" }),
        ]).then((pages) => {
          if (!current()) return;
          ready = pages[0].items.filter((t) => t["status"] === "ready");
          this.homeTasks = pages[1].items
            .filter((t) => t["claimant_id"] === this.identity?.principal_id)
            .slice(0, 5);
        }),
      );
    if (this.can("run.read")) {
      void this.loadWorkflowVersions();
      jobs.push(
        this.api.page("runs", true).then((page) => {
          this.rememberRuns(page.items);
          if (current()) this.homeRuns = page.items.slice(0, 5);
        }),
        Promise.all(
          ["failed", "waiting"].map((status) =>
            this.api.page("runs", true, undefined, { status }),
          ),
        ).then(([failedPage, waitingPage]) => {
          failed = failedPage.items.filter((r) =>
            ["failed", "timed_out"].includes(runStatus(r)),
          );
          waiting = waitingPage.items.filter((r) =>
            ["waiting", "suspended", "paused"].includes(runStatus(r)),
          );
        }),
      );
    }
    if (this.can("definition.write") || this.can("catalog.read"))
      jobs.push(
        this.api.page("drafts", false).then((page) => {
          if (current()) this.homeDrafts = page.items.slice(0, 5);
        }),
      );
    const results = await Promise.allSettled(jobs);
    if (!current()) return;
    this.homeAttention = [
      ...ready.slice(0, 5).map((record) => ({ kind: "task" as const, record })),
      ...failed.slice(0, 5).map((record) => ({ kind: "run" as const, record })),
      ...waiting
        .slice(0, 5)
        .map((record) => ({ kind: "run" as const, record })),
    ];
    for (const result of results)
      if (result.status === "rejected") this.fail(result.reason);
    this.loading = false;
    this.cdr.markForCheck();
  }
  /** Published versions by ID, read once per workspace for run titles. */
  private async loadWorkflowVersions() {
    if (!this.profile || this.versionsScope === this.profile) return;
    const scope = (this.versionsScope = this.profile);
    try {
      const page = await this.api.page("workflows", false);
      if (scope !== this.profile) return;
      for (const item of page.items) this.rememberVersion(item);
      // A new map, so views that read it as an input see the change.
      this.workflowVersions = new Map(this.workflowVersions);
      this.cdr.markForCheck();
    } catch {
      // Runs then show their workflow name without the version.
      if (scope === this.profile) this.versionsScope = null;
    }
  }
  private rememberRuns(runs: Record<string, unknown>[]) {
    for (const run of runs)
      if (typeof run["id"] === "string") this.knownRuns.set(run["id"], run);
  }
  private rememberVersion(item: Record<string, unknown>) {
    if (typeof item["id"] === "string" && typeof item["name"] === "string")
      this.workflowVersions.set(item["id"], {
        name: item["name"],
        version: String(item["version"] ?? ""),
      });
  }
  /** Activations by published version ID, for the Workflows status column. */
  activationsByVersion = new Map<string, Record<string, unknown>>();
  async refresh(append = false) {
    if (this.view === "settings") {
      await this.refreshConnectionStatus();
      await this.loadAdministration();
      return;
    }
    if (this.view === "connect" || this.view === "operations") return;
    if (this.view === "home") {
      await this.refreshHome();
      return;
    }
    if (this.view === "workflows") this.refreshLocalList();
    if (
      !this.profile ||
      !this.paired ||
      (this.view === "workflows" && this.libraryCollection === "local")
    ) {
      // Another view's request still running must not fill this one.
      this.listSequence++;
      this.records = [];
      this.nextCursor = null;
      this.loading = false;
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
    if (this.view === "runs") void this.loadWorkflowVersions();
    if (this.view === "workflows" && this.libraryCollection === "workflows")
      void this.loadActivations(sequence);
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
      if (this.view === "runs") this.rememberRuns(result.items);
      if (this.view === "workflows" && this.libraryCollection === "workflows")
        for (const item of result.items) this.rememberVersion(item);
      this.error = "";
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
      if (sequence === this.listSequence) this.loading = false;
    }
  }
  /** Which published versions are active here (the Workflows status column). */
  private async loadActivations(sequence: number) {
    try {
      const page = await this.api.page("activations", true);
      if (sequence !== this.listSequence) return;
      this.activationsByVersion = new Map(
        page.items
          .filter((a) => !a["retired"])
          .map((a) => [
            String(
              (a["request"] as Record<string, unknown> | undefined)?.[
                "version_id"
              ] ?? "",
            ),
            a,
          ]),
      );
      this.cdr.markForCheck();
    } catch {
      // Without activations the status column says "Published".
    }
  }
  async open(record: Record<string, unknown>) {
    this.emailDetail = null;
    this.emailSubmission = null;
    this.runHistory = null;
    this.runCanvas = null;
    this.runLifecycle = null;
    this.runTask = null;
    this.taskData = {};
    this.taskFormValid = true;
    this.taskConfirm = "";
    this.selectedRecord = record;
    if (this.view === "workflows") {
      this.busy = "load";
      this.flushLocalSave();
      try {
        const detail = await this.api.request<Record<string, unknown>>(
          `${this.api.project}/${this.libraryCollection}/${record["id"]}${this.libraryCollection === "drafts" ? "" : "/export"}`,
        );
        const d = detail["definition"] ?? detail["document"];
        if (d && typeof d === "object" && this.libraryCollection === "drafts")
          this.openPlatformDraft(detail, d as Workflow);
        else if (d && typeof d === "object") {
          this.model.replace(d as Workflow);
          this.resetValidation();
          this.changed();
          this.dirty = false;
          this.view = "designer";
          this.activation = null;
          // A published version is never saved over the draft opened before it.
          this.draftId = crypto.randomUUID();
          this.draftRevision = undefined;
          this.published = record;
          this.saveState = "Published";
          this.openTab();
          void this.router.navigateByUrl(`/workflows/${this.draftId}/designer`);
        } else if (typeof detail["source"] === "string") {
          this.model.setSource(
            detail["source"],
            detail["format"] === "json" ? "json" : "yaml",
          );
          this.model.clearHistory();
          this.model.selected = "";
          this.resetValidation();
          this.changed();
          this.dirty = false;
          this.view = "designer";
          this.draftId = crypto.randomUUID();
          this.draftRevision = undefined;
          this.activation = null;
          this.published = record;
          this.saveState = "Published";
          this.openTab();
          void this.router.navigateByUrl(`/workflows/${this.draftId}/designer`);
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
  /** The person may read human tasks somewhere in this workspace. */
  get canReadTasks() {
    return (
      this.can("human_task.read") ||
      !!this.identity?.grants.some((g) =>
        g.resources.some((id) => this.can("human_task.read", id)),
      )
    );
  }
  /** The human task a waiting run waits for, when the person may read it. */
  runTask: Record<string, unknown> | null = null;
  /** Tasks filtered to one run ("Open My tasks" from a run). */
  taskRunFilter = "";
  private async findRunTask(run: Record<string, unknown>) {
    this.runTask = null;
    const state = (run["state"] ?? {}) as { active?: string[] };
    const node = this.runNodes().find(
      (n) => state.active?.includes(n.step.id) && n.step.kind === "humanTask",
    );
    if (!node || !this.canReadTasks) return;
    try {
      const pages = await Promise.all(
        ["ready", "claimed"].map((status) =>
          this.api.page("human-tasks", true, undefined, { status }),
        ),
      );
      if (this.selectedRecord?.["id"] !== run["id"]) return;
      this.runTask =
        pages
          .flatMap((page) => page.items)
          .find(
            (task) =>
              task["run_id"] === run["id"] && task["node_id"] === node.step.id,
          ) ?? null;
    } catch {
      // The run detail still says who it waits for, without the task.
    } finally {
      this.cdr.markForCheck();
    }
  }
  /** "Open task" from a run, or My tasks filtered to the run. */
  async openRunTask() {
    const run = this.selectedRecord;
    if (!run) return;
    const task = this.runTask;
    await this.navigate("tasks");
    if (this.view !== "tasks") return;
    if (!task) {
      this.taskRunFilter = String(run["id"] ?? "");
      return;
    }
    if (!this.records.some((r) => r["id"] === task["id"]))
      this.records = [task, ...this.records];
    await this.open(task);
  }
  async readDetail() {
    if (!this.selectedRecord) return;
    const collection = (
      {
        tasks: "human-tasks",
        email: "email/conversations",
        runs: "runs",
        connections: "connections",
        workers: "workers",
      } as Partial<Record<View, string>>
    )[this.view];
    // Unavailable list entries have no readable detail; never guess another collection.
    if (!collection || this.selectedRecord["unavailable"]) return;
    try {
      const id = this.selectedRecord["id"];
      const view = this.view;
      const detail = await this.api.request<Record<string, unknown>>(
        `${this.api.environment}/${collection}/${encodeURIComponent(String(id))}`,
      );
      if (view !== this.view || this.selectedRecord?.["id"] !== id) return;
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
          const versionId = activation.request.version_id;
          const exported = await this.api.request<{ document: Workflow }>(
            `${this.api.project}/workflows/${versionId}/export`,
          );
          const canvas = new StructuredCanvasAdapter();
          canvas.setSource(JSON.stringify(exported.document), "json");
          canvas.readonly = true;
          this.runCanvas = canvas;
          const metadata = exported.document?.metadata;
          if (metadata?.name)
            this.workflowVersions.set(versionId, {
              name: metadata.name,
              version: metadata.version ?? "",
            });
        }
        void this.findRunTask(detail);
      }
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
    }
  }
  async save() {
    if (!(await this.ensureApplied())) return;
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
      this.saveState = source === this.model.source ? "Draft saved" : "Unsaved";
      this.draftSavedAt = new Date().toISOString();
      if (source === this.model.source) this.dirty = false;
      this.notify("Draft saved.");
      this.error = "";
    } catch (e) {
      this.saveState = "Not saved";
      if (e instanceof ApiError && e.status === 409) {
        this.conflict = { local: this.model.definition, server: e.detail };
        this.error =
          "Someone else saved this draft. Your version is still here. Compare them before you save.";
      } else this.fail(e);
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  async publish() {
    // "Keep editing" must stop the publish, not only its validation.
    if (!(await this.ensureApplied())) return;
    if (this.model.readonly || this.model.unplaced.length) return;
    if (!(await this.validate())) return;
    if (
      !(await this.dialogs.confirm({
        title: `Publish ${this.model.definition.metadata.name} ${this.model.definition.metadata.version}?`,
        message:
          "Publishing creates a version that cannot be changed. Activate it separately to use it in an environment.",
        confirmLabel: "Publish version",
      }))
    )
      return;
    await this.sendPublish();
  }
  /**
   * Publishes the current source. A compile failure shows the platform's
   * diagnostics; a version that is already taken offers the next patch
   * version, published as a new request.
   */
  private async sendPublish() {
    const source = this.model.source;
    this.busy = "publish";
    try {
      this.published = await this.safeMutation<Record<string, unknown>>(
        `${this.api.project}/workflows`,
        "POST",
        { source, format: this.model.format },
        undefined,
        "Publish workflow",
      );
      this.activation = null;
      // The platform now holds exactly these edits, as a published version.
      if (source === this.model.source) {
        this.dirty = false;
        this.saveState = "Published";
      }
      const { name, version } = this.model.definition.metadata;
      this.notify(`Published ${name} ${version}.`, {
        label: "Activate",
        run: () => void this.runCommand("activate"),
      });
      this.error = "";
      this.errorCode = "";
    } catch (e) {
      const result =
        e instanceof ApiError
          ? ((e.detail as { result?: Validation } | null)?.result ?? null)
          : null;
      if (e instanceof ApiError && e.code === "WV-VERSION-CONFLICT") {
        await this.offerNextVersion();
        return;
      }
      if (e instanceof ApiError && e.code === "WV-COMPILE" && result) {
        this.shownResult = result;
        this.showDiagnostics(result);
        const { countDiagnostics, countLine } = await import(
          "./designer/validation"
        );
        const counts = countLine({
          ...countDiagnostics(result.diagnostics),
          errors: Math.max(
            1,
            result.errorCount,
            countDiagnostics(result.diagnostics).errors,
          ),
        });
        this.validationView = {
          tone: "errors",
          headline: `${counts}. Fix the errors to publish.`,
          countLine: counts,
        };
        this.error =
          "The platform found problems in this workflow. Review the diagnostics below.";
        this.errorCode = e.code;
        this.message = "";
      } else this.fail(e);
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  private async offerNextVersion() {
    const { version } = this.model.definition.metadata;
    const next = nextPatch(version);
    this.busy = "";
    if (
      !(await this.dialogs.confirm({
        title: `Version ${version} is already published`,
        message: `Another workflow is already published as ${this.model.definition.metadata.name} ${version}, and published versions can't change. Publish your changes as version ${next} instead.`,
        confirmLabel: `Publish as ${next}`,
        cancelLabel: "Keep editing",
      }))
    )
      return;
    const document = structuredClone(this.model.definition);
    document.metadata.version = next;
    this.perform(() => this.model.updateWorkflow(document));
    if (this.error) return;
    await this.sendPublish();
  }
  async activate() {
    if (!(await this.ensureApplied())) return;
    if (!this.published || !this.profile) return;
    this.activationError = null;
    this.showActivation = true;
  }
  closeActivation() {
    if (this.busy) return;
    this.showActivation = false;
    this.activationError = null;
  }
  /** Human task assignments declared anywhere in the workflow. */
  get workflowAssignments() {
    return [
      ...new Set(
        this.nodes
          .filter((n) => n.step.kind === "humanTask")
          .map((n) => String(n.step["assignment"] ?? ""))
          .filter(Boolean),
      ),
    ];
  }
  /** Worker task types of action contracts already loaded in this session. */
  get workflowWorkerTypes() {
    const types = new Set<string>();
    for (const n of this.nodes) {
      if (n.step.kind !== "action") continue;
      const contract = this.contractCache.get(String(n.step["uses"] ?? ""));
      const implementation = (
        (contract?.["spec"] ?? {}) as Record<string, Record<string, unknown>>
      )["implementation"];
      if (implementation?.["kind"] === "worker")
        types.add(String(implementation["taskType"]));
    }
    return [...types];
  }
  /** The dialog stays open while the request runs and closes only on success. */
  async confirmActivation(bindings: ActivationBindings) {
    if (!this.published || !this.profile || this.busy) return;
    this.activationError = null;
    const scope = {
      tenant_id: this.profile.tenantId,
      project_id: this.profile.projectId,
      environment_id: this.profile.environmentId,
    };
    try {
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
        undefined,
        "Activate version",
      );
      this.showActivation = false;
      const name = String(
        this.published["name"] ?? this.model.definition.metadata.name,
      );
      const version = String(
        this.published["version"] ?? this.model.definition.metadata.version,
      );
      this.notify(
        `Activated ${name} ${version} in ${this.workspaceText || this.environmentLabel || "this environment"}.`,
        { label: "Start run", run: () => void this.runCommand("run") },
      );
    } catch (e) {
      const plain = describeError(e);
      // An ended session already returned to pairing with its own explanation.
      if (plain.code === "WV-STUDIO-SESSION" && !this.paired) return;
      this.activationError = { message: plain.message, code: plain.code };
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  async startRun() {
    if (this.view === "designer" && !(await this.ensureApplied())) return;
    if (!this.profile) return;
    // From the designer, the dialog starts on the open workflow's version.
    this.runStartPreselect =
      this.view === "designer"
        ? {
            workflow: String(
              this.published?.["name"] ?? this.model.definition.metadata.name,
            ),
            version: String(
              this.published?.["version"] ??
                this.model.definition.metadata.version,
            ),
          }
        : null;
    this.runStartActivation = String(this.activation?.["id"] ?? "");
    this.runStartError = null;
    this.availableActivations = this.activation ? [this.activation] : [];
    this.showStartRun = true;
    this.cdr.markForCheck();
    void this.loadWorkflowVersions();
    try {
      const result = await this.api.page("activations", true);
      const listed = result.items.filter((a) => !a["retired"]);
      if (
        this.activation &&
        !listed.some((a) => a["id"] === this.activation!["id"])
      )
        listed.unshift(this.activation);
      this.availableActivations = listed;
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
    }
  }
  /** Starts the run; failures stay in the dialog, success opens the new run. */
  async confirmStartRun(body: StartRunRequest) {
    this.runStartError = null;
    try {
      const run = await this.safeMutation<Record<string, unknown>>(
        `${this.api.environment}/runs`,
        "POST",
        body,
        undefined,
        "Start run",
      );
      this.showStartRun = false;
      this.notify(`Started run ${shortId(run["id"])}.`, {
        label: "View",
        run: () => void this.viewRun(run),
      });
      if (this.view === "runs") {
        await this.refresh();
        // The list may not include a run this new yet; show it first.
        if (run["id"] && !this.records.some((r) => r["id"] === run["id"]))
          this.records = [run, ...this.records];
      }
    } catch (e) {
      const plain = describeError(e);
      if (plain.code === "WV-STUDIO-SESSION" && !this.paired) return;
      this.runStartError = { ...plain };
    } finally {
      this.cdr.markForCheck();
    }
  }
  /** Opens a platform draft from Home's "Recent workflows". */
  async openHomeDraft(record: Record<string, unknown>) {
    this.busy = "load";
    try {
      const detail = await this.api.request<Record<string, unknown>>(
        `${this.api.project}/drafts/${encodeURIComponent(String(record["id"]))}`,
      );
      const definition = detail["definition"] ?? detail["document"];
      if (!isRecord(definition))
        throw Error(
          "This draft can't be opened. Its source is still on the platform.",
        );
      this.openPlatformDraft(detail, definition as unknown as Workflow);
    } catch (e) {
      this.fail(e);
    } finally {
      this.busy = "";
      this.cdr.markForCheck();
    }
  }
  /** Opens one task in My tasks (from Home's "Needs you"). */
  async viewTask(task: Record<string, unknown>) {
    if (this.view !== "tasks") await this.navigate("tasks");
    if (this.view !== "tasks" || !task["id"]) return;
    if (!this.records.some((r) => r["id"] === task["id"]))
      this.records = [task, ...this.records];
    await this.open(this.records.find((r) => r["id"] === task["id"]) ?? task);
  }
  /** Opens one run's detail on the Runs page. */
  async viewRun(run: Record<string, unknown>) {
    if (this.view !== "runs") await this.navigate("runs");
    if (this.view !== "runs" || !run["id"]) return;
    if (!this.records.some((r) => r["id"] === run["id"]))
      this.records = [run, ...this.records];
    await this.open(this.records.find((r) => r["id"] === run["id"]) ?? run);
  }
  applyRunFilters() {
    if (this.runFilterTimer) clearTimeout(this.runFilterTimer);
    this.runFilterTimer = null;
    this.nextCursor = null;
    this.records = [];
    this.selectedRecord = null;
    void this.refresh();
  }
  private runFilterTimer: ReturnType<typeof setTimeout> | null = null;
  /**
   * Runs filters apply as they change: the typed keys after a 400 ms pause,
   * the status and "Include archived runs" at once.
   */
  setRunFilter(
    key: "business_key" | "correlation_key" | "status",
    value: string,
  ) {
    if (this.runFilters[key] === value) return;
    this.runFilters = { ...this.runFilters, [key]: value };
    if (key === "status") return this.applyRunFilters();
    if (this.runFilterTimer) clearTimeout(this.runFilterTimer);
    this.runFilterTimer = setTimeout(() => this.applyRunFilters(), 400);
  }
  toggleArchivedRuns() {
    this.runIncludeArchived = !this.runIncludeArchived;
    this.applyRunFilters();
  }
  /** How many Runs filters are set ("Filters (2)" on narrow windows). */
  get runFilterCount() {
    return (
      [
        this.runFilters.business_key,
        this.runFilters.correlation_key,
        this.runFilters.status,
      ].filter(Boolean).length + (this.runIncludeArchived ? 1 : 0)
    );
  }
  clearRunFilters() {
    this.runIncludeArchived = false;
    this.runFilters = { business_key: "", correlation_key: "", status: "" };
    this.applyRunFilters();
  }
  applyTaskFilter(event: Event) {
    this.taskFilter = this.value(event);
    this.taskRunFilter = "";
    this.nextCursor = null;
    this.records = [];
    this.selectedRecord = null;
    void this.refresh();
  }
  /**
   * Compiles the workflow (reusing a compile of the same source) and opens
   * the simulation setup for the artifact; the panel emits the requests.
   */
  async simulate() {
    if (!(await this.ensureApplied())) return;
    const source = this.model.source,
      format = this.model.format;
    this.busy = "simulate";
    try {
      const result = this.can("compile")
        ? await (await this.validator()).compile(source, format)
        : await this.api.request<Validation>(
            `${this.api.project}/compiler/compile`,
            "POST",
            { source, format },
          );
      if (!result.artifact) {
        this.shownResult = result;
        this.showDiagnostics(result as Validation);
        throw Error(
          "Simulation needs a fully compiled workflow. Fix the problems in the diagnostics first.",
        );
      }
      // A new artifact starts a new session: controls must match what runs.
      this.debugSession = null;
      this.simulationError = null;
      this.simulation = {
        artifact: result.artifact as Record<string, unknown>,
      };
      this.message = "Set up the simulation. No real systems are called.";
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  get simulationSession() {
    return this.debugSession as DebugSessionData | null;
  }
  createDebug(body: DebugCreateBody) {
    return this.debugRequest(`${this.api.project}/debug/sessions`, body);
  }
  debugCommand(body: DebugCommandBody) {
    const session = this.simulationSession;
    if (!session) return Promise.resolve();
    return this.debugRequest(
      `${this.api.project}/debug/sessions/${encodeURIComponent(session.id)}/commands`,
      body,
      { "If-Match": `"${session.revision}"` },
    );
  }
  /** Failures stay in the simulation panel, never in the page banner. */
  private async debugRequest(
    path: string,
    body: unknown,
    headers: Record<string, string> = {},
  ) {
    if (this.busy) return;
    this.simulationError = null;
    this.busy = "simulate";
    try {
      this.debugSession = await this.api.request<Record<string, unknown>>(
        path,
        "POST",
        body,
        headers,
      );
    } catch (e) {
      const plain = describeError(e);
      if (plain.code === "WV-STUDIO-SESSION" && !this.paired) return;
      this.simulationError = { ...plain };
    } finally {
      this.busy = "";
      this.cdr.markForCheck();
    }
  }
  closeSimulation() {
    this.simulation = null;
    this.debugSession = null;
    this.simulationError = null;
    this.simNodes = {
      status: "",
      current: [],
      active: [],
      breakpoints: [],
      done: [],
    };
  }
  /** Where the simulated run is, for a node's accessible name. */
  simulationNote(id: string) {
    const nodes = this.simNodes;
    return nodes.current.includes(id)
      ? " The simulation is here."
      : nodes.active.includes(id)
        ? " The simulation is waiting here."
        : nodes.done.includes(id)
          ? " The simulation finished this step."
          : nodes.breakpoints.includes(id)
            ? " The simulation pauses before this step."
            : "";
  }
  /**
   * The live thread: where the simulated run is (gold, one pulse per
   * move), the steps it finished (jade check) and those it hasn't reached
   * (faded), and the edges it took.
   */
  simState(id: string): "live" | "done" | "unreached" | "" {
    const nodes = this.simNodes;
    if (!this.simulationSession) return "";
    if (nodes.current.includes(id) || nodes.active.includes(id)) return "live";
    if (nodes.done.includes(id)) return "done";
    return "unreached";
  }
  /** True when the simulated run went from one card to the next. */
  edgeTaken(key: string) {
    if (!this.simulationSession) return false;
    const [from, to] = key.split(">");
    const reached = (id: string) =>
      id === "$start" ||
      this.simNodes.done.includes(id) ||
      (id === "$end" && ["succeeded"].includes(this.simNodes.status));
    const here = (id: string) =>
      reached(id) ||
      this.simNodes.current.includes(id) ||
      this.simNodes.active.includes(id);
    // An empty branch's card counts as passed when what follows it is.
    const through = (id: string) =>
      id.startsWith("$empty:") ? false : here(id);
    return reached(from) && through(to) && from !== to;
  }
  /** Bumped when the live step moves, so its card pulses once. */
  livePulse = 0;
  private liveKey = "";
  simulationNodes(nodes: SimulationNodes) {
    this.simNodes = nodes;
    const key = [...nodes.current, ...nodes.active].join(",");
    if (key && key !== this.liveKey) {
      this.livePulse++;
      const live = nodes.current[0] ?? nodes.active[0];
      if (live) this.revealStep(live, 64);
    }
    this.liveKey = key;
    this.cdr.markForCheck();
  }
  async export() {
    if (!(await this.ensureApplied())) return;
    if (this.model.error || this.model.unplaced.length) {
      this.error = "Fix source and place every step before exporting.";
      this.errorCode = "";
      return;
    }
    const filename = this.model.definition.metadata.name;
    const files: ExportedFile[] = [
      {
        name: `${filename}.${this.model.format}`,
        mime:
          this.model.format === "json"
            ? "application/json"
            : "application/yaml",
        content: this.model.source,
      },
      {
        name: `${filename}.layout.json`,
        mime: "application/json",
        content: JSON.stringify(
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
      },
    ];
    const deliveries = files.map((file) =>
      exportFile(file.name, file.mime, file.content),
    );
    // A download that could not start is offered as copies instead.
    if (deliveries.includes("unconfirmed")) this.exportFallback = files;
    else this.message = exportNotice(files.map((f) => f.name));
    this.cdr.markForCheck();
  }
  async copyExport(file: ExportedFile) {
    try {
      await navigator.clipboard.writeText(file.content);
      this.message = `Copied ${file.name} to the clipboard.`;
    } catch {
      this.message = `Select the text of ${file.name} and copy it.`;
    }
    this.cdr.markForCheck();
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
  async import(event: Event) {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";
    if (file) await this.importWorkflow(file);
  }
  async importWorkflow(file: File) {
    if (this.view === "designer" && !this.leaveInspector()) return;
    this.flushLocalSave();
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
    this.keptLocally = false;
    this.restoredView = false;
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
    this.openTab();
    void this.router.navigateByUrl(`/workflows/${this.draftId}/designer`);
    this.resetValidation();
    this.changed();
    this.dirty = false;
    this.scheduleFit();
  }
  pretty(value: unknown) {
    return JSON.stringify(value, null, 2);
  }
  async loadIdentity() {
    if (!this.profile) return;
    try {
      this.identity = await this.api.request<Identity>(
        "/studio/api/api/v1/identity",
      );
      this.catalogAfterIdentity();
    } catch {
      this.identity = null;
    } finally {
      this.cdr.markForCheck();
    }
  }
  /**
   * An action step chosen while the account was still being checked found no
   * permissions yet; load the catalog for it now that they are known.
   */
  private catalogAfterIdentity() {
    if (
      this.identity &&
      this.catalogState === "idle" &&
      (this.selected?.step.kind === "action" || this.view === "designer")
    )
      void this.loadActionCatalog();
  }
  can(capability: string, resource?: string) {
    const p = this.profile;
    if (!p || !this.identity || this.signInEnded) return false;
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
  /** Forgets everything read from the platform; the open workflow is kept. */
  private clearPlatformState() {
    this.importGeneration++;
    this.listSequence++;
    this.adminGeneration++;
    this.homeTasks = [];
    this.homeRuns = [];
    this.homeAttention = [];
    this.homeDrafts = [];
    this.knownRuns.clear();
    this.workflowVersions.clear();
    this.versionsScope = null;
    this.activationsByVersion = new Map();
    this.runTask = null;
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
    this.actionNextCursor = null;
    this.catalogGeneration++;
    this.catalogState = "idle";
    this.catalogError = null;
    this.catalogScope = null;
    this.contractGeneration++;
    this.contractPending = "";
    this.contractCache.clear();
    this.contractRevision++;
    this.actionContract = null;
    // The integration dialogs and their hand-off belong to the old workspace.
    this.apiBuilder = null;
    this.connectionDialog = null;
    this.lastUse = null;
    this.showActivation = false;
    this.exportFallback = null;
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
    this.published = null;
    this.activation = null;
    this.closeSimulation();
    this.validation?.invalidate();
  }
  // --- platform connection ---------------------------------------------------
  /**
   * Reads the host's platform connection and checks the active platform
   * silently. Failures become a non-blocking notice; local work never waits.
   */
  async verifyPlatform(startup = false) {
    const generation = ++this.platformGeneration;
    const status = await this.connection.status().catch(() => null);
    if (generation !== this.platformGeneration || !this.paired) return;
    this.connectionStatus = isConnectionStatus(status) ? status : null;
    this.cdr.markForCheck();
    const login = this.connectionStatus?.login;
    if (
      startup &&
      login?.id &&
      (login.state === "starting" || login.state === "awaiting_user")
    ) {
      // A sign-in was still waiting when this window reloaded: resume it.
      this.loginWatcher.watch(login);
      this.openWizard("sign-in", {
        platform: this.connectionStatus?.profile?.name ?? this.profile?.name,
      });
      return;
    }
    if (!this.profile) {
      if (startup && this.firstRun()) this.openWizard("choice");
      return;
    }
    await this.checkPlatform(generation);
  }
  private async checkPlatform(generation = ++this.platformGeneration) {
    const name = this.profile?.name ?? "";
    try {
      const result = await this.connection.test();
      if (generation !== this.platformGeneration) return;
      if (!result?.identity) {
        // An older host: read the identity through the API bridge instead.
        await this.loadIdentity();
        return;
      }
      this.api.adopt(result.session);
      this.identity = result.identity;
      this.catalogAfterIdentity();
      this.renewedStatus();
      this.platformNotice = result.workspace_revoked
        ? { kind: "revoked", name }
        : this.profile?.environmentId
          ? null
          : { kind: "workspace", name };
    } catch (e) {
      if (generation !== this.platformGeneration) return;
      const plain = describeError(e);
      if (plain.code === "WV-STUDIO-SESSION") return;
      if (plain.code === "WV-AUTH-NOT-LINKED")
        this.platformNotice = { kind: "not-linked", name };
      else if (plain.status === 401)
        this.platformNotice = { kind: "expired", name };
      else {
        if (plain.code === "WV-AUTH-STORE")
          this.platformNotice = { kind: "store", name };
        else if (
          plain.code === "WV-AUTH-OFFLINE" ||
          plain.code === "WV-STUDIO-UPSTREAM" ||
          [0, 502, 503, 504].includes(plain.status)
        )
          this.platformNotice = { kind: "offline", name };
        await this.loadIdentity();
      }
    } finally {
      this.cdr.markForCheck();
    }
  }
  async refreshConnectionStatus() {
    const generation = this.platformGeneration;
    const status = await this.connection.status().catch(() => null);
    if (generation !== this.platformGeneration) return;
    this.connectionStatus = isConnectionStatus(status) ? status : null;
    this.cdr.markForCheck();
  }
  /**
   * No saved platform yet and the host's start preference is still "ask":
   * ask how the person wants to work. The preference lives on the host.
   */
  private firstRun() {
    const status = this.connectionStatus;
    return (
      !!status?.store?.available &&
      !status.profile &&
      !status.profiles?.length &&
      this.view === "home" &&
      status.preferences?.start !== "local"
    );
  }
  /** The host's start preference; "ask" when an older host keeps none. */
  get startPreference(): StartPreference {
    return this.connectionStatus?.preferences?.start === "local"
      ? "local"
      : "ask";
  }
  /** Saves how Studio starts and records what the host kept. */
  private async saveStartPreference(start: StartPreference) {
    const result = await this.connection.savePreferences(start);
    const saved = result?.preferences?.start === "local" ? "local" : "ask";
    if (this.connectionStatus)
      this.connectionStatus = {
        ...this.connectionStatus,
        preferences: { start: saved },
      };
    this.cdr.markForCheck();
    return saved;
  }
  /**
   * Settings: "When Studio opens". The radios stay enabled, so keyboard focus
   * stays on them while saving; each change is saved in order and only the
   * last one reports. A failed save shows the choice the host kept.
   */
  async chooseStartPreference(start: StartPreference) {
    const sequence = ++this.startSequence;
    this.startNote = "";
    this.startError = null;
    this.cdr.markForCheck();
    const saving = this.startQueue.then(() => this.saveStartPreference(start));
    this.startQueue = saving.catch(() => undefined);
    try {
      await saving;
      if (sequence === this.startSequence) this.startNote = "Saved.";
    } catch (e) {
      if (sequence !== this.startSequence) return;
      const plain = describeError(e);
      if (plain.code === "WV-STUDIO-SESSION" && !this.paired) return;
      this.startError = {
        message:
          plain.code === "WV-PROFILE-STORE"
            ? "Studio couldn't save this setting. Check that your user configuration folder is writable, then try again."
            : `Studio couldn't save this setting. ${plain.message}`,
        code: plain.code,
      };
      // The radio the person chose is checked natively; show the kept one.
      setTimeout(() => {
        const kept = document.querySelector<HTMLInputElement>(
          `input[name="start-preference"][value="${this.startPreference}"]`,
        );
        if (kept) kept.checked = true;
      });
    } finally {
      this.cdr.markForCheck();
    }
  }
  openWizard(
    start: WizardStep,
    options: {
      platform?: string;
      server?: string;
      switchAccount?: boolean;
    } = {},
  ) {
    this.platformMenuOpen = false;
    // The wizard shows a waiting or failed sign-in itself.
    const kind = this.platformNotice?.kind;
    if (kind === "signing-in" || kind === "sign-in-failed")
      this.platformNotice = null;
    if (this.view !== "connect") this.wizardReturn = this.view;
    this.wizard = {
      key: ++this.wizardKey,
      start,
      platform: options.platform ?? "",
      server: options.server ?? "",
      switchAccount: !!options.switchAccount,
    };
    this.view = "connect";
    this.selectedRecord = null;
    this.error = "";
    this.errorCode = "";
    void this.router.navigateByUrl("/connect");
    this.cdr.markForCheck();
  }
  /**
   * Closes the wizard. A sign-in it left waiting keeps running: the shared
   * watcher follows it, and the banner says so until it finishes.
   */
  private releaseWizard() {
    if (!this.wizard) return;
    this.wizard = null;
    if (this.loginWatcher.pending) {
      const name =
        this.profile?.name ?? this.connectionStatus?.profile?.name ?? "";
      this.platformNotice = { kind: "signing-in", name };
    }
  }
  async closeWizard(target?: View) {
    this.releaseWizard();
    const view =
      target ?? (this.wizardReturn === "connect" ? "home" : this.wizardReturn);
    await this.navigate(view);
    this.cdr.markForCheck();
  }
  /** "Start working": back to the open workflow, otherwise Home. */
  finishWizard() {
    void this.closeWizard(
      this.wizardReturn === "designer" ? "designer" : "home",
    );
  }
  /** First-run "Work locally": the host remembers it, so Studio stops asking. */
  wizardWorkLocally() {
    if (!this.profile && !this.connectionStatus?.profile) {
      // Without a saved preference the choice is simply asked again next time.
      void this.saveStartPreference("local").catch(() => undefined);
      void this.closeWizard("home");
      return;
    }
    void this.workLocally();
  }
  /** True when the open workflow holds edits that belong to the current platform. */
  get platformDraftChanged() {
    return (
      this.dirty &&
      (this.draftRevision !== undefined || this.published !== null)
    );
  }
  confirmSwitch = (kind: SwitchKind | "local") =>
    this.confirmPlatformSwitch(kind);
  async confirmPlatformSwitch(kind: SwitchKind | "local"): Promise<boolean> {
    if (this.pendingMutation || this.adminUncertainCreate) {
      this.error =
        "Finish or reconcile the pending command before changing the platform or workspace.";
      this.errorCode = "";
      this.cdr.markForCheck();
      return false;
    }
    if (!this.platformDraftChanged) return true;
    const platform = this.profile?.name ?? "the platform";
    const action =
      kind === "workspace"
        ? "Switch workspace"
        : kind === "local"
          ? "Work locally"
          : "Switch platform";
    return this.dialogs.confirm({
      title: `${action}?`,
      message: `The open workflow has changes that aren't saved to ${platform}. It stays open in Studio as a local copy: afterward, save it as a new draft or export it.`,
      confirmLabel: action,
      cancelLabel: "Stay here",
    });
  }
  /** The open workflow stays as a local copy once its platform or workspace changes. */
  private detachPlatformDraft() {
    if (this.draftRevision === undefined && !this.published) return;
    this.draftId = crypto.randomUUID();
    this.draftRevision = undefined;
    this.published = null;
    this.activation = null;
    this.saveState = "Unsaved";
    if (this.view === "designer")
      void this.router.navigateByUrl(`/workflows/${this.draftId}/designer`, {
        replaceUrl: true,
      });
  }
  /**
   * Adopts a host connection change; a switch forgets platform-scoped state.
   * `removed` names the platform a removal was for; by default the platform
   * that was active, as when the wizard removes the one it saved.
   */
  applyConnection(
    result: Partial<ConnectionResult> | null,
    switched = true,
    removed = "",
  ) {
    if (!result) return;
    const previous =
      removed ||
      this.connectionStatus?.profile?.name ||
      this.profile?.name ||
      "";
    if (result.session) this.api.adopt(result.session);
    if (isConnectionStatus(result.connection))
      this.connectionStatus = result.connection;
    if (switched) {
      // The host ends a pending sign-in when the platform changes.
      this.loginWatcher.stop();
      this.platformGeneration++;
      this.clearPlatformState();
      this.detachPlatformDraft();
      this.platformNotice = null;
    }
    this.reportSignOut(result, previous);
    this.cdr.markForCheck();
  }
  /** Notes a sign-out that stayed on this computer or that the provider did not confirm. */
  private reportSignOut(result: unknown, name = "") {
    const report = signOutReport(result);
    if (report === "incomplete")
      this.platformNotice = { kind: "sign-out-incomplete", name };
    else if (report === "unconfirmed")
      this.platformNotice = { kind: "revocation", name };
  }
  /**
   * The shell's view of the watched sign-in. While the wizard shows it, the
   * wizard reacts; otherwise the top bar and the banner report the outcome.
   */
  private backgroundLogin(event: LoginEvent) {
    this.cdr.markForCheck();
    if (this.loginWatcher.attendant) return;
    const name =
      this.profile?.name ?? this.connectionStatus?.profile?.name ?? "";
    if (event.kind === "waiting") {
      // A sign-in the wizard handed over while it was starting: an outdated
      // "sign in again" notice gives way to the waiting one.
      const kind = this.platformNotice?.kind;
      if (!this.wizard && (kind === "expired" || kind === "sign-in-failed"))
        this.platformNotice = { kind: "signing-in", name };
      return;
    }
    if (this.platformNotice?.kind === "signing-in") this.platformNotice = null;
    if (event.kind === "authenticated") {
      this.notify(`Signed in to ${name}.`);
      void this.afterBackgroundSignIn();
    } else if (event.kind === "ended") {
      this.platformNotice = {
        kind: "sign-in-failed",
        name,
        detail: `${loginOutcome(event.state, event.code).title.replace(/\.$/, "")}.`,
      };
      void this.refreshConnectionStatus();
    }
  }
  private async afterBackgroundSignIn() {
    await this.refreshConnectionStatus();
    await this.checkPlatform();
    await this.refresh();
  }
  wizardStatus(status: ConnectionStatus) {
    this.connectionStatus = status;
    if (this.platformNotice?.kind === "expired") this.platformNotice = null;
    this.cdr.markForCheck();
  }
  wizardVerified(result: VerifiedPlatform) {
    this.api.adopt(result.session);
    this.identity = result.identity;
    this.catalogAfterIdentity();
    this.platformNotice = null;
    this.renewedStatus();
    this.cdr.markForCheck();
  }
  /** A successful check may have renewed the session: reread a stale state. */
  private renewedStatus() {
    if (this.connectionStatus?.authentication?.state !== "signed_in")
      void this.refreshConnectionStatus();
  }
  wizardWorkspace(change: WorkspaceChange) {
    this.api.adopt(change.session);
    this.platformGeneration++;
    this.clearPlatformState();
    this.detachPlatformDraft();
    this.identity = change.identity;
    this.catalogAfterIdentity();
    this.platformNotice = null;
    void this.refreshConnectionStatus();
    this.cdr.markForCheck();
  }
  /**
   * The wizard found the signed-in account unusable. The identity, lists and
   * dialogs held for the previous account go; the notice explains what's next.
   */
  wizardAccountProblem(kind: "not-linked" | "expired") {
    const name =
      this.profile?.name ?? this.connectionStatus?.profile?.name ?? "";
    this.platformGeneration++;
    this.clearPlatformState();
    this.platformNotice = { kind, name };
    void this.refreshConnectionStatus();
    this.cdr.markForCheck();
  }
  platformFail(error: unknown, name = "") {
    const plain = describeError(error);
    if (plain.code === "WV-PROFILE-CHANGED") {
      const server =
        this.connectionStatus?.profiles?.find((p) => p.name === name)?.server ??
        "";
      this.platformNotice = { kind: "changed", name, server: server ?? "" };
      this.cdr.markForCheck();
      return;
    }
    this.fail(error);
    this.cdr.markForCheck();
  }
  async platformAction(event: { action: PlatformAction; name: string }) {
    switch (event.action) {
      case "use":
        return this.usePlatform(event.name);
      case "sign-in":
        return this.signInPlatform(event.name, false);
      case "switch-account":
        return this.signInPlatform(event.name, true);
      case "sign-out":
        return this.signOutPlatform(event.name);
      case "workspace":
        return this.openWizard("workspace", { platform: event.name });
      case "remove":
        return this.removePlatform(event.name);
    }
  }
  async usePlatform(name: string) {
    if (this.platformBusy) return;
    if (!(await this.confirmPlatformSwitch("platform"))) return;
    this.platformBusy = name;
    this.cdr.markForCheck();
    try {
      const result = await this.connection.activate(name);
      this.applyConnection(result);
      // An expired access token with a refresh token is renewed by the check.
      const signedIn = sessionUsable(result.connection?.authentication);
      this.notify(`Now using ${name}.`);
      if (!signedIn) {
        this.openWizard("sign-in", { platform: name });
        return;
      }
      await this.checkPlatform();
      if (this.identity && !this.profile?.environmentId)
        this.openWizard("workspace", { platform: name });
      else await this.refresh();
    } catch (e) {
      this.platformFail(e, name);
    } finally {
      this.platformBusy = "";
      this.cdr.markForCheck();
    }
  }
  async signInPlatform(name: string, switchAccount: boolean) {
    if (this.platformBusy) return;
    if ((this.connectionStatus?.profile?.name ?? this.profile?.name) !== name) {
      if (!(await this.confirmPlatformSwitch("platform"))) return;
      this.platformBusy = name;
      try {
        this.applyConnection(await this.connection.activate(name));
      } catch (e) {
        this.platformFail(e, name);
        return;
      } finally {
        this.platformBusy = "";
        this.cdr.markForCheck();
      }
    }
    this.openWizard("sign-in", { platform: name, switchAccount });
  }
  async signOutPlatform(name: string) {
    if (this.platformBusy) return;
    if (
      !(await this.dialogs.confirm({
        title: `Sign out of ${name}?`,
        message: `Studio removes your sign-in for ${name} from this computer and asks the identity provider to end it. The platform stays saved, so you can sign in again later.`,
        confirmLabel: "Sign out",
        danger: true,
      }))
    )
      return;
    this.platformBusy = name;
    this.cdr.markForCheck();
    try {
      const result = await this.connection.logout(true);
      this.api.adopt(result.session);
      if (isConnectionStatus(result.connection))
        this.connectionStatus = result.connection;
      // Signing out ends a pending sign-in on the host too.
      this.loginWatcher.stop();
      this.platformGeneration++;
      this.clearPlatformState();
      this.platformNotice = null;
      this.reportSignOut(result, name);
      this.notify(`Signed out of ${name}.`);
      await this.refresh();
    } catch (e) {
      this.platformFail(e, name);
    } finally {
      this.platformBusy = "";
      this.cdr.markForCheck();
    }
  }
  async removePlatform(name: string) {
    if (this.platformBusy) return;
    const active = this.connectionStatus?.profile?.name === name;
    if (
      !(await this.dialogs.confirm({
        title: `Remove ${name}?`,
        message: `Studio forgets ${name} and signs you out of it on this computer. Your work on the platform isn't affected, and you can add it again later.${active ? " Studio then works locally." : ""}`,
        confirmLabel: "Remove platform",
        danger: true,
      }))
    )
      return;
    if (active && !(await this.confirmPlatformSwitch("local"))) return;
    this.platformBusy = name;
    this.cdr.markForCheck();
    try {
      const result = await this.connection.remove(name, true);
      this.applyConnection(result, active, name);
      this.notify(`Removed ${name}.`);
      if (active) await this.refresh();
    } catch (e) {
      this.platformFail(e, name);
    } finally {
      this.platformBusy = "";
      this.cdr.markForCheck();
    }
  }
  async workLocally() {
    this.platformMenuOpen = false;
    if (this.platformBusy) return;
    if (!(await this.confirmPlatformSwitch("local"))) return;
    this.platformBusy = "local";
    this.cdr.markForCheck();
    try {
      this.applyConnection(await this.connection.disconnect());
      this.notify(
        "You're working locally. Workflows stay on this computer until you connect to a platform.",
      );
      if (this.view === "connect") await this.closeWizard("home");
      else await this.refresh();
    } catch (e) {
      this.platformFail(e);
    } finally {
      this.platformBusy = "";
      this.cdr.markForCheck();
    }
  }
  noticeAction() {
    const notice = this.platformNotice;
    if (!notice) return;
    switch (notice.kind) {
      case "expired":
        this.openWizard("sign-in", { platform: notice.name });
        break;
      case "not-linked":
      case "revoked":
      case "workspace":
        this.openWizard("workspace", { platform: notice.name });
        break;
      case "offline":
      case "store":
        this.platformNotice = null;
        void this.verifyPlatform();
        break;
      case "changed":
        this.openWizard("server", { server: notice.server });
        break;
      case "sign-out-incomplete":
      case "revocation":
        this.platformNotice = null;
        break;
      case "signing-in":
        this.openWizard("sign-in", { platform: notice.name });
        break;
      case "sign-in-failed":
        this.platformNotice = null;
        this.openWizard("sign-in", { platform: notice.name });
        break;
    }
  }
  /** Informational notices use calm colors; problems keep the warning style. */
  get noticeCalm() {
    const kind = this.platformNotice?.kind;
    return kind === "revocation" || kind === "signing-in";
  }
  get noticeText() {
    const notice = this.platformNotice;
    if (!notice) return "";
    switch (notice.kind) {
      case "expired":
        return `Your session for ${notice.name} expired. Sign in again to publish, run, and manage work. You can keep working locally.`;
      case "not-linked":
        return `You signed in, but ${notice.name} doesn't recognize your account yet.`;
      case "offline":
        return `Studio couldn't reach ${notice.name}. You can keep working locally.`;
      case "store":
        return "Studio can't use this computer's credential store. Unlock or set up the system keychain, then try again.";
      case "changed":
        return `The sign-in settings for ${notice.name} changed on the server. Review them before you use it again.`;
      case "revoked":
        return `The workspace you used on ${notice.name} is no longer available to your account.`;
      case "workspace":
        return `Choose a workspace on ${notice.name} to publish and run work.`;
      case "sign-out-incomplete":
        return `Studio removed ${notice.name || "the platform"}, but couldn't remove its sign-in from this computer's credential store.`;
      case "revocation":
        return revocationNote;
      case "signing-in":
        return `Studio is waiting for you to finish signing in to ${notice.name}.`;
      case "sign-in-failed":
        return `${notice.detail ?? "Sign-in didn't finish."} Sign in to ${notice.name} again to publish, run, and manage work. You can keep working locally.`;
    }
  }
  get noticeActionLabel() {
    switch (this.platformNotice?.kind) {
      case "expired":
      case "sign-in-failed":
        return "Sign in again";
      case "signing-in":
        return "Show sign-in";
      case "not-linked":
        return "See details";
      case "offline":
      case "store":
        return "Try again";
      case "changed":
        return "Review";
      case "revoked":
      case "workspace":
        return "Choose workspace";
      default:
        return "";
    }
  }
  // --- platform indicator ------------------------------------------------------
  /** The platform's sign-in ended, or the account isn't linked: sign in again. */
  get signInEnded() {
    const kind = this.platformNotice?.kind;
    return kind === "expired" || kind === "not-linked";
  }
  get signedIn() {
    if (!this.profile || this.signInEnded) return false;
    if (this.identity) return true;
    const status = this.connectionStatus;
    return (
      !!status?.profile &&
      status.profile.name === this.profile.name &&
      platformState(status.profile, status) === "signed_in" &&
      this.platformNotice?.kind !== "expired"
    );
  }
  /**
   * The host holds no sign-in for the active platform (the person signed out,
   * or never signed in): a refused call is expected, not an expired session.
   */
  get platformSignedOut() {
    const status = this.connectionStatus;
    return (
      !!this.profile &&
      !this.identity &&
      !this.loginWatcher.pending &&
      status?.profile?.name === this.profile.name &&
      !status.authentication?.error_code &&
      platformState(status.profile, status) === "signed_out"
    );
  }
  /** Platform lists can't be read until the person signs in (again). */
  get signInToRead() {
    return (
      !!this.profile &&
      !this.loginWatcher.pending &&
      (this.signInEnded || this.platformSignedOut)
    );
  }
  get platformStatusText() {
    if (!this.profile) return "Local authoring";
    if (this.loginWatcher.pending) return "Signing in";
    const kind = this.platformNotice?.kind;
    if (kind === "expired") return "Session expired";
    if (kind === "not-linked") return "Account not recognized";
    if (kind === "offline") return "Can't reach the platform";
    if (kind === "store") return "Credential store unavailable";
    if (this.identity) return "Signed in";
    const status = this.connectionStatus;
    return status?.profile?.name === this.profile.name
      ? stateLabel(platformState(status.profile, status))
      : "Not signed in";
  }
  /** Workspace names for the selected scope, from the identity or the saved profile. */
  get workspaceNames() {
    const p = this.profile;
    if (!p?.environmentId) return null;
    const tenant = this.identity?.workspaces.find((w) => w.id === p.tenantId);
    const project = tenant?.projects.find((x) => x.id === p.projectId);
    const environment = project?.environments.find(
      (x) => x.id === p.environmentId,
    );
    if (tenant && project && environment)
      return {
        tenant: tenant.name,
        project: project.name,
        environment: environment.name,
      };
    const saved = this.connectionStatus?.profile?.workspace;
    if (
      saved?.environment_id === p.environmentId &&
      saved.project_name &&
      saved.environment_name
    )
      return {
        tenant: saved.tenant_name ?? "",
        project: saved.project_name,
        environment: saved.environment_name,
      };
    return null;
  }
  /** While neither the identity nor the host's status has arrived. */
  private get workspaceLoading() {
    return !this.identity && !this.connectionStatus;
  }
  /** "Payments / Production"; "Loading workspace…" or "Workspace 1a2b3c4d" without names. */
  get workspaceText() {
    const environment = this.profile?.environmentId;
    if (!environment) return "";
    const names = this.workspaceNames;
    if (names) return workspaceShort(names);
    return this.workspaceLoading
      ? loadingWorkspace
      : workspaceFallback(environment);
  }
  /** "Acme / Payments / Production". */
  get workspaceFullText() {
    const environment = this.profile?.environmentId;
    if (!environment) return "";
    const names = this.workspaceNames;
    if (names) return workspaceLong(names);
    return this.workspaceLoading
      ? loadingWorkspace
      : workspaceFallback(environment);
  }
  /** "Acme · Payments / Production": the platform and its workspace. */
  get environmentLabel() {
    const p = this.profile;
    if (!p) return "";
    return this.workspaceText ? `${p.name} · ${this.workspaceText}` : p.name;
  }
  get accountText() {
    const status = this.connectionStatus;
    if (status?.profile?.name !== this.profile?.name) return "";
    return (
      accountName(status?.profile?.account) ||
      accountName(status?.authentication?.account)
    );
  }
  /** The top bar's state, with the account's name once signed in. */
  get indicatorStatusText() {
    const status = this.connectionStatus;
    if (status?.profile?.name !== this.profile?.name)
      return this.platformStatusText;
    return signedInText(
      this.platformStatusText,
      status?.profile?.account?.display_name
        ? status.profile.account
        : status?.authentication?.account,
    );
  }
  get platformIndicatorLabel() {
    if (!this.profile) return "Platform: Local authoring";
    return `Platform: ${this.profile.name} · ${this.indicatorStatusText} · ${
      this.workspaceText
        ? `Workspace ${this.workspaceText}`
        : "No workspace chosen"
    }`;
  }
  /**
   * The wizard is open: the indicator reads "Connecting…", not "Signed out".
   * An account the platform doesn't recognize is a settled answer, so the
   * visible text says so, as the indicator's accessible name does.
   */
  get indicatorConnecting() {
    return (
      this.view === "connect" &&
      !!this.profile &&
      !this.signedIn &&
      this.platformNotice?.kind !== "not-linked"
    );
  }
  /**
   * The one word a narrow window keeps beside the dot when the platform needs
   * attention, so the state never rests on color alone.
   */
  get indicatorWord() {
    if (!this.profile || this.indicatorConnecting) return "";
    const kind = this.platformNotice?.kind;
    if (kind === "offline" || kind === "store") return "Offline";
    if (!this.signedIn) return "Sign in";
    return "";
  }
  /** The platform menu's second line: "Payments / Production · jane@acme.example". */
  get platformMenuDetail() {
    if (!this.profile) return "Workflows stay on this computer.";
    return [this.workspaceText || "No workspace chosen", this.accountText]
      .filter(Boolean)
      .join(" · ");
  }
  togglePlatformMenu() {
    this.platformMenuOpen = !this.platformMenuOpen;
    if (this.platformMenuOpen)
      queueMicrotask(() =>
        setTimeout(() =>
          document.querySelector<HTMLElement>("#platform-menu button")?.focus(),
        ),
      );
  }
  closePlatformMenu(returnFocus = true) {
    if (!this.platformMenuOpen) return;
    this.platformMenuOpen = false;
    if (returnFocus)
      setTimeout(() =>
        document.querySelector<HTMLElement>(".platform-indicator")?.focus(),
      );
  }
  platformMenuKey(event: KeyboardEvent) {
    const items = [
      ...document.querySelectorAll<HTMLElement>("#platform-menu button"),
    ];
    const index = items.indexOf(document.activeElement as HTMLElement);
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      this.closePlatformMenu();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const next =
        (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) %
        items.length;
      items[next]?.focus();
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      items[event.key === "Home" ? 0 : items.length - 1]?.focus();
    }
  }
  platformMenuBlur(event: FocusEvent) {
    const next = event.relatedTarget;
    const anchor = event.currentTarget;
    if (!(next instanceof HTMLElement) || !(anchor instanceof HTMLElement))
      return;
    if (!anchor.contains(next)) this.closePlatformMenu(false);
  }
  @HostListener("document:pointerdown", ["$event"])
  outsidePointer(event: PointerEvent) {
    if (
      this.platformMenuOpen &&
      !(event.target as HTMLElement)?.closest?.(".platform-menu-anchor")
    )
      this.closePlatformMenu(false);
  }
  menuChoice(
    choice:
      | "workspace"
      | "switch"
      | "switch-account"
      | "sign-in"
      | "sign-out"
      | "local"
      | "settings"
      | "connect",
  ) {
    this.platformMenuOpen = false;
    const name = this.profile?.name ?? "";
    switch (choice) {
      case "workspace":
        this.openWizard("workspace", { platform: name });
        break;
      case "switch":
      case "connect":
        this.openWizard("server");
        break;
      case "sign-in":
        void this.signInPlatform(name, false);
        break;
      case "switch-account":
        void this.signInPlatform(name, true);
        break;
      case "sign-out":
        void this.signOutPlatform(name);
        break;
      case "local":
        void this.workLocally();
        break;
      case "settings":
        void this.navigate("settings");
        break;
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
  /** The decision waiting for its inline "can't change this later" answer. */
  taskConfirm = "";
  /** "Approve": asks once, inline in the footer, before it is recorded. */
  askDecision(decision: string) {
    const form = document.querySelector<HTMLFormElement>(
      ".human-decision-form",
    );
    if (form && !form.reportValidity()) return;
    this.taskConfirm = decision;
    afterNextRender(
      () =>
        document
          .querySelector<HTMLElement>(".decision-confirm [data-initial-focus]")
          ?.focus(),
      { injector: this.injector },
    );
  }
  cancelDecision() {
    const decision = this.taskConfirm;
    this.taskConfirm = "";
    afterNextRender(
      () =>
        document
          .querySelector<HTMLElement>(
            `.decision-bar [data-decision="${CSS.escape(decision)}"]`,
          )
          ?.focus(),
      { injector: this.injector },
    );
  }
  async taskCommand(command: string, decision?: string) {
    if (this.taskConflict)
      throw Error("Refresh this task before acting again.");
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
    const title = String(r["title"] || "this task");
    try {
      const result = await this.safeMutation(
        `${this.api.environment}/human-tasks/${r["id"]}/${command}`,
        "POST",
        body,
        undefined,
        command === "complete"
          ? `${decisionVerb(decision ?? "")} task`
          : command === "claim"
            ? "Claim task"
            : "Release task",
      );
      this.taskConfirm = "";
      this.selectedRecord = result;
      this.records = this.records.map((x) =>
        x["id"] === result["id"] ? result : x,
      );
      if (command === "complete") {
        // Straight on to the next ready task; "Next task" moves on again.
        const next = this.nextReadyTask([result["id"]]);
        if (next) await this.open(next);
        this.notifyDecided(decisionPast(decision ?? ""), title, [
          result["id"],
          next?.["id"],
        ]);
      } else {
        this.notify(
          command === "claim"
            ? "Task claimed. Fill in the form, then choose a decision."
            : "Task released. Others can claim it now.",
        );
        // The pressed button is gone: focus moves to what comes next.
        afterNextRender(
          () =>
            (
              document.querySelector<HTMLElement>(
                command === "claim"
                  ? ".human-decision-form :is(input, select, textarea):not([disabled])"
                  : ".record-detail .action-row button.primary",
              ) ?? document.querySelector<HTMLElement>("#record-detail-title")
            )?.focus(),
          { injector: this.injector },
        );
      }
    } catch (e) {
      if (e instanceof ApiError && (e.status === 409 || e.status === 403)) {
        this.taskConflict = true;
        this.taskConfirm = "";
        this.error =
          "This task changed or you lost access to it. Your answers are still here. Refresh before you try again.";
        this.errorCode = e.code;
      } else this.fail(e);
    } finally {
      this.cdr.markForCheck();
    }
  }
  private nextReadyTask(skip: unknown[]) {
    return this.records.find(
      (x) => x["status"] === "ready" && !skip.includes(x["id"]),
    );
  }
  private notifyDecided(decided: string, title: string, skip: unknown[]) {
    const following = this.nextReadyTask(skip);
    this.notify(
      `${decided} ${title}.`,
      following && this.view === "tasks"
        ? {
            label: "Next task",
            run: () => {
              if (this.view !== "tasks") return;
              const next = this.nextReadyTask(skip);
              if (next) void this.open(next);
            },
          }
        : undefined,
    );
  }
  messages() {
    return (this.emailDetail?.["messages"] ?? []) as Record<string, unknown>[];
  }
  mail(message: Record<string, unknown>) {
    return (message["mail"] ?? {}) as Record<string, unknown>;
  }
  /** "Send reply": queues the reply, then sends it. */
  async sendReply() {
    if (!this.selectedRecord || !this.reply.trim()) return;
    const parent = this.messages()
      .filter((m) => m["direction"] === "inbound")
      .at(-1);
    if (!parent) {
      this.error = "This conversation has no message to reply to yet.";
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
        "Send reply",
      );
      this.reply = "";
    } catch (e) {
      this.fail(e);
      this.cdr.markForCheck();
      return;
    }
    await this.executeEmail();
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
        "Send reply",
      );
      const state = this.emailSubmission["state"];
      if (state === "accepted") this.notify("Reply sent to the mail server.");
      else if (state === "queued") this.notify("Reply waiting to send.");
      await this.readDetail();
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
    }
  }
  /** "Check status": reads the submission again; it never sends twice. */
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
        `Studio didn't get an answer for "${label}". It may have worked: use Check now before you do anything else.`,
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
            ? "Draft saved"
            : "Unsaved";
      } else if (p.path.endsWith("/workflows")) this.published = r;
      else if (p.path.endsWith("/activations")) this.activation = r;
      else if (
        /\/runs\/[^/]+\/(pause|resume)$/.test(p.path) &&
        isRecord(r["state"]) &&
        this.selectedRecord?.["id"] === r["id"]
      )
        this.selectedRecord = r;
      this.notify(`Checked. ${p.label} finished.`);
      this.error = "";
      this.errorCode = "";
    } catch (e) {
      this.fail(e);
    } finally {
      this.cdr.markForCheck();
      this.busy = "";
    }
  }
  async runControl(command: "pause" | "resume") {
    if (!this.selectedRecord) return;
    const state = this.selectedRecord["state"] as Record<string, unknown>;
    const reason = await this.dialogs.prompt({
      title: command === "pause" ? "Pause this run?" : "Resume this run?",
      message:
        command === "pause"
          ? "The run stops at its next safe point until someone resumes it. The reason is kept in the audit log."
          : "The run continues from where it stopped. The reason is kept in the audit log.",
      confirmLabel: command === "pause" ? "Pause run" : "Resume run",
      field: { name: "reason", label: "Reason", required: true },
    });
    if (!reason) return;
    try {
      this.selectedRecord = await this.safeMutation(
        `${this.api.environment}/runs/${this.selectedRecord["id"]}/${command}`,
        "POST",
        { expected_revision: state["control_revision"] ?? 0, reason },
        undefined,
        command === "pause" ? "Pause run" : "Resume run",
      );
      this.notify(command === "pause" ? "Run paused." : "Run resumed.");
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
    const label =
      command === "purge"
        ? "Delete run data"
        : command === "archive"
          ? "Archive run"
          : "Restore run";
    const values = await this.dialogs.form({
      title:
        command === "purge"
          ? "Delete this run's data?"
          : command === "archive"
            ? "Archive this run?"
            : "Restore this run?",
      message:
        command === "purge"
          ? "Studio deletes this run's data for good. The audit record of what happened stays. You can't undo this."
          : command === "archive"
            ? "Archived runs are hidden from Runs until you include them. Their data stays. The reason is kept in the audit log."
            : "The run shows in Runs again. The reason is kept in the audit log.",
      confirmLabel: label,
      danger: command === "purge",
      fields: [
        { name: "reason", label: "Reason", required: true },
        ...(command === "purge"
          ? [
              {
                name: "confirm",
                label: "Run ID",
                hint: `Type ${id} to confirm.`,
                validate: (value: string) =>
                  value === id ? "" : "Type the exact run ID to confirm.",
              },
            ]
          : []),
      ],
    });
    if (!values) return;
    const body: Record<string, unknown> = {
      expected_revision: this.runLifecycle["revision"],
      reason: values["reason"],
    };
    if (command === "purge") body["confirm_run_id"] = values["confirm"];
    try {
      await this.safeMutation(
        `${this.api.environment}/runs/${id}/${command}`,
        "POST",
        body,
        undefined,
        label,
      );
      this.runLifecycle = await this.api.request(
        `${this.api.environment}/runs/${id}/lifecycle`,
      );
      this.notify(
        command === "purge"
          ? "Deleted the run's data. Its audit record stays."
          : command === "archive"
            ? "Run archived."
            : "Run restored.",
      );
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
  @HostListener("window:popstate", ["$event"]) popstate(event: PopStateEvent) {
    const position = event.state?.weavePosition;
    if (this.restoringHistory && position === this.historyPosition) {
      this.restoringHistory = false;
      return;
    }
    if (this.view === "designer" && !this.leaveInspector()) {
      if (Number.isInteger(position) && position !== this.historyPosition) {
        // Return to the existing entry: replacing the URL would erase the
        // destination, and pushing a replacement would discard Forward entries.
        this.restoringHistory = true;
        history.go(this.historyPosition - position);
      } else {
        // Entries from an older Studio have no position. Keep that destination
        // available through Back while restoring the current draft's URL.
        history.pushState(
          { weavePosition: this.historyPosition },
          "",
          this.historyUrl,
        );
        void this.router.navigateByUrl(new URL(this.historyUrl).pathname, {
          replaceUrl: true,
        });
      }
      return;
    }
    this.historyPosition = Number.isInteger(position) ? position : 0;
    this.historyUrl = location.href;
    this.releaseWizard();
    this.platformMenuOpen = false;
    const designer = designerPath.exec(location.pathname);
    if (designer) {
      // Back to the workflow that is open, or to another kept one.
      if (designer[1] === this.draftId) {
        this.view = "designer";
        this.loadInspector();
        this.loadWorkflowSettings();
        this.scheduleFit();
      } else void this.restoreWorkflow(designer[1]);
      return;
    }
    const segment = location.pathname.split("/")[1] as View;
    this.view = this.nav.some((n) => n.id === segment) ? segment : "home";
    this.selectedRecord = null;
    void this.refresh();
  }
  @HostListener("window:keydown", ["$event"]) key(event: KeyboardEvent) {
    const target = event.target as HTMLElement;
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
    if (
      this.view === "designer" &&
      target.closest?.(".inspector") &&
      (event.ctrlKey || event.metaKey) &&
      event.key.toLowerCase() === "z"
    ) {
      event.preventDefault();
      event.shiftKey ? this.redo() : this.undo();
      return;
    }
    if (typing) return;
    // Open dialogs own the keyboard; they close themselves on Escape.
    if (target.closest?.(".modal-panel") || this.dialogs.current()) return;
    if (event.key === "Escape") {
      // The first Escape ends a gesture or returns to the workflow settings;
      // the next one hides the inspector.
      if (this.connectingNode || this.dragNode || this.dragPreview) {
        this.cancelMove();
      } else if (this.view === "designer" && this.model.selected)
        void this.deselect();
      else if (this.windowWidth <= 1280) this.closeInspector();
    }
    if (this.view !== "designer") return;
    // Step shortcuts act only while focus is on the canvas or the outline, never
    // on an inspector button, the pane resizers or the palette.
    const onGraph = !!target.closest?.(".canvas, .outline");
    const command = event.metaKey || event.ctrlKey;
    if (command && event.key === "z") {
      event.preventDefault();
      if (this.editingLocked) return;
      event.shiftKey ? this.redo() : this.undo();
    }
    if (command && event.key === "s") {
      event.preventDefault();
      void this.runCommand(this.profile ? "save" : "export");
    }
    // The new canvas runs its own keys.
    if (this.editorNext && target.closest?.(".canvas-v2")) return;
    if (!this.editorNext && command && event.key === "0") {
      event.preventDefault();
      this.zoomReset();
    }
    if (
      !this.editorNext &&
      !command &&
      event.shiftKey &&
      (event.key === "!" || event.code === "Digit1")
    ) {
      event.preventDefault();
      this.fitAll();
    }
    if (!command && event.key === "?") {
      event.preventDefault();
      this.shortcutsOpen = true;
    }
    if (!onGraph) return;
    if (
      !command &&
      !event.altKey &&
      (event.key === "/" || event.key.toLowerCase() === "a")
    ) {
      event.preventDefault();
      this.openInsertionAtFocus(target);
      return;
    }
    if (!command && (event.key === "+" || event.key === "=")) this.zoomBy(1.2);
    if (!command && event.key === "-") this.zoomBy(1 / 1.2);
    if (event.key === "Delete" || event.key === "Backspace") {
      // The step that has focus, not a selection that may be off screen.
      const focused = target
        .closest?.("[data-step]")
        ?.getAttribute("data-step");
      event.preventDefault();
      void this.remove(focused ?? this.model.selected);
    }
    if (target.closest?.(".outline")) return;
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
      if (n) {
        void this.select(n, this.showInspector);
        this.focusStep(n.step.id);
      }
    }
  }
  /**
   * The outline is one Tab stop (roving tabindex): arrow keys, Home and End
   * move through the steps and select them.
   */
  outlineKey(event: KeyboardEvent, index: number) {
    const last = this.nodes.length - 1;
    const next =
      event.key === "ArrowDown"
        ? Math.min(last, index + 1)
        : event.key === "ArrowUp"
          ? Math.max(0, index - 1)
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? last
              : -1;
    if (next < 0) return;
    event.preventDefault();
    const node = this.nodes[next];
    if (!node) return;
    void this.select(node, false);
    this.focusLater(() =>
      document.querySelector<HTMLElement>(
        `.outline [data-outline="${CSS.escape(node.step.id)}"]`,
      ),
    );
  }
  /** The outline step that takes Tab: the selected one, else the first. */
  outlineStop(id: string, index: number) {
    const selected = this.nodes.some((n) => n.step.id === this.model.selected);
    return (selected ? this.model.selected === id : index === 0) ? 0 : -1;
  }
}
