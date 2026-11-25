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
// Step details: a modal dialog over the editor with Input | Parameters |
// Output. It opens on a step (or the trigger, or End), keeps focus inside,
// moves to the previous and next step in Outline order without closing,
// renames the step in place, and closes with its button, Escape or a click
// on the dimmed shell header above it (its own inset padding doesn't close
// it) — there is nothing to save: every edit is already in the workflow as
// an undo step.
import {
  listenFor,
  stopListening,
  workflowTestItem,
} from "../activation/execute-in-environment";
import { listenBlocked } from "../triggers/test-listener";
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  ViewContainerRef,
  ViewEncapsulation,
  type ComponentRef,
  type Type,
  afterEveryRender,
  afterNextRender,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
  viewChild,
} from "@angular/core";
import { parse as parseYaml, stringify as yamlText } from "yaml";
import { DialogService, Modal } from "../../dialog";
import {
  FORM_PANES,
  KEY_STEP,
  fitted,
  largest,
  RAIL,
  TABLE_PANES,
  readFolds,
  readWidths,
  resized,
  toEdge,
  writeFolds,
  writeWidths,
  type PaneFolds,
  type PaneProfile,
  type PaneWidths,
} from "./panes/layout";
import { InputPane } from "./panes/input-pane";
import { OutputPane } from "./panes/output-pane";
import { CloseGuard, type GuardAnswer } from "./panes/close-guard";
import {
  ExecuteDialog,
  type ExecuteIssue,
  type ExecuteQuestion,
} from "./execute-dialogs";
import { executeGate, untakenPath } from "../state/execute-gate";
import { ExecutePathDialog } from "./execute-path-dialog";
import {
  SimulateFailureDialog,
  type FailureChoice,
} from "./panes/simulate-failure-dialog";
import { failureKey, simulateBlock } from "./panes/simulate-failure";
import { simulateOpening } from "./panes/output-pane";
import { failureOf } from "../state/test-data-ops";
import { connectionsPort } from "../connections/connections-port";
import type {
  RunningStepTest,
  StepTestBody,
  StepTestOutcome,
} from "../state/step-test-service";
import { sideEffectOf } from "../state/step-test-service";
import type { SideEffect } from "../state/execution";
import { effectToAcknowledge, withoutAsking } from "./kinds/ai-task-model";
import { realEndWords, refusalWords, upstreamMissing } from "./real-words";
import { pendingOwned } from "./owned/owned-versions";
import { sliceStepIds } from "../state/execute-request";
import type { ExecuteTarget } from "../state/execution";
import type { ExecuteOutcome } from "../state/execution-service";
import { allSteps } from "./step-walk";
import { stepDetailsSheet } from "../state/canvas-commands";
import type { SheetSection } from "../state/keymap";
import { TestEventPane } from "./panes/test-event-pane";
import { TriggerEventPane } from "../triggers/test-event-pane";
import { TriggerSetup } from "../triggers/trigger-setup";
import { Icon } from "../../icon";
import { coveredBy, keepUsable } from "../../modal-sheet";
import { revealTop, scrollingParent, visibleBox } from "./scroll-area";
import { moveToStep } from "./step-move";
import { RowMenu, type RowMenuItem } from "../../row-menu";
import {
  commandFor,
  eventCombo,
  formatCombo,
  KEY_PLATFORM,
  type KeymapCommand,
} from "../state/keymap";
import { docsUrl, headerSubtitle } from "./header";
import { adjacent, breadcrumb, neighbors } from "./navigation";
import { recipeOf } from "./owned/owned-actions";
import { FormSession } from "./params/form-session";
import { IssuesIndicator } from "./issues-indicator";
import {
  fieldFor,
  firstIssueField,
  leavingToast,
  targetName,
  type PlacedSpec,
  type StepIssue,
} from "./issues";
import { getAt } from "../../forms/core/json";
import { ParameterForm } from "./params/param-form";
import { DragMap, activeDrag, canMapFields } from "./params/drag-map";
import { mappedRefs, type DragRef } from "./params/drop";
import {
  ndvRegistry,
  type NdvContext,
  type ParamSpec,
  type ParameterComponent,
  type ParameterPanes,
} from "./registry";
import { renameHint, renameWithExtras, type RenameResult } from "./rename";
import { reservedActionNames } from "./owned/insert-owned";
import { SettingsTab } from "./settings/settings-tab";
import { pendingStopsOf } from "../activation/stored-resources";
import { handOverLinkedRun } from "../runs-tab/run-links";
import { EventStartsTab } from "../triggers/event-starts-tab";
import { retryOn, settingsChanged } from "./settings/settings-rules";
import { stepNotesOf, type TriggerIntent } from "../state/canvas-sidecar";
import {
  TRIGGER_DESCRIPTIONS,
  TRIGGER_ICONS,
  TRIGGER_LABELS,
} from "../canvas/trigger-tiles";
import { isTriggerTarget } from "../triggers/trigger-target";
import type {
  ExecuteNotice,
  StepDetailsController,
} from "./step-details-controller";
import { kinds } from "../../model";
import type { StepDetailsHost } from "./step-details-host";
import {
  StepDetailsService,
  type FocusTarget,
  type Region,
  type StepDetailsRequest,
  type StepDetailsTab,
} from "./step-details-service";

interface MapMenu {
  drag: DragRef;
  label: string;
  row: HTMLElement;
  session: FormSession;
  targets: { spec: ParamSpec; label: string; owns: () => boolean }[];
  top: number;
  left: number;
}

/** Whether the element is wholly inside what the person sees of its scrolling pane. */
const inView = (element: HTMLElement): boolean => {
  const scroller = scrollingParent(element);
  const bounds = element.getBoundingClientRect();
  if (bounds.top < 0 || bounds.bottom > window.innerHeight) return false;
  if (!scroller) return true;
  const view = visibleBox(scroller);
  return bounds.top >= view.top - 0.5 && bounds.bottom <= view.bottom + 0.5;
};

const FOCUSABLE =
  'button:not([disabled]):not([tabindex="-1"]), [href], input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const CONTROL =
  'input:not([type="hidden"]):not([disabled]), textarea:not([disabled]), select:not([disabled]), [role="combobox"], [role="switch"], [role="radio"][tabindex="0"], button:not([disabled]):not([tabindex="-1"])';
/** What the live region says when step details move on from a step that keeps entries. */
/** The words the Output shows for an execution's answer, or null when it ran. */
export function noticeOf(
  outcome: ExecuteOutcome,
  target: string,
): ExecuteNotice | null {
  switch (outcome.kind) {
    case "done": {
      const blocked = outcome.response.blocked;
      if (blocked && blocked.nodeId === target)
        return { target, message: blocked.message, reason: blocked.reason };
      if (outcome.response.status === "failed")
        return {
          target,
          message:
            outcome.response.compile.ok === false
              ? "The workflow doesn't compile. Fix the issues it lists, then execute again."
              : "The simulated run failed. Check the run's issues, then execute again.",
          reason: null,
        };
      if (blocked)
        return {
          target,
          message: `${blocked.message} Open ${blocked.nodeId} to continue.`,
          reason: null,
        };
      return null;
    }
    case "refused": {
      const input = outcome.paths.some((path) => path === "/input");
      if (outcome.code === "WV-SCHEMA-SECRET_VALUE") {
        const mock = outcome.paths
          .find((path) => path.startsWith("/mocks/node:"))
          ?.slice("/mocks/node:".length)
          .replace(/~1/g, "/")
          .replace(/~0/g, "~");
        return input
          ? {
              target,
              message:
                "The test input holds a value marked secret. Remove it from the test input.",
              reason: null,
              input: true,
            }
          : {
              target,
              message: mock
                ? `The pinned output of ${mock} holds a value marked secret. Unpin it.`
                : "The test data holds a value marked secret. Remove it.",
              reason: null,
            };
      }
      if (input)
        return {
          target,
          message:
            "The test input doesn't fit the workflow's input fields. Edit it in the trigger's Test event.",
          reason: null,
          input: true,
        };
      return { target, message: outcome.message, reason: null };
    }
    case "busy":
      return {
        target,
        message: outcome.stopping
          ? "Studio is finishing what you stopped. Try again in a moment."
          : "Studio is still executing. Wait for it to finish, then try again.",
        reason: null,
      };
    case "conflict":
      return {
        target,
        message:
          "An action on this computer has the same name and version as a different one in the catalog. Give it a new version.",
        reason: null,
      };
    case "catalog":
      return {
        target,
        message: "Studio couldn't read the project's catalog, so nothing ran.",
        reason: null,
      };
    case "failed":
      return { target, message: outcome.message, reason: null };
    case "stale":
      return null;
  }
}

export const keptText = (name: string, count: number): string =>
  `${name} keeps ${count} ${count === 1 ? "entry that isn't" : "entries that aren't"} applied yet.`;
const isTyping = (element: Element | null): boolean =>
  !!element &&
  (element.matches(
    'input:not([type="checkbox"]):not([type="radio"]):not([type="button"]), textarea, select',
  ) ||
    (element as HTMLElement).isContentEditable);
const visible = (element: HTMLElement): boolean =>
  element.getClientRects().length > 0 &&
  getComputedStyle(element).visibility !== "hidden";

@Component({
  selector: "weave-step-details",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  encapsulation: ViewEncapsulation.None,
  imports: [
    Icon,
    RowMenu,
    Modal,
    ParameterForm,
    DragMap,
    InputPane,
    OutputPane,
    TestEventPane,
    TriggerEventPane,
    TriggerSetup,
    CloseGuard,
    ExecuteDialog,
    ExecutePathDialog,
    SimulateFailureDialog,
    EventStartsTab,
    SettingsTab,
    IssuesIndicator,
  ],
  templateUrl: "./step-details.html",
  styleUrls: ["./step-details.css", "./panes/panes.css"],
  host: { "(document:focusin)": "focusIn($event)" },
})
export class StepDetails {
  host = input.required<StepDetailsHost>();
  request = input.required<StepDetailsRequest>();
  controller = input.required<StepDetailsController>();
  closed = output<string>();

