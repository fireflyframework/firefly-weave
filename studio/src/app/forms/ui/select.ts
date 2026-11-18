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
// Shared choice control. The top-layer list remains inside the viewport even
// when its field lives at the bottom of a scrolling inspector or modal sheet.
import {
  Component,
  ElementRef,
  computed,
  inject,
  input,
  output,
  signal,
} from "@angular/core";
import { Icon } from "../../icon";
import {
  AnchoredPopover,
  pageOptionIndex,
  revealPopoverOption,
} from "./anchored-popover";

export interface SelectOption {
  value: string;
  label: string;
  description?: string;
  group?: string;
  disabled?: boolean;
  /** A command rather than a choice ("Create connection"): set apart below a rule, with a plus, in the link color. */
  action?: boolean;
  /** A short caution after the description ("Not encrypted"), in the warning ink. */
  note?: string;
}

export function selectOptions(
  options: readonly SelectOption[],
  query: string,
): SelectOption[] {
  const words = query.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  const seen = new Set<string>();
  return options.filter((option) => {
    if (seen.has(option.value)) return false;
    seen.add(option.value);
    const text = [option.label, option.description, option.group]
      .join(" ")
      .toLocaleLowerCase();
    return words.every((word) => text.includes(word));
  });
}
let sequence = 0;

