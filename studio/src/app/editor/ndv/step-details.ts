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
// on the dimmed editor — there is nothing to save: every edit is already in
// the workflow as an undo step.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  ViewEncapsulation,
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
import { Modal } from "../../dialog";
import {
  KEY_STEP,
  readWidths,
  resized,
  toEdge,
  writeWidths,
  defaultWidths,
  type PaneWidths,
} from "./panes/layout";
import { Icon } from "../../icon";
import { RowMenu, type RowMenuItem } from "../../row-menu";
import {
  commandFor,
  eventCombo,
  formatCombo,
  type KeymapCommand,
  type KeyPlatform,
} from "../state/keymap";
import { docsUrl, headerSubtitle } from "./header";
import { adjacent, breadcrumb, neighbors } from "./navigation";
import { recipeOf } from "./owned/owned-actions";
import { ndvRegistry } from "./registry";
import { renameHint, renameWithExtras, type RenameResult } from "./rename";
import type { StepDetailsController } from "./step-details-controller";
import type { StepDetailsHost } from "./step-details-host";
import {
  StepDetailsService,
  type FocusTarget,
  type Region,
  type StepDetailsRequest,
  type StepDetailsTab,
} from "./step-details-service";

export const KEY_PLATFORM: KeyPlatform =
  typeof navigator !== "undefined" &&
  /Mac|iPhone|iPad|iPod/i.test(navigator.platform)
    ? "mac"
    : "other";
export const EXECUTE_REASON =
  "Executing a single step isn't available yet. Simulate the workflow runs every step.";

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const CONTROL =
  'input:not([type="hidden"]):not([disabled]), textarea:not([disabled]), select:not([disabled]), [role="combobox"], [role="switch"], [role="radio"][tabindex="0"], button:not([disabled])';
const NAMES: Record<string, string> = {
  $trigger: "Manual form trigger",
  $end: "End",
};
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
  imports: [Icon, RowMenu, Modal],
  templateUrl: "./step-details.html",
  styleUrl: "./step-details.css",
  host: { "(document:focusin)": "focusIn($event)" },
})
export class StepDetails {
  host = input.required<StepDetailsHost>();
  request = input.required<StepDetailsRequest>();
  controller = input.required<StepDetailsController>();
  closed = output<string>();

  private readonly details = inject(StepDetailsService);
  private readonly injector = inject(Injector);
  private readonly dialog =
    viewChild.required<ElementRef<HTMLElement>>("dialog");
  readonly tab = signal<StepDetailsTab>("parameters");
  readonly renaming = signal<string | null>(null);
  renameText = "";
  private readonly renames: { from: string; to: string }[] = [];
  private lastInside: HTMLElement | null = null;
  readonly titles = {
    previous: `Previous step (${formatCombo("Mod+Alt+Shift+ArrowLeft", KEY_PLATFORM)})`,
    next: `Next step (${formatCombo("Mod+Alt+Shift+ArrowRight", KEY_PLATFORM)})`,
  };

  readonly pane = signal<"input" | "parameters" | "output">("parameters");
  readonly dataPane = signal<"input" | "output">("input");
  readonly widths = signal<PaneWidths>({ input: 0, output: 0 });
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
  readonly executeReason = EXECUTE_REASON;

  constructor() {
    effect(() => {
      this.details.opening();
      const request = this.request();
      untracked(() => {
        this.tab.set(request.tab);
        this.renaming.set(null);
        afterNextRender(() => this.applyFocus(request.focus), {
          injector: this.injector,
        });
      });
    });
    effect(() => {
      this.host().tick();
      untracked(() => this.keepTarget());
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
      this.dragEnd();
      observer.disconnect();
    });
  }

