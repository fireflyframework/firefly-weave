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
        <weave-icon name="chevron" />
      </button>
    </div>
    <div
      class="choice-list"
      role="listbox"
      [id]="id + '-list'"
      [attr.aria-label]="label() + ' options'"
      [hidden]="!open()"
      [weaveAnchoredPopover]="open()"
      [popoverAnchor]="anchor"
      (popoverClosed)="close()"
      (mousedown)="$event.preventDefault()"
    >
      @if (open()) {
        @for (option of filtered(); track option.value; let index = $index) {
          @if (option.group && option.group !== filtered()[index - 1]?.group) {
            <div class="choice-group" role="presentation">
              {{ option.group }}
            </div>
          }
          <div
            role="option"
            class="choice-option"
            [id]="id + '-option-' + index"
            [attr.data-value]="option.value"
            [attr.aria-selected]="value() === option.value"
            [attr.aria-disabled]="option.disabled || null"
            [class.active]="active() === index"
            (mousemove)="hover(index)"
            (click)="$event.preventDefault(); pick(index)"
          >
            <span>{{ option.label }}</span>
            @if (option.description) {
              <small>{{ option.description }}</small>
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
      .choice-option weave-icon {
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
    if (this.open() && !event.composedPath().includes(this.host)) this.close();
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
      const list = this.host.querySelector<HTMLElement>('[role="listbox"]');
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
        const list = this.host.querySelector<HTMLElement>('[role="listbox"]');
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
