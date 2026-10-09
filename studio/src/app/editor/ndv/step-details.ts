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
  imports: [Icon, RowMenu],
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
    const region = (document.activeElement as HTMLElement | null)
      ?.closest("[data-region]")
      ?.getAttribute("data-region");
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
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const tabs = [
      ...this.dialog().nativeElement.querySelectorAll<HTMLElement>(
        '.sd-tabs [role="tab"]',
      ),
    ];
    const at = tabs.indexOf(document.activeElement as HTMLElement);
    if (at < 0) return;
    event.preventDefault();
    const next =
      tabs[
        (at + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length
      ];
    next.click();
    next.focus();
  }

  // -------------------------------------------------------------- focus
  applyFocus(focus: FocusTarget) {
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
    else if (focus.kind === "region")
      found = control(root.querySelector(`[data-region="${focus.region}"]`));
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
