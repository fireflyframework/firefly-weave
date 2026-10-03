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
// Popover that inserts a step at a canvas "+" target. It renders with fixed
// positioning, so the host must place it outside f-flow and any transformed
// ancestor; the canvas zoom and pan then cannot clip or scale it.
import {
  AfterViewInit,
  Component,
  ElementRef,
  OnDestroy,
  computed,
  inject,
  input,
  output,
  signal,
} from "@angular/core";
import { NgTemplateOutlet } from "@angular/common";
import { Icon } from "../icon";
import { kinds as allKinds, type Kind } from "../model";
import {
  stepKindDescriptions,
  stepKindKeywords,
  stepKindLabels,
} from "./step-kinds";

export {
  stepKindDescriptions,
  stepKindGroups,
  stepKindKeywords,
  stepKindLabels,
} from "./step-kinds";

/** A published action the picker can insert directly. */
export interface PickerAction {
  name: string;
  version: string;
  description?: string;
  /** Connector or worker that runs the action, shown as secondary text. */
  group?: string;
}
/** What the person chose: a step kind, plus the action reference for actions. */
export interface StepPickerChoice {
  kind: Kind;
  uses?: string;
}
export interface PickerOption {
  id: string;
  kind: Kind;
  uses?: string;
  label: string;
  description: string;
  section: "steps" | "actions";
}
export interface AnchorRect {
  left: number;
  top: number;
  width: number;
  height: number;
}
export interface Size {
  width: number;
  height: number;
}
/**
 * Viewport position for a fixed popover. Below the anchor it is pinned by its
 * top edge, above the anchor by its bottom edge, so filtering that shrinks the
 * list keeps it attached to the anchor.
 */
export interface PopoverPlacement {
  left: number;
  top?: number;
  bottom?: number;
  maxHeight: number;
  side: "below" | "above";
}
export type PickerDismissReason = "escape" | "outside" | "tab";

/** Most actions listed at once; the search narrows the rest. */
export const ACTION_LIMIT = 50;

const words = (query: string) =>
  query.toLowerCase().split(/\s+/).filter(Boolean);
const matches = (terms: string[], ...fields: (string | undefined)[]) => {
  const text = fields.join(" ").toLowerCase();
  return terms.every((term) => text.includes(term));
};

/**
 * Filters step kinds and published actions by every word of the query,
 * matching names, descriptions, kinds and action references.
 */
export function pickerOptions(
  query: string,
  kinds: readonly Kind[],
  actions: readonly PickerAction[],
  limit = ACTION_LIMIT,
): { steps: PickerOption[]; actions: PickerOption[]; hiddenActions: number } {
  const terms = words(query);
  const steps = kinds
    .filter((kind) =>
      matches(
        terms,
        stepKindLabels[kind],
        stepKindDescriptions[kind],
        stepKindKeywords[kind],
        kind,
      ),
    )
    .map(
      (kind): PickerOption => ({
        id: `kind:${kind}`,
        kind,
        label: stepKindLabels[kind],
        description: stepKindDescriptions[kind],
        section: "steps",
      }),
    );
  const found = actions.filter((action) =>
    matches(
      terms,
      action.name,
      `${action.name}@${action.version}`,
      action.description,
      action.group,
    ),
  );
  return {
    steps,
    actions: found.slice(0, limit).map((action): PickerOption => {
      const uses = `${action.name}@${action.version}`;
      return {
        id: `action:${uses}`,
        kind: "action",
        uses,
        label: action.name,
        description: [
          `Version ${action.version}`,
          action.group,
          action.description,
        ]
          .filter(Boolean)
          .join(" · "),
        section: "actions",
      };
    }),
    hiddenActions: Math.max(0, found.length - limit),
  };
}

/**
 * Places a popover next to an anchor inside the viewport. It opens below the
 * anchor unless more room is available above, aligns with the anchor's left
 * edge and is clamped to the viewport margins. All values are viewport pixels.
 */