  // ---------------------------------------------------------------- what
  target(): string {
    return this.request().target;
  }
  pseudo(): boolean {
    return this.target().startsWith("$");
  }
  name(): string {
    return NAMES[this.target()] ?? this.target();
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
  subtitle(): string {
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
    const active = document.activeElement as HTMLElement | null;
    const region = active
      ?.closest("[data-region]")
      ?.getAttribute("data-region");
    if (!region && active?.closest(".sd-segments")) return this.pane();
    if (!region && active?.closest(".sd-data-tabs")) return this.dataPane();
    return (region as Region | null) ?? null;
  }
  go(id: string) {
    const region = this.currentRegion() ?? "parameters";
    if (!id.startsWith("$")) this.host().model.selected = id;
    this.details.open({
      ...this.request(),
      target: id,
      tab: id === "$end" ? "parameters" : this.tab(),
      focus: { kind: "region", region },
      fresh: false,
      revealAll: false,
    });
  }
  move(delta: 1 | -1) {
    const id = delta > 0 ? this.next() : this.previous();
    if (id) this.go(id);
  }
  close() {
    this.closed.emit(this.target());
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
      this.pane.set(next === "header" ? "parameters" : next);
    if (this.layout() === "two" && (next === "input" || next === "output"))
      this.dataPane.set(next);
    afterNextRender(() => this.applyFocus({ kind: "region", region: next }), {
      injector: this.injector,
    });
  }

  // ------------------------------------------------------- execute step
  executeLabel(): string {
    return this.target() === "$trigger" ? "Execute workflow" : "Execute step";
  }
  canExecute(): boolean {
    return this.target() !== "$end" && !this.controller().readOnlyReason();
  }
  explainExecute() {
    this.host().notify(EXECUTE_REASON);
  }
  executeItems(): RowMenuItem[] {
    const blocked = this.host().blocker("simulate");
    return [
      {
        label: "Simulate the workflow",
        detail:
          blocked ||
          "Runs every step with test data. No real systems are called.",
        disabled: !!blocked,
        run: () => {
          const host = this.host();
          this.close();
          void host.runCommand("simulate");
        },
      },
    ];
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
      result = renameWithExtras(host.model, old, text);
    });
    const renamed = result as RenameResult | null;
    if (!renamed || renamed.id === old) return this.focusName();
    this.renames.push({ from: old, to: renamed.id });
    const revision = host.model.revision;
    const count = renamed.references;
    host.notify(
      `Renamed to ${renamed.id}.${count ? ` ${count === 1 ? "1 reference" : `${count} references`} updated.` : ""}`,
      {
        label: "Undo",
        run: () => {
          if (host.model.revision === revision) host.undo();
        },
      },
    );
    this.details.open({
      ...this.request(),
      target: renamed.id,
      focus: { kind: "region", region: "header" },
    });
  }

  // --------------------------------------------------------------- tabs
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
  applyFocus(focus: FocusTarget) {
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
    const control = (scope: Element | null | undefined): HTMLElement | null =>
      scope
        ? ([...scope.querySelectorAll<HTMLElement>(CONTROL)].find(visible) ??
          null)
        : null;
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
    found.focus();
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
      this.lastInside = target;
      return;
    }
    // Confirmations and toasts sit above step details; everything else is behind it.
    if (target.closest(".modal-panel, .toast-region")) return;
    (this.lastInside && root.contains(this.lastInside)
      ? this.lastInside
      : root
    ).focus();
  }

  // ---------------------------------------------------------------- keys
  key(event: KeyboardEvent) {
    if (event.defaultPrevented) return;
    if ((event.target as HTMLElement).closest(".modal-panel")) return;
    if (event.key === "Tab") return this.trapTab(event);
    if (event.key === "Escape") return this.escape(event);
    const target = event.target as HTMLElement;
    const surfaces = target.closest('[data-mode="fixed"]')
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
        event.preventDefault();
        if (this.canExecute()) this.explainExecute();
        return;
      default:
        return;
    }
  }
  private escape(event: KeyboardEvent) {
    event.preventDefault();
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
