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
// The Add a step panel: 384 px on the right (a full sheet below 768 px),
// starting with "What triggers this workflow?" or "What happens next?". It
// searches steps, apps and actions, opens categories and apps with →, goes
// back with ←, and inserts with Enter or a click. Plain step kinds also
// drag onto a "+".
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  inject,
  input,
  type DoCheck,
  type OnInit,
} from "@angular/core";
import { Icon } from "../../icon";
import { catalogItems } from "../../integrations/catalog-picker";
import { ModalSheet, sheetWhen } from "../../modal-sheet";
import type { Kind } from "../../model";
import { memo } from "../memo";
import { loadKindRegistrations } from "../ndv/kinds";
import { ndvRegistry } from "../ndv/registry";
import { triggerInputs, type TriggerIntent } from "../state/canvas-sidecar";
import { keyPlatform } from "../state/canvas-commands";
import { commandFor, eventCombo } from "../state/keymap";
import type { AddStepHost, AddStepRequest } from "./add-step-host";
import { loadConnectors, loadLanguage } from "./catalog-sources";
import {
  buildCatalog,
  type Catalog,
  type CatalogState,
  type ConnectorInfo,
  type LanguageTarget,
  type PanelEntry,
} from "./entries";
import { GONE, placeExists } from "./insert";
import {
  reduce,
  shown,
  startState,
  type PanelAction,
  type PanelState,
  type Shown,
} from "./panel-model";

let sequence = 0;

@Component({
  selector: "weave-add-step-panel",
  standalone: true,
  imports: [Icon, ModalSheet],
  changeDetection: ChangeDetectionStrategy.Eager,
  templateUrl: "./add-step-panel.html",
  styleUrl: "./add-step.css",
  host: { "(document:pointerdown)": "outside($event)" },
})
export class AddStepPanel implements OnInit, DoCheck {
  /** The editor: it owns the workflow and inserts. */
  host = input.required<AddStepHost>();
  private readonly cdr = inject(ChangeDetectorRef);
  private readonly element: HTMLElement = inject(ElementRef).nativeElement;
  private readonly injector = inject(Injector);
  readonly platform = keyPlatform();
  readonly sheetWhen = sheetWhen;
  readonly prefix = `add-step-${++sequence}`;
  /** The step kinds are registered: entries can use their names. */
  ready = false;
  language: LanguageTarget | null = null;
  connectors: readonly ConnectorInfo[] | null = null;
  state: PanelState = startState("next");
  private request: AddStepRequest | null = null;
  private busy = false;
  /** The published actions' contracts were asked for. */
  private warmed = false;
  private readonly optionIds = new Map<string, string>();

  ngOnInit() {
    // A lost chunk still lists entries, with Studio's own names; the failure is logged.
    loadKindRegistrations()
      .catch((error: unknown) =>
        console.error("Could not load the step kinds.", error),
      )
      .finally(() => {
        this.ready = true;
        this.replayEarlyKeys();
        this.cdr.markForCheck();
      });
    const h = this.host();
    void loadLanguage(h.api, !!h.profile).then((target) => {
      this.language = target;
      this.cdr.markForCheck();
    });
    if (h.profile)
      void loadConnectors(h.api).then((connectors) => {
        this.connectors = connectors;
        this.cdr.markForCheck();
      });
  }

  ngDoCheck() {
    const h = this.host();
    const request = h.addStep;
    if (!request) return;
    if (!this.warmed && h.profile && h.catalogState === "ready") {
      this.warmed = true;
      h.warmActionContracts();
    }
    if (request !== this.request) {
      this.request = request;
      this.state = startState(request.start);
      this.focusSearch();
    }
    if (
      h.model.opened !== request.opened ||
      h.editingLocked ||
      h.model.readonly
    )
      return this.close(false);
    if (!this.busy && !placeExists(h.model, request.insert)) {
      h.notify(GONE);
      this.close(true);
    }
  }

  // ------------------------------------------------------------ catalog