export function placePopover(
  anchor: AnchorRect,
  size: Size,
  viewport: Size,
  gap = 6,
  margin = 8,
): PopoverPlacement {
  const width = Math.min(size.width, Math.max(0, viewport.width - 2 * margin));
  const below = viewport.height - (anchor.top + anchor.height) - gap - margin;
  const above = anchor.top - gap - margin;
  const side =
    below >= Math.min(size.height, 240) || below >= above ? "below" : "above";
  const left = Math.min(
    Math.max(margin, anchor.left),
    Math.max(margin, viewport.width - width - margin),
  );
  return side === "below"
    ? {
        left,
        top: anchor.top + anchor.height + gap,
        maxHeight: Math.max(0, below),
        side,
      }
    : {
        left,
        bottom: viewport.height - anchor.top + gap,
        maxHeight: Math.max(0, above),
        side,
      };
}

let sequence = 0;

/**
 * Searchable step picker. Keyboard: type to filter, ArrowUp/ArrowDown to move,
 * Enter to insert, Escape to close (focus returns to the opener), Tab closes
 * and returns focus to the opener as well. Pointer: click an option to insert,
 * click outside to close.
 */
@Component({
  selector: "weave-step-picker",
  standalone: true,
  imports: [Icon, NgTemplateOutlet],
  // tabindex="-1": a press on the title, a group header or the list padding
  // focuses the popover itself instead of the page, so its keys keep working.
  template: `<div
    class="step-picker"
    role="dialog"
    tabindex="-1"
    [attr.aria-labelledby]="prefix + '-title'"
    (keydown)="keydown($event)"
  >
    <div class="step-picker-title" [id]="prefix + '-title'">
      {{ label() }}
      @if (place()) {
        <span class="sr-only">, {{ place() }}</span>
      }
    </div>
    <div class="search-field step-picker-search">
      <weave-icon name="search" />
      <input
        type="text"
        role="combobox"
        autocomplete="off"
        spellcheck="false"
        aria-autocomplete="list"
        aria-expanded="true"
        [attr.aria-controls]="prefix + '-list'"
        [attr.aria-activedescendant]="activeId()"
        [attr.aria-describedby]="prefix + '-count'"
        aria-label="Search steps and actions"
        placeholder="Search steps and actions"
        [value]="query()"
        (input)="search($event)"
      />
    </div>
    <p class="sr-only" [id]="prefix + '-count'" aria-live="polite">
      {{ countText() }}
    </p>
    <div
      class="step-picker-list"
      role="listbox"
      [id]="prefix + '-list'"
      [attr.aria-label]="label()"
    >
      @if (results().steps.length) {
        <div role="group" [attr.aria-labelledby]="prefix + '-steps'">
          <div
            class="step-picker-group"
            role="presentation"
            [id]="prefix + '-steps'"
          >
            Steps
          </div>
          @for (option of results().steps; track option.id) {
            <ng-container
              [ngTemplateOutlet]="row"
              [ngTemplateOutletContext]="{ $implicit: option, index: $index }"
            />
          }
        </div>
      }
      @if (showActions()) {
        <div role="group" [attr.aria-labelledby]="prefix + '-actions'">
          <div
            class="step-picker-group"
            role="presentation"
            [id]="prefix + '-actions'"
          >
            Published actions
          </div>
          @if (actionsLoading()) {
            <div class="step-picker-note" role="presentation">
              Loading actions…
            </div>
          } @else if (actionsNote()) {
            <div class="step-picker-note" role="presentation">
              {{ actionsNote() }}
            </div>
          }
          @for (option of results().actions; track option.id) {
            <ng-container
              [ngTemplateOutlet]="row"
              [ngTemplateOutletContext]="{
                $implicit: option,
                index: results().steps.length + $index,
              }"
            />
          }
          @if (results().hiddenActions) {
            <div class="step-picker-note" role="presentation">
              {{ results().hiddenActions }} more actions. Refine the search to
              see them.
            </div>
          }
        </div>
      }
      @if (!options().length) {
        <div class="step-picker-empty" role="presentation">
          Nothing matches “{{ query() }}”.
        </div>
      }
    </div>
    <ng-template #row let-option let-index="index">
      <div
        class="step-picker-option"
        role="option"
        [id]="optionId(option)"
        [attr.aria-selected]="index === activeIndex()"
        [class.active]="index === activeIndex()"
        [attr.data-option]="option.id"
        (mousedown)="$event.preventDefault()"
        (mousemove)="hover(index)"
        (click)="pick(option)"
      >
        <span class="step-icon" aria-hidden="true"
          ><weave-icon [name]="option.kind"
        /></span>
        <span class="step-picker-text">
          <span class="step-picker-label">{{ option.label }}</span>
          <small>{{ option.description }}</small>
        </span>
      </div>
    </ng-template>
  </div>`,
  styles: [
    `
      /* Above the inspector and menus (15), below modal dialogs (20). */
      :host {
        display: block;
        position: fixed;
        z-index: 19;
        left: 0;
        top: 0;
        visibility: hidden;
        /* Only the card takes clicks, not its rounded corners' box. */
        pointer-events: none;
      }
      :host(.placed) {
        visibility: visible;
      }
      .step-picker {
        pointer-events: auto;
        display: flex;
        flex-direction: column;
        width: min(340px, calc(100vw - 16px));
        max-height: inherit;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        box-shadow: var(--shadow-2);
        padding: 10px;
        gap: 8px;
      }
      .step-picker:focus {
        outline: none;
      }
      .step-picker-title {
        font-weight: 600;
        font-size: 13px;
        overflow-wrap: anywhere;
      }
      .step-picker-search {
        width: 100%;
      }
      .step-picker-search input {
        flex: 1;
        min-width: 0;
        font-size: 13px;
      }
      /* The ring goes on the field, so it does not overlap the field border. */
      .step-picker-search:focus-within {
        outline: 2px solid var(--focus);
        outline-offset: 1px;
        border-color: var(--focus);
      }
      .step-picker-search input:focus-visible {
        outline: none;
      }
      .step-picker-list {
        overflow: auto;
        overscroll-behavior: contain;
        min-height: 0;
        flex: 1 1 auto;
      }
      .step-picker-group {
        font: 600 12px/16px var(--font-sans);
        color: var(--muted);
        padding: 8px 6px 4px;
      }
      .step-picker-option {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 7px 6px;
        border-radius: var(--radius-sm);
        cursor: pointer;
        border: 1px solid transparent;
      }
      .step-picker-option.active {
        background: var(--hover);
        border-color: var(--border-hover);
      }
      .step-picker-text {
        display: flex;
        flex-direction: column;
        min-width: 0;
      }
      .step-picker-label {
        font-size: 13px;
        font-weight: 600;
        overflow-wrap: anywhere;
      }
      .step-picker-text small {
        color: var(--muted);
        font-size: 12px;
        overflow-wrap: anywhere;
      }
      .step-picker-note,
      .step-picker-empty {
        font-size: 12px;
        color: var(--muted);
        padding: 6px;
      }
    `,
  ],
})
export class StepPicker implements AfterViewInit, OnDestroy {
  /** The "+" element (or its viewport rectangle) the picker opens next to. */
  anchor = input<HTMLElement | AnchorRect | null>(null);
  /** Heading and list name. */
  label = input("Add a step");
  /** Where the step goes, for example "after check"; read after the heading. */
  place = input("");
  kinds = input<readonly Kind[]>(allKinds);
  actions = input<readonly PickerAction[]>([]);
  /** Shows the "Published actions" group even when no action matches. */
  actionsLoading = input(false);
  /** Plain note in the actions group, for example why none are listed. */
  actionsNote = input("");
  choose = output<StepPickerChoice>();
  dismiss = output<PickerDismissReason>();