@Component({
  selector: "weave-select",
  standalone: true,
  imports: [Icon, AnchoredPopover],
  host: {
    "(focusout)": "leave($event)",
    "(document:pointerdown)": "outside($event)",
  },
  template: `
    <label [for]="controlId() || id + '-input'" [class.sr-only]="hideLabel()">{{
      label()
    }}</label>
    <div #anchor class="choice-field" [class.is-disabled]="disabled()">
      <input
        [id]="controlId() || id + '-input'"
        type="text"
        role="combobox"
        autocomplete="off"
        spellcheck="false"
        [readOnly]="!searchable()"
        [disabled]="disabled()"
        [value]="open() ? query() : (selected()?.label ?? '')"
        [placeholder]="
          open() && searchable() ? 'Search choices…' : placeholder()
        "
        [attr.aria-label]="label()"
        [attr.data-value]="value()"
        [attr.aria-invalid]="invalid() || null"
        [attr.aria-describedby]="describedBy() || null"
        [attr.aria-expanded]="open()"
        [attr.aria-controls]="id + '-list'"
        [attr.aria-autocomplete]="searchable() ? 'list' : 'none'"
        [attr.aria-activedescendant]="
          open() && active() >= 0 ? id + '-option-' + active() : null
        "
        (click)="show()"
        (keydown)="key($event)"
        (input)="typed($event)"
      />
      <button
        type="button"
        tabindex="-1"
        [disabled]="disabled()"
        [attr.aria-label]="'Show choices for ' + label()"
        (mousedown)="$event.preventDefault()"
        (click)="toggle()"
      >
        <weave-icon [name]="icon()" />
      </button>
    </div>
    <!-- The popup scrolls; inside it, a phone sheet's title row and the listbox. -->
    <div
      class="choice-list"
      [hidden]="!open()"
      [weaveAnchoredPopover]="open()"
      [popoverAnchor]="anchor"
      [popoverSheet]="!!sheetTitle()"
      (popoverClosed)="close()"
      (mousedown)="$event.preventDefault()"
    >
      @if (open() && sheetTitle()) {
        <div class="choice-sheet-title">
          <span>{{ sheetTitle() }}</span>
          <!-- For touch and pointer; the keyboard closes the list with Escape
               and keeps its focus in the field. -->
          <button
            type="button"
            class="icon-button choice-sheet-close"
            tabindex="-1"
            aria-label="Close choices"
            (click)="close()"
          >
            <weave-icon name="close" />
          </button>
        </div>
      }
      <div
        class="choice-options"
        role="listbox"
        [id]="id + '-list'"
        [attr.aria-label]="label() + ' options'"
      >
        @if (open()) {
          @for (option of filtered(); track option.value; let index = $index) {
            @if (option.action && index > 0 && !filtered()[index - 1]?.action) {
              <div class="choice-rule" role="presentation"></div>
            }
            @if (
              option.group && option.group !== filtered()[index - 1]?.group
            ) {
              <div class="choice-group" role="presentation">
                {{ option.group }}
              </div>
            }
            <div
              role="option"
              class="choice-option"
              [class.choice-action]="option.action"
              [id]="id + '-option-' + index"
              [attr.data-value]="option.value"
              [attr.aria-selected]="value() === option.value"
              [attr.aria-disabled]="option.disabled || null"
              [class.active]="active() === index"
              (mousemove)="hover(index)"
              (click)="$event.preventDefault(); pick(index)"
            >
              @if (option.action) {
                <span class="choice-label"
                  ><weave-icon name="plus" [size]="16" />{{
                    option.label
                  }}</span
                >
              } @else {
                <span>{{ option.label }}</span>
              }
              @if (option.description) {
                <small
                  >{{ option.description }}
                  @if (option.note) {
                    · <span class="choice-note">{{ option.note }}</span>
                  }
                </small>
              }
              @if (value() === option.value) {
                <weave-icon name="check" />
              }
            </div>
          } @empty {
            <p class="choice-empty" role="presentation">No matching choices.</p>
          }
        }
      </div>
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        min-width: 0;
      }
      label {
        display: block;
        font-size: 13px;
        font-weight: 600;
        margin-bottom: 6px;
      }
      .choice-field {
        display: flex;
        align-items: center;
        min-height: 36px;
        border: 1px solid var(--field-border);
        border-radius: var(--radius-sm);
        background: var(--surface);
        color: var(--text);
      }
      /* data-control: exactly one control height (--select-height, 36 px by
         default), as the inputs beside it, instead of growing around the
         chevron button's own height. */
      :host([data-control]) .choice-field {
        box-sizing: border-box;
        height: var(--select-height, var(--control-md));
        min-height: 0;
      }
      :host([data-control]) .choice-field button {
        min-height: 0;
        height: 100%;
      }
      /* Its text box fills the field to the border, as in the parameter
         form, so a tap anywhere in the field opens the choices. */
      :host([data-control]) .choice-field input {
        min-height: var(--select-height, var(--control-md));
        margin-block: -1px;
      }
      .choice-field:focus-within {
        outline: 2px solid var(--focus);
        outline-offset: 2px;
      }
      .choice-field input {
        flex: 1;
        width: 0;
        min-width: 0;
        padding: 8px 10px;
        font: inherit;
        font-size: 13px;
        background: transparent;
        color: inherit;
        border: 0;
        outline: none;
        box-shadow: none;
      }
      .choice-field input[readonly] {
        cursor: pointer;
      }
      .choice-field button {
        display: grid;
        place-items: center;
        padding: 4px;
        width: 32px;
        min-width: 32px;
        height: 32px;
        border: 0;
        background: transparent;
        color: var(--text);
      }
      .is-disabled {
        background: var(--disabled-bg);
        color: var(--muted);
      }
      .choice-list {
        padding: 4px;
        background: var(--surface);
        color: var(--text);
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        box-shadow: var(--shadow-2);
        overflow: auto;
      }
      .choice-option {
        position: relative;
        display: grid;
        gap: 3px;
        min-height: 36px;
        padding: 8px 30px 8px 10px;
        border-radius: var(--radius-sm);
        cursor: pointer;
        font-size: 13px;
        overflow-wrap: anywhere;
      }
      .choice-option.active {
        background: var(--accent);
        color: var(--on-accent);
      }
      .choice-option small {
        font-size: 12px;
        color: var(--muted);
      }
      .choice-option.active small {
        color: var(--on-accent);
      }
      .choice-option > weave-icon {
        position: absolute;
        top: 10px;
        right: 8px;
        width: 16px;
        height: 16px;
      }
      .choice-option[aria-disabled="true"] {
        color: var(--muted);
        cursor: not-allowed;
      }
      .choice-rule {
        margin: 4px 6px;
        border-top: 1px solid var(--line);
      }
      .choice-action:not([aria-disabled="true"]) {
        color: var(--link);
      }
      .choice-action.active {
        color: var(--on-accent);
      }
      .choice-label {
        display: inline-flex;
        align-items: center;
        gap: 6px;
      }
      .choice-note {
        color: var(--warning-ink);
      }
      .choice-option.active .choice-note {
        color: inherit;
      }
      /* Below 768 px, with a title: a sheet along the bottom edge over a
         scrim, with 48 px rows, instead of a list over the fields above. */
      .choice-sheet-title {
        display: none;
      }
      .choice-list[data-side="sheet"] {
        padding: var(--space-2) var(--space-2) var(--space-4);
        border-width: 1px 0 0;
        border-radius: var(--radius-lg) var(--radius-lg) 0 0;
        box-shadow: var(--shadow-3);
      }
      .choice-list[data-side="sheet"]::backdrop {
        background: var(--scrim);
      }
      .choice-list[data-side="sheet"] .choice-sheet-title {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--space-2);
        padding: 0 0 var(--space-1) 10px;
        font: var(--type-label);
      }
      .choice-sheet-close {
        flex: none;
        width: 44px;
        min-width: 44px;
        height: 44px;
      }
      .choice-list[data-side="sheet"] .choice-option {
        align-content: center;
        min-height: 48px;
      }
      .choice-group {
        font-size: 12px;
        font-weight: 700;
        padding: 10px 10px 4px;
        color: var(--muted);
      }
      .choice-empty {
        font-size: 13px;
        padding: 8px;
        margin: 0;
        color: var(--muted);
      }
    `,
  ],
})
export class Select {
  label = input.required<string>();
  options = input<readonly SelectOption[]>([]);
  value = input("");
  placeholder = input("Choose an option");
  disabled = input(false);
  hideLabel = input(false);
  controlId = input("");
  invalid = input<boolean | null>(false);
  describedBy = input<string | null>(null);
  /** The toggle's icon; "chevronDown" where the list opens in place, under the field. */
  icon = input("chevron");
  /**
   * Below 768 px the choices open as a sheet along the bottom edge with this
   * title ("Connection for crm-lookup"); without one, they open at the field.
   */
  sheetTitle = input<string | null>(null);
  choose = output<string>();
  id = `choice-${++sequence}`;
  open = signal(false);
  query = signal("");
  active = signal(-1);
  selected = computed(() =>
    this.options().find((option) => option.value === this.value()),
  );
  searchable = computed(() => this.options().length > 7);
  filtered = computed(() => selectOptions(this.options(), this.query()));
  private host = inject<ElementRef<HTMLElement>>(ElementRef).nativeElement;
  private typeahead = "";
  private typedAt = 0;

