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
  KEY_STEP,
  readWidths,
  resized,
  toEdge,
  writeWidths,
  defaultWidths,
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
import { executeGate } from "../state/execute-gate";
import { sliceStepIds } from "../state/execute-request";
import type { ExecuteTarget } from "../state/execution";
import type { ExecuteOutcome } from "../state/execution-service";
import { allSteps } from "./step-walk";
import { stepDetailsSheet } from "../state/canvas-commands";
import type { SheetSection } from "../state/keymap";
import { TestEventPane } from "./panes/test-event-pane";
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
} from "./registry";
import { renameHint, renameWithExtras, type RenameResult } from "./rename";
import { reservedActionNames } from "./owned/insert-owned";
import { SettingsTab } from "./settings/settings-tab";
import { retryOn, settingsChanged } from "./settings/settings-rules";
import { stepNotesOf } from "../state/canvas-sidecar";
import type {
  ExecuteNotice,
  StepDetailsController,
} from "./step-details-controller";
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
    CloseGuard,
    ExecuteDialog,
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
  readonly tab = signal<StepDetailsTab>("parameters");
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
  /** A folded pane opened over the wide layout's edge; null while every pane is folded. */
  readonly rail = signal<"input" | "output" | "data" | null>(null);
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
  /** True while a wide component (the decision table grid) is open: the data panes fold to rails. */
  wide(): boolean {
    return !!this.component()?.wide && this.layout() !== "sheet";
  }
  /** Where the open component is drawn, in place of the form. */
  private readonly componentHost = viewChild("componentHost", {
    read: ViewContainerRef,
  });
  private componentRef: ComponentRef<ParameterComponent> | null = null;
  /** What the open component reads and edits. */
  componentContext(): NdvContext {
    return this.controller().ndvContext(this.target());
  }
  toggleRail(which: "input" | "output" | "data") {
    this.rail.set(this.rail() === which ? null : which);
  }
  /** A click into the grid closes a pane opened over its edge. */
  mainPointer() {
    if (this.rail()) this.rail.set(null);
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
        this.tab.set(request.tab);
        this.renaming.set(null);
        // Opening a step shows its form; the grid opens from Edit table.
        this.controller().parameterComponent.set(null);
        afterNextRender(() => this.applyFocus(request.focus), {
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
      });
    });
    // A registered component asked for: load its chunk once.
    effect(() => {
      const open = this.controller().parameterComponent();
      if (!open) {
        untracked(() => this.rail.set(null));
        return;
      }
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
      this.widths.set(readWidths(this.total));
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
    return targetName(this.target());
  }
  layout(): "three" | "two" | "sheet" {
    const width = this.host().windowWidth;
    return width >= 1100 ? "three" : width >= 768 ? "two" : "sheet";
  }
  private iconOf(id: string): string {
    if (id === "$trigger") return "manualTrigger";
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
    if (this.target() === "$end") return "End · what the workflow returns";
    const step = this.controller().step(this.target());
    return step
      ? headerSubtitle(
          step,
          this.controller().kindContext(),
          this.host().label(step.kind),
        )
      : "";
  }
  crumb(): string {
    return this.pseudo()
      ? ""
      : breadcrumb(this.host().model.definition, this.target());
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
    if (this.target() === "$trigger") return this.firstStep();
    return this.pseudo()
      ? null
      : adjacent(this.host().model.definition, this.target(), 1);
  }
  neighborList(): { before: string[]; after: string[] } {
    if (this.target() === "$trigger")
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
    return id === "$trigger" ? "trigger" : id;
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
    const w = this.widths();
    return w.input
      ? { "--sd-input": `${w.input}px`, "--sd-output": `${w.output}px` }
      : {};
  }
  showPane(which: "input" | "parameters" | "output"): boolean {
    const layout = this.layout();
    if (this.wide() && which !== "parameters") {
      const rail = this.rail();
      return layout === "three"
        ? rail === which
        : rail === "data" && which === this.dataPane();
    }
    if (layout === "three") return true;
    if (layout === "two")
      return which === "parameters" || which === this.dataPane();
    return which === this.pane();
  }
  maxOf(side: keyof PaneWidths): number {
    return toEdge(this.widths(), side, "max", this.total)[side];
  }
  private setWidths(next: PaneWidths) {
    this.widths.set(next);
    writeWidths(next, this.total);
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
    this.widths.set(resized(drag.start, drag.side, delta, this.total));
  }
  dragEnd(event?: PointerEvent) {
    const drag = this.dragging;
    if (!drag || (event && drag.pointer !== event.pointerId)) return;
    this.dragging = null;
    if (drag.handle.hasPointerCapture(drag.pointer))
      drag.handle.releasePointerCapture(drag.pointer);
    writeWidths(this.widths(), this.total);
  }
  separatorKey(event: KeyboardEvent, side: keyof PaneWidths) {
    if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey)
      return;
    const grow = side === "input" ? "ArrowRight" : "ArrowLeft";
    const shrink = side === "input" ? "ArrowLeft" : "ArrowRight";
    let next: PaneWidths | null = null;
    if (event.key === grow)
      next = resized(this.widths(), side, KEY_STEP, this.total);
    else if (event.key === shrink)
      next = resized(this.widths(), side, -KEY_STEP, this.total);
    else if (event.key === "Home")
      next = toEdge(this.widths(), side, "min", this.total);
    else if (event.key === "End")
      next = toEdge(this.widths(), side, "max", this.total);
    if (!next) return;
    event.preventDefault();
    this.setWidths(next);
  }
  resetWidths() {
    this.setWidths(defaultWidths(this.total));
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
    return this.target() === "$trigger" ? "Execute workflow" : "Execute step";
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
    return !!this.host().execution?.running();
  }
  executeItems(): RowMenuItem[] {
    if (this.target() === "$trigger") return [];
    return [
      {
        label: "Execute previous steps",
        detail:
          "Runs from the trigger up to this step, reusing pinned and current results",
        run: () => void this.execute("before"),
      },
    ];
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
    const target: ExecuteTarget =
      id === "$trigger" ? { mode: "workflow" } : { mode, stepId: id };
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
      mode === "step" && target.mode === "step" && !!controller.pinOfTarget(id),
    );
    if (gate.kind === "fix") {
      this.question.set({ ...gate, issues: gate.issues as ExecuteIssue[] });
      return;
    }
    if (gate.kind === "unpin") {
      this.question.set({ ...gate, stepId: id });
      return;
    }
    await this.executeNow(target);
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
  /** Stop: the running execution's answer is dropped. */
  stop() {
    this.host().execution?.stop();
    this.say("Stopped executing.");
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
      this.tab.set(field.tab);
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
    this.yamlOpen = true;
  }
  applyYaml() {
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
    this.tab.set(tab);
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
        this.tab.set(focus.tab);
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
    this.tab.set("parameters");
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
    const rail = this.rail();
    if (rail) {
      this.rail.set(null);
      afterNextRender(
        () =>
          this.dialog()
            .nativeElement.querySelector<HTMLElement>(`.sd-rail-${rail}`)
            ?.focus(),
        { injector: this.injector },
      );
      return;
    }
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