  prefix = `step-picker-${++sequence}`;
  query = signal("");
  activeIndex = signal(0);
  placement = signal<PopoverPlacement | null>(null);
  results = computed(() =>
    pickerOptions(this.query(), this.kinds(), this.actions()),
  );
  options = computed(() => [
    ...this.results().steps,
    ...this.results().actions,
  ]);
  activeOption = computed(
    (): PickerOption | undefined => this.options()[this.activeIndex()],
  );
  /**
   * Element ID of the active option. IDs follow the option, not its position,
   * so filtering that changes the first option also changes this reference
   * and screen readers announce the new active option.
   */
  activeId = computed(() => {
    const option = this.activeOption();
    return option ? this.optionId(option) : null;
  });
  showActions = computed(
    () =>
      this.results().actions.length > 0 ||
      this.actionsLoading() ||
      (!!this.actionsNote() && !this.query()),
  );
  countText = computed(() => {
    const count = this.options().length;
    return count === 1 ? "1 result" : `${count} results`;
  });

  private host = inject(ElementRef<HTMLElement>);
  private opener: HTMLElement | null =
    document.activeElement instanceof HTMLElement &&
    document.activeElement !== document.body
      ? document.activeElement
      : null;
  private closed = false;
  private readonly optionIds = new Map<string, string>();
  private readonly onPointer = (event: PointerEvent) => {
    const target = event.target as Node | null;
    const anchor = this.anchor();
    if (!target || this.host.nativeElement.contains(target)) return;
    // The host toggles the picker from its own anchor's click handler.
    if (anchor instanceof HTMLElement && anchor.contains(target)) return;
    this.close("outside", false);
  };
  private readonly onViewport = () => this.reposition();
  private frame = 0;
  private anchorKey = "";
  /** Follows an anchor element that moves with the canvas (pan or zoom). */
  private readonly follow = () => {
    const anchor = this.anchor();
    if (anchor instanceof HTMLElement) {
      const r = anchor.getBoundingClientRect();
      if (`${r.left},${r.top},${r.width},${r.height}` !== this.anchorKey)
        this.reposition();
    }
    if (!this.closed) this.frame = requestAnimationFrame(this.follow);
  };