  private readonly catalogMemo = memo(
    (
      ready: boolean,
      language: LanguageTarget | null,
      rows: Record<string, unknown>[],
      contracts: ReadonlyMap<string, Record<string, unknown>>,
      state: CatalogState,
      more: boolean,
      connectors: readonly ConnectorInfo[] | null,
      triggers: readonly TriggerIntent[] | undefined,
      hasSteps: boolean,
      canCreate: boolean,
    ): Catalog =>
      buildCatalog({
        kinds: ready
          ? ndvRegistry.kinds().map((kind) => ({
              kind: kind.kind,
              label: kind.label,
              description: kind.description,
              keywords: kind.keywords,
              icon: kind.icon,
            }))
          : [],
        language,
        actions: catalogItems(rows, contracts),
        catalog: state,
        moreActions: more,
        connectors,
        triggers: triggers ?? [],
        hasSteps,
        canCreateActions: canCreate,
      }),
  );
  catalog(): Catalog {
    const h = this.host();
    return this.catalogMemo(
      this.ready,
      this.language,
      h.actionVersions,
      h.catalogContracts,
      this.catalogState(),
      !!h.actionNextCursor,
      this.connectors,
      h.model.canvas.triggers,
      h.model.definition.spec.steps.length > 0,
      !h.profile || h.can("definition.publish"),
    );
  }
  private catalogState(): CatalogState {
    const h = this.host();
    if (!h.profile) return "offline";
    if (!h.identity) return "loading";
    if (!h.can("catalog.read")) return "forbidden";
    if (h.catalogState === "error") return "error";
    return h.catalogState === "ready" ? "ready" : "loading";
  }
  /** Triggers come first in search while the workflow has no trigger at all. */
  private triggersFirst(): boolean {
    const h = this.host();
    return !triggerInputs(
      h.model.canvas,
      h.model.definition.spec.steps.length > 0,
    ).length;
  }
  private readonly shownMemo = memo(
    (state: PanelState, catalog: Catalog, triggersFirst: boolean): Shown =>
      shown(state, catalog, triggersFirst),
  );
  view(): Shown {
    return this.shownMemo(this.state, this.catalog(), this.triggersFirst());
  }

  // ------------------------------------------------------------ template

  optionId(entry: PanelEntry): string {
    let id = this.optionIds.get(entry.id);
    if (!id) {
      id = `${this.prefix}-option-${this.optionIds.size}`;
      this.optionIds.set(entry.id, id);
    }
    return id;
  }
  activeId(view: Shown): string | null {
    const entry = view.options[this.state.active];
    return entry ? this.optionId(entry) : null;
  }
  searchLabel(): string {
    return this.state.stack[0] === "triggers"
      ? "Search triggers, steps and apps"
      : "Search steps, apps and actions";
  }
  countText(view: Shown): string {
    if (!view.searching) return "";
    const count = view.options.length;
    return count === 1 ? "1 result" : `${count} results`;
  }
  /** Where a step goes, read after the title: "Add a step after check". */
  place(): string {
    return this.host().addStep?.label ?? "";
  }

  // ------------------------------------------------------------ actions