  show() {
    if (this.disabled() || this.open()) return;
    this.query.set("");
    this.open.set(true);
    this.active.set(
      this.filtered().findIndex(
        (option) => option.value === this.value() && !option.disabled,
      ),
    );
  }
  toggle() {
    if (this.open()) this.close();
    else this.show();
    this.host.querySelector<HTMLInputElement>("input")?.focus();
  }
  close() {
    this.open.set(false);
    this.query.set("");
    this.active.set(-1);
  }
  typed(event: Event) {
    if (!this.searchable() || this.disabled()) return;
    this.open.set(true);
    this.query.set((event.target as HTMLInputElement).value);
    this.active.set(this.filtered().findIndex((option) => !option.disabled));
  }
  hover(index: number) {
    if (!this.filtered()[index]?.disabled) this.active.set(index);
  }
  pick(index: number) {
    const option = this.filtered()[index];
    if (!option || option.disabled || this.disabled()) return;
    this.choose.emit(option.value);
    this.close();
    this.host.querySelector<HTMLInputElement>("input")?.focus();
  }
  leave(event: FocusEvent) {
    if (!this.host.contains(event.relatedTarget as Node | null)) this.close();
  }
  outside(event: PointerEvent) {
    if (!this.open()) return;
    const list = this.host.querySelector<HTMLElement>(".choice-list");
    // A press on a sheet's scrim lands on the list itself, above its top edge.
    const scrim =
      !!list &&
      event.target === list &&
      event.clientY < list.getBoundingClientRect().top;
    if (scrim || !event.composedPath().includes(this.host)) this.close();
  }
  private move(next: number, direction: 1 | -1) {
    const items = this.filtered();
    next = Math.max(0, Math.min(items.length - 1, next));
    while (items[next]?.disabled) {
      next += direction;
      if (next < 0 || next >= items.length) return;
    }
    this.active.set(next);
    requestAnimationFrame(() => {
      const list = this.host.querySelector<HTMLElement>(".choice-list");
      const row = list?.querySelector<HTMLElement>(
        `[id="${this.id}-option-${next}"]`,
      );
      if (list && row) revealPopoverOption(list, row);
    });
  }
  key(event: KeyboardEvent) {
    if (this.disabled()) return;
    if (event.key === "Escape") {
      if (this.open()) {
        event.preventDefault();
        event.stopPropagation();
        this.close();
      }
      return;
    }
    if (event.key === "Tab") {
      this.close();
      return;
    }
    if (event.key === "Enter" || (event.key === " " && !this.searchable())) {
      event.preventDefault();
      if (!this.open()) this.show();
      else this.pick(this.active());
      return;
    }
    if (
      ["ArrowDown", "ArrowUp", "Home", "End", "PageDown", "PageUp"].includes(
        event.key,
      )
    ) {
      event.preventDefault();
      const wasOpen = this.open();
      this.show();
      const direction: 1 | -1 = ["ArrowUp", "PageUp", "Home"].includes(
        event.key,
      )
        ? -1
        : 1;
      let next = this.active();
      if (event.key === "Home") next = 0;
      else if (event.key === "End") next = this.filtered().length - 1;
      else if (event.key.startsWith("Page")) {
        const list = this.host.querySelector<HTMLElement>(".choice-list");
        next = list
          ? pageOptionIndex(list, this.active(), direction)
          : this.active() + direction;
      } else if (wasOpen || next < 0)
        next =
          next < 0
            ? direction === 1
              ? 0
              : this.filtered().length - 1
            : next + direction;
      this.move(next, direction);
      return;
    }
    if (
      !this.searchable() &&
      event.key.length === 1 &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey
    ) {
      event.preventDefault();
      this.show();
      const now = Date.now();
      this.typeahead =
        (now - this.typedAt > 600 ? "" : this.typeahead) +
        event.key.toLocaleLowerCase();
      this.typedAt = now;
      const index = this.filtered().findIndex(
        (option) =>
          !option.disabled &&
          option.label.toLocaleLowerCase().startsWith(this.typeahead),
      );
      if (index >= 0) this.move(index, 1);
    }
  }
}