  private readonly details = inject(StepDetailsService);
  private readonly dialogs = inject(DialogService);
  private readonly lifetime = inject(DestroyRef);
  readonly announcement = signal("");
  private sessionCache: {
    request: StepDetailsRequest;
    controller: StepDetailsController;
    session: FormSession;
    opening: number;
  } | null = null;
  session(): FormSession {
    const request = this.request();
    const controller = this.controller();
    const opening = this.details.opening();
    if (
      this.sessionCache?.request !== request ||
      this.sessionCache.controller !== controller ||
      this.sessionCache.opening !== opening ||
      !this.sessionCache.session.isCurrent()
    ) {
      untracked(() => this.clearMapping());
      this.sessionCache = {
        request,
        controller,
        opening,
        session: new FormSession(
          this.host(),
          controller,
          request,
          {
            confirm: (options) => this.dialogs.confirm(options),
            prompt: (options) => this.dialogs.prompt(options),
            focusField: (field, open) => {
              if (
                !this.lifetime.destroyed &&
                this.details.request() === request &&
                this.details.opening() === opening
              )
                this.focusField(field, open);
            },
            announce: (text) => {
              this.announcement.set("");
              setTimeout(() => {
                if (
                  !this.lifetime.destroyed &&
                  this.details.request() === request &&
                  this.details.opening() === opening
                )
                  this.announcement.set(text);
              });
            },
          },
          () =>
            !this.lifetime.destroyed &&
            this.details.request() === request &&
            this.details.opening() === opening,
        ),
      };
    }
    return this.sessionCache.session;
  }
  private readonly injector = inject(Injector);
  private readonly dialog =
    viewChild.required<ElementRef<HTMLElement>>("dialog");
  private readonly chosenTab = signal<StepDetailsTab>("parameters");
  /** The tab shown: Parameters where the one chosen isn't offered. */
  tab(): StepDetailsTab {
    const chosen = this.chosenTab();
    return chosen === "starts" && !this.startsTrigger() ? "parameters" : chosen;
  }
  readonly renaming = signal<string | null>(null);
  renameText = "";
  private readonly renames: { from: string; to: string }[] = [];
  private lastInside: HTMLElement | null = null;
  private lastRegion: Region | null = null;
  private renderedLayout: ReturnType<StepDetails["layout"]> | null = null;
  readonly titles = {
    previous: `Previous step (${formatCombo("Mod+Alt+Shift+ArrowLeft", KEY_PLATFORM)})`,
    next: `Next step (${formatCombo("Mod+Alt+Shift+ArrowRight", KEY_PLATFORM)})`,
  };

  readonly pane = signal<"input" | "parameters" | "output">("parameters");
  readonly dataPane = signal<"input" | "output">("input");
  /** The side panes the person folded to rails in table view. */
  readonly folds = signal<PaneFolds>({
    input: false,
    output: false,
    data: true,
  });
  readonly sides = ["input", "output"] as const;
  /** What the open component draws in the Input and Output panes (the table's test values and result). */
  readonly tablePanes = signal<ParameterPanes | null>(null);
  /** The registered parameter component last loaded, by its kind and slot. */
  private readonly loadedComponent = signal<{
    key: string;
    type: Type<ParameterComponent>;
    wide: boolean;
  } | null>(null);
  /** The parameter component open in place of this step's form, once its chunk has loaded. */
  component(): { type: Type<ParameterComponent>; wide: boolean } | null {
    const open = this.controller().parameterComponent();
    const loaded = this.loadedComponent();
    if (!open || open.target !== this.target() || this.tab() !== "parameters")
      return null;
    return loaded?.key === `${open.kind}/${open.subNode ?? ""}` ? loaded : null;
  }
  /**
   * True while table view is open (the decision table grid): the panes keep
   * widths of their own, Input and Output hold the test values and the
   * result, and three panes need 1280 px.
   */
  table(): boolean {
    return !!this.component()?.wide;
  }
  /** The pane widths in use: table view's own while it is open. */
  private profile(): PaneProfile {
    return this.table() ? TABLE_PANES : FORM_PANES;
  }
  /** Execute step is the primary button unless the open component shows its own. */
  executePrimary(): boolean {
    return !(this.tablePanes() && this.componentRef?.instance.ownsPrimary?.());
  }
  /** Where the open component is drawn, in place of the form. */
  private readonly componentHost = viewChild("componentHost", {
    read: ViewContainerRef,
  });
  private componentRef: ComponentRef<ParameterComponent> | null = null;
  /** Where the open component's Input and Output panes are drawn. */
  private readonly tableInputHost = viewChild("tableInputHost", {
    read: ViewContainerRef,
  });
  private readonly tableOutputHost = viewChild("tableOutputHost", {
    read: ViewContainerRef,
  });
  private paneRefs: {
    input: ComponentRef<unknown>;
    output: ComponentRef<unknown>;
  } | null = null;
  /** What the open component reads and edits. */
  componentContext(): NdvContext {
    return this.controller().ndvContext(this.target());
  }
  /**
   * True from 768 to 1099 px in table view: the grid's compact columns, and
   * the Data pane folds to a rail beside it.
   */
  compact(): boolean {
    return (
      this.table() && this.layout() === "two" && this.host().windowWidth < 1100
    );
  }
  /** A pane's Fold button shows where the pane can fold. */
  foldable(): boolean {
    return this.layout() === "three" || this.compact();
  }
  /** Fold in a pane's header: the pane folds to its rail, and the rail takes focus. */
  fold(pane: "input" | "output") {
    const side: keyof PaneFolds = this.layout() === "two" ? "data" : pane;
    this.setFolds({ ...this.folds(), [side]: true });
    afterNextRender(
      () =>
        this.dialog()
          .nativeElement.querySelector<HTMLElement>(`.sd-rail-${side}`)
          ?.focus(),
      { injector: this.injector },
    );
  }
  /** A rail opens its pane again, and its Fold button takes focus. */
  unfold(side: keyof PaneFolds) {
    this.setFolds({ ...this.folds(), [side]: false });
    // The pane opens at its width, and the other side gives way so the
    // grid keeps its minimum.
    if (side !== "data" && this.total)
      this.setWidths(fitted(this.widths(), side, this.total, this.profile()));
    afterNextRender(
      () =>
        this.dialog()
          .nativeElement.querySelector<HTMLElement>(
            side === "data"
              ? `.sd-${this.dataPane()} .dg-fold`
              : `.sd-${side} .dg-fold`,
          )
          ?.focus(),
      { injector: this.injector },
    );
  }
  private setFolds(folds: PaneFolds) {
    this.folds.set(folds);
    writeFolds(folds);
  }
  /** True while a side pane shows as its rail. */
  folded(side: keyof PaneFolds): boolean {
    if (side === "data") return this.compact() && this.folds().data;
    return this.table() && this.layout() === "three" && this.folds()[side];
  }
  readonly widths = signal<PaneWidths>({ input: 0, output: 0 });
  private readonly inputPane = viewChild(InputPane);
  private readonly outputPane = viewChild(OutputPane);
  private readonly panes = viewChild<ElementRef<HTMLElement>>("panes");
  private total = 0;
  private dragging: {
    side: keyof PaneWidths;
    x: number;
    start: PaneWidths;
    pointer: number;
    handle: HTMLElement;
  } | null = null;
  yamlOpen = false;
  yamlText = "";
  yamlError = "";

  constructor() {
    // Leaving the editor asks the same question about an unsaved output edit.
    this.details.askBeforeLeaving = () =>
      new Promise<boolean>((resolve) =>
        this.guard(
          () => resolve(true),
          () => resolve(false),
        ),
      );
    this.details.outputEditDirty = () => !!this.outputPane()?.editDirty();
    this.lifetime.onDestroy(() => {
      this.details.askBeforeLeaving = null;
      this.details.outputEditDirty = null;
      // A question still open when step details goes keeps editing.
      this.guarding()?.keep?.();
    });
    effect(() => {
      this.details.opening();
      const request = this.request();
      untracked(() => {
        this.chosenTab.set(request.tab);
        this.startsRead.set(null);
        this.renaming.set(null);
        // Opening a step shows its form; the grid opens from Edit table.
        this.controller().parameterComponent.set(null);
        // A step of a run opens on what it returned.
        const focus: FocusTarget =
          this.host().runData && request.focus.kind === "first"
            ? { kind: "region", region: "output" }
            : request.focus;
        afterNextRender(() => this.applyFocus(focus), {
          injector: this.injector,
        });
      });
    });
    effect(() => {
      this.host().tick();
      untracked(() => this.keepTarget());
    });
    // The open component is drawn where the form was, and reads the step
    // again whenever the workflow changes.
    effect(() => {
      const open = this.component();
      const place = this.componentHost();
      this.host().tick();
      this.target();
      untracked(() => {
        if (!open || !place) {
          this.componentRef = null;
          this.tablePanes.set(null);
          return;
        }
        if (
          this.componentRef?.componentType !== open.type ||
          place.indexOf(this.componentRef.hostView) < 0
        ) {
          place.clear();
          this.componentRef = place.createComponent(open.type);
        }
        this.componentRef.setInput("context", this.componentContext());
        this.tablePanes.set(this.componentRef.instance.panes ?? null);
      });
    });
    // Its Input and Output panes, drawn in place of the step's data.
    effect(() => {
      const panes = this.tablePanes();
      const input = this.tableInputHost();
      const output = this.tableOutputHost();
      untracked(() => {
        if (!panes || !input || !output) {
          this.paneRefs = null;
          return;
        }
        const drawn = this.paneRefs;
        if (
          drawn &&
          drawn.input.componentType === panes.input &&
          input.indexOf(drawn.input.hostView) >= 0 &&
          output.indexOf(drawn.output.hostView) >= 0
        ) {
          drawn.input.setInput("state", panes.state);
          drawn.output.setInput("state", panes.state);
          return;
        }
        input.clear();
        output.clear();
        const refs = {
          input: input.createComponent(panes.input),
          output: output.createComponent(panes.output),
        };
        for (const [side, ref] of Object.entries(refs) as [
          keyof PaneFolds,
          ComponentRef<unknown>,
        ][]) {
          ref.setInput("state", panes.state);
          ref.setInput("foldable", this.foldable());
          (
            ref.instance as {
              fold?: { subscribe(next: () => void): unknown };
            }
          ).fold?.subscribe(() => this.fold(side as "input" | "output"));
        }
        this.paneRefs = refs;
      });
    });
    // Table view keeps widths and folds of its own; in two panes it shows
    // Output first, where trying the table answers.
    effect(() => {
      const table = this.table();
      untracked(() => {
        if (this.total)
          this.widths.set(readWidths(this.total, undefined, this.profile()));
        this.folds.set(
          table ? readFolds() : { input: false, output: false, data: true },
        );
        if (table && this.layout() === "two") this.dataPane.set("output");
      });
    });
    // A registered component asked for: load its chunk once.
    effect(() => {
      const open = this.controller().parameterComponent();
      if (!open) return;
      const key = `${open.kind}/${open.subNode ?? ""}`;
      if (untracked(() => this.loadedComponent())?.key === key) return;
      const registration = ndvRegistry.parameters(open.kind, open.subNode);
      if (!registration) return;
      void registration.load().then(
        (type) => {
          if (this.lifetime.destroyed) return;
          this.loadedComponent.set({
            key,
            type,
            wide: !!registration.wide,
          });
          this.host().refreshView();
        },
        () =>
          this.host().notify(
            "Studio couldn't load the table editor. Reload Studio to try again.",
            undefined,
            "danger",
          ),
      );
    });
    // The sheet's sticky pane tabs and step header change height as the
    // name wraps or the pane changes: keep their height measured.
    const sticky = new ResizeObserver(() => this.measureSticky());
    afterEveryRender(() => {
      this.paneRefs?.input.setInput("foldable", this.foldable());
      this.paneRefs?.output.setInput("foldable", this.foldable());
      this.coverShell();
      this.recoverLayoutFocus();
      this.measureSticky(sticky);
      const menu = this.mapMenu();
      if (
        menu &&
        (!menu.session.isCurrent() ||
          !menu.row.isConnected ||
          menu.targets.some((target) => !target.owns()))
      )
        this.clearMapping();
    });

    const observer = new ResizeObserver(([entry]) => {
      this.dragEnd();
      this.total = Math.round(entry.contentRect.width);
      this.widths.set(readWidths(this.total, undefined, this.profile()));
    });
    afterNextRender(
      () => {
        const panes = this.panes()?.nativeElement;
        if (panes) observer.observe(panes);
      },
      { injector: this.injector },
    );
    inject(DestroyRef).onDestroy(() => {
      this.releaseShell();
      this.dragEnd();
      observer.disconnect();
      sticky.disconnect();
      this.clearMapping();
    });
  }