  type(event: Event) {
    this.apply({
      type: "type",
      query: (event.target as HTMLInputElement).value,
    });
  }
  hover(index: number) {
    if (index !== this.state.active) this.apply({ type: "hover", index });
  }
  pick(index: number) {
    this.apply({ type: "hover", index });
    this.apply({ type: "enter" });
  }
  back() {
    this.apply({ type: "back" });
    this.focusSearch();
  }
  /** A press on an entry keeps focus in the search, except where it starts a drag. */
  press(event: MouseEvent, entry: PanelEntry) {
    if (!entry.drag) event.preventDefault();
  }
  key(event: KeyboardEvent) {
    const target = event.target as HTMLElement;
    const search = this.searchInput();
    const inSearch = !!search && target === search;
    const command = commandFor(eventCombo(event, this.platform), "addStep", {
      platform: this.platform,
    });
    switch (command) {
      case "panelUp":
      case "panelDown":
        return this.apply(
          { type: "move", delta: command === "panelDown" ? 1 : -1 },
          event,
        );
      case "panelInsert":
        if (target.closest("button")) return;
        return this.apply({ type: "enter" }, event);
      case "panelOpen":
        if (inSearch && search!.selectionStart !== search!.value.length) return;
        return this.apply({ type: "open" }, event);
      case "panelBack":
        if (inSearch && (search!.selectionStart ?? 0) > 0) return;
        return this.apply({ type: "back" }, event);
      case "undo":
      case "redo":
        // Typing in the search keeps its own undo; elsewhere it undoes the workflow.
        if (inSearch) return;
        event.preventDefault();
        event.stopPropagation();
        return command === "undo" ? this.host().undo() : this.host().redo();
      case "escape":
        event.preventDefault();
        event.stopPropagation();
        return this.close(true);
    }
    // Typing anywhere in the panel types into the search.
    if (
      !inSearch &&
      search &&
      event.key.length === 1 &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey
    )
      search.focus();
  }
  private apply(action: PanelAction, event?: Event) {
    const result = reduce(
      this.state,
      action,
      this.catalog(),
      this.triggersFirst(),
    );
    if (!result.handled) return;
    event?.preventDefault();
    this.state = result.state;
    if (result.effect.kind === "choose") void this.choose(result.effect.entry);
    this.scrollActive();
    this.cdr.markForCheck();
  }
  private async choose(entry: PanelEntry) {
    const h = this.host();
    const request = h.addStep;
    if (!request || this.busy) return;
    this.busy = true;
    try {
      const outcome = await h.insertFromPanel(entry, {
        at: request.insert,
        opened: request.opened,
      });
      if (outcome === "done") this.close(false);
      else if (outcome === "gone") this.close(true);
    } finally {
      this.busy = false;
      this.cdr.markForCheck();
    }
  }
  dragStart(event: DragEvent, entry: PanelEntry) {
    if (!entry.drag || entry.disabled) {
      event.preventDefault();
      return;
    }
    this.host().dragPalette(event, entry.drag as Kind);
  }
  dragEnd(event: DragEvent) {
    const h = this.host();
    h.dragPreview = null;
    if (event.dataTransfer && event.dataTransfer.dropEffect !== "none")
      this.close(false);
  }
  /** A press outside closes the panel, except on a "+" (it reopens there), a menu, a toast or a dialog. */
  outside(event: PointerEvent) {
    const target = event.target as Element | null;
    if (!target || this.element.contains(target)) return;
    if (
      target.closest?.(
        '[data-action="insert"], .row-menu-list, .modal-panel, .sheet-scrim, .toast-region, weave-dialog-host',
      )
    )
      return;
    const anchor = this.host().addStep?.anchor;
    if (anchor instanceof HTMLElement && anchor.contains(target)) return;
    this.close(false);
  }
  close(returnFocus: boolean) {
    const h = this.host();
    if (!h.addStep) return;
    const anchor = h.addStep.anchor;
    this.request = null;
    h.closePicker();
    if (!returnFocus) return;
    queueMicrotask(() => {
      if (anchor instanceof HTMLElement && anchor.isConnected) anchor.focus();
      else document.querySelector<HTMLElement>(".canvas-v2")?.focus();
    });
  }
  private searchInput(): HTMLInputElement | null {
    return this.element.querySelector<HTMLInputElement>(
      ".add-step-search input",
    );
  }
  private focusSearch() {
    afterNextRender(
      () => {
        this.searchInput()?.focus();
        this.replayEarlyKeys();
      },
      { injector: this.injector },
    );
  }
  /**
   * Keys pressed before the search had focus, replayed into it once the step
   * kinds are known (so a typed name finds them): characters and Backspace
   * edit the search, the others act as pressed there.
   */
  private replayEarlyKeys() {
    const search = this.searchInput();
    if (!this.ready || !search || !this.host().addStep) return;
    for (const key of this.host().takeEarlyKeys()) {
      if (!this.host().addStep) return;
      if (key.key.length === 1 || key.key === "Backspace") {
        search.value =
          key.key === "Backspace"
            ? search.value.slice(0, -1)
            : search.value + key.key;
        this.apply({ type: "type", query: search.value });
      } else
        search.dispatchEvent(
          new KeyboardEvent("keydown", {
            key: key.key,
            code: key.code,
            shiftKey: key.shiftKey,
            bubbles: true,
            cancelable: true,
          }),
        );
    }
  }
  private scrollActive() {
    queueMicrotask(() => {
      const id = this.activeId(this.view());
      if (id) document.getElementById(id)?.scrollIntoView({ block: "nearest" });
    });
  }
}