  ngAfterViewInit() {
    document.addEventListener("pointerdown", this.onPointer, true);
    window.addEventListener("resize", this.onViewport);
    window.addEventListener("scroll", this.onViewport, true);
    this.reposition();
    this.frame = requestAnimationFrame(this.follow);
    queueMicrotask(() => this.searchInput()?.focus());
  }
  ngOnDestroy() {
    cancelAnimationFrame(this.frame);
    document.removeEventListener("pointerdown", this.onPointer, true);
    window.removeEventListener("resize", this.onViewport);
    window.removeEventListener("scroll", this.onViewport, true);
    // When the picker took focus with it, give it back to the opener unless
    // the host has already moved focus somewhere meaningful.
    const returnTo = this.returnTarget();
    queueMicrotask(() => {
      const active = document.activeElement;
      if (
        returnTo?.isConnected &&
        (!active || active === document.body || !active.isConnected)
      )
        returnTo.focus();
    });
  }

  /**
   * Recomputes the position from the anchor. Window resizes and scrolls, and
   * an anchor element that moves with the canvas, are followed automatically;
   * call this after changing a plain AnchorRect anchor or the content size.
   */
  reposition() {
    if (this.closed) return;
    const anchor = this.anchor();
    if (anchor instanceof HTMLElement && !anchor.isConnected) {
      this.close("outside", false);
      return;
    }
    const rect =
      anchor instanceof HTMLElement
        ? anchor.getBoundingClientRect()
        : (anchor ?? { left: 8, top: 8, width: 0, height: 0 });
    this.anchorKey = `${rect.left},${rect.top},${rect.width},${rect.height}`;
    const element = this.host.nativeElement as HTMLElement;
    const panel = element.firstElementChild as HTMLElement | null;
    const list = panel?.querySelector<HTMLElement>(".step-picker-list");
    // Natural height: the chrome around the list plus the list's full content.
    const natural =
      panel && list
        ? {
            width: panel.offsetWidth,
            height: panel.offsetHeight - list.clientHeight + list.scrollHeight,
          }
        : { width: 340, height: 420 };
    const placement = placePopover(
      rect,
      { width: natural.width, height: Math.min(natural.height, 440) },
      { width: window.innerWidth, height: window.innerHeight },
    );
    // Direct styles: positioning never needs a change-detection pass.
    element.style.left = `${placement.left}px`;
    element.style.top =
      placement.top === undefined ? "auto" : `${placement.top}px`;
    element.style.bottom =
      placement.bottom === undefined ? "auto" : `${placement.bottom}px`;
    element.style.maxHeight = `${Math.min(placement.maxHeight, 440)}px`;
    element.dataset["side"] = placement.side;
    element.classList.add("placed");
    this.placement.set(placement);
  }
  /** A stable, unique element ID per option for this picker instance. */
  optionId(option: PickerOption) {
    let id = this.optionIds.get(option.id);
    if (!id) {
      id = `${this.prefix}-option-${this.optionIds.size}`;
      this.optionIds.set(option.id, id);
    }
    return id;
  }
  search(event: Event) {
    this.query.set((event.target as HTMLInputElement).value);
    this.activeIndex.set(0);
    this.scrollActive();
  }
  hover(index: number) {
    if (index !== this.activeIndex()) this.activeIndex.set(index);
  }
  pick(option: PickerOption) {
    if (this.closed) return;
    this.closed = true;
    this.choose.emit(
      option.uses
        ? { kind: option.kind, uses: option.uses }
        : { kind: option.kind },
    );
  }
  keydown(event: KeyboardEvent) {
    const count = this.options().length;
    switch (event.key) {
      case "ArrowDown":
      case "ArrowUp": {
        event.preventDefault();
        if (!count) return;
        const step = event.key === "ArrowDown" ? 1 : -1;
        this.activeIndex.set((this.activeIndex() + step + count) % count);
        this.scrollActive();
        return;
      }
      case "PageDown":
      case "PageUp": {
        event.preventDefault();
        if (!count) return;
        this.activeIndex.set(event.key === "PageDown" ? count - 1 : 0);
        this.scrollActive();
        return;
      }
      case "Enter": {
        event.preventDefault();
        const option = this.activeOption();
        if (option) this.pick(option);
        return;
      }
      case "Escape":
        event.preventDefault();
        // The canvas also listens for Escape; this one only closes the picker.
        event.stopPropagation();
        this.close("escape", true);
        return;
      case "Tab":
        event.preventDefault();
        this.close("tab", true);
        return;
    }
  }
  /** Returns focus to the element that opened the picker (or the anchor). */
  restoreFocus() {
    this.returnTarget()?.focus();
  }
  private returnTarget(): HTMLElement | null {
    const anchor = this.anchor();
    if (anchor instanceof HTMLElement && anchor.isConnected) return anchor;
    return this.opener?.isConnected ? this.opener : null;
  }
  private close(reason: PickerDismissReason, restore: boolean) {
    if (this.closed) return;
    this.closed = true;
    if (restore) this.restoreFocus();
    this.dismiss.emit(reason);
  }
  private searchInput() {
    return (
      this.host.nativeElement as HTMLElement
    ).querySelector<HTMLInputElement>("input[role=combobox]");
  }
  private scrollActive() {
    const id = this.activeId();
    if (!id) return;
    queueMicrotask(() =>
      document.getElementById(id)?.scrollIntoView({ block: "nearest" }),
    );
  }
}