  /**
   * How much of the sheet's top its sticky bars cover (the pane tabs, and
   * the step header over Parameters), as --sd-sticky on the scroller: what
   * scrolls into view, by Tab and Shift+Tab too, stops below them instead
   * of under the header (WCAG 2.4.11).
   */
  private measureSticky(observer?: ResizeObserver) {
    const panes = this.panes()?.nativeElement;
    if (!panes) return;
    let covered = 0;
    if (this.layout() === "sheet") {
      const top = panes.getBoundingClientRect().top;
      for (const bar of panes.querySelectorAll<HTMLElement>(
        ":scope > .sd-segments, :scope > .sd-main > .sd-head",
      )) {
        observer?.observe(bar);
        // A bar in a hidden pane has no box, and covers nothing.
        covered = Math.max(covered, bar.getBoundingClientRect().bottom - top);
      }
    }
    const value = covered > 0 ? `${Math.ceil(covered)}px` : "";
    if (panes.style.getPropertyValue("--sd-sticky") === value) return;
    if (value) panes.style.setProperty("--sd-sticky", value);
    else panes.style.removeProperty("--sd-sticky");
  }

  // ---------------------------------------------------------- modality
  /** Shell elements this dialog made inert (others may be inert for their own reasons). */
  private readonly inerted = new Set<Element>();
  /**
   * While step details is open, the rest of the shell is inert: no pointer,
   * keyboard or assistive technology reaches it. Live regions, toasts and
   * the dialogs step details opens stay usable.
   */
  private coverShell() {
    if (this.details.request() !== this.request()) return this.releaseShell();
    const dialog = this.dialog().nativeElement;
    const covered = new Set(
      coveredBy<Element>(
        dialog,
        document.body,
        (node) => node.matches(".sd-scrim") || node.matches(keepUsable),
      ),
    );
    for (const element of this.inerted)
      if (!covered.has(element)) {
        element.removeAttribute("inert");
        this.inerted.delete(element);
      }
    for (const element of covered)
      if (!element.hasAttribute("inert")) {
        element.setAttribute("inert", "");
        this.inerted.add(element);
      }
  }
  private releaseShell() {
    for (const element of this.inerted) element.removeAttribute("inert");
    this.inerted.clear();
  }

  // ---------------------------------------------------------------- what
  target(): string {
    return this.request().target;
  }
  pseudo(): boolean {
    return this.target().startsWith("$");
  }
  name(): string {
    const choice = this.triggerChoice();
    return choice
      ? `${TRIGGER_LABELS[choice.kind]} trigger`
      : targetName(this.target());
  }
  /** The trigger choice these step details show, or null. */
  triggerChoice(): TriggerIntent | null {
    return this.controller().triggerChoice(this.target());
  }
  /** The input form or a trigger choice: what starts the workflow. */
  triggerTarget(): boolean {
    return isTriggerTarget(this.target());
  }
  layout(): "three" | "two" | "sheet" {
    const width = this.host().windowWidth;
    // Table view's three panes need room for the grid's tracks.
    const three = this.table() ? 1280 : 1100;
    return width >= three ? "three" : width >= 768 ? "two" : "sheet";
  }
  private iconOf(id: string): string {
    if (id === "$trigger") return "manualTrigger";
    const choice = this.controller().triggerChoice(id);
    if (choice) return TRIGGER_ICONS[choice.kind];
    if (id === "$end") return "check";
    const step = this.controller().step(id);
    return step
      ? (ndvRegistry.kind(step.kind)?.icon ?? step.kind)
      : "workflows";
  }
  kindIcon(): string {
    return this.iconOf(this.target());
  }
  /** The subtitle last shown, kept while a formula field has focus. */
  private shownSubtitle: { session: FormSession; text: string } | null = null;
  /**
   * The subtitle under the step's name. While a formula field has focus its
   * intermediate formulas don't change it; it updates when the field is left.
   */
  subtitle(): string {
    const session = this.session();
    const held = this.shownSubtitle;
    if (session.formulaFocused() && held?.session === session) return held.text;
    const text = this.liveSubtitle();
    this.shownSubtitle = { session, text };
    return text;
  }
  private liveSubtitle(): string {
    if (this.target() === "$trigger")
      return "Manual form · starts a run with its input fields";
    const choice = this.triggerChoice();
    if (choice?.kind === "called")
      return "Runs when another workflow calls it.";
    if (choice) return TRIGGER_DESCRIPTIONS[choice.kind];
    if (this.target() === "$end") return "End · what the workflow returns";
    const step = this.controller().step(this.target());
    // A kind that comes with its own descriptor (Call a workflow) names itself and its tile's summary.
    const own =
      step && !(kinds as readonly string[]).includes(step.kind)
        ? this.controller().descriptor(this.target())
        : null;
    if (step && own)
      return [own.label, own.summary(step, this.controller().kindContext())]
        .filter(Boolean)
        .join(" · ");
    return step
      ? headerSubtitle(
          step,
          this.controller().kindContext(),
          this.host().label(step.kind),
          (slot) =>
            connectionsPort(this.host())?.binding(
              this.host().model.definition.metadata.name,
              slot,
            )?.name ?? null,
        )
      : "";
  }
  crumb(): string {
    const where = this.pseudo()
      ? ""
      : breadcrumb(this.host().model.definition, this.target());
    // On a phone the run's chip stands in for it; the step's name in
    // Parameters says which step this is.
    if (this.host().windowWidth < 480 && this.runChip()) return "";
    return where;
  }
  docs(): string {
    const step = this.controller().step(this.target());
    return docsUrl(
      step?.kind ?? this.target(),
      step ? recipeOf(step, this.controller().kindContext()) : null,
    );
  }
  private firstStep(): string | null {
    return this.host().model.definition.spec.steps[0]?.id ?? null;
  }
  previous(): string | null {
    if (this.pseudo()) return null;
    return (
      adjacent(this.host().model.definition, this.target(), -1) ??
      (this.target() === this.firstStep() ? "$trigger" : null)
    );
  }
  next(): string | null {
    if (this.triggerTarget()) return this.firstStep();
    return this.pseudo()
      ? null
      : adjacent(this.host().model.definition, this.target(), 1);
  }
  neighborList(): { before: string[]; after: string[] } {
    if (this.triggerTarget())
      return {
        before: [],
        after: [this.firstStep()].filter((id): id is string => !!id),
      };
    if (this.pseudo()) return { before: [], after: [] };
    return neighbors(this.host().model.definition, this.target());
  }
  shown(list: string[]): string[] {
    return list.slice(0, 6);
  }
  overflow(list: string[]): RowMenuItem[] {
    return list.slice(6).map((id) => ({
      label: `Open ${this.neighborName(id)}`,
      run: () => this.go(id),
    }));
  }
  neighborName(id: string): string {
    return isTriggerTarget(id) ? "trigger" : id;
  }
  neighborIcon(id: string): string {
    return this.iconOf(id);
  }

  // ------------------------------------------------------------- moving
  currentRegion(): Region | null {
    return this.regionOf(document.activeElement as HTMLElement | null);
  }
  private regionOf(active: HTMLElement | null): Region | null {
    const region = active
      ?.closest("[data-region]")
      ?.getAttribute("data-region");
    if (!region && active?.closest(".sd-segments, .sd-data-tabs"))
      return (
        (active.getAttribute("aria-controls")?.replace("sd-region-", "") as
          | Region
          | undefined) ?? null
      );
    return (region as Region | null) ?? null;
  }
  /**
   * Moves to another step through the editor's own opener, so the step
   * becomes the selection with the inspector following it; writing the
   * selection here would leave the inspector on the previous step, holding
   * what looks like an unapplied edit that blocks every later open.
   */
  go(id: string) {
    const region = this.currentRegion() ?? "parameters";
    const request = this.request();
    // Entries this step keeps are said politely; a toast would cover fields.
    const kept = this.session().heldLabels().length;
    const name = this.name();
    void moveToStep(
      this.host(),
      id,
      {
        tab: id === "$end" ? "parameters" : this.tab(),
        focus: { kind: "region", region },
        fresh: false,
        revealAll: false,
      },
      () => !this.lifetime.destroyed && this.details.request() === request,
    ).then((moved) => {
      if (moved && kept) this.say(keptText(name, kept));
    });
  }
  /** A polite announcement in step details' live region. */
  private say(text: string) {
    this.announcement.set("");
    setTimeout(() => {
      if (!this.lifetime.destroyed) this.announcement.set(text);
    });
  }
  move(delta: 1 | -1) {
    const id = delta > 0 ? this.next() : this.previous();
    if (id) this.guard(() => this.go(id));
  }
  // ------------------------------------------------- unsaved output edits
  /** The question before an unsaved output edit goes, and what waits for its answer. */
  readonly guarding = signal<{ next(): void; keep?(): void } | null>(null);
  /** Runs `next` at once when no output edit is unsaved; otherwise asks first. */
  guard(next: () => void, keep?: () => void) {
    if (!this.outputPane()?.editDirty()) return next();
    this.guarding.set({ next, keep });
  }
  async answerGuard(answer: GuardAnswer) {
    const pending = this.guarding();
    this.guarding.set(null);
    if (!pending) return;
    const pane = this.outputPane();
    if (answer === "save" && !(await pane?.saveEdit())) answer = "keep";
    if (answer === "keep") {
      pending.keep?.();
      // The editor shows the words when Save was refused.
      setTimeout(() => pane?.focusEditor());
      return;
    }
    if (answer === "discard") pane?.closeEditor();
    pending.next();
  }
  /** Switches the shown pane (the tabs below 768 px); leaving an unsaved output edit asks first. */
  choosePane(which: "pane" | "dataPane", choice: string) {
    const current = which === "pane" ? this.pane() : this.dataPane();
    const set = () =>
      which === "pane"
        ? this.pane.set(choice as "input" | "parameters" | "output")
        : this.dataPane.set(choice as "input" | "output");
    if (current === "output" && choice !== "output") this.guard(set);
    else set();
  }
  /** Closes step details; an unsaved output edit asks first. */
  close() {
    this.guard(() => this.closeNow());
  }
  private closeNow() {
    // What doesn't apply yet stays for the next time; with step details
    // gone nothing else shows it, so a toast names the entries still kept,
    // on this step and on the steps it was left on, in the workflow's order.
    const kept = leavingToast(this.session().heldOnLeaving());
    const host = this.host();
    // The opener gets focus back: the shell is usable again first.
    this.releaseShell();
    this.closed.emit(this.target());
    if (kept) host.notify(kept);
  }
  /** The open step was renamed back by Undo, or deleted: follow it, or close. */
  private keepTarget() {
    const target = this.target();
    if (target.startsWith("$") || this.controller().step(target)) return;
    const selected = this.host().model.selected;
    const followsRename = this.renames.some(
      ({ from, to }) =>
        (from === target && to === selected) ||
        (to === target && from === selected),
    );
    if (followsRename && this.controller().step(selected)) {
      this.controller().renamePending(target, selected);
      this.details.open({
        ...this.request(),
        target: selected,
        focus: { kind: "region", region: "header" },
      });
      return;
    }
    this.details.close();
  }

  // ------------------------------------------------------------- panes
  paneStyle(): Record<string, string> {
    const w = this.effective();
    return w.input
      ? { "--sd-input": `${w.input}px`, "--sd-output": `${w.output}px` }
      : {};
  }
  /** The widths drawn: a folded pane is its rail. */
  private effective(): PaneWidths {
    const w = this.widths();
    return {
      input: this.folded("input") ? RAIL : w.input,
      output: this.folded("output") ? RAIL : w.output,
    };
  }
  showPane(which: "input" | "parameters" | "output"): boolean {
    const layout = this.layout();
    if (layout === "three")
      return which === "parameters" || !this.folded(which);
    if (layout === "two" && which !== "parameters" && this.folded("data"))
      return false;
    if (layout === "two")
      return which === "parameters" || which === this.dataPane();
    return which === this.pane();
  }
  maxOf(side: keyof PaneWidths): number {
    return largest(this.effective(), side, this.total, this.profile());
  }
  /** A side's new width, with the other side as drawn (its rail when folded). */
  private withSide(side: keyof PaneWidths, next: PaneWidths): PaneWidths {
    return { ...this.widths(), [side]: next[side] };
  }
  private setWidths(next: PaneWidths) {
    this.widths.set(next);
    writeWidths(next, this.total, undefined, this.profile());
  }
  dragStart(event: PointerEvent, side: keyof PaneWidths) {
    if (event.button !== 0 || this.dragging || this.layout() !== "three")
      return;
    event.preventDefault();
    const handle = event.currentTarget as HTMLElement;
    handle.setPointerCapture(event.pointerId);
    this.dragging = {
      side,
      x: event.clientX,
      start: this.widths(),
      pointer: event.pointerId,
      handle,
    };
  }
  dragMove(event: PointerEvent) {
    const drag = this.dragging;
    if (!drag || drag.pointer !== event.pointerId) return;
    const delta = (event.clientX - drag.x) * (drag.side === "input" ? 1 : -1);
    const start = {
      ...drag.start,
      input: this.folded("input") ? RAIL : drag.start.input,
      output: this.folded("output") ? RAIL : drag.start.output,
    };
    this.widths.set(
      this.withSide(
        drag.side,
        resized(start, drag.side, delta, this.total, this.profile()),
      ),
    );
  }
  dragEnd(event?: PointerEvent) {
    const drag = this.dragging;
    if (!drag || (event && drag.pointer !== event.pointerId)) return;
    this.dragging = null;
    if (drag.handle.hasPointerCapture(drag.pointer))
      drag.handle.releasePointerCapture(drag.pointer);
    writeWidths(this.widths(), this.total, undefined, this.profile());
  }
  separatorKey(event: KeyboardEvent, side: keyof PaneWidths) {
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey)
      return;
    const grow = side === "input" ? "ArrowRight" : "ArrowLeft";
    const shrink = side === "input" ? "ArrowLeft" : "ArrowRight";
    let next: PaneWidths | null = null;
    const now = this.effective();
    const profile = this.profile();
    if (event.key === grow)
      next = resized(now, side, KEY_STEP, this.total, profile);
    else if (event.key === shrink)
      next = resized(now, side, -KEY_STEP, this.total, profile);
    else if (event.key === "Home")
      next = toEdge(now, side, "min", this.total, profile);
    else if (event.key === "End")
      next = toEdge(now, side, "max", this.total, profile);
    if (!next) return;
    event.preventDefault();
    this.setWidths(this.withSide(side, next));
  }
  resetWidths() {
    this.setWidths(this.profile().defaults(this.total));
  }

  // ------------------------------------------------------------ regions
  private regions(): Region[] {
    return ["header", "input", "parameters", "output"];
  }
  cycleRegion(delta: 1 | -1) {
    const order = this.regions();
    const at = order.indexOf(this.currentRegion() ?? "parameters");
    const next = order[(at + delta + order.length) % order.length];
    if (this.layout() === "sheet")
      this.choosePane("pane", next === "header" ? "parameters" : next);
    if (this.layout() === "two" && (next === "input" || next === "output"))
      this.choosePane("dataPane", next);
    afterNextRender(() => this.applyFocus({ kind: "region", region: next }), {
      injector: this.injector,
    });
  }

  // ------------------------------------------------------- execute step
  executeLabel(): string {
    return this.runsWorkflow() ? "Execute workflow" : "Execute step";
  }
  /**
   * The primary button's tooltip: why a call step executes the whole
   * workflow, the item a step inside a loop executes on, and what a loop's
   * own Execute step runs.
   */
  executeTitle(): string {
    const item = this.pickedItem();
    const loop =
      (this.controller().step(this.target())?.kind as string) === "forEach";
    return this.callTarget()
      ? "A call runs only when the whole workflow executes."
      : item && !this.runsWorkflow()
        ? `Execute step on item ${item} (Ctrl/Cmd+Enter)`
        : loop
          ? "Execute the steps inside for every item (Ctrl/Cmd+Enter)"
          : `${this.executeLabel()} (Ctrl/Cmd+Enter)`;
  }
  /** Inside a loop's body: the item picked, as people count it ("3", "2 › 1"); null elsewhere. */
  pickedItem(): string | null {
    const key = this.controller().iterations.instanceKey(this.target());
    if (!key) return null;
    return [...key.matchAll(/\[(\d+)\]/g)]
      .map((match) => (Number(match[1]) + 1).toLocaleString("en-US"))
      .join(" › ");
  }
  /** A step that calls a workflow: it runs only when the whole workflow does. */
  callTarget(): boolean {
    return (
      (this.controller().step(this.target())?.kind as string) === "callWorkflow"
    );
  }
  /** What starts the workflow, or a call step: the primary button executes the whole workflow. */
  runsWorkflow(): boolean {
    return this.triggerTarget() || this.callTarget();
  }
  // ------------------------------------------------------- run mode
  /** "Run 1c0d7f8a": the run step details reads, on the Runs tab; "" otherwise. */
  runChip(): string {
    const run = this.host().runData;
    return run ? `Run ${run.runId.slice(0, 8)}` : "";
  }
  /** A step that ran several times: each iteration with its state, the failed one first. */
  runIterations(): { key: string; label: string }[] {
    const run = this.host().runData;
    const target = this.target();
    if (!run || target.startsWith("$")) return [];
    run.version();
    const states = {
      failed: "Failed",
      handled: "Failed, continued",
      running: "Running",
      finished: "Finished",
      "not-reached": "Not reached",
    } as const;
    // Inside a loop: every item of the loop, the failed ones first.
    const items = this.controller().runItems(target);
    if (items) {
      const failed = (key: string) => (run.state(key) === "failed" ? 0 : 1);
      return [...items.keys]
        .sort((a, b) => failed(a) - failed(b) || indexOf(a) - indexOf(b))
        .map((key) => ({
          key,
          label: `Item ${(indexOf(key) + 1).toLocaleString("en-US")} of ${items.count.toLocaleString("en-US")} · ${states[run.state(key)]}`,
        }));
    }
    const keys = run.iterations(target);
    if (keys.length < 2) return [];
    // Numbered in loop order; listed failed first, as iterations() gives them.
    const ordered = [...keys].sort((a, b) => indexOf(a) - indexOf(b));
    return keys.map((key) => ({
      key,
      label: `Item ${ordered.indexOf(key) + 1} of ${keys.length} · ${states[run.state(key)]}`,
    }));
  }
  chooseIteration(key: string): void {
    this.controller().chooseRunIteration(this.target(), key);
  }
  backToEditing(): void {
    void this.host().selectTab?.("Designer");
  }
  canExecute(): boolean {
    return (
      this.target() !== "$end" &&
      !this.controller().readOnlyReason() &&
      !!this.host().executeIn
    );
  }
  /** True while an execution runs; the primary button then reads Stop. */
  executing(): boolean {
    return !!this.host().execution?.running() || !!this.realRunning();
  }
  /** The step test of this step running in the environment now, if any. */
  realRunning(): RunningStepTest | null {
    const running =
      connectionsPort(this.host())?.runningTests()?.running() ?? null;
    return running?.stepId === this.target() ? running : null;
  }
  /** A webhook's header button listens for its test event, where it can. */
  listensHere(): boolean {
    const choice = this.triggerChoice();
    if (choice?.kind !== "webhook" || this.controller().viewingRuns())
      return false;
    const host = this.host();
    return !listenBlocked(this.controller().environmentOf(), (role) =>
      host.can(role),
    );
  }
  /** True while this webhook listens for its test event. */
  listening(): boolean {
    const choice = this.triggerChoice();
    return (
      !!choice && connectionsPort(this.host())?.listeningTo() === choice.id
    );
  }
  toggleListen(): void {
    const choice = this.triggerChoice();
    if (!choice) return;
    if (this.listening()) return stopListening(this.host());
    listenFor(this.host(), choice, this.dialogs, this.controller().testEvent());
    // The test URL shows in the Test event pane: behind its tab on a narrow window.
    if (!this.showPane("input"))
      this.choosePane(this.layout() === "sheet" ? "pane" : "dataPane", "input");
  }
  executeItems(): RowMenuItem[] {
    // A trigger, or a call step, executes the workflow: here, or in the environment.
    if (this.runsWorkflow()) {
      const real = this.controller().viewingRuns()
        ? null
        : workflowTestItem(this.host(), this.dialogs);
      // Listening takes the button: Execute workflow moves to the menu.
      const here: RowMenuItem[] = this.listensHere()
        ? [
            {
              label: "Execute workflow",
              detail: "Runs the workflow on this computer from its test event",
              run: () => void this.execute("step"),
            },
          ]
        : [];
      return [...here, ...(real ? [real] : [])];
    }
    const real = this.realItem();
    const item = this.pickedItem();
    return [
      {
        label: item
          ? `Execute previous steps for item ${item}`
          : "Execute previous steps",
        detail:
          "Runs from the trigger up to this step, reusing pinned and current results",
        run: () => void this.execute("before"),
      },
      ...(real ? [real] : []),
    ];
  }
  /**
   * Execute in <environment>: runs this step for real as a test run. Absent
   * without the step tester role, working locally, on the Runs tab and for
   * kinds that can't run for real; disabled with the reason otherwise.
   */
  realItem(): RowMenuItem | null {
    const host = this.host();
    const id = this.target();
    const controller = this.controller();
    if (id.startsWith("$") || controller.viewingRuns()) return null;
    const port = connectionsPort(host);
    if (!port || !controller.descriptor(id)?.real || !host.can("step.test"))
      return null;
    const reason = this.realBlocked();
    return {
      label: `Execute in ${port.environment.name}`,
      detail: reason ?? "Runs this step for real, as a test run",
      disabled: !!reason,
      run: () => void this.executeReal(),
    };
  }
  /** Why Execute in the environment can't run now; null when it can (a missing connection is said after trying). */
  realBlocked(): string | null {
    const host = this.host();
    const port = connectionsPort(host);
    if (!port) return null;
    const env = port.environment.name;
    if (this.controller().readOnlyReason())
      return this.controller().readOnlyReason();
    if (host.keptLocally && host.draftRevision === undefined)
      return `Save this workflow to the platform to execute its steps in ${env}.`;
    const policy = port.policyNow();
    if (policy === null)
      return `Studio is checking whether test calls are on in ${env}.`;
    if (policy === "unreadable")
      return `Your account can't see whether test calls are on in ${env}.`;
    if (policy.test_calls === "disabled")
      return `Test calls are off in ${env}. Choose an environment where they're on to execute steps there.`;
    // What the step itself still needs (an action, a connection, data from
    // earlier steps) is said after trying, with the way out: working it out
    // evaluates the step's input, which the menu must not do while it draws.
    return null;
  }
  /** The step's slot without a connection chosen in this environment, or "". */
  private missingSlot(): string {
    const id = this.target();
    const step = this.controller().step(id);
    const slot =
      typeof step?.["connection"] === "string" ? step["connection"] : "";
    if (!slot) return "";
    return this.controller().ndvContext(id).connections.devBinding(slot)
      ? ""
      : slot;
  }
  private realRequest() {
    const id = this.target();
    const controller = this.controller();
    const real = controller.descriptor(id)?.real;
    return real
      ? real.request(controller.ndvContext(id), controller.kindContext())
      : null;
  }
  /** Fix: the step's Connection field, in view (Parameters on a narrow window), with its choices open. */
  private openConnection() {
    // An AI task chooses its connection with its model, in the Model field.
    const field = this.session()
      .placedSpecs()
      .find(
        (placed) =>
          placed.spec.type === "connection" || placed.spec.type === "model",
      );
    if (field) this.focusField(field, true);
  }
  /** A notice in this step's Output, said aloud as well. */
  private realNotice(
    message: string,
    action?: { label: string; run: () => void },
  ) {
    this.controller().executeNotice.set({
      target: this.target(),
      message,
      reason: null,
      ...(action ? { action } : {}),
    });
    this.say(message);
  }
  /**
   * Execute in the environment: the same error check as Execute step, then
   * the connection, the step's own actions published, a confirmation for a
   * step that changes data (every time), and the step test itself.
   */
  async executeReal(): Promise<void> {
    const host = this.host();
    const port = connectionsPort(host);
    const id = this.target();
    const controller = this.controller();
    if (!port || this.realBlocked() || this.executing()) return;
    const env = port.environment.name;
    controller.reveal(id);
    controller.executeNotice.set(null);
    const issues = this.blockingIssues([id]);
    const gate = executeGate(issues, [id], false);
    if (gate.kind === "fix") {
      this.question.set({ ...gate, issues: gate.issues as ExecuteIssue[] });
      return;
    }
    const slot = this.missingSlot();
    if (slot) {
      port.attempted(host, id, slot);
      this.realNotice(
        `Choose a connection for slot ${slot} to execute ${id} in ${env}.`,
        { label: "Fix", run: () => this.openConnection() },
      );
      return;
    }
    if (!(await this.publishOwnedFirst(env))) return;
    const request = this.realRequest();
    if (!request) return;
    if ("blocked" in request) {
      const upstream = upstreamMissing(request.blocked);
      this.realNotice(
        request.blocked,
        upstream
          ? {
              label: "Execute previous steps",
              run: () => void this.execute("before"),
            }
          : undefined,
      );
      return;
    }
    const uses = String(controller.step(id)?.["uses"] ?? "");
    const effect = sideEffectOf(controller.kindContext().actionContract(uses));
    // An AI task on a local connection: a model call with no cost to name.
    const accepted = withoutAsking(request, effect);
    if (accepted) return this.runReal(request.body, accepted);
    if (
      effect === "non_idempotent" ||
      effect === "idempotency_key" ||
      request.confirm
    ) {
      this.realPending = {
        body: request.body,
        ack: effectToAcknowledge(request.body, effect),
      };
      this.question.set({
        kind: "real",
        title: `Execute ${id} in ${env}?`,
        stepId: id,
        environment: env,
        ...(request.confirm ? { extra: request.confirm } : {}),
      });
      return;
    }
    await this.runReal(request.body, null);
  }
  /** The step test a question waits on: its body and the effect it acknowledges. */
  private realPending: { body: StepTestBody; ack: SideEffect | null } | null =
    null;
  /** A step that calls its own unpublished action publishes the workflow's actions first. */
  private async publishOwnedFirst(env: string): Promise<boolean> {
    const host = this.host();
    const step = this.controller().step(this.target());
    if (!step || !pendingOwned(host.model).includes(String(step["uses"] ?? "")))
      return true;
    const { name, version } = host.model.definition.metadata;
    const { publishOwned } = await import("./owned/owned-commands");
    const outcome = await publishOwned(this.details, {
      purpose: "execute",
      workflow: { name, version },
      step: this.target(),
      environment: env,
    });
    if (outcome.done) return true;
    if (outcome.reason) this.realNotice(outcome.reason);
    return false;
  }
  /** Sends the step test and shows how it went in this step's Output. */
  private async runReal(body: StepTestBody, ack: SideEffect | null) {
    const host = this.host();
    const port = connectionsPort(host);
    const controller = this.controller();
    const id = this.target();
    if (!port) return;
    const env = port.environment.name;
    const service = port.stepTests(host);
    controller.executeNotice.set(null);
    this.say(`Executing ${id} in ${env}.`);
    // The service tells the canvas which step it follows (the live border), and when it stops.
    const outcome: StepTestOutcome = await service.execute({
      stepId: id,
      body,
      ack,
    });
    if (outcome.kind === "stale") return;
    if (outcome.kind === "done") {
      // The Output pane shows how it ended; this says it aloud.
      this.say(
        realEndWords(id, env, outcome.view, body.timeout_seconds) ??
          `Executed ${id} in ${env}.`,
      );
      return;
    }
    if (outcome.kind === "stopped") {
      if (outcome.why === "aborted") return;
      const runId = outcome.runId;
      const open = runId
        ? {
            label: "Open in Operate",
            run: () => void host.viewRun?.({ id: runId }),
          }
        : undefined;
      this.realNotice(
        outcome.why === "timeout"
          ? `Studio stopped waiting for ${id}. Find its test run in Operate › Runs with Include test runs.`
          : `Studio lost touch with ${env} while ${id} was executing. The test keeps running there; find it in Operate › Runs with Include test runs.`,
        open,
      );
      return;
    }
    const refusal = outcome.refusal;
    switch (refusal.kind) {
      case "confirm":
        this.realPending = { body, ack: refusal.sideEffect };
        this.question.set({
          kind: "real",
          title: `Execute ${id} in ${env}?`,
          stepId: id,
          environment: env,
        });
        return;
      case "release":
        // No release here can run a dependency: there's nothing to choose.
        if (Object.values(refusal.choices).some((list) => !list.length)) {
          this.realNotice(refusal.message);
          return;
        }
        this.realPending = { body, ack };
        this.question.set({
          kind: "release",
          title: "Choose a release",
          stepId: id,
          environment: env,
          choices: refusal.choices,
        });
        return;
      case "connection": {
        const why = Object.values(refusal.slots)[0] ?? "";
        this.realNotice(why || `A connection for ${id} can't be used.`, {
          label: "Fix",
          run: () => this.openConnection(),
        });
        return;
      }
      default:
        this.realNotice(refusalWords(refusal, id, env));
    }
  }
  /** The "?" sheet: the keys of step details and the run log; null while closed. */
  readonly sheet = signal<SheetSection[] | null>(null);
  closeSheet() {
    this.sheet.set(null);
    setTimeout(() =>
      this.dialog().nativeElement.focus({ preventScroll: true }),
    );
  }
  /** What Execute asks first: errors to fix, or whether to unpin. */
  readonly question = signal<ExecuteQuestion | null>(null);
  /** The panes' Execute step and Execute previous steps buttons run step details' Execute. */
  private readonly executeHook = effect(() => {
    const controller = this.controller();
    controller.executeHook = (mode) => void this.execute(mode);
  });
  /** Debug in editor stopped at one item of a loop: step details open on that item. */
  private readonly runItem = effect(() => {
    const target = this.target();
    const controller = this.controller();
    untracked(() => controller.pickRunItem(target));
  });
  /** A run log row opened a step on one item of a loop: step details show that item. */
  private readonly openedItem = effect(() => {
    const opened = this.host().openedItem?.();
    const target = this.target();
    const controller = this.controller();
    untracked(() => controller.pickOpenedItem(opened, target));
  });
  /**
   * Execute step (or Execute previous steps): asks first about errors in the
   * steps it runs and, for a pinned step, about its pin; then executes on the
   * Studio host. While one runs, the primary button stops it.
   */
  async execute(mode: "step" | "before"): Promise<void> {
    const host = this.host();
    if (!this.canExecute()) return;
    if (this.executing()) return this.stop();
    const id = this.target();
    const controller = this.controller();
    const workflow = host.model.definition;
    // Inside a loop's body, the item picked: Execute runs that one.
    const item = controller.iterations.instanceKey(id);
    const target: ExecuteTarget = this.runsWorkflow()
      ? { mode: "workflow" }
      : { mode, stepId: id, ...(item ? { instanceKey: item } : {}) };
    // Execute step shows every message of the step, required ones too.
    controller.reveal(id);
    const runs =
      target.mode === "workflow"
        ? allSteps(workflow.spec.steps).map((step) => step.id)
        : (sliceStepIds(workflow, id) ?? [id]).filter(
            (step) => mode === "step" || step !== id,
          );
    const issues = this.blockingIssues(runs);
    const gate = executeGate(
      issues,
      runs,
      mode === "step" &&
        target.mode === "step" &&
        !!(controller.pinOfTarget(item ?? id) ?? controller.pinOfTarget(id)),
    );
    if (gate.kind === "fix") {
      this.question.set({ ...gate, issues: gate.issues as ExecuteIssue[] });
      return;
    }
    // Inside an error path its step won't take: simulate that step's failure first.
    const data = controller.testData.data();
    const untaken =
      target.mode === "workflow"
        ? null
        : untakenPath(
            workflow.spec.steps,
            id,
            (owner) => !!failureOf(data, controller.pinKey(owner)),
            (owner) => simulateBlock(owner, false) === null,
          );
    if (untaken) {
      this.pathAsked.set({
        ...untaken,
        ownerIcon: this.iconOf(untaken.owner),
        stepIcon: this.iconOf(id),
      });
      return;
    }
    if (gate.kind === "unpin") {
      this.question.set({ ...gate, stepId: id });
      return;
    }
    await this.executeNow(target);
  }
  /** "Simulate a failure of submit first", while it is asked. */
  readonly pathAsked = signal<{
    owner: string;
    stepId: string;
    ownerIcon: string;
    stepIcon: string;
  } | null>(null);
  /** Simulate a failure of the step whose error path holds this one, while its dialog is open. */
  readonly pathSimulating = signal<
    (NonNullable<ReturnType<typeof simulateOpening>> & { owner: string }) | null
  >(null);
  /** The question closed: Simulate a failure opens its dialog for that step; Cancel goes back to Execute step. */
  pathAnswered(simulate: boolean) {
    const asked = this.pathAsked();
    this.pathAsked.set(null);
    const opening =
      simulate && asked
        ? simulateOpening(this.controller(), asked.owner)
        : null;
    if (asked && opening)
      this.pathSimulating.set({ ...opening, owner: asked.owner });
    else setTimeout(() => this.focusExecute());
  }
  /** Writes the failure chosen for the step whose path holds this one, for the item picked when it opened. */
  async pathFailureChosen(choice: FailureChoice) {
    const open = this.pathSimulating();
    if (!open) return;
    const controller = this.controller();
    const id = this.target();
    if (controller.pinKey(open.owner) !== open.key) {
      this.say(
        "The picked item changed while the dialog was open, so nothing was simulated.",
      );
      return;
    }
    const written = await controller.setFailure(
      failureKey(open.key, choice.every),
      {
        code: choice.code,
        ...(choice.outcome !== "failed" ? { outcome: choice.outcome } : {}),
      },
    );
    if (this.lifetime.destroyed) return;
    this.say(
      written.ok
        ? `Simulated a failure of ${open.owner}: Execute step now reaches ${id}.`
        : written.message,
    );
  }
  closePathSimulate() {
    this.pathSimulating.set(null);
    setTimeout(() => this.focusExecute());
  }
  /** The errors in the steps an execution runs: the host's check, and this step's own. */
  private blockingIssues(runs: readonly string[]): ExecuteIssue[] {
    const controller = this.controller();
    const id = this.target();
    const found: ExecuteIssue[] = [];
    for (const stepId of runs) {
      if (stepId === id) continue;
      for (const diagnostic of controller.diagnostics(stepId))
        if (diagnostic.severity === "error")
          found.push({
            stepId,
            severity: "error",
            message: diagnostic.message,
            at: diagnostic.at,
          });
    }
    if (runs.includes(id))
      for (const issue of this.issues())
        if (issue.severity === "error")
          found.push({
            stepId: id,
            severity: "error",
            message: issue.message,
            key: issue.key,
          });
    return found;
  }
  /** Executes, then shows the answer in this step's panes. */
  private async executeNow(target: ExecuteTarget): Promise<void> {
    const host = this.host();
    const controller = this.controller();
    const id = this.target();
    controller.executeNotice.set(null);
    const outcome = await host.executeIn!(target);
    if (this.lifetime.destroyed) return;
    const notice = noticeOf(outcome, id);
    controller.executeNotice.set(notice);
    if (outcome.kind === "done") {
      const blocked = outcome.response.blocked;
      this.say(
        blocked
          ? blocked.message
          : target.mode === "before"
            ? `Executed the steps before ${id}.`
            : `Executed ${target.mode === "workflow" ? "the workflow" : id}.`,
      );
    } else if (notice) this.say(notice.message);
  }
  /** Stop: the running execution's answer is dropped; a step test is canceled on the platform. */
  stop() {
    if (this.realRunning()) {
      void connectionsPort(this.host())?.runningTests()?.stop();
      this.say("Stopping the test run.");
      return;
    }
    this.host().execution?.stop();
    this.say("Stopped executing.");
  }
  /** The question's primary answer: Unpin and execute, or Run it for a step test. */
  confirmQuestion() {
    const asked = this.question();
    if (asked?.kind !== "real") return void this.unpinAndExecute();
    this.question.set(null);
    const pending = this.realPending;
    this.realPending = null;
    // Each click asks again; the acknowledgement goes with this request only.
    if (pending) void this.runReal(pending.body, pending.ack);
  }
  /** The release question's answer: remembered for this environment this session. */
  releasesChosen(chosen: Record<string, string>) {
    this.question.set(null);
    const port = connectionsPort(this.host());
    const pending = this.realPending;
    this.realPending = null;
    if (!port || !pending) return;
    for (const [dependency, release] of Object.entries(chosen))
      port.releases.set(dependency, release);
    void this.runReal(pending.body, pending.ack);
  }
  /** Fix: the question closes and the field that holds the issue gets focus. */
  fixIssue(issue: ExecuteIssue) {
    this.question.set(null);
    if (issue.stepId === this.target()) {
      const found = this.issues().find((item) => item.key === issue.key);
      if (found) return this.goToIssue(found);
      return;
    }
    const request = this.request();
    void moveToStep(
      this.host(),
      issue.stepId,
      {
        tab: "parameters",
        focus: issue.at
          ? { kind: "path", path: issue.at }
          : { kind: "firstIssue" },
        fresh: false,
        revealAll: true,
      },
      () => !this.lifetime.destroyed && this.details.request() === request,
    );
  }
  /** Unpin and execute: unpins (with Undo, as the pin toggle does), then executes. */
  async unpinAndExecute() {
    const asked = this.question();
    this.question.set(null);
    if (asked?.kind !== "unpin") return;
    if (!(await this.controller().unpin(asked.stepId))) return;
    await this.executeNow({ mode: "step", stepId: asked.stepId });
  }
  closeQuestion() {
    // Cancel sends nothing.
    this.realPending = null;
    this.question.set(null);
    setTimeout(() => this.focusExecute());
  }
  private focusExecute() {
    this.dialog()
      .nativeElement.querySelector<HTMLElement>(".sd-execute > button")
      ?.focus({ preventScroll: true });
  }

  // -------------------------------------------------------------- issues
  issues(): StepIssue[] {
    return this.session().issues();
  }
  /**
   * Focuses a field's control, switching pane and tab first when needed;
   * `open` also opens its choices (a connection's list, the data picker).
   */
  focusField(field: PlacedSpec, open = false): void {
    if (this.layout() === "sheet" && this.pane() !== "parameters") {
      this.pane.set("parameters");
      afterNextRender(() => this.focusField(field, open), {
        injector: this.injector,
      });
      return;
    }
    if (this.tab() !== field.tab) {
      this.chosenTab.set(field.tab);
      afterNextRender(() => this.focusField(field, open), {
        injector: this.injector,
      });
      return;
    }
    const panel = this.dialog().nativeElement.querySelector<HTMLElement>(
      ".sd-tabpanel:not([hidden])",
    );
    const id = CSS.escape(field.spec.id);
    const host =
      panel?.querySelector<HTMLElement>(
        `[data-param="${id}"][data-param-path="${CSS.escape(JSON.stringify(field.spec.path))}"]`,
      ) ?? panel?.querySelector<HTMLElement>(`[data-param="${id}"]`);
    if (!panel || !host)
      return this.applyFocus({ kind: "region", region: "parameters" });
    const controls = [...host.querySelectorAll<HTMLElement>(".param-control")]
      .flatMap((part) => [...part.querySelectorAll<HTMLElement>(CONTROL)])
      .filter(
        (element) =>
          !element.closest(".param-label-row, .param-tools") &&
          visible(element),
      );
    // The value, not a choice of how to give it (From list | By name).
    const control =
      controls.find((element) => !element.matches('[role="radio"]')) ??
      controls[0];
    if (!control)
      return this.applyFocus({ kind: "region", region: "parameters" });
    control.focus({ preventScroll: true });
    revealTop(host);
    if (!inView(control))
      control.scrollIntoView({ block: "center", inline: "nearest" });
    if (open) this.openChoices(host, control);
  }
  /**
   * Opens a field's list: a dropdown opens on a click; the data picker
   * opens with ArrowDown, also when it replaces a pill pressed for it.
   */
  private openChoices(host: HTMLElement, control: HTMLElement) {
    const picker = (element: HTMLElement | null) =>
      element?.matches('weave-reference-combobox input[role="combobox"]')
        ? element
        : null;
    const show = (input: HTMLElement) =>
      input.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "ArrowDown",
          bubbles: true,
          cancelable: true,
        }),
      );
    if (picker(control)) return void show(control);
    control.click();
    afterNextRender(
      () => {
        const input = picker(
          host.isConnected
            ? host.querySelector<HTMLElement>(
                '.param-control weave-reference-combobox input[role="combobox"]',
              )
            : null,
        );
        if (!input || input.getAttribute("aria-expanded") === "true") return;
        input.focus({ preventScroll: true });
        show(input);
      },
      { injector: this.injector },
    );
  }
  /** An entry of the issues list: its field, else its fix, else the step's name. */
  goToIssue(issue: StepIssue): void {
    const fix = issue.fix?.kind;
    // The data picker or the dropdown opens, as the fix says.
    if (issue.field && fix === "picker") return this.session().runFix(issue);
    if (issue.field) return this.focusField(issue.field, fix === "connection");
    if (issue.fix) return this.session().runFix(issue);
    this.dialog().nativeElement.querySelector<HTMLElement>(".sd-name")?.focus();
  }

  // ---------------------------------------------------------------- more
  moreItems(): RowMenuItem[] {
    const host = this.host();
    const id = this.target();
    const locked = !!this.controller().readOnlyReason();
    if (this.pseudo()) return [];
    return [
      { label: "Rename", disabled: locked, run: () => this.startRename() },
      {
        label: "Duplicate",
        disabled: locked,
        run: () => void host.duplicate(id),
      },
      { label: "Copy as YAML", run: () => void this.copyYaml() },
      { label: "Edit as YAML", disabled: locked, run: () => this.openYaml() },
      ...(this.httpActionDraft()
        ? [
            {
              label: "Edit action as YAML",
              disabled: locked,
              run: () => this.openActionYaml(),
            },
          ]
        : []),
      {
        label: "Delete",
        danger: true,
        disabled: locked,
        run: () => void host.remove(id),
      },
    ];
  }
  private async copyYaml() {
    const step = this.controller().step(this.target());
    if (!step) return;
    try {
      await navigator.clipboard.writeText(yamlText(step));
      this.host().notify(`Copied ${step.id} as YAML.`);
    } catch {
      this.host().notify(
        "Studio couldn't reach the clipboard. Use Edit as YAML and copy from there.",
      );
    }
  }
  openYaml() {
    const step = this.controller().step(this.target());
    if (!step || this.controller().readOnlyReason()) return;
    this.yamlText = yamlText(step);
    this.yamlError = "";
    this.yamlFor = "step";
    this.yamlOpen = true;
  }
  /** What Edit as YAML edits: the step, or the draft of its action's next version. */
  yamlFor: "step" | "action" = "step";
  /** The draft of an HTTP action's next version this step calls, kept on this computer. */
  httpActionDraft() {
    const step = this.controller().step(this.target());
    const draft = step
      ? this.controller().actionDraft(String(step["uses"] ?? ""))
      : null;
    const implementation = draft?.document.spec["implementation"] as
      | { kind?: unknown }
      | undefined;
    return draft && implementation?.kind === "http" ? draft : null;
  }
  yamlHeading(): string {
    const draft = this.yamlFor === "action" ? this.httpActionDraft() : null;
    return draft
      ? `Edit ${draft.name} ${draft.version} as YAML`
      : `Edit ${this.name()} as YAML`;
  }
  /** Edit action as YAML: the request of an HTTP action draft changes here. */
  openActionYaml() {
    const draft = this.httpActionDraft();
    if (!draft || this.controller().readOnlyReason()) return;
    this.yamlText = yamlText(draft.document);
    this.yamlError = "";
    this.yamlFor = "action";
    this.yamlOpen = true;
  }
  private async applyActionYaml() {
    const draft = this.httpActionDraft();
    if (!draft) return;
    let next: unknown;
    try {
      next = parseYaml(this.yamlText);
    } catch (error) {
      this.yamlError = `This isn't valid YAML: ${(error as Error).message}`;
      return;
    }
    const value = next as {
      kind?: unknown;
      metadata?: { name?: unknown };
    } | null;
    if (
      !value ||
      value.kind !== "Action" ||
      value.metadata?.name !== draft.name
    ) {
      this.yamlError = `Keep the action's kind and name: ${draft.name}.`;
      return;
    }
    const problem = await this.controller().saveActionDraft(
      this.target(),
      this.yamlText,
    );
    if (problem) {
      this.yamlError = problem;
      return;
    }
    this.yamlOpen = false;
    this.host().refreshView();
  }
  applyYaml() {
    if (this.yamlFor === "action") {
      void this.applyActionYaml();
      return;
    }
    const step = this.controller().step(this.target());
    if (!step) return;
    const locked = this.controller().readOnlyReason();
    if (locked) {
      this.yamlError = locked;
      return;
    }
    let next: unknown;
    try {
      next = parseYaml(this.yamlText);
    } catch (error) {
      this.yamlError = `This isn't valid YAML: ${(error as Error).message}`;
      return;
    }
    const value = next as { id?: unknown; kind?: unknown } | null;
    if (!value || value.id !== step.id || value.kind !== step.kind) {
      this.yamlError =
        "Keep the step's ID and kind. Rename the step with Rename.";
      return;
    }
    const host = this.host();
    host.error = "";
    host.perform(() => host.model.update(step.id, JSON.stringify(value)));
    if (host.error) {
      this.yamlError = host.error;
      host.error = "";
      return;
    }
    this.yamlOpen = false;
  }

  // -------------------------------------------------------------- rename
  private focusName() {
    afterNextRender(
      () =>
        this.dialog()
          .nativeElement.querySelector<HTMLElement>(".sd-name")
          ?.focus(),
      {
        injector: this.injector,
      },
    );
  }
  startRename() {
    if (this.pseudo() || this.controller().readOnlyReason()) return;
    this.renameText = this.target();
    this.renaming.set(this.target());
    afterNextRender(
      () =>
        this.dialog()
          .nativeElement.querySelector<HTMLInputElement>(".sd-rename")
          ?.select(),
      {
        injector: this.injector,
      },
    );
  }
  renameHintText(): string {
    return renameHint(
      this.renameText,
      this.target(),
      new Set(
        this.host()
          .model.nodes()
          .map((n) => n.step.id),
      ),
    );
  }
  renameKey(event: KeyboardEvent) {
    if (event.key === "Enter") {
      event.preventDefault();
      this.commitRename();
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      this.renaming.set(null);
      this.focusName();
    }
  }
  commitRename() {
    if (this.renaming() === null) return;
    const old = this.target();
    const text = this.renameText;
    this.renaming.set(null);
    if (!text.trim() || text === old) return this.focusName();
    const host = this.host();
    let result = null as RenameResult | null;
    host.perform(() => {
      result = renameWithExtras(
        host.model,
        old,
        text,
        reservedActionNames(
          host.catalogContracts,
          host.actionVersions,
          host.model.canvas,
        ),
      );
    });
    const renamed = result as RenameResult | null;
    if (!renamed || renamed.id === old) return this.focusName();
    this.renames.push({ from: old, to: renamed.id });
    this.controller().renamePending(old, renamed.id);
    const revision = host.model.revision;
    const count = renamed.references;
    host.notify(
      `Renamed to ${renamed.id}.${count ? ` ${count === 1 ? "1 reference" : `${count} references`} updated.` : ""}`,
      {
        label: "Undo",
        run: () =>
          void host.undoIfUnchanged(() => host.model.revision === revision),
      },
    );
    this.details.open({
      ...this.request(),
      target: renamed.id,
      focus: { kind: "region", region: "header" },
    });
  }

  // --------------------------------------------------------------- tabs
  /**
   * The Settings dot: a setting the step can change differs from its
   * default (retry counts while it is on), or the step has a note. What a
   * published action sets is not the step's change; a source error or a
   * running simulation, which lock every field, don't hide a change.
   */
  settingsChanged(): boolean {
    const session = this.session();
    const controller = this.controller();
    const step = controller.step(this.target());
    const kind = controller.kindContext();
    return settingsChanged(
      session
        .state("settings")
        .fields.filter((entry) => !(step && entry.spec.readOnly?.(step, kind)))
        .map((entry) =>
          entry.spec.id === "retry"
            ? !retryOn(this.fixedValue(session, entry.spec))
            : session.atDefault(entry.spec),
        ),
      stepNotesOf(this.host().model.canvas)[this.target()],
    );
  }
  private fixedValue(session: FormSession, spec: ParamSpec): unknown {
    const value = session.read(spec);
    return value.mode === "fixed" ? value.value : undefined;
  }
  selectTab(tab: StepDetailsTab) {
    this.chosenTab.set(tab);
  }
  /** The platform event trigger these step details show what it started of; null for any other step. */
  startsTrigger(): TriggerIntent | null {
    const choice = this.triggerChoice();
    return choice?.kind === "event" &&
      this.controller().features().includes("flow.eventTrigger")
      ? choice
      : null;
  }
  /** Open run: a run the trigger started, on the Runs tab in place of these step details. */
  openStartedRun(run: string) {
    this.guard(() => {
      const host = this.host();
      this.closeNow();
      handOverLinkedRun(host.draftId ?? "", run);
      void host.selectTab?.("Runs");
    });
  }
  /** What the platform says of the stop still owed, once Starts read it; null before. */
  readonly startsRead = signal<boolean | null>(null);
  /** Whether a stop is owed in this environment. */
  startsOwed(): boolean {
    const choice = this.startsTrigger();
    const environment = this.host().profile?.environmentId;
    if (!choice || !environment) return false;
    return this.startsRead() ?? pendingStopsOf(choice, environment).length > 0;
  }
  tabKey(event: KeyboardEvent) {
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey)
      return;
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = [
      ...(event.currentTarget as HTMLElement).querySelectorAll<HTMLElement>(
        '[role="tab"]',
      ),
    ];
    const at = tabs.indexOf(document.activeElement as HTMLElement);
    if (at < 0) return;
    event.preventDefault();
    const next =
      tabs[
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? tabs.length - 1
            : (at + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) %
              tabs.length
      ];
    next.click();
    next.focus();
  }

  // -------------------------------------------------------------- focus
  private recoverLayoutFocus() {
    const layout = this.layout();
    if (
      layout === this.renderedLayout ||
      document.querySelector(".modal-panel")
    )
      return;
    this.renderedLayout = layout;
    const root = this.dialog().nativeElement;
    const active = document.activeElement as HTMLElement | null;
    if (
      !this.lastInside ||
      (active &&
        (active.closest(".toast-region") ||
          (root.contains(active) && visible(active))))
    )
      return;
    // Hiding/removing a focused pane can silently reset browser focus to BODY.
    if (root.contains(this.lastInside) && visible(this.lastInside))
      this.lastInside.focus();
    else
      this.applyFocus({
        kind: "region",
        region: this.lastRegion ?? "parameters",
      });
  }
  applyFocus(focus: FocusTarget): void {
    if (focus.kind === "rename") {
      this.startRename();
      return;
    }
    if (
      focus.kind === "firstIssue" ||
      focus.kind === "path" ||
      focus.kind === "issue"
    ) {
      // The field of the issue, on its tab: Settings for a Settings field.
      const session = this.session();
      const reported =
        focus.kind === "path"
          ? fieldFor(
              focus.path,
              session
                .placedSpecs()
                .filter(
                  ({ spec }) =>
                    (spec.scope ?? "step") ===
                    (this.pseudo() ? "workflow" : "step"),
                ),
            )
          : focus.kind === "issue"
            ? (session.issues().find((issue) => issue.key === focus.key)
                ?.field ?? null)
            : null;
      const field = reported ?? firstIssueField(session.issues());
      if (field) return this.focusField(field);
    }
    const region = focus.kind === "region" ? focus.region : "parameters";
    if (this.layout() === "sheet") {
      const pane = region === "header" ? "parameters" : region;
      if (this.pane() !== pane) {
        this.pane.set(pane);
        afterNextRender(() => this.applyFocus(focus), {
          injector: this.injector,
        });
        return;
      }
    } else if (
      this.layout() === "two" &&
      (region === "input" || region === "output") &&
      this.dataPane() !== region
    ) {
      this.dataPane.set(region);
      afterNextRender(() => this.applyFocus(focus), {
        injector: this.injector,
      });
      return;
    }
    const root = this.dialog().nativeElement;
    const control = (scope: Element | null | undefined): HTMLElement | null => {
      if (!scope) return null;
      const fields = scope.matches("[data-param], .sd-tabpanel");
      // A field opens on its control, never on a label row's or a row's tools.
      const candidates = fields
        ? [...scope.querySelectorAll<HTMLElement>(".param-control")]
            .flatMap((field) => [
              ...field.querySelectorAll<HTMLElement>(CONTROL),
            ])
            .filter(
              (element) => !element.closest(".param-label-row, .param-tools"),
            )
        : [...scope.querySelectorAll<HTMLElement>(CONTROL)];
      return candidates.find(visible) ?? null;
    };
    const panel = root.querySelector(".sd-tabpanel:not([hidden])");
    let found: HTMLElement | null = null;
    if (focus.kind === "field") {
      if (this.tab() !== focus.tab) {
        this.chosenTab.set(focus.tab);
        afterNextRender(() => this.applyFocus(focus), {
          injector: this.injector,
        });
        return;
      }
      found = control(
        panel?.querySelector(`[data-param="${CSS.escape(focus.id)}"]`),
      );
    } else if (focus.kind === "firstRequiredEmpty")
      found = control(
        panel?.querySelector("[data-param][data-required-empty]"),
      );
    else if (focus.kind === "region") {
      const region = root.querySelector<HTMLElement>(
        `[data-region="${focus.region}"]`,
      );
      found =
        focus.region === "parameters"
          ? (control(panel) ?? control(root.querySelector(".sd-tabs")))
          : // A run's step opens on its Output pane itself, ringed whole.
            focus.region === "output" && this.controller().viewingRuns()
            ? region
            : (control(region) ?? region);
    }
    const name = root.querySelector<HTMLElement>(".sd-name:not([disabled])");
    found ??=
      control(panel?.querySelector("[data-param]")) ??
      (name && visible(name) ? name : null) ??
      control(root) ??
      root;
    // In the form, focus never scrolls on its own: the field's top comes
    // into view instead, so a field whose first control is further down (a
    // decision's paths) still opens at its start.
    const field =
      focus.kind === "field"
        ? panel?.querySelector<HTMLElement>(
            `[data-param="${CSS.escape(focus.id)}"]`,
          )
        : found.closest<HTMLElement>(".param-form > weave-param-field");
    if (field && panel?.contains(found)) {
      found.focus({ preventScroll: true });
      revealTop(field);
      // Focus never sits out of sight: when the control is further down, the
      // first control in view takes it, or else the step's name.
      if (!inView(found)) {
        const shown = [...panel.querySelectorAll<HTMLElement>(CONTROL)].find(
          (element) =>
            !element.closest(".param-label-row, .param-tools") &&
            visible(element) &&
            inView(element),
        );
        (shown ?? (name && visible(name) ? name : found)).focus({
          preventScroll: true,
        });
      }
    } else found.focus();
  }
  private trapTab(event: KeyboardEvent) {
    const root = this.dialog().nativeElement;
    const items = [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
      visible,
    );
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === root)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }
  focusIn(event: FocusEvent) {
    const target = event.target as HTMLElement | null;
    const root = this.dialog().nativeElement;
    if (!target) return;
    if (root.contains(target)) {
      if (!target.closest(".modal-panel")) {
        this.lastInside = target;
        this.lastRegion = this.regionOf(target);
      }
      return;
    }
    // Confirmations and toasts sit above step details; everything else is behind it.
    if (target.closest(".modal-panel, .toast-region")) return;
    (this.lastInside &&
    root.contains(this.lastInside) &&
    visible(this.lastInside)
      ? this.lastInside
      : root
    ).focus();
  }

  readonly mapMenu = signal<MapMenu | null>(null);
  private readonly mappingDrag = viewChild(DragMap);
  private clearMapping() {
    this.mapMenu.set(null);
    this.mappingDrag()?.end();
    activeDrag.set(null);
  }
  mappedRefs(): string[] {
    const subject = this.session().subject();
    if (this.target() === "$end")
      return mappedRefs(subject.workflow.spec["output"]);
    return [
      ...new Set(
        subject.roots.flatMap((root) => mappedRefs(getAt(subject.step, root))),
      ),
    ];
  }
  openMapTo(row: HTMLElement) {
    const ref = row.dataset["ref"];
    const session = this.session();
    const pane = this.inputPane();
    const source = pane
      ?.rows(pane.selectedSource())
      .find((entry) => entry.ref === ref);
    if (!source || !session.isCurrent()) return;
    const box = row.getBoundingClientRect();
    const label = source.label;
    const specs = session.mapTargets();
    const menu: MapMenu = {
      drag: { ref: ref!, breadcrumb: label, schema: {} },
      label,
      row,
      session,
      targets: specs.map((spec) => ({
        spec,
        owns: session.owns(spec),
        label:
          specs.filter((other) => other.label === spec.label).length > 1
            ? `${spec.label} · ${spec.path.slice(1).join(" › ")}`
            : spec.label,
      })),
      top: Math.max(8, Math.min(box.bottom + 4, window.innerHeight - 248)),
      left: Math.max(8, Math.min(box.left, window.innerWidth - 288)),
    };
    this.mapMenu.set(menu);
    setTimeout(() => {
      if (
        this.lifetime.destroyed ||
        this.mapMenu() !== menu ||
        !session.isCurrent()
      )
        return;
      const element =
        this.dialog().nativeElement.querySelector<HTMLElement>(".sd-map-menu");
      (
        element?.querySelector<HTMLElement>("[role=menuitem]") ?? element
      )?.focus();
    });
  }
  closeMapMenu() {
    const menu = this.mapMenu();
    this.mapMenu.set(null);
    if (menu?.session.isCurrent() && menu.row.isConnected) menu.row.focus();
  }
  mapTo(target: MapMenu["targets"][number]) {
    const menu = this.mapMenu();
    if (
      !menu ||
      !menu.targets.includes(target) ||
      !target.owns() ||
      menu.session !== this.session()
    )
      return this.clearMapping();
    this.mapMenu.set(null);
    const selector = `[data-param-path="${CSS.escape(JSON.stringify(target.spec.path))}"]`;
    const field =
      this.dialog().nativeElement.querySelector<HTMLElement>(selector);
    if (
      !field ||
      !canMapFields(field, menu.session) ||
      !menu.session.applyDrop(target.spec, menu.drag, { caret: null })
    ) {
      menu.row.focus();
      return;
    }
    this.chosenTab.set("parameters");
    if (this.layout() === "sheet") this.pane.set("parameters");
    setTimeout(() => {
      if (
        this.lifetime.destroyed ||
        !menu.session.isCurrent() ||
        this.session() !== menu.session
      )
        return;
      // A field can name the control to continue on (a condition's test).
      const field = `.sd-tabpanel:not([hidden]) ${selector}`;
      const root = this.dialog().nativeElement;
      const control =
        root.querySelector<HTMLElement>(`${field} [data-map-focus] input`) ??
        root.querySelector<HTMLElement>(
          `${field} .param-control :is(input, textarea, button, [tabindex='0'])`,
        );
      control?.focus({ preventScroll: true });
      control?.scrollIntoView({ block: "center", inline: "nearest" });
    });
  }
  mapMenuKey(event: KeyboardEvent) {
    const items = [
      ...(event.currentTarget as HTMLElement).querySelectorAll<HTMLElement>(
        "[role=menuitem]",
      ),
    ];
    const at = items.indexOf(document.activeElement as HTMLElement);
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      event.stopPropagation();
      const index =
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? items.length - 1
            : (at + (event.key === "ArrowDown" ? 1 : items.length - 1)) %
              items.length;
      items[index]?.focus();
    } else if (event.key === "Escape" || event.key === "Tab") {
      event.preventDefault();
      event.stopPropagation();
      this.closeMapMenu();
    }
  }

  // ---------------------------------------------------------------- keys
  key(event: KeyboardEvent) {
    if (event.defaultPrevented) return;
    if ((event.target as HTMLElement).closest(".modal-panel")) return;
    if (event.key === "Tab") return this.trapTab(event);
    if (event.key === "Escape") return this.escape(event);
    const target = event.target as HTMLElement;
    // The most specific surface first: the decision grid binds no
    // Ctrl/Cmd+Enter, so executing never starts from it.
    const surfaces = target.closest("weave-decision-grid")
      ? (["decisionGrid", "stepDetails"] as const)
      : target.closest('[data-mode="fixed"]')
        ? (["fixedField", "stepDetails"] as const)
        : (["stepDetails"] as const);
    const command = commandFor(eventCombo(event, KEY_PLATFORM), surfaces, {
      platform: KEY_PLATFORM,
      typing: isTyping(target),
    });
    if (command) this.run(command, event);
  }
  /** What a key does in step details; later features add their commands here. */
  run(command: KeymapCommand, event: KeyboardEvent) {
    const host = this.host();
    switch (command) {
      case "mapTo": {
        const row = (event.target as HTMLElement).closest<HTMLElement>(
          ".sd-input .data-row[data-ref]",
        );
        if (!row) return;
        event.preventDefault();
        return this.openMapTo(row);
      }
      case "searchPane": {
        const region = this.regionOf(event.target as HTMLElement);
        if (region === "input" && this.showPane("input") && this.inputPane()) {
          event.preventDefault();
          this.inputPane()!.focusSearch();
        } else if (region === "output" && this.showPane("output")) {
          event.preventDefault();
          this.outputPane()?.focusSearch();
        }
        return;
      }
      case "previousStepDetails":
        event.preventDefault();
        return this.move(-1);
      case "nextStepDetails":
        event.preventDefault();
        return this.move(1);
      case "renameStep":
        if ((event.target as HTMLElement).closest('[data-region="header"]')) {
          event.preventDefault();
          this.startRename();
        }
        return;
      case "undo":
        event.preventDefault();
        return host.undo();
      case "redo":
        event.preventDefault();
        return host.redo();
      case "save":
        event.preventDefault();
        void host.runCommand(host.profile ? "save" : "export");
        return;
      case "nextRegion":
        event.preventDefault();
        return this.cycleRegion(1);
      case "previousRegion":
        event.preventDefault();
        return this.cycleRegion(-1);
      case "executeStep":
        // Also while typing: Ctrl/Cmd+Enter never types anything.
        event.preventDefault();
        if (this.canExecute()) void this.execute("step");
        return;
      case "showShortcuts":
        if (isTyping(event.target as HTMLElement)) return;
        event.preventDefault();
        this.sheet.set(stepDetailsSheet(KEY_PLATFORM));
        return;
      case "togglePin": {
        const target = this.session().target;
        const controller = this.controller();
        if (isTyping(event.target as HTMLElement)) return;
        if (target.startsWith("$") || !controller.pinnable(target)) return;
        event.preventDefault();
        if (controller.pinOfTarget(target)) controller.unpin(target);
        else host.notify("Set test output or generate a sample to pin it.");
        return;
      }
      default:
        return;
    }
  }
  private escape(event: KeyboardEvent) {
    event.preventDefault();
    if (this.mapMenu()) return this.closeMapMenu();
    if (activeDrag()) return this.clearMapping();
    if (this.renaming() !== null) {
      this.renaming.set(null);
      return this.focusName();
    }
    const target = event.target as HTMLElement;
    if (isTyping(target)) {
      target.blur();
      this.dialog().nativeElement.focus({ preventScroll: true });
      return;
    }
    this.close();
  }
}

/** An instance key's loop index ("send[2]" is 2); 0 without one. */
function indexOf(key: string): number {
  const match = /\[(\d+)\]$/.exec(key);
  return match ? Number(match[1]) : 0;
}
